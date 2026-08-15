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

from user_jobs import housing_access_store, propotsdam_store

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
# Перевірка доступу стоїть на шляху промальовування меню, тож чекати на приймач
# стільки ж, скільки на збереження фільтра, там не можна.
ALLOW_CHECK_TIMEOUT = int(os.getenv("HOUSING_ALLOW_CHECK_TIMEOUT", "3") or 3)
BTN_ADMIN_ADD = "➕ Додати Immowelt користувача"
BTN_ADMIN_ADD_PROPOT = "🏢 Додати ProPotsdam користувача"
BTN_ADMIN_LIST = "📋 Користувачі житла"
BTN_ADMIN_ACCESS_ADD = "👤 Додати доступ користувачу"
BTN_ADMIN_ACCESS_LIST = "👥 Доступ до моніторингу"
BTN_CANCEL = "✖ Скасувати"
BTN_SELF_ADD = "➕ Додати Immowelt"
BTN_SELF_ADD_PROPOT = "🏢 Додати ProPotsdam"
BTN_SELF_MANAGE = "⚙️ Мої фільтри"
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
# Райони пишемо так, як вони стоять в адресах Immowelt: збіг там пошуком
# підрядка, тож «Waldstadt 2» з переліку ProPotsdam не знайшов би жодної адреси.
IMMOWELT_DISTRICTS = [
    "Babelsberg",
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
    "Waldstadt I",
    "Waldstadt II",
]
# Три питання замість шести: довгий майстер люди кидають на середині, а решту
# завжди можна дописати потім.
IMMOWELT_NUMERIC_STEPS = ["max_price_eur", "min_rooms", "min_area_m2"]
IMMOWELT_PROMPTS = {
    "max_price_eur": "Максимальна холодна оренда в євро або «-», щоб не обмежувати.",
    "min_rooms": "Мінімальна кількість кімнат або «-».",
    "min_area_m2": "Мінімальна площа в м² або «-».",
}
ADMIN_PAGE_SIZE = 20
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


def _request(method: str, path: str, timeout: Optional[int] = None, **kwargs) -> Dict[str, object]:
    url = f"{CHECK_WOHNUNG_BASE_URL}{path}"
    response = requests.request(method, url, timeout=timeout or TIMEOUT, **kwargs)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or not payload.get("ok"):
        raise RuntimeError(str(payload.get("error") if isinstance(payload, dict) else payload))
    return payload


def _tasks() -> list:
    """Завдання для браузера, а не список фільтрів користувачів.

    Приймач обходить Immowelt одним широким проходом на всіх, тому віддає тут
    одне зведене завдання без `filter_id`, `user_id` і `last_checked_at`.
    Для всього, що показуємо людині, треба `_all_immowelt_filters()`.
    """
    try:
        payload = _request("GET", "/api/housing/tasks")
    except Exception:
        logger.exception("Could not load housing tasks")
        return []
    tasks = payload.get("tasks")
    return tasks if isinstance(tasks, list) else []


def _all_immowelt_filters(timeout: Optional[int] = None) -> list:
    try:
        payload = _request("GET", "/api/housing/filters", timeout=timeout)
    except Exception:
        logger.exception("Could not load all housing filters")
        return []
    filters = payload.get("filters")
    return filters if isinstance(filters, list) else []


def _receiver_status() -> Dict[str, object]:
    try:
        return _request("GET", "/api/status")
    except Exception:
        logger.exception("Could not load receiver status")
        return {}


def _filter_id(item: Dict[str, object]) -> Optional[int]:
    try:
        return int(item.get("filter_id"))
    except (TypeError, ValueError):
        return None


def _preview_criteria(criteria: Dict[str, object]) -> Dict[str, object]:
    """Питає приймач, що підійшло б фільтру просто зараз.

    Перший обхід лише запамʼятовує базову лінію й нікому нічого не шле, тож без
    цього нова людина годинами не знає, чи взагалі працює її фільтр.
    """
    try:
        return _request("POST", "/api/housing/preview", json=criteria)
    except Exception:
        logger.exception("Could not preview housing filter")
        return {}


def _criteria_from_state(state: Dict[str, object]) -> Dict[str, object]:
    criteria = {"districts": list(state.get("districts_selected") or [])}
    for step in IMMOWELT_NUMERIC_STEPS:
        criteria[step] = state.get(step)
    return criteria


def _describe_criteria(criteria: Dict[str, object]) -> str:
    districts = criteria.get("districts") or []
    parts = [", ".join(str(item) for item in districts) if districts else "усі райони"]
    if criteria.get("max_price_eur") is not None:
        parts.append(f"до {int(criteria['max_price_eur'])} €")
    if criteria.get("min_rooms") is not None:
        parts.append(f"від {criteria['min_rooms']:g} кімн.")
    if criteria.get("min_area_m2") is not None:
        parts.append(f"від {criteria['min_area_m2']:g} м²")
    return html.escape(" · ".join(parts))


def _sync_propot_filters() -> None:
    try:
        _request("POST", "/api/propotsdam/filters", json={"filters": propotsdam_store.list_filters()})
    except Exception:
        logger.exception("Could not sync ProPotsdam filters to shared browser receiver")


def user_filters(user_id: Optional[int]) -> list:
    if not user_id:
        return []
    immowelt = [
        item
        for item in _all_immowelt_filters()
        if int(item.get("user_id") or 0) == int(user_id) and item.get("active")
    ]
    propot = propotsdam_store.list_filters(user_id=int(user_id), active_only=True)
    return immowelt + propot


def manageable_filters(user_id: Optional[int]) -> list:
    if not user_id:
        return []
    immowelt = [
        item for item in _all_immowelt_filters()
        if int(item.get("user_id") or 0) == int(user_id)
    ]
    propot = propotsdam_store.list_filters(user_id=int(user_id))
    for item in propot:
        item.setdefault("source", "propotsdam")
    return immowelt + propot


def _has_grandfathered_filter(user_id: int) -> bool:
    """Чи є у людини фільтр, заведений до появи окремого списку доступу.

    Цей шлях іде в приймач по мережі, і на ньому висне промальовування меню.
    Раніше сюди потрапляли одиниці, а тепер закритий екран бачить кожен, тож
    чекати повні 20 секунд на непіднятому приймачі стало нікуди не годиться.
    """
    immowelt = [
        item for item in _all_immowelt_filters(timeout=ALLOW_CHECK_TIMEOUT)
        if int(item.get("user_id") or 0) == int(user_id) and item.get("active")
    ]
    return bool(immowelt or propotsdam_store.list_filters(user_id=int(user_id), active_only=True))


def is_allowed(user_id: Optional[int]) -> bool:
    return bool(
        user_id
        and (
            int(user_id) == ADMIN_ID
            or int(user_id) in ALLOWED_USER_IDS
            or housing_access_store.is_allowed(int(user_id))
            or _has_grandfathered_filter(int(user_id))
        )
    )


def private_home_rows(user_id: Optional[int]) -> Iterable[list]:
    # Кнопку бачать усі: без неї людині без доступу не було чим про нього
    # попросити, а закритий екран сам пояснює, що робити далі.
    if not user_id:
        return []
    return [[InlineKeyboardButton("🏠 Моніторинг житла", callback_data="housing:menu")]]


def _menu_keyboard(user_id: Optional[int] = None) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton("🔄 Оновити статус", callback_data="housing:menu")]]
    if user_id and int(user_id) == ADMIN_ID:
        rows.insert(0, [InlineKeyboardButton("⚙️ Адмінка житла", callback_data="housing:admin")])
    elif is_allowed(user_id):
        rows.insert(0, [InlineKeyboardButton(BTN_SELF_ADD, callback_data="housing:self_add")])
        rows.insert(1, [InlineKeyboardButton(BTN_SELF_ADD_PROPOT, callback_data="housing:self_propot_add")])
        rows.insert(2, [InlineKeyboardButton(BTN_SELF_MANAGE, callback_data="housing:self_manage")])
    rows.append([InlineKeyboardButton("⬅ Головне меню", callback_data="anon:home")])
    return InlineKeyboardMarkup(rows)


def _admin_keyboard(page: int = 0) -> InlineKeyboardMarkup:
    rows = []
    pages = _admin_page_count()
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("◀", callback_data=f"housing:list:{page - 1}"))
        nav.append(InlineKeyboardButton(f"{page + 1}/{pages}", callback_data=f"housing:list:{page}"))
        if page < pages - 1:
            nav.append(InlineKeyboardButton("▶", callback_data=f"housing:list:{page + 1}"))
        rows.append(nav)
    rows.extend([
        [InlineKeyboardButton(BTN_ADMIN_ADD, callback_data="housing:add")],
        [InlineKeyboardButton(BTN_ADMIN_ADD_PROPOT, callback_data="housing:propot_add")],
        [InlineKeyboardButton(BTN_ADMIN_LIST, callback_data="housing:list:0")],
        [InlineKeyboardButton(BTN_ADMIN_ACCESS_ADD, callback_data="housing:access_add")],
        [InlineKeyboardButton(BTN_ADMIN_ACCESS_LIST, callback_data="housing:access_list")],
        [InlineKeyboardButton("⬅ До моніторингу", callback_data="housing:menu")],
    ])
    return InlineKeyboardMarkup(rows)


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


def _immowelt_status_lines() -> list:
    """Рядки про стан обходу Immowelt.

    Час беремо з `/api/status`: обхід один на всіх, тому власної відмітки у
    фільтрів немає. Поки приймач її не віддає, відкочуємось на
    `last_checked_at` самих фільтрів, інакше панель мовчала б про перевірку.
    """
    filters = [item for item in _all_immowelt_filters() if item.get("active")]
    if not filters:
        return ["Immowelt: активних фільтрів немає."]

    status = _receiver_status()
    checked_at = str(status.get("immowelt_last_check_at") or "")
    if not checked_at:
        checked_at = max((str(item.get("last_checked_at") or "") for item in filters), default="")
    seen_total = sum(int(item.get("seen_count") or 0) for item in filters)
    error = str(status.get("immowelt_last_error") or "")
    skip_reason = str(status.get("immowelt_last_skip_reason") or "")

    lines = []
    if not checked_at:
        lines.append("Immowelt: перевірка ще не запускалась.")
    elif _is_stale(checked_at, IMMOWELT_STALE_AFTER):
        lines.append(
            f"⚠️ Immowelt: перевірка прострочена; остання {_format_time(checked_at)}, "
            f"збережено: {seen_total}."
        )
    else:
        lines.append(f"Immowelt: остання перевірка {_format_time(checked_at)}, збережено: {seen_total}.")
    # Мовчазна поломка виглядала як звичайна перевірка без новин, тож причину
    # показуємо окремим рядком, а не ховаємо за старою відміткою часу.
    if error:
        lines.append(f"Остання помилка Immowelt: {html.escape(error)}")
    elif skip_reason:
        lines.append(f"Перевірку Immowelt пропущено: {html.escape(skip_reason)}")
    return lines


def _status_lines() -> list:
    lines = []
    try:
        lines.extend(_immowelt_status_lines())
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


def _locked_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📩 Запросити доступ", callback_data="housing:access_request")],
        [InlineKeyboardButton("⬅ Головне меню", callback_data="anon:home")],
    ])


def show_menu(update: Update, context: CallbackContext, edit: bool = False) -> None:
    user = update.effective_user
    if not user or not is_allowed(user.id):
        # Доступ видавався лише тим, що адмін вручну вбивав Telegram ID, і людині
        # не було чим про нього попросити.
        text = (
            "🏠 <b>Моніторинг житла</b>\n\n"
            "Бот стежить за Immowelt і ProPotsdam та надсилає нові оголошення "
            "за вашими фільтрами.\n\n"
            "Доступ поки не відкрито. Натисніть кнопку — адміністратор побачить запит."
        )
        if edit and update.callback_query:
            update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=_locked_keyboard())
        else:
            update.effective_message.reply_text(text, parse_mode="HTML", reply_markup=_locked_keyboard())
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


def _admin_rows() -> list:
    """Один плаский перелік фільтрів для посторінкового показу."""
    # Раніше сюди йшов `_tasks()`, а зведене завдання браузера не має
    # `filter_id`: `int(None)` валив колбек, і адмінка просто не відкривалась.
    rows = []
    for item in _all_immowelt_filters():
        filter_id = _filter_id(item)
        label = f"#{filter_id}" if filter_id is not None else "#?"
        title = html.escape(str(item.get("title") or "Пошук житла"))
        suffix = "" if item.get("active") else " · вимкнено"
        rows.append(f"• {label} · {int(item.get('user_id') or 0)} · {title}{suffix}")
    for item in propotsdam_store.list_filters():
        title = html.escape(str(item.get("title") or "ProPotsdam"))
        rows.append(f"• P#{int(item.get('filter_id'))} · {int(item.get('user_id'))} · {title}")
    return rows


def _render_admin(page: int = 0) -> str:
    """Показує сторінку переліку фільтрів.

    Перелік друкувався цілком, а Telegram ріже повідомлення на 4096 символах:
    приблизно з вісімдесятого фільтра адмінка перестала б відкриватися зовсім.
    """
    rows = _admin_rows()
    pages = max(1, (len(rows) + ADMIN_PAGE_SIZE - 1) // ADMIN_PAGE_SIZE)
    page = max(0, min(int(page), pages - 1))
    lines = ["⚙️ <b>Адмінка житла</b>", ""]
    if not rows:
        lines.append("Фільтрів поки немає.")
        return "\n".join(lines)
    lines.append(f"Усього фільтрів: {len(rows)} · сторінка {page + 1} з {pages}")
    lines.append("")
    lines.extend(rows[page * ADMIN_PAGE_SIZE:(page + 1) * ADMIN_PAGE_SIZE])
    return "\n".join(lines)


def _admin_page_count() -> int:
    return max(1, (len(_admin_rows()) + ADMIN_PAGE_SIZE - 1) // ADMIN_PAGE_SIZE)


def show_admin(update: Update, context: CallbackContext, edit: bool = False, page: int = 0) -> None:
    user = update.effective_user
    if not user or int(user.id) != ADMIN_ID:
        return
    text = _render_admin(page)
    keyboard = _admin_keyboard(page)
    if edit and update.callback_query:
        try:
            update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)
        except BadRequest as exc:
            # Повторний тап по тій самій сторінці інакше валив увесь колбек.
            if "Message is not modified" not in str(exc):
                raise
    else:
        update.effective_message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)


def _display_name(user) -> str:
    parts = [user.first_name or "", user.last_name or ""]
    name = " ".join(part for part in parts if part).strip()
    if user.username:
        name = f"{name} (@{user.username})".strip()
    return name[:120] or str(user.id)


def request_access(update: Update, context: CallbackContext) -> None:
    """Надсилає адміну запит на доступ до моніторингу житла."""
    query = update.callback_query
    user = update.effective_user
    if not query or not user:
        return
    if is_allowed(user.id):
        query.answer("Доступ уже відкрито.")
        show_menu(update, context, edit=True)
        return
    if not ADMIN_ID:
        query.answer("Адміністратора не налаштовано.", show_alert=True)
        return
    if context.user_data.get("housing_access_requested"):
        query.answer("Запит уже надіслано, зачекайте на відповідь.", show_alert=True)
        return
    name = _display_name(user)
    try:
        context.bot.send_message(
            chat_id=ADMIN_ID,
            text=(
                "📩 <b>Запит на моніторинг житла</b>\n\n"
                f"Користувач: {html.escape(name)}\n"
                f"Telegram ID: <code>{int(user.id)}</code>"
            ),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Дозволити", callback_data=f"housing:access_grant:{int(user.id)}"),
                InlineKeyboardButton("✖ Відмовити", callback_data=f"housing:access_deny:{int(user.id)}"),
            ]]),
        )
    except Exception:
        logger.exception("Could not deliver housing access request to the admin")
        query.answer("Не вдалося надіслати запит. Спробуйте пізніше.", show_alert=True)
        return
    context.user_data["housing_access_requested"] = True
    context.bot_data.setdefault("housing_access_names", {})[int(user.id)] = name
    query.answer("Запит надіслано.")
    query.edit_message_text(
        "📩 Запит надіслано адміністратору.\n\nМи повідомимо, щойно доступ відкриють."
    )


def _resolve_access_request(update: Update, context: CallbackContext, grant: bool) -> None:
    query = update.callback_query
    user = update.effective_user
    if not query or not user or int(user.id) != ADMIN_ID:
        return
    try:
        target_id = int(query.data.split(":")[2])
    except (IndexError, ValueError):
        query.answer("Некоректна команда.", show_alert=True)
        return
    name = str(context.bot_data.get("housing_access_names", {}).get(target_id, ""))
    if grant:
        housing_access_store.grant_access(target_id, name)
    verdict = "✅ Доступ надано" if grant else "✖ У доступі відмовлено"
    query.answer(verdict)
    query.edit_message_text(
        f"{verdict}\n\nКористувач: {html.escape(name or str(target_id))}\n"
        f"Telegram ID: <code>{target_id}</code>",
        parse_mode="HTML",
    )
    try:
        context.bot.send_message(
            chat_id=target_id,
            text=(
                "✅ Доступ до моніторингу житла відкрито. Натисніть «🏠 Моніторинг житла», "
                "щоб додати фільтр."
                if grant else
                "На жаль, доступ до моніторингу житла зараз не відкрито."
            ),
        )
    except Exception:
        # Людина могла заблокувати бота; рішення адміна від цього не залежить.
        logger.exception("Could not notify user %s about the housing access decision", target_id)


def start_access_add_flow(update: Update, context: CallbackContext, edit: bool = False) -> None:
    user = update.effective_user
    if not user or int(user.id) != ADMIN_ID:
        return
    context.user_data["housing_access_admin"] = {"step": "user_id"}
    text = "👤 <b>Додати доступ користувачу</b>\n\nНадішліть Telegram ID користувача."
    if edit and update.callback_query:
        update.callback_query.edit_message_text(text, parse_mode="HTML")
    else:
        update.effective_message.reply_text(text, parse_mode="HTML")


def _render_access_users() -> str:
    users = housing_access_store.list_users()
    lines = ["👥 <b>Доступ до моніторингу житла</b>", ""]
    if not users:
        lines.append("Окремо доданих користувачів поки немає.")
    for item in users:
        mark = "✅" if item.get("active") else "⏸"
        name = html.escape(str(item.get("display_name") or "без назви"))
        lines.append(f"{mark} {int(item['user_id'])} · {name}")
    return "\n".join(lines)


def show_access_users(update: Update, context: CallbackContext, edit: bool = False) -> None:
    user = update.effective_user
    if not user or int(user.id) != ADMIN_ID:
        return
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(BTN_ADMIN_ACCESS_ADD, callback_data="housing:access_add")],
        [InlineKeyboardButton("⬅ До адмінки", callback_data="housing:admin")],
    ])
    if edit and update.callback_query:
        update.callback_query.edit_message_text(
            _render_access_users(), parse_mode="HTML", reply_markup=keyboard
        )
    else:
        update.effective_message.reply_text(
            _render_access_users(), parse_mode="HTML", reply_markup=keyboard
        )


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


def _immowelt_district_keyboard(selected=None) -> InlineKeyboardMarkup:
    selected = set(selected or [])
    rows = []
    for district in IMMOWELT_DISTRICTS:
        mark = "✅" if district in selected else "☐"
        rows.append([InlineKeyboardButton(
            f"{mark} {district}", callback_data=f"housing:imm_district:{district}"
        )])
    rows.append([InlineKeyboardButton("✅ Готово", callback_data="housing:imm_district_done")])
    rows.append([InlineKeyboardButton("🌍 Усі райони", callback_data="housing:imm_district_all")])
    rows.append([InlineKeyboardButton(BTN_CANCEL, callback_data="housing:imm_cancel")])
    return InlineKeyboardMarkup(rows)


def _immowelt_district_text(selected=None) -> str:
    selected = selected or []
    suffix = ", ".join(selected) if selected else "усі райони"
    return (
        "🏙 <b>Райони Immowelt</b>\n\nОберіть райони галочками.\n"
        f"Поточний вибір: {html.escape(suffix)}"
    )


def _preview_text(title: str, criteria: Dict[str, object], preview: Dict[str, object]) -> str:
    lines = [
        "🔍 <b>Перевірка фільтра</b>",
        "",
        f"Назва: {html.escape(str(title))}",
        f"Умови: {_describe_criteria(criteria)}",
        "",
    ]
    if not preview:
        lines.append("Не вдалося звʼязатися з перевіркою, але фільтр можна зберегти.")
        return "\n".join(lines)
    match_count = int(preview.get("match_count") or 0)
    catalog_size = int(preview.get("catalog_size") or 0)
    if not catalog_size:
        lines.append("Каталог ще порожній — перший обхід збирає його мовчки.")
    elif not match_count:
        lines.append(
            f"Зараз під ці умови не підходить жодна з {catalog_size} квартир у каталозі. "
            "Фільтр працюватиме, але новин може довго не бути — спробуйте послабити умови."
        )
    else:
        lines.append(f"Зараз підходить {match_count} з {catalog_size} квартир у каталозі, наприклад:")
        for item in preview.get("matches") or []:
            title_text = html.escape(str(item.get("title") or "Wohnung"))
            details = []
            if item.get("price_eur"):
                details.append(f"{int(item['price_eur'])} €")
            if item.get("rooms"):
                details.append(f"{item['rooms']:g} кімн.")
            if item.get("area_m2"):
                details.append(f"{item['area_m2']:g} м²")
            suffix = f" — {' · '.join(details)}" if details else ""
            lines.append(f"• <a href=\"{html.escape(str(item.get('url')))}\">{title_text}</a>{suffix}")
        lines.append("")
        lines.append("Нові оголошення надходитимуть у міру появи.")
    return "\n".join(lines)


def _preview_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Зберегти фільтр", callback_data="housing:imm_save")],
        [InlineKeyboardButton(BTN_CANCEL, callback_data="housing:imm_cancel")],
    ])


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


def start_self_add_flow(update: Update, context: CallbackContext, edit: bool = False) -> None:
    user = update.effective_user
    if not user or not is_allowed(user.id):
        return
    context.user_data["housing_admin"] = {
        "mode": "immowelt", "step": "title", "user_id": int(user.id)
    }
    text = "➕ <b>Додати Immowelt</b>\n\nНадішліть назву фільтра."
    if edit and update.callback_query:
        update.callback_query.edit_message_text(text, parse_mode="HTML")
    else:
        update.effective_message.reply_text(text, parse_mode="HTML")


def start_self_propot_add_flow(update: Update, context: CallbackContext, edit: bool = False) -> None:
    user = update.effective_user
    if not user or not is_allowed(user.id):
        return
    context.user_data["housing_admin"] = {
        "mode": "propotsdam", "step": "title", "user_id": int(user.id),
        "districts_selected": [],
    }
    text = "🏢 <b>Додати ProPotsdam</b>\n\nНадішліть назву фільтра."
    if edit and update.callback_query:
        update.callback_query.edit_message_text(text, parse_mode="HTML")
    else:
        update.effective_message.reply_text(text, parse_mode="HTML")


def _self_manage_keyboard(user_id: int) -> InlineKeyboardMarkup:
    rows = []
    for item in manageable_filters(user_id):
        is_propot = item.get("source") == "propotsdam" or "districts" in item
        source = "propotsdam" if is_propot else "immowelt"
        filter_id = int(item.get("filter_id"))
        active = bool(item.get("active", True))
        mark = "✅" if active else "⏸"
        title = str(item.get("title") or "Пошук житла")[:30]
        rows.append([InlineKeyboardButton(
            f"{mark} {title}",
            callback_data=f"housing:toggle:{source}:{filter_id}:{0 if active else 1}",
        )])
    rows.append([InlineKeyboardButton("⬅ До моніторингу", callback_data="housing:menu")])
    return InlineKeyboardMarkup(rows)


def show_self_manage(update: Update, context: CallbackContext, edit: bool = False) -> None:
    user = update.effective_user
    if not user or not is_allowed(user.id):
        return
    filters = manageable_filters(user.id)
    text = (
        "⚙️ <b>Мої фільтри</b>\n\nНатисніть фільтр, щоб увімкнути або призупинити його."
        if filters else "⚙️ <b>Мої фільтри</b>\n\nУ вас ще немає фільтрів."
    )
    keyboard = _self_manage_keyboard(user.id)
    if edit and update.callback_query:
        update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)
    else:
        update.effective_message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)


def _toggle_owned_filter(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    user = update.effective_user
    if not query or not user or not is_allowed(user.id):
        return
    try:
        _, _, source, raw_id, raw_active = query.data.split(":", 4)
        filter_id = int(raw_id)
        active = bool(int(raw_active))
    except (TypeError, ValueError):
        query.answer("Некоректна команда.", show_alert=True)
        return
    own = []
    for item in manageable_filters(user.id):
        item_is_propot = item.get("source") == "propotsdam" or "districts" in item
        source_matches = (source == "propotsdam" and item_is_propot) or (
            source == "immowelt" and not item_is_propot
        )
        if (int(item.get("filter_id") or 0) == filter_id
                and int(item.get("user_id") or 0) == int(user.id)
                and source_matches):
            own.append(item)
    if not own:
        query.answer("Цей фільтр вам не належить.", show_alert=True)
        return
    if source == "propotsdam":
        ok = propotsdam_store.set_filter_active(filter_id, active, user_id=user.id)
        if ok:
            _sync_propot_filters()
    else:
        try:
            _request("PATCH", f"/api/housing/filters/{filter_id}/active", json={"active": active})
            ok = True
        except Exception:
            logger.exception("Could not update owned housing filter")
            ok = False
    if not ok:
        query.answer("Не вдалося оновити фільтр.", show_alert=True)
        return
    query.answer("Фільтр оновлено.")
    show_self_manage(update, context, edit=True)


def _show_immowelt_preview(message, state: dict) -> None:
    criteria = _criteria_from_state(state)
    state["step"] = "preview"
    message.reply_text(
        _preview_text(str(state.get("title") or ""), criteria, _preview_criteria(criteria)),
        parse_mode="HTML",
        reply_markup=_preview_keyboard(),
        disable_web_page_preview=True,
    )


def _save_immowelt_filter(update: Update, context: CallbackContext) -> None:
    """Зберігає зібраний майстром фільтр разом із його умовами.

    Раніше сюди йшли лише назва й посилання, а відбір іде за умовами в самому
    записі: фільтр без умов збігається з будь-якою квартирою Потсдама.
    """
    query = update.callback_query
    state = context.user_data.get("housing_admin") or {}
    if state.get("mode") != "immowelt" or state.get("step") != "preview":
        query.answer()
        return
    criteria = _criteria_from_state(state)
    try:
        payload = _request("POST", "/api/housing/filters", json={
            "user_id": state["user_id"], "title": state["title"], **criteria,
        })
    except Exception as exc:
        logger.exception("Could not add housing filter")
        query.answer("Не вдалося зберегти фільтр.", show_alert=True)
        query.edit_message_text(f"⚠️ Не вдалося додати фільтр: {html.escape(str(exc))}")
        context.user_data.pop("housing_admin", None)
        return
    context.user_data.pop("housing_admin", None)
    query.answer("Фільтр збережено.")
    query.edit_message_text(
        "✅ <b>Фільтр житла додано</b>\n\n"
        f"ID: {payload.get('filter_id')}\n"
        f"Назва: {html.escape(str(state['title']))}\n"
        f"Умови: {_describe_criteria(criteria)}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅ До моніторингу", callback_data="housing:menu")]
        ]),
    )


def _handle_immowelt_flow(update: Update, context: CallbackContext, state: dict, text: str) -> bool:
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
        state["step"] = "districts"
        state.setdefault("districts_selected", [])
        update.message.reply_text(
            _immowelt_district_text(state["districts_selected"]),
            parse_mode="HTML",
            reply_markup=_immowelt_district_keyboard(state["districts_selected"]),
        )
        return True
    if step == "districts":
        update.message.reply_text(
            "Оберіть райони кнопками нижче і натисніть «Готово».",
            reply_markup=_immowelt_district_keyboard(state.get("districts_selected")),
        )
        return True
    if step == "preview":
        update.message.reply_text("Натисніть «Зберегти фільтр» або «Скасувати».", reply_markup=_preview_keyboard())
        return True
    if step in IMMOWELT_NUMERIC_STEPS:
        value = propotsdam_store.parse_optional_number(text)
        if value is None and text.strip() not in {"", "-", "—", "–"}:
            update.message.reply_text("Потрібне число або «-». Надішліть значення ще раз.")
            return True
        state[step] = value
        index = IMMOWELT_NUMERIC_STEPS.index(step)
        if index < len(IMMOWELT_NUMERIC_STEPS) - 1:
            next_step = IMMOWELT_NUMERIC_STEPS[index + 1]
            state["step"] = next_step
            update.message.reply_text(IMMOWELT_PROMPTS[next_step])
            return True
        _show_immowelt_preview(update.message, state)
        return True
    return False


def _toggle_immowelt_district(update: Update, context: CallbackContext, district: str) -> None:
    query = update.callback_query
    state = context.user_data.get("housing_admin") or {}
    if state.get("mode") != "immowelt" or state.get("step") != "districts":
        query.answer()
        return
    selected = list(state.get("districts_selected") or [])
    if district in selected:
        selected.remove(district)
    else:
        selected.append(district)
    state["districts_selected"] = selected
    query.answer()
    query.edit_message_text(
        _immowelt_district_text(selected),
        parse_mode="HTML",
        reply_markup=_immowelt_district_keyboard(selected),
    )


def _finish_immowelt_districts(update: Update, context: CallbackContext, all_districts: bool = False) -> None:
    query = update.callback_query
    state = context.user_data.get("housing_admin") or {}
    if state.get("mode") != "immowelt" or state.get("step") != "districts":
        query.answer()
        return
    if all_districts:
        state["districts_selected"] = []
    first_step = IMMOWELT_NUMERIC_STEPS[0]
    state["step"] = first_step
    query.answer()
    query.edit_message_text(IMMOWELT_PROMPTS[first_step])


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
    user_id = int(update.effective_user.id)
    if user_id != ADMIN_ID and not is_allowed(user_id):
        return False
    text = update.message.text.strip()
    if user_id != ADMIN_ID:
        if text == BTN_SELF_ADD:
            start_self_add_flow(update, context)
            return True
        if text == BTN_SELF_ADD_PROPOT:
            start_self_propot_add_flow(update, context)
            return True
        if text == BTN_SELF_MANAGE:
            show_self_manage(update, context)
            return True
    if text == BTN_ADMIN_ADD:
        if user_id != ADMIN_ID:
            return False
        start_add_flow(update, context)
        return True
    if text == BTN_ADMIN_ADD_PROPOT:
        if user_id != ADMIN_ID:
            return False
        start_propot_add_flow(update, context)
        return True
    if text == BTN_ADMIN_LIST:
        if user_id != ADMIN_ID:
            return False
        show_admin(update, context)
        return True
    if text == BTN_ADMIN_ACCESS_ADD:
        if user_id != ADMIN_ID:
            return False
        start_access_add_flow(update, context)
        return True
    if text == BTN_ADMIN_ACCESS_LIST:
        if user_id != ADMIN_ID:
            return False
        show_access_users(update, context)
        return True
    access_state = context.user_data.get("housing_access_admin")
    if access_state:
        if text == BTN_CANCEL:
            context.user_data.pop("housing_access_admin", None)
            update.message.reply_text("Скасовано.")
            return True
        if access_state.get("step") == "user_id":
            if not text.lstrip("-").isdigit():
                update.message.reply_text("Telegram ID має бути числом. Надішліть ID ще раз.")
                return True
            access_state["user_id"] = int(text)
            access_state["step"] = "name"
            update.message.reply_text("Надішліть імʼя або назву користувача.")
            return True
        if access_state.get("step") == "name":
            if not text:
                update.message.reply_text("Імʼя не може бути порожнім.")
                return True
            housing_access_store.grant_access(access_state["user_id"], text[:120])
            granted_id = access_state["user_id"]
            context.user_data.pop("housing_access_admin", None)
            update.message.reply_text(
                f"✅ Доступ до моніторингу житла надано.\n"
                f"Користувач: {granted_id}\nІмʼя: {html.escape(text[:120])}"
            )
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
    return _handle_immowelt_flow(update, context, state, text)


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
    elif query.data == "housing:access_request":
        request_access(update, context)
    elif query.data.startswith("housing:access_grant:"):
        _resolve_access_request(update, context, grant=True)
    elif query.data.startswith("housing:access_deny:"):
        _resolve_access_request(update, context, grant=False)
    elif query.data == "housing:access_add":
        query.answer()
        start_access_add_flow(update, context, edit=True)
    elif query.data == "housing:access_list":
        query.answer()
        show_access_users(update, context, edit=True)
    elif query.data.startswith("housing:imm_district:"):
        _toggle_immowelt_district(update, context, query.data.split(":", 2)[2])
    elif query.data == "housing:imm_district_done":
        _finish_immowelt_districts(update, context)
    elif query.data == "housing:imm_district_all":
        _finish_immowelt_districts(update, context, all_districts=True)
    elif query.data == "housing:imm_save":
        _save_immowelt_filter(update, context)
    elif query.data == "housing:imm_cancel":
        context.user_data.pop("housing_admin", None)
        query.answer()
        query.edit_message_text("Скасовано.")
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
    elif query.data == "housing:self_add":
        query.answer()
        start_self_add_flow(update, context, edit=True)
    elif query.data == "housing:self_propot_add":
        query.answer()
        start_self_propot_add_flow(update, context, edit=True)
    elif query.data == "housing:self_manage":
        query.answer()
        show_self_manage(update, context, edit=True)
    elif query.data.startswith("housing:toggle:"):
        _toggle_owned_filter(update, context)
    elif query.data.startswith("housing:list"):
        query.answer()
        parts = query.data.split(":")
        try:
            page = int(parts[2]) if len(parts) > 2 else 0
        except ValueError:
            page = 0
        show_admin(update, context, edit=True, page=page)


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
