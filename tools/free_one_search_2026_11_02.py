"""02.11.2026: безкоштовно працює лише один пошук. Безкоштовним, у кого
працює кілька, лишаємо найстаріший, решту ставимо на паузу й пишемо їм.

  python tools/free_one_search_2026_11_02.py --dry           — хто й що, без змін
  python tools/free_one_search_2026_11_02.py --sample        — лише приклад листа адміну
  python tools/free_one_search_2026_11_02.py --send          — пауза + повідомлення
"""

import html
import os
import sys
import time

sys.path.insert(0, "/app")

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup  # noqa: E402

import i18n  # noqa: E402
from user_handlers import housing_card, housing_monitor as hm, housing_web  # noqa: E402

ADMIN = 312029534

TEXT = {
    "uk": """🔔 <b>Зміна в пошуку житла</b>

Безкоштовно тепер працює <b>один</b> пошук. Ваш пошук «{kept}» працює, як і раніше.

На паузу поставлено:
{paused}

Щоб працювали всі пошуки — підписка «⚡ Отримувати одразу» (5 €/міс): скільки завгодно пошуків, квартири без затримки й листи орендодавцям.
Або в «⚙️ Мої фільтри» поставте на паузу один пошук і ввімкніть інший.""",
    "ru": """🔔 <b>Изменение в поиске жилья</b>

Бесплатно теперь работает <b>один</b> поиск. Ваш поиск «{kept}» работает, как и раньше.

На паузу поставлено:
{paused}

Чтобы работали все поиски — подписка «⚡ Получать сразу» (5 €/мес): сколько угодно поисков, квартиры без задержки и письма арендодателям.
Или в «⚙️ Мои фильтры» поставьте один поиск на паузу и включите другой.""",
    "de": """🔔 <b>Änderung bei der Wohnungssuche</b>

Kostenlos läuft jetzt <b>eine</b> Suche. Ihre Suche „{kept}“ läuft weiter wie bisher.

Pausiert wurde:
{paused}

Damit alle Suchen laufen — Abo „⚡ Sofort erhalten“ (5 €/Monat): beliebig viele Suchen, Wohnungen ohne Verzögerung und Briefe an Vermieter.
Oder pausieren Sie unter „⚙️ Meine Filter“ eine Suche und schalten Sie eine andere ein.""",
}


def candidates():
    from database import DBSession, HousingSearch
    session = DBSession()
    try:
        users = sorted({int(row.user_id) for row in session.query(HousingSearch.user_id).all()})
    finally:
        session.close()
    result = []
    for user_id in users:
        if user_id == ADMIN or housing_card._unlimited(user_id):
            continue
        active = [s for s in hm.user_searches(user_id) if not hm._search_paused(s)]
        if len(active) > housing_card.FREE_SEARCH_LIMIT:
            result.append((user_id, active))
    return result


def message(lang, kept, paused):
    summary = lambda s: html.escape(housing_web._criteria_summary(s["criteria"], lang))  # noqa: E731
    return TEXT[lang].format(kept=summary(kept), paused="\n".join(f"⏸ {summary(s)}" for s in paused))


def markup(lang):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(i18n.t("housing.btn.request_access", lang), callback_data="housing:access_request")],
        [InlineKeyboardButton(i18n.t("housing.btn.self_manage", lang), callback_data="housing:self_manage")],
    ])


def main():
    mode = (sys.argv[1:] or ["--dry"])[0]
    found = candidates()
    print({"users": len(found), "searches_to_pause": sum(len(a) - 1 for _u, a in found)})
    bot = Bot(token=os.environ["BOT_TOKEN"])
    if mode == "--sample":
        user_id, active = found[0]
        lang = "uk"
        bot.send_message(chat_id=ADMIN, text=message(lang, active[0], active[1:]), parse_mode="HTML",
                         disable_web_page_preview=True, reply_markup=markup(lang))
        print("sample sent")
        return
    sent = failed = 0
    for user_id, active in found:
        lang = i18n.get_lang(user_id)
        if mode == "--dry":
            print(user_id, lang, "keep", active[0]["search_id"], "pause", [s["search_id"] for s in active[1:]])
            continue
        paused_ids = housing_card.pause_extra_searches(user_id)
        paused = [s for s in active if s["search_id"] in paused_ids]
        if not paused:
            continue
        try:
            bot.send_message(chat_id=user_id, text=message(lang, active[0], paused), parse_mode="HTML",
                             disable_web_page_preview=True, reply_markup=markup(lang))
            sent += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print("failed", user_id, exc)
        time.sleep(0.07)
    if mode == "--send":
        print({"sent": sent, "failed": failed})


if __name__ == "__main__":
    main()
