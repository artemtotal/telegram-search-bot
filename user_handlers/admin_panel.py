"""Private admin panel for host maintenance actions."""

import json
import logging
import os
import subprocess

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackContext, CallbackQueryHandler, CommandHandler

logger = logging.getLogger(__name__)

ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
ORGANIZER_SCRIPT = os.getenv("ORGANIZER_SCRIPT", "/app/tools/kodi_organizer.py")
SERIES_LIBRARY_ROOT = os.getenv("SERIES_LIBRARY_ROOT", "/media/series")
SERIES_CALLBACK = "admin_organize_series"


def _is_private_admin(update: Update) -> bool:
    user = update.effective_user
    chat = update.effective_chat
    return bool(
        ADMIN_ID
        and user
        and user.id == ADMIN_ID
        and chat
        and chat.type == "private"
    )


def _run_mounted_series_organizer(runner=subprocess.run) -> dict:
    command = [
        "python",
        ORGANIZER_SCRIPT,
        "--scan-series",
        "--library-root",
        SERIES_LIBRARY_ROOT,
        "--log",
        "/tmp/kodi-organizer.log",
    ]
    completed = runner(
        command,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    if completed.returncode != 0:
        details = (completed.stderr or completed.stdout or "unknown error").strip()
        raise RuntimeError(details[-800:])
    result = json.loads(completed.stdout)
    message = (
        f"Готово: перемещено серий — {result.get('moved', 0)}; "
        f"недокачанных пропущено — {result.get('skipped_incomplete', 0)}."
    )
    if result.get("skipped_existing"):
        message += f" Дубликатов пропущено — {result['skipped_existing']}."
    return {"ok": True, "message": message, "result": result}


def show_admin_panel(update: Update, context: CallbackContext) -> None:
    """Show the private panel only to the configured administrator."""
    if not _is_private_admin(update):
        return
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Разобрать сериалы", callback_data=SERIES_CALLBACK)]]
    )
    context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="Админ-панель",
        reply_markup=keyboard,
    )


def run_series_organizer(
    update: Update,
    context: CallbackContext,
    action_client=_run_mounted_series_organizer,
) -> None:
    """Run the organizer against the mounted Series library."""
    query = update.callback_query
    if not query:
        return
    if not _is_private_admin(update):
        query.answer("Нет доступа.", show_alert=True)
        return

    query.answer("Запускаю разбор сериалов…")
    try:
        payload = action_client()
        message = payload.get("message") or "Готово."
    except Exception as exc:
        logger.exception("Series organizer admin action failed")
        message = f"Ошибка разбора сериалов: {exc}"
    query.edit_message_text(message)


command_handler = CommandHandler("admin", show_admin_panel)
callback_handler = CallbackQueryHandler(
    run_series_organizer,
    pattern=f"^{SERIES_CALLBACK}$",
)
