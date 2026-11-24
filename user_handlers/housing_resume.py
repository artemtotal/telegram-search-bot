"""Кнопка «▶️ Почати пошук» поза карткою фільтра (25.11.2026).

Новий пошук із картки створюється на паузі й стартує лише після «▶️ Почати пошук»
(housing_card). Хто цю кнопку не натиснув, сидить без квартир, навіть оплативши
підписку: так було з підписником від 11.11 - 13 днів платив і не отримав жодної.
Тож особисте повідомлення (tools/nudge_2026_11_25_zero.py) несе кнопку
`housing:resume:<search_id>`; вона вмикає пошук так само, як кнопка в картці,
зі звітом «за 30 днів підійшло б N».
"""

import logging

from telegram import Update
from telegram.ext import CallbackContext

import i18n
from user_handlers import housing_card, housing_monitor as hm

logger = logging.getLogger(__name__)

CALLBACK_PREFIX = "housing:resume:"


def apply(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    user = update.effective_user
    if not query or not user:
        return
    lang = i18n.get_lang(user.id)
    try:
        search_id = int(query.data.split(":")[2])
    except (IndexError, ValueError):
        query.answer()
        return
    number, search = housing_card._load(int(user.id), search_id)
    if search is None or int(search["user_id"]) != int(user.id) or not hm.is_allowed(user.id):
        query.answer(i18n.t("housing.toast.not_your_filter", lang), show_alert=True)
        return
    if not hm._search_paused(search):
        query.answer(i18n.t("housing.card.started_toast", lang))
        return
    if not housing_card.can_activate(int(user.id), search_id):
        query.answer(i18n.t("housing.card.only_one_active", lang), show_alert=True)
        return
    query.answer(i18n.t("housing.card.started_toast", lang))
    try:
        query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    # Той самий перший запуск, що й після кнопки в новій картці: звіт, привітання, подія.
    context.user_data["hcard"] = {"sid": search_id, "new": True}
    housing_card._set_active(update, context, search, True)
    context.user_data.pop("hcard", None)
    logger.info("Search %s of %s started from the reminder button", search_id, user.id)
