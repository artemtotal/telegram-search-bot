"""Вмикає пошуки, що були створені на паузі й жодного разу не запускались (25.11.2026).

Контекст: новий пошук із картки створювався на паузі й стартував лише після «▶️ Почати пошук»;
хто кнопку не натиснув - не отримував нічого. Тепер пошук створюється активним, а цей
одноразовий скрипт підтягує старі. Як відрізняємо «ні разу не запускався» від чиєїсь паузи -
у user_jobs/housing_never_started.py (відправки, події запуску, дата, записи площадок).

За замовчуванням - СУХИЙ ПРОГІН: друкує, скільки людей і пошуків (платні/безкоштовні),
кого пропущено і чому, і список сумнівних (їх не вмикаємо ніколи).

    python activate_never_started_2026_11_25.py              # сухий прогін
    python activate_never_started_2026_11_25.py --user 123   # лише ця людина (для перевірки)
    python activate_never_started_2026_11_25.py --apply      # увімкнути й написати кожному

Правила: платним вмикаємо всі такі пошуки; безкоштовним - не більше одного (найстаріший), а в кого
вже є активний - не чіпаємо. Перед вмиканням запис «перебазується» (усе, що вже висить на ринку, -
не нове), тож людині не прийде весь накопичений ринок. Одне коротке повідомлення на людину.
Вночі (21:00-08:00 за Берліном) --apply без --any-time не працює. Повторний запуск пропускає
вже оброблених (файл .done). Адмін не чіпається.
"""

import argparse
import json
import os
import sys
import time
from typing import Dict, List, Optional

sys.path.insert(0, os.getenv("APP_DIR", "/app"))

import i18n  # noqa: E402
from database import DBSession, HousingSearch  # noqa: E402
from user_handlers import housing_monitor as hm  # noqa: E402
from user_jobs import housing_never_started as ns, housing_tier  # noqa: E402

DONE_FILE = os.getenv("ACTIVATE_NEVER_DONE", "config/activate_never_started_2026_11_25.done")
SEND_PAUSE_SECONDS = 0.3

TEXT = {
    "uk": "▶️ Ваш пошук був на паузі, бо не була натиснута кнопка запуску. Ми це прибрали: пошук увімкнено. "
          "Якщо він вам не потрібен — кнопка «⏸» у «Мої фільтри».",
    "ru": "▶️ Ваш поиск был на паузе, потому что не была нажата кнопка запуска. Мы это убрали: поиск включён. "
          "Если он вам не нужен — кнопка «⏸» в «Мои фильтры».",
    "de": "▶️ Ihre Suche war pausiert, weil die Start-Schaltfläche nicht gedrückt wurde. Das haben wir behoben: "
          "Die Suche ist eingeschaltet. Falls Sie sie nicht brauchen — Schaltfläche „⏸“ unter „Meine Filter“.",
}


def _lang(user_id: int) -> str:
    lang = i18n.get_lang(int(user_id))
    return lang if lang in TEXT else "uk"


def message_for(user_id: int) -> str:
    return TEXT[_lang(user_id)]


def search_user_ids() -> List[int]:
    session = DBSession()
    try:
        return sorted({int(row[0]) for row in session.query(HousingSearch.user_id).distinct()})
    finally:
        session.close()


def build_plan(user_ids: List[int]) -> Dict:
    """{'todo': [{user_id, premium, searches: [...]}], 'skipped': [...], 'doubtful': [...]}"""
    plan = {"todo": [], "skipped": [], "doubtful": []}
    for user_id in user_ids:
        if hm.ADMIN_ID and int(user_id) == int(hm.ADMIN_ID):
            continue
        inspected = ns.inspect_user(user_id)
        for item in inspected:
            if item["status"] == ns.DOUBTFUL:
                plan["doubtful"].append({"user_id": user_id, "search_id": item["search_id"], "reason": item["reason"]})
        chosen, note = ns.pick(user_id, inspected)
        never = [item for item in inspected if item["status"] == ns.NEVER]
        if never and not chosen:
            plan["skipped"].append({"user_id": user_id, "reason": note, "searches": [i["search_id"] for i in never]})
        if chosen:
            plan["todo"].append({
                "user_id": user_id, "premium": housing_tier.is_premium(user_id), "note": note,
                "searches": [{"search_id": i["search_id"], "created_at": i["created_at"], "view": i["view"]}
                             for i in chosen],
                "left_paused": [i["search_id"] for i in never if i not in chosen],
            })
    return plan


def summary(plan: Dict) -> Dict:
    paid = [t for t in plan["todo"] if t["premium"]]
    free = [t for t in plan["todo"] if not t["premium"]]
    count = lambda rows: sum(len(t["searches"]) for t in rows)  # noqa: E731
    return {
        "people": len(plan["todo"]), "searches": count(plan["todo"]),
        "paid": {"people": len(paid), "searches": count(paid)},
        "free": {"people": len(free), "searches": count(free)},
        "skipped_people": len(plan["skipped"]), "doubtful_searches": len(plan["doubtful"]),
    }


def _load_done() -> set:
    try:
        with open(DONE_FILE, encoding="utf-8") as handle:
            return {int(x) for x in json.load(handle)}
    except (OSError, ValueError):
        return set()


def _save_done(done: set) -> None:
    with open(DONE_FILE, "w", encoding="utf-8") as handle:
        json.dump(sorted(done), handle)


def _night_now() -> bool:
    import pytz  # noqa: PLC0415
    from datetime import datetime  # noqa: PLC0415

    hour = datetime.now(pytz.timezone("Europe/Berlin")).hour
    return hour >= 21 or hour < 8


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="увімкнути й написати (інакше сухий прогін)")
    parser.add_argument("--user", type=int, action="append", help="лише ці люди")
    parser.add_argument("--any-time", action="store_true", help="дозволити вночі")
    parser.add_argument("--no-message", action="store_true", help="увімкнути без повідомлення")
    args = parser.parse_args(argv)

    # Один запит до приймача Immowelt на весь прохід (а не на кожну людину).
    cached = hm._all_immowelt_filters()
    hm._all_immowelt_filters = lambda timeout=None: cached  # noqa: E731
    if not cached:
        print("увага: приймач Immowelt не відповів - стан Immowelt-записів невідомий")
        if args.apply:
            print("--apply без стану Immowelt небезпечний: зупиняюсь")
            return 2
    if args.apply and _night_now() and not args.any_time:
        print("зараз ніч за Берліном (21:00-08:00): без --any-time не працюю")
        return 2

    done = _load_done()
    users = args.user or search_user_ids()
    plan = build_plan(users)
    for entry in plan["todo"]:
        tag = "платний" if entry["premium"] else "безкоштовний"
        ids = ",".join(str(s["search_id"]) for s in entry["searches"])
        extra = f"; {entry['note']}" if entry["note"] else ""
        left = f"; лишається на паузі: {entry['left_paused']}" if entry["left_paused"] else ""
        print(f"{entry['user_id']} ({tag}, {_lang(entry['user_id'])}) пошуки: {ids}{extra}{left}")
    for entry in plan["skipped"]:
        print(f"пропуск {entry['user_id']}: {entry['reason']} (пошуки {entry['searches']})")
    for entry in plan["doubtful"]:
        print(f"сумнівний (не вмикаю) {entry['user_id']} пошук {entry['search_id']}: {entry['reason']}")
    print(summary(plan))
    if not args.apply:
        print("сухий прогін: нічого не змінено й не надіслано")
        return 0

    from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup  # noqa: PLC0415

    bot = None if args.no_message else Bot(token=os.environ["BOT_TOKEN"])
    turned = told = 0
    for entry in plan["todo"]:
        user_id = entry["user_id"]
        if user_id in done:
            print(f"{user_id}: уже оброблено (.done)")
            continue
        for item in entry["searches"]:
            marked = ns.activate(user_id, item["view"])
            print(f"{user_id}: пошук {item['search_id']} увімкнено (база: {marked} пар)")
            turned += 1
        done.add(user_id)
        _save_done(done)
        if bot is None:
            continue
        markup = InlineKeyboardMarkup([[InlineKeyboardButton(
            i18n.t("housing.btn.self_manage", _lang(user_id)), callback_data="housing:self_manage")]])
        try:
            bot.send_message(chat_id=user_id, text=message_for(user_id), reply_markup=markup)
            told += 1
        except Exception as exc:
            print(f"повідомлення {user_id}: {type(exc).__name__}: {str(exc)[:80]}")
        time.sleep(SEND_PAUSE_SECONDS)
    print({"turned_on": turned, "told": told})
    return 0


if __name__ == "__main__":
    sys.exit(main())
