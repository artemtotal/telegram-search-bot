"""Сервісні повідомлення Mini App каталогу (27.11.2026): `write_access_allowed` і `web_app_data`.

Людина в Mini App натиснула «Дозволити» на запит `requestWriteAccess` (або відкрила застосунок через меню бота): Telegram
присилає боту службове повідомлення `write_access_allowed`. Бот лише відмічає її в таблиці webapp_user, нічого не відповідає
(для майбутньої розсилки, яку Артем запустить окремо).

python-telegram-bot 13.15 про це поле не знає (Bot API 6.7) і при розборі його відкидає, тому Message.de_json обгорнуто:
сире поле зберігається в `message.raw_write_access_allowed`.
"""
import logging

from telegram import Message
from telegram.ext import MessageHandler
from telegram.ext.filters import MessageFilter

from user_jobs import webapp_store

logger = logging.getLogger(__name__)

_orig_de_json = Message.de_json.__func__


def _de_json(cls, data, bot):
    message = _orig_de_json(cls, data, bot)
    if message is not None and isinstance(data, dict) and "write_access_allowed" in data:
        raw = data.get("write_access_allowed")
        message.raw_write_access_allowed = raw if isinstance(raw, dict) else {}
    return message


if not getattr(Message, "_webapp_access_patched", False):
    Message.de_json = classmethod(_de_json)
    Message._webapp_access_patched = True


class _WebappService(MessageFilter):
    name = "WebappService"

    def filter(self, message):
        return getattr(message, "raw_write_access_allowed", None) is not None or getattr(message, "web_app_data", None) is not None


def handle(update, context) -> None:
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None or getattr(user, "is_bot", False):
        return
    if getattr(message, "raw_write_access_allowed", None) is not None:
        source = "write_access_allowed"
        app_name = str((message.raw_write_access_allowed or {}).get("web_app_name") or "")
    else:
        source, app_name = "web_app_data", ""
    try:
        created = webapp_store.mark_write_allowed(user.id, source, app_name)
        logger.info("webapp_access: %s (%s)", source, "new" if created else "again")
    except Exception:
        logger.exception("webapp_access: could not save")


handler = MessageHandler(_WebappService(), handle)
