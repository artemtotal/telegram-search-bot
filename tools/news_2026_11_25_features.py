"""Новина про нове в пошуку житла (заготовка на 25.11.2026): ціни, «Мої заявки», «Папка заявника»,
«Запроси друга», видимість оголошення, Берлін і Бранденбург - усім, хто користувався житлом.

Текст складається з абзаців-перемикачів: оголошуємо лише те, що справді виложено.
    --with price,tracker,mappe,referral,stats,region

Отримувачі: хто користувався житлом (є пошук, або відкривав меню житла, або мав доступ,
або йому вже надсилали квартири) і не вимкнув новини. Кожному - його мовою (uk/ru/de).
Безкоштовним унизу кнопка підписки, платним - рядок «у вас уже все працює».

За замовчуванням - СУХИЙ ПРОГІН: друкує кількість отримувачів за мовами й тексти, нічого не шле.

    python news_2026_11_25_features.py                              # сухий прогін, усі абзаци
    python news_2026_11_25_features.py --with price,mappe           # сухий прогін, лише ці абзаци
    python news_2026_11_25_features.py --with price,mappe --send    # справді розіслати

Безпека відправки:
- без явного --with розсилка не йде (щоб випадково не оголосити невиложене);
- файл `<done>.sent` - кому вже надіслано (перервану розсилку можна продовжити без дублів);
- файл `<done>` (.done) створюється по завершенні: другий запуск `--send` відмовляє (`--force` - свідомо повторити);
- журнал - `<done>.log`: час, id, результат.
Шляхи - у config/ (NEWS_DONE_FILE), поряд із базою.
"""

import argparse
import os
import sys
import time
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Set

sys.path.insert(0, os.getenv("APP_DIR", "/app"))

import i18n  # noqa: E402
from database import DBSession, HousingAccessUser, HousingSearch, HousingSendLog, HousingUserJourney  # noqa: E402
from user_jobs import housing_tier, user_settings_store  # noqa: E402

DONE_FILE = os.getenv("NEWS_DONE_FILE", "config/news_2026_11_25_features.done")
EXCLUDE = {int(x) for x in os.getenv("NEWS_EXCLUDE", "312029534").split(",") if x.strip().isdigit()}
SEND_PAUSE_SECONDS = 0.1
TELEGRAM_LIMIT = 4096
ORDER = ("price", "tracker", "mappe", "referral", "stats", "region")

HEAD = {
    "uk": "🆕 <b>Пошук житла: що нового</b>",
    "ru": "🆕 <b>Поиск жилья: что нового</b>",
    "de": "🆕 <b>Wohnungssuche: Neuigkeiten</b>",
}
PARTS = {
    "price": {
        "uk": "💶 <b>Нові ціни підписки.</b>\n1 місяць — 5 €, 3 місяці — 12 €, «поки не знайду житло» — 15 € разово.",
        "ru": "💶 <b>Новые цены подписки.</b>\n1 месяц — 5 €, 3 месяца — 12 €, «пока не найду жильё» — 15 € разово.",
        "de": "💶 <b>Neue Abo-Preise.</b>\n1 Monat — 5 €, 3 Monate — 12 €, „bis ich eine Wohnung finde“ — 15 € einmalig.",
    },
    "tracker": {
        "uk": "📋 <b>«Мої заявки».</b>\nВідмічайте, куди ви вже подалися, — бот нагадає про перегляд квартири.",
        "ru": "📋 <b>«Мои заявки».</b>\nОтмечайте, куда вы уже подались, — бот напомнит о просмотре квартиры.",
        "de": "📋 <b>„Meine Bewerbungen“.</b>\nMarkieren Sie, wo Sie sich beworben haben — der Bot erinnert Sie an den Besichtigungstermin.",
    },
    "mappe": {
        "uk": "📁 <b>«Папка заявника».</b>\nОдин PDF німецькою для орендодавця: титульний лист, супровідний лист, анкета орендаря й список документів. Для підписників ⚡",
        "ru": "📁 <b>«Папка заявителя».</b>\nОдин PDF на немецком для арендодателя: титульный лист, сопроводительное письмо, анкета арендатора и список документов. Для подписчиков ⚡",
        "de": "📁 <b>„Bewerbungsmappe“.</b>\nEin PDF auf Deutsch für den Vermieter: Deckblatt, Anschreiben, Mieterselbstauskunft und Liste der Unterlagen. Für Abonnenten ⚡",
    },
    "referral": {
        "uk": "🎁 <b>«Запроси друга».</b>\nМісяць у подарунок вам, коли друг оформить підписку.",
        "ru": "🎁 <b>«Пригласи друга».</b>\nМесяц в подарок вам, когда друг оформит подписку.",
        "de": "🎁 <b>„Freund einladen“.</b>\nEin Monat geschenkt für Sie, sobald Ihr Freund ein Abo abschließt.",
    },
    "stats": {
        "uk": "👀 <b>Під оголошенням</b> тепер видно, коли його отримали підписники й скільки людей його вже відкрили.",
        "ru": "👀 <b>Под объявлением</b> теперь видно, когда его получили подписчики и сколько людей его уже открыли.",
        "de": "👀 <b>Unter der Anzeige</b> sehen Sie jetzt, wann Abonnenten sie erhalten haben und wie viele Personen sie schon geöffnet haben.",
    },
    "region": {
        "uk": "📍 <b>Нагадую:</b> пошук працює по Берліну й усьому Бранденбургу. Місто чи індекс і радіус — кнопка «📍 Де» у вашому фільтрі.",
        "ru": "📍 <b>Напоминаю:</b> поиск работает по Берлину и всему Бранденбургу. Город или индекс и радиус — кнопка «📍 Где» в вашем фильтре.",
        "de": "📍 <b>Zur Erinnerung:</b> Die Suche funktioniert in Berlin und ganz Brandenburg. Stadt oder PLZ und Umkreis — Schaltfläche „📍 Wo“ in Ihrem Filter.",
    },
}
PAID_LINE = {
    "uk": "✅ У вас уже все працює.",
    "ru": "✅ У вас уже всё работает.",
    "de": "✅ Bei Ihnen läuft schon alles.",
}
OPEN_BUTTON = {"uk": "🏠 Відкрити пошук житла", "ru": "🏠 Открыть поиск жилья", "de": "🏠 Wohnungssuche öffnen"}


def parse_parts(raw: Optional[str]) -> List[str]:
    """«price,mappe» -> [price, mappe] у сталому порядку; невідоме - помилка."""
    names = [x.strip() for x in (raw or "").split(",") if x.strip()]
    unknown = [x for x in names if x not in PARTS]
    if unknown:
        raise ValueError(f"невідомі абзаци: {', '.join(unknown)} (є: {', '.join(ORDER)})")
    return [x for x in ORDER if x in names]


def compose(lang: str, parts: Iterable[str], paid: bool) -> str:
    lang = lang if lang in HEAD else "uk"
    blocks = [HEAD[lang]] + [PARTS[name][lang] for name in parts]
    if paid:
        blocks.append(PAID_LINE[lang])
    return "\n\n".join(blocks)


def housing_users() -> Set[int]:
    """Хто користувався житлом: пошук, меню, доступ або вже надіслані квартири."""
    session = DBSession()
    try:
        ids: Set[int] = set()
        for column in (HousingSearch.user_id, HousingUserJourney.user_id, HousingAccessUser.user_id,
                       HousingSendLog.user_id):
            ids.update(int(row[0]) for row in session.query(column).distinct())
        return ids
    finally:
        session.close()


def recipients() -> List[int]:
    wanted = housing_users()
    subscribed = set(user_settings_store.list_subscribed_user_ids())
    return sorted(uid for uid in wanted & subscribed if uid not in EXCLUDE)


def _read_ids(path: str) -> Set[int]:
    try:
        with open(path, encoding="utf-8") as handle:
            return {int(line) for line in handle.read().split() if line.strip().lstrip("-").isdigit()}
    except OSError:
        return set()


def _log(line: str) -> None:
    with open(DONE_FILE + ".log", "a", encoding="utf-8") as handle:
        handle.write(f"{datetime.utcnow().isoformat(timespec='seconds')} {line}\n")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--with", dest="with_parts", default=None, help="абзаци: " + ",".join(ORDER))
    parser.add_argument("--send", action="store_true", help="справді розіслати (інакше сухий прогін)")
    parser.add_argument("--force", action="store_true", help="повторити, хоч .done уже є")
    args = parser.parse_args(argv)
    if args.send and not args.with_parts:
        parser.error("--send потребує явного --with (що саме виложено)")
    try:
        parts = parse_parts(args.with_parts if args.with_parts is not None else ",".join(ORDER))
    except ValueError as exc:
        parser.error(str(exc))
    if not parts:
        parser.error("жодного абзацу")

    users = recipients()
    paid_ids = housing_tier.premium_user_ids()
    by_lang: Dict[str, Dict[str, int]] = {}
    for uid in users:
        lang = i18n.get_lang(uid)
        row = by_lang.setdefault(lang, {"free": 0, "paid": 0})
        row["paid" if uid in paid_ids else "free"] += 1
    lengths = {lang: len(compose(lang, parts, True)) for lang in HEAD}
    too_long = [lang for lang, size in lengths.items() if size > TELEGRAM_LIMIT]
    if too_long:
        print("текст задовгий для Telegram:", too_long)
        return 1

    print(f"абзаци: {', '.join(parts)}")
    print({"recipients": len(users), "by_lang": by_lang, "lengths": lengths})
    if not args.send:
        for lang in ("uk", "ru", "de"):
            print(f"\n----- {lang} (платному додається останній рядок) -----")
            print(compose(lang, parts, False).replace("<b>", "").replace("</b>", ""))
            print("  + платним:", PAID_LINE[lang])
        print("\nсухий прогін: нічого не надіслано")
        return 0

    if os.path.exists(DONE_FILE) and not args.force:
        print(f"уже розсилали ({DONE_FILE}); --force, якщо справді треба ще раз")
        return 3
    from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup  # noqa: PLC0415
    from telegram.error import RetryAfter  # noqa: PLC0415

    bot = Bot(token=os.environ["BOT_TOKEN"])
    sent_path = DONE_FILE + ".sent"
    already = set() if args.force else _read_ids(sent_path)
    sent = failed = 0
    for uid in users:
        if uid in already:
            continue
        lang = i18n.get_lang(uid)
        lang = lang if lang in HEAD else "uk"
        paid = uid in paid_ids
        rows = [[InlineKeyboardButton(OPEN_BUTTON[lang], callback_data="housing:menu")]]
        if not paid:
            rows.insert(0, [InlineKeyboardButton(i18n.t("housing.btn.request_access", lang),
                                                 callback_data="housing:access_request")])
        markup = InlineKeyboardMarkup(rows)
        text = compose(lang, parts, paid)
        for attempt in (1, 2):
            try:
                bot.send_message(chat_id=uid, text=text, parse_mode="HTML", disable_web_page_preview=True,
                                 reply_markup=markup)
                sent += 1
                _log(f"{uid} ok")
                with open(sent_path, "a", encoding="utf-8") as handle:
                    handle.write(f"{uid}\n")
                break
            except RetryAfter as exc:
                if attempt == 2:
                    failed += 1
                    _log(f"{uid} fail RetryAfter")
                    break
                time.sleep(float(getattr(exc, "retry_after", 5)) + 1)
            except Exception as exc:
                failed += 1
                _log(f"{uid} fail {type(exc).__name__}: {str(exc)[:80]}")
                print(f"failed {uid}: {type(exc).__name__}: {str(exc)[:80]}")
                break
        time.sleep(SEND_PAUSE_SECONDS)
    with open(DONE_FILE, "w", encoding="utf-8") as handle:
        handle.write(f"{datetime.utcnow().isoformat(timespec='seconds')} sent={sent} failed={failed} parts={','.join(parts)}\n")
    print({"recipients": len(users), "sent": sent, "failed": failed})
    return 0


if __name__ == "__main__":
    sys.exit(main())
