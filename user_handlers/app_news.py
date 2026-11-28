"""28.11.2026: кнопка «Не надсилати новини» під разовою розсилкою про застосунок (tools/broadcast_20261128_app.py).

callback `appnews:off` вимикає news_subscribed (user_settings_store.set_news_subscribed), коротко підтверджує
і підказує, де увімкнути назад (Житло → Сповіщення). Саму розсилку дивись у tools/broadcast_20261128_app.py.
"""
import logging

from telegram import InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import CallbackContext, CallbackQueryHandler

import i18n
from user_jobs import user_settings_store

logger = logging.getLogger(__name__)

NEWS_OFF_DATA = "appnews:off"


def handle_off(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    user = update.effective_user
    if query is None or user is None:
        return
    lang = i18n.get_lang(int(user.id))
    try:
        user_settings_store.set_news_subscribed(int(user.id), False)
    except Exception:
        logger.exception("app_news: could not unsubscribe %s", user.id)
        query.answer(i18n.t("appnews.off.failed", lang), show_alert=True)
        return
    query.answer(i18n.t("appnews.off.done", lang), show_alert=True)
    # Прибираємо кнопку «Не надсилати», лишаємо «Відкрити».
    try:
        markup = query.message.reply_markup if query.message else None
        if markup is not None:
            rows = [[b for b in row if b.callback_data != NEWS_OFF_DATA] for row in markup.inline_keyboard]
            rows = [row for row in rows if row]
            query.edit_message_reply_markup(InlineKeyboardMarkup(rows) if rows else None)
    except TelegramError:
        logger.debug("app_news: keyboard not updated", exc_info=True)


callback_handler = CallbackQueryHandler(handle_off, pattern=r"^appnews:off$")
