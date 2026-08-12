"""Private housing monitoring menu backed by the local check-Wohnung receiver."""

import html
import logging
import os
from typing import Dict, Iterable, Optional

import requests
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackContext, CallbackQueryHandler, CommandHandler, Filters
from telegram.error import BadRequest

logger = logging.getLogger(__name__)

ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
CHECK_WOHNUNG_BASE_URL = os.getenv(
    "CHECK_WOHNUNG_BASE_URL",
    "http://host.docker.internal:18765",
).rstrip("/")
TIMEOUT = int(os.getenv("HOUSING_MONITOR_TIMEOUT", "20") or 20)
BTN_ADMIN_ADD = "➕ Додати користувача житла"
BTN_ADMIN_LIST = "📋 Користувачі житла"
BTN_CANCEL = "✖ Скасувати"


def _request(method: str, path: str, **kwargs) -> Dict[str, object]:
    url = f"{CHECK_WOHNUNG_BASE_URL}{path}"
    response = requests.request(method, url, timeout=TIMEOUT, **kwargs)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or not payload.get("ok"):
        raise RuntimeError(str(payload.get("error") if isinstance(payload, dict) else payload))
    return payload


def _tasks() -> list:
    try:
        payload = _request("GET", "/api/housing/tasks")
    except Exception:
        logger.exception("Could not load housing tasks")
        return []
    tasks = payload.get("tasks")
    return tasks if isinstance(tasks, list) else []


def user_filters(user_id: Optional[int]) -> list:
    if not user_id:
        return []
    return [task for task in _tasks() if int(task.get("user_id") or 0) == int(user_id)]


def is_allowed(user_id: Optional[int]) -> bool:
    return bool(user_id and (int(user_id) == ADMIN_ID or user_filters(int(user_id))))


def private_home_rows(user_id: Optional[int]) -> Iterable[list]:
    if not is_allowed(user_id):
        return []
    return [[InlineKeyboardButton("🏠 Моніторинг житла", callback_data="housing:menu")]]


def _menu_keyboard(user_id: Optional[int] = None) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton("🔄 Оновити статус", callback_data="housing:menu")]]
    if user_id and int(user_id) == ADMIN_ID:
        rows.insert(0, [InlineKeyboardButton("⚙️ Адмінка житла", callback_data="housing:admin")])
    rows.append([InlineKeyboardButton("⬅ Головне меню", callback_data="anon:home")])
    return InlineKeyboardMarkup(rows)


def _admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(BTN_ADMIN_ADD, callback_data="housing:add")],
        [InlineKeyboardButton(BTN_ADMIN_LIST, callback_data="housing:list")],
        [InlineKeyboardButton("⬅ До моніторингу", callback_data="housing:menu")],
    ])


def _render_menu(user_id: int) -> str:
    filters = user_filters(user_id)
    lines = [
        "🏠 <b>Моніторинг житла</b>",
        "",
        "Бот перевіряє ваші посилання Immowelt через браузерний профіль Артема й надсилає нові оголошення.",
        "",
    ]
    if not filters:
        lines.append("Для вашого Telegram ID поки немає активних фільтрів.")
    else:
        lines.append("Ваші фільтри:")
        for item in filters:
            lines.append(
                f"• #{int(item.get('filter_id'))}: {html.escape(str(item.get('title') or 'Пошук житла'))}"
            )
    return "\n".join(lines)


def show_menu(update: Update, context: CallbackContext, edit: bool = False) -> None:
    user = update.effective_user
    if not user or not is_allowed(user.id):
        text = "Моніторинг житла зараз доступний тільки користувачам із дозволеним Telegram ID."
        if edit and update.callback_query:
            update.callback_query.edit_message_text(text)
        else:
            update.effective_message.reply_text(text)
        return
    text = _render_menu(user.id)
    if edit and update.callback_query:
        try:
            update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=_menu_keyboard(user.id))
        except BadRequest as exc:
            if "Message is not modified" not in str(exc):
                raise
    else:
        update.effective_message.reply_text(text, parse_mode="HTML", reply_markup=_menu_keyboard(user.id))



def _render_admin() -> str:
    tasks = _tasks()
    lines = ["⚙️ <b>Адмінка житла</b>", "", "Тут можна додати користувача до моніторингу Immowelt.", ""]
    if not tasks:
        lines.append("Активних фільтрів поки немає.")
    else:
        lines.append("Активні користувачі житла:")
        for item in tasks:
            lines.append(f"• #{int(item.get('filter_id'))} · {int(item.get('user_id'))} · {html.escape(str(item.get('title') or 'Пошук житла'))}")
    return "\n".join(lines)


def show_admin(update: Update, context: CallbackContext, edit: bool = False) -> None:
    user = update.effective_user
    if not user or int(user.id) != ADMIN_ID:
        return
    text = _render_admin()
    if edit and update.callback_query:
        update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=_admin_keyboard())
    else:
        update.effective_message.reply_text(text, parse_mode="HTML", reply_markup=_admin_keyboard())


def start_add_flow(update: Update, context: CallbackContext, edit: bool = False) -> None:
    user = update.effective_user
    if not user or int(user.id) != ADMIN_ID:
        return
    context.user_data["housing_admin"] = {"step": "user_id"}
    text = "➕ <b>Додати користувача житла</b>\n\nНадішліть Telegram ID користувача."
    if edit and update.callback_query:
        update.callback_query.edit_message_text(text, parse_mode="HTML")
    else:
        update.effective_message.reply_text(text, parse_mode="HTML")


def _finish_add_flow(update: Update, context: CallbackContext, state: dict, url: str) -> None:
    try:
        payload = _request("POST", "/api/housing/filters", json={"user_id": state["user_id"], "title": state["title"], "url": url})
    except Exception as exc:
        logger.exception("Could not add housing filter")
        update.message.reply_text(f"⚠️ Не вдалося додати фільтр: {exc}")
        return
    context.user_data.pop("housing_admin", None)
    update.message.reply_text(
        f"✅ Фільтр житла додано.\nID: {payload.get('filter_id')}\nКористувач: {state['user_id']}\nІмʼя: {html.escape(str(state['title']))}"
    )


def handle_private_text(update: Update, context: CallbackContext) -> bool:
    if not update.message or not update.message.text or not update.effective_user:
        return False
    if int(update.effective_user.id) != ADMIN_ID:
        return False
    text = update.message.text.strip()
    if text == BTN_ADMIN_ADD:
        start_add_flow(update, context)
        return True
    if text == BTN_ADMIN_LIST:
        show_admin(update, context)
        return True
    state = context.user_data.get("housing_admin")
    if not state:
        return False
    if text == BTN_CANCEL:
        context.user_data.pop("housing_admin", None)
        update.message.reply_text("Скасовано.")
        return True
    step = state.get("step")
    if step == "user_id":
        if not text.lstrip("-").isdigit():
            update.message.reply_text("Telegram ID має бути числом. Надішліть ID ще раз.")
            return True
        state["user_id"] = int(text)
        state["step"] = "title"
        update.message.reply_text("Тепер надішліть імʼя або назву фільтра для цього користувача.")
        return True
    if step == "title":
        if not text:
            update.message.reply_text("Імʼя не може бути порожнім. Надішліть імʼя ще раз.")
            return True
        state["title"] = text[:120]
        state["step"] = "url"
        update.message.reply_text("Тепер надішліть посилання Immowelt для моніторингу.")
        return True
    if step == "url":
        if "immowelt.de" not in text.lower():
            update.message.reply_text("Потрібне посилання саме з Immowelt. Надішліть посилання ще раз.")
            return True
        _finish_add_flow(update, context, state, text)
        return True
    return False


def handle_callback(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    if not query or not query.data:
        return
    if query.data == "housing:menu":
        query.answer()
        show_menu(update, context, edit=True)
    elif query.data == "housing:admin":
        query.answer()
        show_admin(update, context, edit=True)
    elif query.data == "housing:add":
        query.answer()
        start_add_flow(update, context, edit=True)
    elif query.data == "housing:list":
        query.answer()
        show_admin(update, context, edit=True)


def _admin_only(update: Update) -> bool:
    return bool(update.message and update.message.from_user and update.message.from_user.id == ADMIN_ID)


def add_filter(update: Update, context: CallbackContext) -> None:
    if not _admin_only(update):
        return
    raw = " ".join(context.args).strip()
    if "|" not in raw:
        update.message.reply_text("Використання: /housing_add USER_ID Назва | https://www.immowelt.de/...")
        return
    left, url = raw.split("|", 1)
    parts = left.strip().split(maxsplit=1)
    if len(parts) != 2 or not parts[0].lstrip("-").isdigit():
        update.message.reply_text("Використання: /housing_add USER_ID Назва | https://www.immowelt.de/...")
        return
    user_id = int(parts[0])
    title = parts[1].strip()
    url = url.strip()
    try:
        payload = _request("POST", "/api/housing/filters", json={"user_id": user_id, "title": title, "url": url})
    except Exception as exc:
        logger.exception("Could not add housing filter")
        update.message.reply_text(f"⚠️ Не вдалося додати фільтр: {exc}")
        return
    update.message.reply_text(
        f"✅ Фільтр житла додано.\nID: {payload.get('filter_id')}\nКористувач: {user_id}\nНазва: {title}"
    )


def list_filters(update: Update, context: CallbackContext) -> None:
    if not _admin_only(update):
        return
    tasks = _tasks()
    if not tasks:
        update.message.reply_text("Активних фільтрів житла поки немає.")
        return
    lines = ["🏠 Активні фільтри житла:", ""]
    for item in tasks:
        lines.append(
            f"#{int(item.get('filter_id'))} · user_id={int(item.get('user_id'))} · {item.get('title')}"
        )
    update.message.reply_text("\n".join(lines))


def _set_active(update: Update, context: CallbackContext, active: bool) -> None:
    if not _admin_only(update):
        return
    if len(context.args) != 1 or not context.args[0].isdigit():
        cmd = "/housing_enable" if active else "/housing_disable"
        update.message.reply_text(f"Використання: {cmd} FILTER_ID")
        return
    filter_id = int(context.args[0])
    try:
        _request("PATCH", f"/api/housing/filters/{filter_id}/active", json={"active": active})
    except Exception as exc:
        logger.exception("Could not update housing filter")
        update.message.reply_text(f"⚠️ Не вдалося оновити фільтр: {exc}")
        return
    status = "увімкнено" if active else "вимкнено"
    update.message.reply_text(f"✅ Фільтр #{filter_id} {status}.")


def enable_filter(update: Update, context: CallbackContext) -> None:
    _set_active(update, context, True)


def disable_filter(update: Update, context: CallbackContext) -> None:
    _set_active(update, context, False)


command_handler = CommandHandler("housing", show_menu, Filters.chat_type.private)
callback_handler = CallbackQueryHandler(handle_callback, pattern=r"^housing:")
add_handler = CommandHandler("housing_add", add_filter, Filters.chat_type.private)
list_handler = CommandHandler("housing_list", list_filters, Filters.chat_type.private)
enable_handler = CommandHandler("housing_enable", enable_filter, Filters.chat_type.private)
disable_handler = CommandHandler("housing_disable", disable_filter, Filters.chat_type.private)
