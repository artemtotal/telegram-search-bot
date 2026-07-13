"""
Handler for admin FAQ approval/rejection via inline keyboard buttons.
"""

import json
import logging
import os

from telegram import Update
from telegram.ext import CallbackContext, CallbackQueryHandler

logger = logging.getLogger(__name__)

FAQ_PATH = os.getenv("FAQ_PATH", "/app/config/faq.json")
FAQ_PENDING_PATH = os.getenv("FAQ_PENDING_PATH", "/app/config/faq_pending.json")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))


def _load_json(path: str, default):
    """Load JSON file, return default if missing."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except Exception as e:
        logger.error(f"Failed to load {path}: {e}")
        return default


def _save_json(path: str, data) -> bool:
    """Save data to JSON file without BOM."""
    try:
        content = json.dumps(data, ensure_ascii=False, indent=2)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return True
    except Exception as e:
        logger.error(f"Failed to save {path}: {e}")
        return False


def handle_faq_callback(update: Update, context: CallbackContext) -> None:
    """Handle approve/reject button presses from admin."""
    query = update.callback_query
    if not query:
        return

    # Only admin can approve
    if query.from_user.id != ADMIN_ID:
        query.answer("Нет доступа.")
        return

    data = query.data or ""

    if data.startswith("faq_approve_"):
        index = int(data.split("_")[-1])
        pending = _load_json(FAQ_PENDING_PATH, [])

        if index >= len(pending):
            query.answer("Запись не найдена.")
            query.edit_message_text(query.message.text + "\n\n❌ Ошибка: запись не найдена.")
            return

        entry = pending[index]
        faq = _load_json(FAQ_PATH, [])
        faq.append(entry)
        _save_json(FAQ_PATH, faq)

        # Mark as approved in pending
        pending[index]["_approved"] = True
        _save_json(FAQ_PENDING_PATH, pending)

        keywords_str = ", ".join(entry.get("keywords", []))
        query.answer("Принято!")
        query.edit_message_text(
            query.message.text + f"\n\n✅ Принято и добавлено в FAQ.\nКлючевые слова: {keywords_str}"
        )
        logger.info(f"FAQ entry #{index} approved: {keywords_str}")

    elif data.startswith("faq_reject_"):
        index = int(data.split("_")[-1])
        pending = _load_json(FAQ_PENDING_PATH, [])

        if index >= len(pending):
            query.answer("Запись не найдена.")
            return

        pending[index]["_rejected"] = True
        _save_json(FAQ_PENDING_PATH, pending)

        query.answer("Отклонено.")
        query.edit_message_text(query.message.text + "\n\n❌ Отклонено.")
        logger.info(f"FAQ entry #{index} rejected")


# Handler registration
handler = CallbackQueryHandler(
    handle_faq_callback,
    pattern=r"^faq_(approve|reject)_\d+$"
)
