"""Новина 02.11.2026 (друга): сайт, ohne-makler.net, кооперативи Берліна — усім, хто користувався ботом.

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
    "uk": """🆕 <b>Пошук житла: ще кілька новинок</b>

🌐 <b>Сайт для вашої зручності.</b> Тепер пошуки можна налаштовувати на великому екрані: місто й радіус, ціни, кімнати, площа, WBS, портали, пауза. Там же статистика — скільки квартир прийшло й звідки — та історія: надіслані квартири й листи орендодавцям. Вхід без пароля: «🏘 Житло» → «🌐 Сайт» (для підписників «⚡ Отримувати одразу»).

🔑 <b>Новий портал — ohne-makler.net:</b> квартири від власників без комісії маклера, у Берліні й Бранденбурзі (і в Потсдамі). Уже додано до ваших пошуків.

🏘 <b>Кооперативи Берліна.</b> Стежимо за сторінками пропозицій 19 житлових кооперативів (Freie Scholle, IDEAL, Märkische Scholle, Wilhelmsruh та інші). Щойно там з'явиться нова квартира — бот напише з посиланням. Працює, якщо у вашому пошуку обрано Берлін; вимкнути можна в картці фільтра → «🌐 Портали».

Нагадаю: місто чи індекс і радіус — кнопка «📍 Де» у вашому фільтрі.""",
    "ru": """🆕 <b>Поиск жилья: ещё несколько новинок</b>

🌐 <b>Сайт для вашего удобства.</b> Теперь поиски можно настраивать на большом экране: город и радиус, цены, комнаты, площадь, WBS, порталы, пауза. Там же статистика — сколько квартир пришло и откуда — и история: присланные квартиры и письма арендодателям. Вход без пароля: «🏘 Жильё» → «🌐 Сайт» (для подписчиков «⚡ Получать сразу»).

🔑 <b>Новый портал — ohne-makler.net:</b> квартиры от владельцев без комиссии маклера, в Берлине и Бранденбурге (и в Потсдаме). Уже добавлен в ваши поиски.

🏘 <b>Кооперативы Берлина.</b> Следим за страницами предложений 19 жилищных кооперативов (Freie Scholle, IDEAL, Märkische Scholle, Wilhelmsruh и другие). Как только там появится новая квартира — бот напишет со ссылкой. Работает, если в вашем поиске выбран Берлин; отключить можно в карточке фильтра → «🌐 Порталы».

Напомню: город или индекс и радиус — кнопка «📍 Где» в вашем фильтре.""",
    "de": """🆕 <b>Wohnungssuche: noch ein paar Neuheiten</b>

🌐 <b>Eine Website für mehr Komfort.</b> Suchen lassen sich jetzt am großen Bildschirm einstellen: Ort und Umkreis, Preise, Zimmer, Fläche, WBS, Portale, Pause. Dazu Statistik — wie viele Wohnungen von wo kamen — und Verlauf: geschickte Wohnungen und Briefe an Vermieter. Anmeldung ohne Passwort: „🏘 Wohnungen“ → „🌐 Website“ (für Abonnenten „⚡ Sofort erhalten“).

🔑 <b>Neues Portal — ohne-makler.net:</b> Wohnungen direkt vom Eigentümer ohne Maklerprovision, in Berlin und Brandenburg (auch Potsdam). Bereits in Ihre Suchen aufgenommen.

🏘 <b>Berliner Genossenschaften.</b> Wir beobachten die Angebotsseiten von 19 Wohnungsgenossenschaften (Freie Scholle, IDEAL, Märkische Scholle, Wilhelmsruh u. a.). Sobald dort eine neue Wohnung erscheint, schreibt der Bot mit Link. Gilt, wenn Berlin in Ihrer Suche liegt; abschalten in der Filterkarte → „🌐 Portale“.

Zur Erinnerung: Stadt oder PLZ und Umkreis — Schaltfläche „📍 Wo“ in Ihrem Filter.""",
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
