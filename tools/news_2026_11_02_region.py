"""Новина 02.11.2026: пошук житла в Берліні й Бранденбурзі — усім, хто користувався ботом.

Отримувачі — user_settings_store.list_subscribed_user_ids() (новини не вимкнені).
Кожному — його мовою, з кнопкою «відкрити пошук житла» (housing:menu).
Запуск: `python news_region.py --dry` (лише рахує) або `python news_region.py --send`.
"""

import os
import sys
import time

sys.path.insert(0, "/app")

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup  # noqa: E402

import i18n  # noqa: E402
from user_jobs import user_settings_store  # noqa: E402

TEXT = {
    "uk": """🏠 <b>Пошук житла: тепер Берлін і весь Бранденбург</b>

Бот більше не обмежується Потсдамом. Що нового:

📍 <b>Де шукати — обираєте самі.</b> Місто або поштовий індекс у Берліні чи Бранденбурзі (можна писати й українською: «Фалькензее», «Берлін») і радіус: лише місто або +5, +10, +20, +30, +50 км. У Берліні можна обрати округи (Mitte, Pankow…), у Потсдамі — як і раніше, райони.

🔎 <b>Більше порталів.</b> Поза Потсдамом шукаємо на ImmoScout24, Immowelt, Kleinanzeigen і Vonovia, а в Берліні — ще й на inberlinwohnen.de: квартири шести міських компаній (degewo, Gewobag, HOWOGE, Stadt und Land, WBM, berlinovo). Потсдамські агентства працюють, коли в зону пошуку потрапляє Потсдам.

🪪 <b>WBS.</b> Нова кнопка у фільтрі: «немає WBS» — не надсилатимемо квартир, де він обов'язковий; або вкажіть свій рівень (WBS 100…220).

🏛 <b>Ліміти Jobcenter</b> — тепер не лише для Потсдама, а й для Берліна та округів Havelland, Potsdam-Mittelmark, Oberhavel: бот сам поставить межі ціни за кількістю людей.

<b>Як увімкнути:</b> «🏘 Житло» → ваш фільтр → кнопка «📍 Де». Або «➕ Додати фільтр» — бот спитає місце, Jobcenter і WBS.
Ваші теперішні фільтри працюють як і раніше — лише Потсдам, доки ви не зміните місце.""",
    "ru": """🏠 <b>Поиск жилья: теперь Берлин и весь Бранденбург</b>

Бот больше не ограничивается Потсдамом. Что нового:

📍 <b>Где искать — выбираете сами.</b> Город или почтовый индекс в Берлине или Бранденбурге (можно писать и по-русски: «Фалькензее», «Берлин») и радиус: только город или +5, +10, +20, +30, +50 км. В Берлине можно выбрать округа (Mitte, Pankow…), в Потсдаме — как и раньше, районы.

🔎 <b>Больше порталов.</b> За пределами Потсдама ищем на ImmoScout24, Immowelt, Kleinanzeigen и Vonovia, а в Берлине — ещё и на inberlinwohnen.de: квартиры шести городских компаний (degewo, Gewobag, HOWOGE, Stadt und Land, WBM, berlinovo). Потсдамские агентства работают, когда в зону поиска попадает Потсдам.

🪪 <b>WBS.</b> Новая кнопка в фильтре: «нет WBS» — не будем присылать квартиры, где он обязателен; или укажите свой уровень (WBS 100…220).

🏛 <b>Лимиты Jobcenter</b> — теперь не только для Потсдама, но и для Берлина и округов Havelland, Potsdam-Mittelmark, Oberhavel: бот сам поставит границы цены по числу людей.

<b>Как включить:</b> «🏘 Жильё» → ваш фильтр → кнопка «📍 Где». Или «➕ Добавить фильтр» — бот спросит место, Jobcenter и WBS.
Ваши нынешние фильтры работают как раньше — только Потсдам, пока вы не смените место.""",
    "de": """🏠 <b>Wohnungssuche: jetzt auch Berlin und ganz Brandenburg</b>

Der Bot sucht nicht mehr nur in Potsdam. Was neu ist:

📍 <b>Wo gesucht wird, bestimmen Sie.</b> Stadt oder Postleitzahl in Berlin oder Brandenburg und ein Umkreis: nur die Stadt oder +5, +10, +20, +30, +50 km. In Berlin lassen sich Bezirke wählen (Mitte, Pankow…), in Potsdam wie bisher Stadtteile.

🔎 <b>Mehr Portale.</b> Außerhalb von Potsdam suchen wir auf ImmoScout24, Immowelt, Kleinanzeigen und Vonovia, in Berlin zusätzlich auf inberlinwohnen.de — Wohnungen der sechs städtischen Gesellschaften (degewo, Gewobag, HOWOGE, Stadt und Land, WBM, berlinovo). Die Potsdamer Anbieter suchen mit, sobald Potsdam im Suchgebiet liegt.

🪪 <b>WBS.</b> Neue Schaltfläche im Filter: „keinen WBS“ — Wohnungen mit WBS-Pflicht schicken wir nicht; oder geben Sie Ihre Stufe an (WBS 100…220).

🏛 <b>Jobcenter-Grenzen</b> jetzt nicht nur für Potsdam, sondern auch für Berlin und die Landkreise Havelland, Potsdam-Mittelmark und Oberhavel: Der Bot setzt die Preisgrenzen nach Personenzahl selbst.

<b>So geht's:</b> „🏘 Wohnungen“ → Ihr Filter → „📍 Wo“. Oder „➕ Filter hinzufügen“ — der Bot fragt nach Ort, Jobcenter und WBS.
Ihre bisherigen Filter laufen wie gewohnt weiter — nur Potsdam, bis Sie den Ort ändern.""",
}
BUTTON = {"uk": "🏠 Відкрити пошук житла", "ru": "🏠 Открыть поиск жилья", "de": "🏠 Wohnungssuche öffnen"}


def main() -> None:
    send = "--send" in sys.argv
    only = [int(arg) for arg in sys.argv[1:] if arg.isdigit()]
    recipients = only or [u for u in user_settings_store.list_subscribed_user_ids() if int(u) != 312029534]
    bot = Bot(token=os.environ["BOT_TOKEN"])
    sent = failed = 0
    by_lang: dict = {}
    for user_id in recipients:
        lang = i18n.get_lang(int(user_id))
        lang = lang if lang in TEXT else "uk"
        by_lang[lang] = by_lang.get(lang, 0) + 1
        if not send:
            continue
        markup = InlineKeyboardMarkup([[InlineKeyboardButton(BUTTON[lang], callback_data="housing:menu")]])
        try:
            bot.send_message(chat_id=int(user_id), text=TEXT[lang], parse_mode="HTML",
                             disable_web_page_preview=True, reply_markup=markup)
            sent += 1
        except Exception as exc:
            failed += 1
            print(f"failed {user_id}: {type(exc).__name__}: {str(exc)[:80]}")
        time.sleep(0.07)
    print({"recipients": len(recipients), "by_lang": by_lang, "sent": sent, "failed": failed,
           "lengths": {lang: len(text) for lang, text in TEXT.items()}})


if __name__ == "__main__":
    main()
