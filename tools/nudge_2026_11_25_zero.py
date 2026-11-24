"""Особисте повідомлення платному підписнику, якому бот ще не надіслав жодної квартири (25.11.2026).

Чесно: за цей час під його пошук справді не було квартир (рахуємо тими самими матчерами,
що й розсилка), і що дало б кожне розширення — з кнопками «одним натисканням»
(`housing:relax:<search_id>:<code>` - їх обробляє housing_relax.apply).

Нічого не шле без `--send`. За замовчуванням - сухий прогін: друкує текст і кнопки.

    python nudge_zero.py --candidates              # хто платний і без жодної квартири
    python nudge_zero.py 459543419                 # сухий прогін для однієї людини
    python nudge_zero.py 459543419 --send          # справді надіслати

Пошук на паузі (напр. ніколи не запускався) сюди не потрапляє: його вмикає
tools/activate_never_started_2026_11_25.py одним повідомленням, щоб людина не отримала два.

Людина пропускається (з причиною), якщо: не підписник; їй уже щось надсилали; повідомлення
вже було (файл .done); за `--days` днів під її пошук підходили квартири (тоді «їх не було»
було б неправдою - це інша проблема, її треба розбирати окремо); пошуків немає.
`--force` вимикає перші дві перевірки, але не третю (правду про ринок не обходимо).
Вночі (21:00-08:00 за Берліном) шле лише з `--any-time`.
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.getenv("APP_DIR", "/app"))

import i18n  # noqa: E402
from database import DBSession, HousingPayment, HousingSendLog  # noqa: E402
from user_handlers import housing_monitor as hm, housing_relax  # noqa: E402
from user_jobs import housing_journey_store, housing_search_store, housing_tier  # noqa: E402

ZERO_DAYS = 14
MAX_SEARCHES = 3
SEND_PAUSE_SECONDS = 0.3
DONE_FILE = os.getenv("NUDGE_ZERO_DONE", "config/nudge_2026_11_25_zero.done")

TEXT = {
    "uk": {
        "head": "🔕 <b>Чесно про ваш пошук</b>",
        "paid": "Ви оформили підписку {date}, а квартир від бота ще не було.",
        "zero": "Це не збій: за останні {days} днів під ваш пошук{n} на жодній площадці не з'явилось жодної квартири.",
        "base": "Для порівняння: підходящих квартир за {month} днів без змін у пошуку: {total}.",
        "offers": "Що дало б розширення (за {month} днів):",
        "line": "• {label} — <b>{count}</b>",
    },
    "ru": {
        "head": "🔕 <b>Честно о вашем поиске</b>",
        "paid": "Вы оформили подписку {date}, а квартир от бота ещё не было.",
        "zero": "Это не сбой: за последние {days} дней под ваш поиск{n} ни на одной площадке не появилось ни одной квартиры.",
        "base": "Для сравнения: подходящих квартир за {month} дней без изменений в поиске: {total}.",
        "offers": "Что дало бы расширение (за {month} дней):",
        "line": "• {label} — <b>{count}</b>",
    },
    "de": {
        "head": "🔕 <b>Ehrlich zu Ihrer Suche</b>",
        "paid": "Sie haben das Abo am {date} abgeschlossen, aber der Bot hat noch keine Wohnung geschickt.",
        "zero": "Das ist keine Störung: In den letzten {days} Tagen ist zu Ihrer Suche{n} auf keinem Portal eine einzige Wohnung erschienen.",
        "base": "Zum Vergleich: passende Wohnungen in {month} Tagen bei unveränderter Suche: {total}.",
        "offers": "Was eine Lockerung bringen würde (in {month} Tagen):",
        "line": "• {label} — <b>{count}</b>",
    },
}


def _lang(user_id: int) -> str:
    lang = i18n.get_lang(int(user_id))
    return lang if lang in TEXT else "uk"


def last_payment_date(user_id: int) -> Optional[datetime]:
    session = DBSession()
    try:
        row = (session.query(HousingPayment)
               .filter(HousingPayment.user_id == int(user_id), HousingPayment.status == "approved")
               .order_by(HousingPayment.decided_at.desc()).first())
        return row.decided_at if row is not None else None
    finally:
        session.close()


def sends_since(user_id: int, since: Optional[datetime] = None) -> int:
    session = DBSession()
    try:
        query = session.query(HousingSendLog).filter(HousingSendLog.user_id == int(user_id))
        if since is not None:
            query = query.filter(HousingSendLog.sent_at >= since)
        return int(query.count())
    finally:
        session.close()


def candidates() -> List[int]:
    """Платні, з підтвердженою оплатою і нулем надісланих квартир."""
    result = []
    for user_id in sorted(housing_tier.premium_user_ids()):
        paid = last_payment_date(user_id)
        if paid is not None and sends_since(user_id) == 0:
            result.append(user_id)
    return result


def _load_done() -> set:
    try:
        with open(DONE_FILE, encoding="utf-8") as handle:
            return {int(x) for x in json.load(handle)}
    except (OSError, ValueError):
        return set()


def _save_done(done: set) -> None:
    with open(DONE_FILE, "w", encoding="utf-8") as handle:
        json.dump(sorted(done), handle)


def build_messages(user_id: int, week: "housing_relax.Market", month: "housing_relax.Market") -> Tuple[List[Dict], List[str]]:
    """([{text, buttons: [(label, callback)]}], причини, чому когось не слати)."""
    lang = _lang(user_id)
    words = TEXT[lang]
    notes: List[str] = []
    messages: List[Dict] = []
    searches = housing_search_store.list_for_user(int(user_id))
    if not searches:
        return [], ["немає пошуків"]
    paid = last_payment_date(user_id)
    try:
        live = {item["search_id"]: item for item in hm.user_searches(int(user_id))}
    except Exception:
        live = {}
    for number, search in enumerate(searches, 1):
        filters = month.member_filters(search["members"])
        if not filters:
            notes.append(f"пошук {search['search_id']}: фільтрів площадок немає")
            continue
        recent = week.count(week.member_filters(search["members"]))
        if recent is None:
            notes.append(f"пошук {search['search_id']}: за {ZERO_DAYS} днів порахувати не вдалося")
            continue
        if search["search_id"] in live and hm._search_paused(live[search["search_id"]]):
            # Пошук на паузі - не «квартир не було». Таких вмикає activate_never_started_2026_11_25.py
            # (одне повідомлення), тут вони другого не отримують.
            notes.append(f"пошук {search['search_id']}: на паузі - див. activate_never_started_2026_11_25.py")
            continue
        if recent > 0:
            notes.append(f"пошук {search['search_id']}: за {ZERO_DAYS} днів підходило {recent} - слати не можна")
            continue
        base, found = housing_relax.offers(month, search["criteria"], filters)
        if base is None:
            notes.append(f"пошук {search['search_id']}: за {month.days} днів порахувати не вдалося")
            continue
        parts = [words["head"]]
        if paid is not None:
            parts.append(words["paid"].format(date=paid.strftime("%d.%m")))
        parts.append(words["zero"].format(days=ZERO_DAYS, n=f" №{number}" if len(searches) > 1 else ""))
        if base > 0:
            parts.append(words["base"].format(month=month.days, total=base))
        buttons: List[Tuple[str, str]] = []
        if found:
            lines = [words["offers"].format(month=month.days)]
            for code, count in found:
                label = housing_relax.offer_label(code, search["criteria"], lang)
                lines.append(words["line"].format(label=label, count=count))
                buttons.append((
                    i18n.t("housing.relax.btn", lang, label=label, gain=count - base),
                    f"{housing_relax.CALLBACK_PREFIX}{int(search['search_id'])}:{code}",
                ))
            lines.append(i18n.t("housing.relax.tap", lang))
            parts.append("\n".join(lines))
        else:
            parts.append(i18n.t("housing.relax.quiet_no_offers", lang))
        buttons.append((i18n.t("housing.btn.self_manage", lang), "housing:self_manage"))
        messages.append({"search_id": int(search["search_id"]), "text": "\n\n".join(parts), "buttons": buttons,
                         "base": base, "found": found})
        if len(messages) >= MAX_SEARCHES:
            break
    return messages, notes


def _night_now() -> bool:
    import pytz  # noqa: PLC0415

    hour = datetime.now(pytz.timezone("Europe/Berlin")).hour
    return hour >= 21 or hour < 8


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("user_ids", nargs="*", type=int)
    parser.add_argument("--send", action="store_true", help="справді надіслати (інакше сухий прогін)")
    parser.add_argument("--candidates", action="store_true", help="лише показати, кому це пасує")
    parser.add_argument("--force", action="store_true", help="не перевіряти «ще не надсилали» і .done")
    parser.add_argument("--any-time", action="store_true", help="дозволити відправку вночі")
    args = parser.parse_args(argv)

    if args.candidates:
        print({"candidates": candidates()})
        return 0
    if not args.user_ids:
        parser.error("потрібен id людини (або --candidates)")

    done = _load_done()
    now = datetime.utcnow()
    week = housing_relax.Market(ZERO_DAYS)
    month = housing_relax.Market(housing_relax.RELAX_DAYS)
    bot = None
    if args.send:
        if _night_now() and not args.any_time:
            print("зараз ніч за Берліном (21:00-08:00): без --any-time не шлю")
            return 2
        from telegram import Bot  # noqa: PLC0415

        bot = Bot(token=os.environ["BOT_TOKEN"])
    sent = 0
    for user_id in args.user_ids:
        print(f"=== {user_id} ({_lang(user_id)}) ===")
        if not housing_tier.is_premium(user_id):
            print("пропуск: не підписник")
            continue
        if not args.force:
            if user_id in done:
                print("пропуск: повідомлення вже надсилалось (.done)")
                continue
            count = sends_since(user_id)
            if count:
                print(f"пропуск: квартир уже надсилали - {count}")
                continue
        messages, notes = build_messages(user_id, week, month)
        for note in notes:
            print("увага:", note)
        for message in messages:
            print(message["text"].replace("<b>", "").replace("</b>", ""))
            for label, callback in message["buttons"]:
                print(f"   [{label}] -> {callback}")
            print()
        if not messages:
            print("нічого слати")
            continue
        if not args.send:
            continue
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup  # noqa: PLC0415

        try:
            for message in messages:
                markup = InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=callback)]
                                               for label, callback in message["buttons"]])
                bot.send_message(chat_id=int(user_id), text=message["text"], parse_mode="HTML", reply_markup=markup)
                time.sleep(SEND_PAUSE_SECONDS)
        except Exception as exc:
            print(f"помилка {user_id}: {type(exc).__name__}: {str(exc)[:80]}")
            continue
        housing_journey_store.mark_quiet_nudge_sent(user_id)  # щоб щоденна «тиха» перевірка не дублювала
        done.add(user_id)
        _save_done(done)
        sent += 1
    print({"sent": sent, "dry_run": not args.send, "at": now.isoformat(timespec="seconds")})
    return 0


if __name__ == "__main__":
    sys.exit(main())
