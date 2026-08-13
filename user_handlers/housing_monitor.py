"""Private housing monitoring menu backed by local housing receivers."""

import html
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, Optional
from zoneinfo import ZoneInfo

import requests
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackContext, CallbackQueryHandler, CommandHandler, Filters
from telegram.error import BadRequest

from user_jobs import propotsdam_store

logger = logging.getLogger(__name__)

ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
ALLOWED_USER_IDS = {
    int(value.strip())
    for value in os.getenv("HOUSING_ALLOWED_USER_IDS", "").split(",")
    if value.strip().lstrip("-").isdigit()
}
CHECK_WOHNUNG_BASE_URL = os.getenv(
    "CHECK_WOHNUNG_BASE_URL",
    "http://host.docker.internal:18765",
).rstrip("/")
TIMEOUT = int(os.getenv("HOUSING_MONITOR_TIMEOUT", "20") or 20)
BTN_ADMIN_ADD = "➕ Додати Immowelt користувача"
BTN_ADMIN_ADD_PROPOT = "🏢 Додати ProPotsdam користувача"
BTN_ADMIN_LIST = "📋 Користувачі житла"
BTN_CANCEL = "✖ Скасувати"
BERLIN_TZ = ZoneInfo("Europe/Berlin")
IMMOWELT_STALE_AFTER = timedelta(minutes=30)
PROPOTSDAM_STALE_AFTER = timedelta(minutes=45)
PROPOT_DISTRICTS = [
    "Babelsberg",
    "Babelsberg Nord",
    "Babelsberg Süd",
    "Berliner Vorstadt",
    "Bornim",
    "Bornstedt",
    "Brandenburger Vorstadt",
    "Drewitz",
    "Eiche",
    "Fahrland",
    "Golm",
    "Groß Glienicke",
    "Innenstadt",
    "Jägervorstadt",
    "Kirchsteigfeld",
    "Nauener Vorstadt",
    "Potsdam West",
    "Schlaatz",
    "Stern",
    "Teltower Vorstadt",
    "Waldstadt 1",
    "Waldstadt 2",
]
PROPOT_NUMERIC_STEPS = [
    "min_rooms",
    "max_rooms",
    "min_area_m2",
    "max_area_m2",
    "min_total_rent_eur",
    "max_total_rent_eur",
]
PROPOT_PROMPTS = {
    "title": "Надішліть імʼя або назву фільтра.",
    "min_rooms": "Мінімум кімнат або '-'.",
    "max_rooms": "Максимум кімнат або '-'.",
    "min_area_m2": "Мінімальна площа або '-'.",
    "max_area_m2": "Максимальна площа або '-'.",
    "min_total_rent_eur": "Мінімальна загальна оренда або '-'.",
    "max_total_rent_eur": "Максимальна загальна оренда або '-'.",
}


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


def _sync_propot_filters() -> None:
    try:
        _request("POST", "/api/propotsdam/filters", json={"filters": propotsdam_store.list_filters()})
    except Exception:
        logger.exception("Could not sync ProPotsdam filters to shared browser receiver")


def user_filters(user_id: Optional[int]) -> list:
    if not user_id:
        return []
    immowelt = [
        task
        for task in _tasks()
        if int(task.get("user_id") or 0) == int(user_id)
        and task.get("source") == "immowelt"
    ]
    propot = propotsdam_store.list_filters(user_id=int(user_id), active_only=True)
    return immowelt + propot


def is_allowed(user_id: Optional[int]) -> bool:
    return bool(
        user_id
        and (
            int(user_id) == ADMIN_ID
            or int(user_id) in ALLOWED_USER_IDS
            or user_filters(int(user_id))
        )
    )


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
        [InlineKeyboardButton(BTN_ADMIN_ADD_PROPOT, callback_data="housing:propot_add")],
        [InlineKeyboardButton(BTN_ADMIN_LIST, callback_data="housing:list")],
        [InlineKeyboardButton("⬅ До моніторингу", callback_data="housing:menu")],
    ])


def _format_time(value) -> str:
    if not value:
        return "ще не було"
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return html.escape(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(BERLIN_TZ).strftime("%d.%m.%Y %H:%M")


def _as_berlin_datetime(value):
    if not value:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(BERLIN_TZ)


def _now_berlin() -> datetime:
    return datetime.now(BERLIN_TZ)


def _is_stale(value, max_age: timedelta) -> bool:
    checked_at = _as_berlin_datetime(value)
    return bool(checked_at and _now_berlin() - checked_at > max_age)


def _status_lines() -> list:
    lines = []
    try:
        tasks = _tasks()
        immowelt_tasks = [task for task in tasks if task.get("source") == "immowelt"]
        if immowelt_tasks:
            latest = max(
                (str(task.get("last_checked_at") or "") for task in immowelt_tasks),
                default="",
            )
            seen_total = sum(int(task.get("seen_count") or 0) for task in immowelt_tasks)
            if latest:
                if _is_stale(latest, IMMOWELT_STALE_AFTER):
                    lines.append(
                        f"⚠️ Immowelt: перевірка прострочена; остання {_format_time(latest)}, "
                        f"збережено: {seen_total}."
                    )
                else:
                    lines.append(f"Immowelt: остання перевірка {_format_time(latest)}, збережено: {seen_total}.")
            else:
                lines.append("Immowelt: перевірка ще не запускалась.")
    except Exception:
        logger.exception("Could not load Immowelt status")

    status = propotsdam_store.latest_status()
    if status:
        last = _format_time(status.get("last_checked_at"))
        label = status.get("last_status") or "unknown"
        count = status.get("listings_count") or 0
        if _is_stale(status.get("last_checked_at"), PROPOTSDAM_STALE_AFTER):
            lines.append(
                f"⚠️ ProPotsdam: перевірка прострочена; остання {last}, "
                f"статус {html.escape(str(label))}, квартир: {count}."
            )
        else:
            lines.append(f"ProPotsdam: остання перевірка {last}, статус {html.escape(str(label))}, квартир: {count}.")
        if status.get("last_error"):
            lines.append(f"Остання помилка ProPotsdam: {html.escape(str(status.get('last_error')))}")
    else:
        lines.append("ProPotsdam: перевірка ще не запускалась.")
    return lines


def _render_menu(user_id: int) -> str:
    filters = user_filters(user_id)
    lines = [
        "🏠 <b>Моніторинг житла</b>",
        "",
        "Бот перевіряє Immowelt та ProPotsdam і надсилає нові оголошення за вашими фільтрами.",
        "",
        "Статус перевірки:",
        *_status_lines(),
        "",
    ]
    if not filters:
        lines.append("Для вашого Telegram ID поки немає активних фільтрів.")
    else:
        lines.append("Ваші фільтри:")
        for item in filters:
            prefix = "P" if "districts" in item else ""
            title = html.escape(str(item.get('title') or 'Пошук житла'))
            lines.append(f"• #{prefix}{int(item.get('filter_id'))}: {title}")
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
    propot_filters = propotsdam_store.list_filters()
    lines = ["⚙️ <b>Адмінка житла</b>", "", "Тут можна додати користувача до моніторингу Immowelt або ProPotsdam.", ""]
    if not tasks and not propot_filters:
        lines.append("Активних фільтрів поки немає.")
    if tasks:
        lines.append("Фільтри Immowelt:")
        for item in tasks:
            lines.append(f"• #{int(item.get('filter_id'))} · {int(item.get('user_id'))} · {html.escape(str(item.get('title') or 'Пошук житла'))}")
    if propot_filters:
        lines.extend(["", "Фільтри ProPotsdam:"])
        for item in propot_filters:
            lines.append(f"• P#{int(item.get('filter_id'))} · {int(item.get('user_id'))} · {html.escape(str(item.get('title') or 'ProPotsdam'))}")
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
    context.user_data["housing_admin"] = {"mode": "immowelt", "step": "user_id"}
    text = "➕ <b>Додати Immowelt користувача</b>\n\nНадішліть Telegram ID користувача."
    if edit and update.callback_query:
        update.callback_query.edit_message_text(text, parse_mode="HTML")
    else:
        update.effective_message.reply_text(text, parse_mode="HTML")


def _district_keyboard(selected=None) -> InlineKeyboardMarkup:
    selected = set(selected or [])
    rows = []
    for district in PROPOT_DISTRICTS:
        mark = "✅" if district in selected else "☐"
        rows.append([InlineKeyboardButton(f"{mark} {district}", callback_data=f"housing:propot_district:{district}")])
    rows.append([InlineKeyboardButton("✅ Готово", callback_data="housing:propot_district_done")])
    rows.append([InlineKeyboardButton("🌍 Усі райони", callback_data="housing:propot_district_all")])
    rows.append([InlineKeyboardButton(BTN_CANCEL, callback_data="housing:propot_cancel")])
    return InlineKeyboardMarkup(rows)


def _district_text(selected=None) -> str:
    selected = selected or []
    suffix = ", ".join(selected) if selected else "усі райони"
    return f"🏢 <b>Райони ProPotsdam</b>\n\nОберіть райони галочками.\nПоточний вибір: {html.escape(suffix)}"


def start_propot_add_flow(update: Update, context: CallbackContext, edit: bool = False) -> None:
    user = update.effective_user
    if not user or int(user.id) != ADMIN_ID:
        return
    context.user_data["housing_admin"] = {"mode": "propotsdam", "step": "user_id", "districts_selected": []}
    text = "🏢 <b>Додати ProPotsdam користувача</b>\n\nНадішліть Telegram ID користувача."
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


def _handle_propot_flow(update: Update, context: CallbackContext, state: dict, text: str) -> bool:
    step = state.get("step")
    if step == "user_id":
        if not text.lstrip("-").isdigit():
            update.message.reply_text("Telegram ID має бути числом. Надішліть ID ще раз.")
            return True
        state["user_id"] = int(text)
        state["step"] = "title"
        update.message.reply_text(PROPOT_PROMPTS["title"])
        return True
    if step == "title":
        if not text:
            update.message.reply_text("Назва не може бути порожньою. Надішліть назву ще раз.")
            return True
        state["title"] = text[:120]
        state["step"] = "districts"
        update.message.reply_text(_district_text(state.get("districts_selected")), parse_mode="HTML", reply_markup=_district_keyboard(state.get("districts_selected")))
        return True
    if step == "districts":
        update.message.reply_text("Оберіть райони кнопками нижче і натисніть 'Готово'.", reply_markup=_district_keyboard(state.get("districts_selected")))
        return True
    if step in PROPOT_NUMERIC_STEPS:
        value = propotsdam_store.parse_optional_number(text)
        if value is None and text.strip() not in {"", "-", "—", "–"}:
            update.message.reply_text("Потрібне число або '-'. Надішліть значення ще раз.")
            return True
        state[step] = value
        index = PROPOT_NUMERIC_STEPS.index(step)
        if index < len(PROPOT_NUMERIC_STEPS) - 1:
            next_step = PROPOT_NUMERIC_STEPS[index + 1]
            state["step"] = next_step
            update.message.reply_text(PROPOT_PROMPTS[next_step])
            return True
        filter_id = propotsdam_store.create_filter(
            user_id=state["user_id"],
            title=state["title"],
            districts=state.get("districts", ""),
            min_rooms=state.get("min_rooms"),
            max_rooms=state.get("max_rooms"),
            min_area_m2=state.get("min_area_m2"),
            max_area_m2=state.get("max_area_m2"),
            min_total_rent_eur=state.get("min_total_rent_eur"),
            max_total_rent_eur=state.get("max_total_rent_eur"),
        )
        _sync_propot_filters()
        context.user_data.pop("housing_admin", None)
        update.message.reply_text(f"✅ Фільтр ProPotsdam додано.\nID: P{filter_id}\nКористувач: {state['user_id']}\nНазва: {html.escape(str(state['title']))}")
        return True
    return False


def handle_private_text(update: Update, context: CallbackContext) -> bool:
    if not update.message or not update.message.text or not update.effective_user:
        return False
    if int(update.effective_user.id) != ADMIN_ID:
        return False
    text = update.message.text.strip()
    if text == BTN_ADMIN_ADD:
        start_add_flow(update, context)
        return True
    if text == BTN_ADMIN_ADD_PROPOT:
        start_propot_add_flow(update, context)
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
    if state.get("mode") == "propotsdam":
        return _handle_propot_flow(update, context, state, text)
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


def _toggle_district(update: Update, context: CallbackContext, district: str) -> None:
    query = update.callback_query
    state = context.user_data.get("housing_admin") or {}
    if state.get("mode") != "propotsdam" or state.get("step") != "districts":
        query.answer()
        return
    selected = list(state.get("districts_selected") or [])
    if district in selected:
        selected.remove(district)
    else:
        selected.append(district)
    state["districts_selected"] = selected
    query.answer()
    query.edit_message_text(_district_text(selected), parse_mode="HTML", reply_markup=_district_keyboard(selected))


def _finish_districts(update: Update, context: CallbackContext, all_districts: bool = False) -> None:
    query = update.callback_query
    state = context.user_data.get("housing_admin") or {}
    if state.get("mode") != "propotsdam" or state.get("step") != "districts":
        query.answer()
        return
    selected = [] if all_districts else list(state.get("districts_selected") or [])
    state["districts"] = propotsdam_store.normalize_districts(",".join(selected))
    state["step"] = "min_rooms"
    query.answer()
    query.edit_message_text(PROPOT_PROMPTS["min_rooms"])


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
    elif query.data == "housing:propot_add":
        query.answer()
        start_propot_add_flow(update, context, edit=True)
    elif query.data.startswith("housing:propot_district:"):
        _toggle_district(update, context, query.data.split(":", 2)[2])
    elif query.data == "housing:propot_district_done":
        _finish_districts(update, context)
    elif query.data == "housing:propot_district_all":
        _finish_districts(update, context, all_districts=True)
    elif query.data == "housing:propot_cancel":
        context.user_data.pop("housing_admin", None)
        query.answer()
        query.edit_message_text("Скасовано.")
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
    update.message.reply_text(f"✅ Фільтр житла додано.\nID: {payload.get('filter_id')}\nКористувач: {user_id}\nНазва: {title}")


def list_filters(update: Update, context: CallbackContext) -> None:
    if not _admin_only(update):
        return
    update.message.reply_text(_render_admin(), parse_mode="HTML")


def _set_active(update: Update, context: CallbackContext, active: bool) -> None:
    if not _admin_only(update):
        return
    if len(context.args) != 1:
        cmd = "/housing_enable" if active else "/housing_disable"
        update.message.reply_text(f"Використання: {cmd} FILTER_ID або PPRO_FILTER_ID")
        return
    raw_id = context.args[0]
    if raw_id.upper().startswith("P"):
        ok = propotsdam_store.set_filter_active(int(raw_id[1:]), active) if raw_id[1:].isdigit() else False
        if not ok:
            update.message.reply_text(f"⚠️ Не знайдено ProPotsdam фільтр {raw_id}.")
            return
        status = "увімкнено" if active else "вимкнено"
        _sync_propot_filters()
        update.message.reply_text(f"✅ Фільтр {raw_id.upper()} {status}.")
        return
    if not raw_id.isdigit():
        cmd = "/housing_enable" if active else "/housing_disable"
        update.message.reply_text(f"Використання: {cmd} FILTER_ID або PPRO_FILTER_ID")
        return
    filter_id = int(raw_id)
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
