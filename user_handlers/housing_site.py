"""Вхід у браузері на сайт «Потсдам» (пакет E, 27.11.2026).

Сайт каталогу (masters.json "url") показує людині «Моє житло»: статус, фільтри й квартири, які бот їй надіслав. У Telegram сайт
знає людину з підпису Mini App, а в звичайному браузері — по одноразовому посиланню з бота: `/site` або кнопка «🌐 Сайт» у меню
житла дають `<сайт>/?k=<код>`.

Код = `<id>.<час>.<випадкове>.<підпис>`, підпис — HMAC-SHA256 секретом із файла SECRET_FILE (в томі даних бота, сайт читає той самий
файл лише на читання). Живе 10 хвилин; сайт запам'ятовує використані коди й приймає кожен лише раз, тому HTTP між ботом і сайтом
не потрібен. Той самий формат перевіряє `housing_api.py` сайту.

Правило: сайт нічого не пише в базу бота; бот не знає про сесії сайту.
"""

import hashlib
import hmac
import logging
import os
import secrets
import time
from typing import Optional
from urllib.parse import urlparse

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CommandHandler

import i18n

logger = logging.getLogger(__name__)

SECRET_FILE = os.environ.get("SITE_LOGIN_SECRET_FILE", "/app/config/site_login_secret")
DEFAULT_SITE = "https://mastera.2-29-18-111.sslip.io"
CODE_TTL = 600


def _secret() -> Optional[bytes]:
    try:
        with open(SECRET_FILE, "rb") as handle:
            value = handle.read().strip()
        return value or None
    except OSError:
        return None


def _sign(secret: bytes, text: str) -> str:
    return hmac.new(secret, ("login:" + text).encode(), hashlib.sha256).hexdigest()[:40]


def make_code(user_id: int, now: Optional[float] = None) -> Optional[str]:
    """Одноразовий код входу або None, якщо файла з секретом немає."""
    secret = _secret()
    if not secret:
        return None
    body = "%d.%d.%s" % (int(user_id), int(now if now is not None else time.time()), secrets.token_hex(4))
    return body + "." + _sign(secret, body)


def site_origin() -> str:
    from user_handlers import masters_hint

    parsed = urlparse(masters_hint.MASTERS_URL or "")
    if parsed.scheme == "https" and parsed.netloc:
        return f"https://{parsed.netloc}"
    return DEFAULT_SITE


def site_link(user_id: int) -> Optional[str]:
    code = make_code(user_id)
    return f"{site_origin()}/?k={code}" if code else None


def _keyboard(user_id: int, lang: str) -> Optional[InlineKeyboardMarkup]:
    link = site_link(user_id)
    if not link:
        return None
    rows = [[InlineKeyboardButton(i18n.t("housing.site.btn", lang), url=link)]]
    return InlineKeyboardMarkup(rows)


def _old_cabinet_row(user_id: int, lang: str):
    """Старий кабінет бота (зміна фільтрів на великому екрані) лишається поруч, доки нове не вміє те саме."""
    try:
        from user_handlers import housing_web

        if housing_web.PUBLIC_URL and housing_web.allowed(user_id):
            return [InlineKeyboardButton(i18n.t("housing.site.btn_old", lang), url=housing_web.create_login_link(user_id))]
    except Exception:
        logger.exception("housing_site: old cabinet link failed")
    return None


def send_link(context, user_id: int, with_old: bool) -> bool:
    lang = i18n.get_lang(user_id)
    markup = _keyboard(user_id, lang)
    if markup is None:
        context.bot.send_message(chat_id=user_id, text=i18n.t("housing.site.off", lang))
        return False
    if with_old:
        row = _old_cabinet_row(user_id, lang)
        if row:
            markup = InlineKeyboardMarkup(list(markup.inline_keyboard) + [row])
    context.bot.send_message(chat_id=user_id, text=i18n.t("housing.site.text", lang), reply_markup=markup,
                             parse_mode="HTML", disable_web_page_preview=True)
    return True


def site_command(update, context) -> None:
    chat = update.effective_chat
    user = update.effective_user
    if chat is None or user is None or chat.type != "private":
        return
    try:
        send_link(context, int(user.id), with_old=True)
    except Exception:
        logger.exception("housing_site: /site failed")


def site_callback(update, context) -> None:
    """Кнопка «🌐 Сайт» у меню житла (housing:web): нове посилання на «Потсдам» і, поруч, старий кабінет."""
    query = update.callback_query
    user_id = int(update.effective_user.id)
    try:
        query.answer()
        send_link(context, user_id, with_old=True)
    except Exception:
        logger.exception("housing_site: button failed")


command_handler = CommandHandler(["site", "sait"], site_command)
