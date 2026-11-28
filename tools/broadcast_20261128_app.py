"""Разова розсилка 28.11.2026: «у Потсдамбота з'явився застосунок усередині Telegram».

Отримувачі: user_settings_store.list_subscribed_user_ids() (news_subscribed = True), без тих, у кого
останнє подія в housing_leave_event - блокування бота. Дві групи: «paid» (діючі платні підписники житла,
housing_tier.premium_user_ids()) отримують спокійний текст про те, що моніторинг працює як раніше;
«free» (усі інші) - загальний. Кожному своєю мовою (uk/ru/de, невідома -> uk).

Під повідомленням дві кнопки: «Відкрити» (web_app на головну застосунку, як на /start; без Mini App - URL-кнопка на
app_link з masters.json) і «Не надсилати новини» (callback `appnews:off`, обробник - user_handlers/app_news.py).

Запуск (завжди окремим процесом, поллінгу немає, лише Bot API з токеном із середовища контейнера):

  python tools/broadcast_20261128_app.py --dry          # лише підрахунок за групами й мовами, нічого не шле
  python tools/broadcast_20261128_app.py --only-admin   # шле лише ADMIN_ID обидва варіанти тексту його мовою
  python tools/broadcast_20261128_app.py --send         # бойовий запуск (лише без файлу-маркера)

Безпека:
- без явного --send нічого людям не йде; --send відмовляє, якщо є маркер `<base>.done`;
- маркер ставиться наприкінці успішного проходу;
- журнал `<base>.log` (TSV: час, user_id, група, мова, ok|blocked|error, пояснення). Перезапуск пропускає тих,
  кому вже ok, і тих, хто заблокував бота (blocked); помилки error повторяються на наступному запуску;
- швидкість 1 повідомлення за секунду; RetryAfter - чекаємо вказаний час і повторюємо;
  Unauthorized/Forbidden (бот заблокований) - у журнал, без повтору; інші помилки - у журнал, далі.
Шляхи (зміна лише для тестів): BROADCAST_BASE (за замовчуванням /app/config/broadcast_20261128_app).
"""

import argparse
import os
import sys
import time
from datetime import datetime
from typing import Callable, Dict, List, Optional, Set, Tuple

sys.path.insert(0, os.getenv("APP_DIR", "/app"))

import i18n  # noqa: E402
from database import DBSession, HousingLeaveEvent  # noqa: E402
from user_jobs import housing_tier, user_settings_store  # noqa: E402

BASE = os.getenv("BROADCAST_BASE", "/app/config/broadcast_20261128_app")
MARKER = BASE + ".done"
LOG = BASE + ".log"
ADMIN_LOG = BASE + ".admin.log"
PAUSE_SECONDS = 1.0
MAX_RETRY_AFTER_ATTEMPTS = 5
LANGS = ("uk", "ru", "de")

TEXT_ALL = {
    "uk": "Привіт! У Потсдамбота з'явився застосунок просто в Telegram. Майстри, моніторинг житла, терміни й "
          "анонімні питання тепер в одному місці, без команд і меню.",
    "ru": "Привет! У Потсдамбота появилось приложение прямо в Telegram. Мастера, мониторинг жилья, сроки и "
          "анонимные вопросы теперь в одном месте, без команд и меню.",
    "de": "Hallo! Der Potsdambot hat jetzt eine App direkt in Telegram. Handwerker, Wohnungsmonitoring, Termine "
          "und anonyme Fragen sind jetzt an einem Ort, ohne Befehle und Menüs.",
}
TEXT_PAID = {
    "uk": "Ваш моніторинг житла працює як раніше, квартири приходять у цей чат. Нове: пошуки, заявки, листи "
          "орендодавцю та статистика тепер відкриваються в застосунку. Під кожною квартирою є кнопка "
          "«Відкрити в Потсдамботі».",
    "ru": "Ваш мониторинг жилья работает как раньше, квартиры приходят в этот чат. Новое: поиски, заявки, письма "
          "арендодателю и статистика теперь открываются в приложении. Под каждой квартирой есть кнопка "
          "«Открыть в Потсдамботе».",
    "de": "Ihr Wohnungsmonitoring funktioniert wie bisher, die Wohnungen kommen in diesen Chat. Neu: Suchen, "
          "Bewerbungen, Briefe an den Vermieter und Statistik öffnen sich jetzt in der App. Unter jeder Wohnung "
          "gibt es die Schaltfläche „Im Potsdambot öffnen“.",
}


def text_for(lang: str, paid: bool) -> str:
    lang = lang if lang in LANGS else "uk"
    return (TEXT_PAID if paid else TEXT_ALL)[lang]


def keyboard(lang: str):
    """«Відкрити» (web_app, як на /start) + «Не надсилати новини» (callback appnews:off)."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup  # noqa: PLC0415
    from user_handlers import app_news, masters_hint  # noqa: PLC0415

    lang = lang if lang in LANGS else "uk"
    label = i18n.t("appnews.btn.open", lang)
    info = masters_hint.go_info()
    if info is not None:
        open_btn = InlineKeyboardButton(label, web_app=info)
    else:
        open_btn = InlineKeyboardButton(label, url=masters_hint.APP_LINK or masters_hint.link("bot"))
    off_btn = InlineKeyboardButton(i18n.t("appnews.btn.off", lang), callback_data=app_news.NEWS_OFF_DATA)
    return InlineKeyboardMarkup([[open_btn], [off_btn]])


def blocked_user_ids() -> Set[int]:
    """У кого ОСТАННЄ подія housing_leave_event - blocked."""
    session = DBSession()
    try:
        last: Dict[int, str] = {}
        for uid, kind in session.query(HousingLeaveEvent.user_id, HousingLeaveEvent.kind).order_by(
                HousingLeaveEvent.created_at, HousingLeaveEvent.id):
            last[int(uid)] = kind
        return {uid for uid, kind in last.items() if kind == "blocked"}
    finally:
        session.close()


def build_plan() -> Tuple[List[Tuple[int, str, str]], int]:
    """([(user_id, group, lang)], скільки пропущено як заблокували). group: paid | free."""
    subscribed = sorted(set(user_settings_store.list_subscribed_user_ids()))
    blocked = blocked_user_ids()
    paid_ids = housing_tier.premium_user_ids()
    plan = []
    skipped = 0
    for uid in subscribed:
        if uid in blocked:
            skipped += 1
            continue
        lang = i18n.get_lang(uid)
        lang = lang if lang in LANGS else "uk"
        plan.append((uid, "paid" if uid in paid_ids else "free", lang))
    return plan, skipped


def read_journal(path: str) -> Dict[int, str]:
    """user_id -> останній статус (ok|blocked|error) із журналу."""
    result: Dict[int, str] = {}
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                parts = line.rstrip("\n").split("\t")
                if len(parts) >= 5 and parts[1].lstrip("-").isdigit():
                    result[int(parts[1])] = parts[4]
    except OSError:
        pass
    return result


def journal(path: str, uid: int, group: str, lang: str, status: str, note: str = "") -> None:
    stamp = datetime.utcnow().isoformat(timespec="seconds")
    note = " ".join(str(note).split())[:120]
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(f"{stamp}\t{uid}\t{group}\t{lang}\t{status}\t{note}\n")


def deliver(send: Callable, sleep: Callable, uid: int, group: str, lang: str, log_path: str) -> str:
    """Одна відправка з повторами RetryAfter; повертає ok | blocked | error. Пауза 1 с після спроби - у run()."""
    from telegram.error import RetryAfter, Unauthorized  # noqa: PLC0415

    try:
        from telegram.error import Forbidden  # noqa: PLC0415  (є не в усіх версіях)
        blocked_errors: tuple = (Unauthorized, Forbidden)
    except ImportError:
        blocked_errors = (Unauthorized,)

    for attempt in range(1, MAX_RETRY_AFTER_ATTEMPTS + 1):
        try:
            send(uid, text_for(lang, group == "paid"), keyboard(lang))
            journal(log_path, uid, group, lang, "ok")
            return "ok"
        except RetryAfter as exc:
            wait = float(getattr(exc, "retry_after", 5)) + 1
            if attempt == MAX_RETRY_AFTER_ATTEMPTS:
                journal(log_path, uid, group, lang, "error", f"RetryAfter x{attempt}")
                return "error"
            sleep(wait)
        except blocked_errors as exc:
            journal(log_path, uid, group, lang, "blocked", f"{type(exc).__name__}: {exc}")
            return "blocked"
        except Exception as exc:  # noqa: BLE001 - одна помилка не зупиняє розсилку
            journal(log_path, uid, group, lang, "error", f"{type(exc).__name__}: {exc}")
            return "error"
    return "error"


def run(plan: List[Tuple[int, str, str]], send: Callable, sleep: Callable = time.sleep,
        log_path: Optional[str] = None, marker: Optional[str] = None) -> Dict[str, int]:
    """Проходить план, пропускаючи тих, кому вже ok/blocked у журналі. Маркер - наприкінці проходу."""
    log_path = log_path or LOG
    marker = marker or MARKER
    done = read_journal(log_path)
    counts = {"ok": 0, "blocked": 0, "error": 0, "skipped": 0}
    for uid, group, lang in plan:
        if done.get(uid) in ("ok", "blocked"):
            counts["skipped"] += 1
            continue
        counts[deliver(send, sleep, uid, group, lang, log_path)] += 1
        sleep(PAUSE_SECONDS)
    if marker:
        with open(marker, "w", encoding="utf-8") as handle:
            handle.write(f"{datetime.utcnow().isoformat(timespec='seconds')} {counts}\n")
    return counts


def summarize(plan, skipped_blocked: int, already: Optional[Dict[int, str]] = None) -> Dict[str, object]:
    by_group: Dict[str, Dict[str, int]] = {}
    for _, group, lang in plan:
        row = by_group.setdefault(group, {})
        row[lang] = row.get(lang, 0) + 1
        row["all"] = row.get("all", 0) + 1
    out: Dict[str, object] = {"recipients": len(plan), "by_group": by_group, "skipped_blocked": skipped_blocked}
    if already is not None:
        todo = sum(1 for uid, _, _ in plan if already.get(uid) not in ("ok", "blocked"))
        out["already_done_in_log"] = len(plan) - todo
        out["to_send"] = todo
        out["eta_minutes"] = round(todo * (PAUSE_SECONDS + 0.15) / 60, 1)
    return out


def _bot_sender():
    from telegram import Bot  # noqa: PLC0415

    bot = Bot(token=os.environ["BOT_TOKEN"])

    def send(uid, text, markup):
        bot.send_message(chat_id=uid, text=text, reply_markup=markup, disable_web_page_preview=True)

    return send


def main(argv: Optional[List[str]] = None, send_factory: Callable = _bot_sender) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry", action="store_true", help="лише підрахунок, нічого не шле")
    parser.add_argument("--only-admin", action="store_true", help="лише ADMIN_ID, обидва тексти його мовою")
    parser.add_argument("--send", action="store_true", help="бойовий запуск")
    args = parser.parse_args(argv)
    if sum([args.dry, args.only_admin, args.send]) != 1:
        parser.error("потрібен рівно один режим: --dry | --only-admin | --send")

    if args.only_admin:
        admin = int(os.getenv("ADMIN_ID", "0") or 0)
        if not admin:
            print("ADMIN_ID не задано")
            return 2
        lang = i18n.get_lang(admin)
        lang = lang if lang in LANGS else "uk"
        send = send_factory()
        for group in ("free", "paid"):
            status = deliver(send, time.sleep, admin, group, lang, ADMIN_LOG)
            print(f"admin {group}/{lang}: {status}")
            time.sleep(PAUSE_SECONDS)
        return 0

    plan, skipped = build_plan()
    if args.dry:
        print(summarize(plan, skipped, read_journal(LOG)))
        print("маркер:", "є" if os.path.exists(MARKER) else "немає", "| сухий прогін: нічого не надіслано")
        return 0

    if os.path.exists(MARKER):
        print(f"уже розсилали: {MARKER}")
        return 3
    print("start", summarize(plan, skipped, read_journal(LOG)), flush=True)
    counts = run(plan, send_factory())
    print("done", counts)
    return 0


if __name__ == "__main__":
    sys.exit(main())
