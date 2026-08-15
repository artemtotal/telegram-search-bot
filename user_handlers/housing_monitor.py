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
# Тільки Waldstadt пишеться по-різному між джерелами — решта райони збігаються
# рядок-в-рядок. У ProPotsdam ще є «Babelsberg Nord»/«Süd» без відповідника в
# Immowelt; такі райони при клонуванні фільтра просто відкидаються.
IMMOWELT_TO_PROPOT_DISTRICT = {"Waldstadt I": "Waldstadt 1", "Waldstadt II": "Waldstadt 2"}
PROPOT_TO_IMMOWELT_DISTRICT = {value: key for key, value in IMMOWELT_TO_PROPOT_DISTRICT.items()}


def _translate_districts(districts, mapping: Dict[str, str], valid_targets) -> list:
    translated = [mapping.get(d, d) for d in districts]
    return [d for d in translated if d in valid_targets]


# Галочками людина каже, ЩО саме хоче задати, а потім майстер веде її по
# цьому самому списку й питає кожну умову окремим числом. Довгий майстер із
# шести запитань поспіль люди кидають на середині — але тут ніхто не бачить
# запитань про те, що сам не обрав, тож зайвих кроків просто немає.
IMMOWELT_CRITERIA_FIELDS = [
    {"key": "min_price_eur", "label": "Ціна: мінімум (від)", "prompt": "Мінімальна холодна оренда (Kaltmiete) в євро:"},
    {"key": "max_price_eur", "label": "Ціна: максимум (до)", "prompt": "Максимальна холодна оренда (Kaltmiete) в євро:"},
    {"key": "min_rooms", "label": "Кімнати: мінімум (від)", "prompt": "Мінімальна кількість кімнат:"},
    {"key": "max_rooms", "label": "Кімнати: максимум (до)", "prompt": "Максимальна кількість кімнат:"},
    {"key": "min_area_m2", "label": "Площа: мінімум (від)", "prompt": "Мінімальна площа в м²:"},
    {"key": "max_area_m2", "label": "Площа: максимум (до)", "prompt": "Максимальна площа в м²:"},
]
IMMOWELT_CRITERIA_KEYS = [spec["key"] for spec in IMMOWELT_CRITERIA_FIELDS]
IMMOWELT_CRITERIA_BY_KEY = {spec["key"]: spec for spec in IMMOWELT_CRITERIA_FIELDS}
ADMIN_PAGE_SIZE = 20
PROPOT_CRITERIA_FIELDS = [
    {"key": "min_rooms", "label": "Кімнати: мінімум (від)", "prompt": "Мінімальна кількість кімнат:"},
    {"key": "max_rooms", "label": "Кімнати: максимум (до)", "prompt": "Максимальна кількість кімнат:"},
    {"key": "min_area_m2", "label": "Площа: мінімум (від)", "prompt": "Мінімальна площа в м²:"},
    {"key": "max_area_m2", "label": "Площа: максимум (до)", "prompt": "Максимальна площа в м²:"},
    {"key": "min_total_rent_eur", "label": "Оренда: мінімум (від)", "prompt": "Мінімальна загальна оренда (Gesamtmiete) в євро:"},
    {"key": "max_total_rent_eur", "label": "Оренда: максимум (до)", "prompt": "Максимальна загальна оренда (Gesamtmiete) в євро:"},
]
PROPOT_CRITERIA_KEYS = [spec["key"] for spec in PROPOT_CRITERIA_FIELDS]
PROPOT_CRITERIA_BY_KEY = {spec["key"]: spec for spec in PROPOT_CRITERIA_FIELDS}
PROPOT_PROMPTS = {
    "title": "Надішліть імʼя або назву фільтра.",
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


_INVALID_NUMBER = object()


def _parse_single_number(text: str):
    """Розбирає одне число або «-» (без обмежень).

    Повертає число, `None` для порожнього/«-», або сентинел `_INVALID_NUMBER`,
    якщо текст не розпізнано — саме `None` тут не годиться як ознака помилки,
    бо `None` це водночас і легальне значення «немає обмеження».
    """
    raw = (text or "").strip()
    if not raw or raw in {"-", "—", "–"}:
        return None
    value = propotsdam_store.parse_optional_number(raw)
    return value if value is not None else _INVALID_NUMBER


def _sibling_field(key: str) -> Optional[str]:
    """Друга половина пари: max_rooms <-> min_rooms і так далі."""
    if key.startswith("min_"):
        return "max_" + key[4:]
    if key.startswith("max_"):
        return "min_" + key[4:]
    return None


def _violates_sibling_bound(state: Dict[str, object], key: str, value) -> bool:
    if value is None:
        return False
    sibling = _sibling_field(key)
    sibling_value = state.get(sibling) if sibling else None
    if sibling_value is None:
        return False
    lo, hi = (value, sibling_value) if key.startswith("min_") else (sibling_value, value)
    return lo > hi


def _criteria_from_state(state: Dict[str, object]) -> Dict[str, object]:
    criteria = {"districts": list(state.get("districts_selected") or [])}
    for key in IMMOWELT_CRITERIA_KEYS:
        criteria[key] = state.get(key)
    return criteria


def _describe_range(min_val, max_val, *, unit: str, is_int: bool = False) -> Optional[str]:
    if min_val is None and max_val is None:
        return None
    fmt = (lambda v: str(int(round(v)))) if is_int else (lambda v: f"{v:g}")
    if min_val is not None and max_val is not None:
        return f"{fmt(min_val)}–{fmt(max_val)}{unit}"
    if min_val is not None:
        return f"від {fmt(min_val)}{unit}"
    return f"до {fmt(max_val)}{unit}"


def _describe_criteria(criteria: Dict[str, object]) -> str:
    districts = criteria.get("districts") or []
    parts = [", ".join(str(item) for item in districts) if districts else "усі райони"]
    price = _describe_range(criteria.get("min_price_eur"), criteria.get("max_price_eur"), unit=" €", is_int=True)
    if price:
        parts.append(price)
    rooms = _describe_range(criteria.get("min_rooms"), criteria.get("max_rooms"), unit=" кімн.")
    if rooms:
        parts.append(rooms)
    area = _describe_range(criteria.get("min_area_m2"), criteria.get("max_area_m2"), unit=" м²")
    if area:
        parts.append(area)
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
        rows.insert(1, [InlineKeyboardButton("🔔 Сповіщення", callback_data="housing:notify_settings")])
    elif is_allowed(user_id):
        rows.insert(0, [InlineKeyboardButton(BTN_SELF_ADD, callback_data="housing:self_add")])
        rows.insert(1, [InlineKeyboardButton(BTN_SELF_ADD_PROPOT, callback_data="housing:self_propot_add")])
        rows.insert(2, [InlineKeyboardButton(BTN_SELF_MANAGE, callback_data="housing:self_manage")])
        rows.insert(3, [InlineKeyboardButton("🔔 Сповіщення", callback_data="housing:notify_settings")])
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


def _relative_time(value) -> str:
    """Час словами замість дати-часу, яку людина йде перевіряти вручну.

    Технічна відмітка «13.08.2026 02:25» вимагає рахувати самому, скільки це
    було тому. «3 хв тому» відповідає на реальне питання одразу.
    """
    checked_at = _as_berlin_datetime(value)
    if checked_at is None:
        return "ще не було"
    now = _now_berlin()
    seconds = max(0.0, (now - checked_at).total_seconds())
    if seconds < 60:
        return "щойно"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} хв тому"
    hours = int(minutes // 60)
    if hours < 24:
        return f"{hours} год тому"
    if checked_at.date() == (now - timedelta(days=1)).date():
        return f"учора о {checked_at.strftime('%H:%M')}"
    return checked_at.strftime("%d.%m.%Y %H:%M")


def _traffic_light(value, max_age: timedelta) -> str:
    """🟢 свіжо, 🟡 наближається до простроченого, 🔴 прострочено або й не було.

    Абсолютна мітка часу вимагала подумки порівнювати її із зараз; колір видно
    одним поглядом ще до читання тексту.
    """
    checked_at = _as_berlin_datetime(value)
    if checked_at is None:
        return "🔴"
    age = _now_berlin() - checked_at
    if age <= max_age / 2:
        return "🟢"
    if age <= max_age:
        return "🟡"
    return "🔴"


def _immowelt_status_lines() -> list:
    """Рядки про стан обходу Immowelt.

    Час беремо з `/api/status`: обхід один на всіх, тому власної відмітки у
    фільтрів немає. Поки приймач її не віддає, відкочуємось на
    `last_checked_at` самих фільтрів, інакше панель мовчала б про перевірку.
    """
    filters = [item for item in _all_immowelt_filters() if item.get("active")]
    if not filters:
        return ["⚪ Immowelt: активних фільтрів немає."]

    status = _receiver_status()
    checked_at = str(status.get("immowelt_last_check_at") or "")
    if not checked_at:
        checked_at = max((str(item.get("last_checked_at") or "") for item in filters), default="")
    seen_total = sum(int(item.get("seen_count") or 0) for item in filters)
    error = str(status.get("immowelt_last_error") or "")
    skip_reason = str(status.get("immowelt_last_skip_reason") or "")

    light = _traffic_light(checked_at, IMMOWELT_STALE_AFTER)
    if not checked_at:
        lines = ["🔴 Immowelt: перевірка ще не запускалась."]
    else:
        lines = [f"{light} Immowelt: перевірка {_relative_time(checked_at)}, збережено: {seen_total}."]
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
        light = _traffic_light(status.get("last_checked_at"), PROPOTSDAM_STALE_AFTER)
        label = status.get("last_status") or "unknown"
        count = status.get("listings_count") or 0
        lines.append(
            f"{light} ProPotsdam: перевірка {_relative_time(status.get('last_checked_at'))}, "
            f"статус {html.escape(str(label))}, квартир: {count}."
        )
        if status.get("last_error"):
            lines.append(f"Остання помилка ProPotsdam: {html.escape(str(status.get('last_error')))}")
    else:
        lines.append("🔴 ProPotsdam: перевірка ще не запускалась.")
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


def _immowelt_criteria_keyboard(selected=None) -> InlineKeyboardMarkup:
    selected = set(selected or [])
    rows = []
    for spec in IMMOWELT_CRITERIA_FIELDS:
        mark = "✅" if spec["key"] in selected else "☐"
        rows.append([InlineKeyboardButton(
            f"{mark} {spec['label']}", callback_data=f"housing:imm_crit:{spec['key']}"
        )])
    rows.append([InlineKeyboardButton("➡️ Далі", callback_data="housing:imm_crit_done")])
    rows.append([InlineKeyboardButton(BTN_CANCEL, callback_data="housing:imm_cancel")])
    return InlineKeyboardMarkup(rows)


def _immowelt_criteria_text(selected=None) -> str:
    selected = selected or []
    labels = [spec["label"] for spec in IMMOWELT_CRITERIA_FIELDS if spec["key"] in selected]
    suffix = ", ".join(labels) if labels else "без додаткових умов"
    return (
        "🎚 <b>Умови Immowelt</b>\n\n"
        "Оберіть, які умови хочете задати — далі спитаю кожну окремим числом.\n"
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


def _propot_criteria_keyboard(selected=None) -> InlineKeyboardMarkup:
    selected = set(selected or [])
    rows = []
    for spec in PROPOT_CRITERIA_FIELDS:
        mark = "✅" if spec["key"] in selected else "☐"
        rows.append([InlineKeyboardButton(
            f"{mark} {spec['label']}", callback_data=f"housing:propot_crit:{spec['key']}"
        )])
    rows.append([InlineKeyboardButton("➡️ Далі", callback_data="housing:propot_crit_done")])
    rows.append([InlineKeyboardButton(BTN_CANCEL, callback_data="housing:propot_cancel")])
    return InlineKeyboardMarkup(rows)


def _propot_criteria_text(selected=None) -> str:
    selected = selected or []
    labels = [spec["label"] for spec in PROPOT_CRITERIA_FIELDS if spec["key"] in selected]
    suffix = ", ".join(labels) if labels else "без додаткових умов"
    return (
        "🎚 <b>Умови ProPotsdam</b>\n\n"
        "Оберіть, які умови хочете задати — далі спитаю кожну окремим числом.\n"
        f"Поточний вибір: {html.escape(suffix)}"
    )


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


def _item_source(item: Dict[str, object]) -> str:
    """Джерело фільтра: довіряємо явному полю, а не вгадуємо його з набору ключів.

    Immowelt-записи від receiver теж носять `districts` (критерії фільтра),
    тож стара перевірка `"districts" in item` плутала будь-який Immowelt-
    фільтр із ProPotsdam, щойно в нього з'являлись критерії — пауза чи
    редагування йшли не в ту таблицю і тихо нічого не змінювали.
    """
    source = item.get("source")
    if source:
        return str(source)
    return "propotsdam" if "districts" in item else "immowelt"


def _self_manage_keyboard(user_id: int) -> InlineKeyboardMarkup:
    rows = []
    for item in manageable_filters(user_id):
        source = _item_source(item)
        filter_id = int(item.get("filter_id"))
        active = bool(item.get("active", True))
        mark = "✅" if active else "⏸"
        title = str(item.get("title") or "Пошук житла")[:30]
        rows.append([InlineKeyboardButton(
            f"{mark} {title}",
            callback_data=f"housing:toggle:{source}:{filter_id}:{0 if active else 1}",
        )])
        # Раніше тут можна було лише поставити фільтр на паузу: одруківся в
        # ціні — і йшли зі скаргою до адміна, бо змінити нічого не могли.
        actions = []
        if source == "immowelt":
            actions.append(InlineKeyboardButton(
                "✏️ Редагувати", callback_data=f"housing:edit:{source}:{filter_id}"
            ))
        actions.append(InlineKeyboardButton(
            "🗑 Видалити", callback_data=f"housing:delete:{source}:{filter_id}"
        ))
        rows.append(actions)
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


# Вікно тихої ночі й ліміт на годину задаються приймачу через змінні
# оточення (HOUSING_QUIET_HOURS_*, HOUSING_HOURLY_SEND_CAP) і спільні для всіх
# користувачів; тут лише типові значення для тексту, самі не змінюють нічого.
QUIET_HOURS_LABEL = "23:00–08:00"
DEFAULT_HOURLY_CAP_LABEL = "5 оголошень"


def _notification_prefs(user_id: int) -> Dict[str, object]:
    try:
        return _request("GET", "/api/housing/notification-prefs", params={"user_id": user_id})
    except Exception:
        logger.exception("Could not load notification prefs")
        return {"quiet_hours_enabled": False, "digest_mode": "instant"}


def _set_notification_prefs(user_id: int, **kwargs) -> Dict[str, object]:
    return _request("POST", "/api/housing/notification-prefs", json={"user_id": user_id, **kwargs})


def _notify_settings_text(prefs: Dict[str, object]) -> str:
    quiet = "✅ увімкнена" if prefs.get("quiet_hours_enabled") else "вимкнена"
    mode = "раз на день" if prefs.get("digest_mode") == "daily" else "одразу"
    return (
        "🔔 <b>Сповіщення</b>\n\n"
        f"🌙 Тиха ніч ({QUIET_HOURS_LABEL}): {quiet}\n"
        f"📬 Режим доставки: {mode}\n\n"
        "У тиху ніч і в режимі «раз на день» оголошення не пропадають — "
        "прийдуть, щойно ніч закінчиться чи настане час зведення. Понад "
        f"{DEFAULT_HOURLY_CAP_LABEL} за годину теж не приходить одразу: "
        "решта — одним підсумковим повідомленням."
    )


def _notify_settings_keyboard(prefs: Dict[str, object]) -> InlineKeyboardMarkup:
    quiet_on = bool(prefs.get("quiet_hours_enabled"))
    mode = str(prefs.get("digest_mode") or "instant")
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "🌙 Тиха ніч: увімкнена ✅" if quiet_on else "🌙 Тиха ніч: вимкнена",
            callback_data=f"housing:notify_quiet:{0 if quiet_on else 1}",
        )],
        [
            InlineKeyboardButton(
                ("✅ " if mode == "instant" else "") + "📬 Одразу",
                callback_data="housing:notify_digest:instant",
            ),
            InlineKeyboardButton(
                ("✅ " if mode == "daily" else "") + "📮 Раз на день",
                callback_data="housing:notify_digest:daily",
            ),
        ],
        [InlineKeyboardButton("⬅ До моніторингу", callback_data="housing:menu")],
    ])


def show_notify_settings(update: Update, context: CallbackContext, edit: bool = False) -> None:
    user = update.effective_user
    if not user or not is_allowed(user.id):
        return
    prefs = _notification_prefs(int(user.id))
    text = _notify_settings_text(prefs)
    keyboard = _notify_settings_keyboard(prefs)
    if edit and update.callback_query:
        try:
            update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)
        except BadRequest as exc:
            if "Message is not modified" not in str(exc):
                raise
    else:
        update.effective_message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)


def toggle_quiet_hours(update: Update, context: CallbackContext, enabled: bool) -> None:
    query = update.callback_query
    user = update.effective_user
    if not query or not user or not is_allowed(user.id):
        return
    try:
        _set_notification_prefs(int(user.id), quiet_hours_enabled=enabled)
    except Exception:
        logger.exception("Could not update notification prefs")
        query.answer("Не вдалося оновити налаштування.", show_alert=True)
        return
    query.answer("Оновлено.")
    show_notify_settings(update, context, edit=True)


def set_digest_mode(update: Update, context: CallbackContext, mode: str) -> None:
    query = update.callback_query
    user = update.effective_user
    if not query or not user or not is_allowed(user.id):
        return
    try:
        _set_notification_prefs(int(user.id), digest_mode=mode)
    except Exception:
        logger.exception("Could not update notification prefs")
        query.answer("Не вдалося оновити налаштування.", show_alert=True)
        return
    query.answer("Оновлено.")
    show_notify_settings(update, context, edit=True)


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
    own = [
        item for item in manageable_filters(user.id)
        if int(item.get("filter_id") or 0) == filter_id
        and int(item.get("user_id") or 0) == int(user.id)
        and _item_source(item) == source
    ]
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


def _delete_confirm_keyboard(source: str, filter_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "🗑 Так, видалити", callback_data=f"housing:delete_confirm:{source}:{filter_id}"
        ),
        InlineKeyboardButton(BTN_CANCEL, callback_data="housing:self_manage"),
    ]])


def _own_filter(user_id: int, source: str, filter_id: int) -> Optional[Dict[str, object]]:
    for item in manageable_filters(user_id):
        if (
            int(item.get("filter_id") or 0) == filter_id
            and int(item.get("user_id") or 0) == user_id
            and _item_source(item) == source
        ):
            return item
    return None


def start_delete_flow(update: Update, context: CallbackContext, source: str, filter_id: int) -> None:
    query = update.callback_query
    user = update.effective_user
    if not query or not user or not is_allowed(user.id):
        return
    item = _own_filter(int(user.id), source, filter_id)
    if not item:
        query.answer("Цей фільтр вам не належить.", show_alert=True)
        return
    title = html.escape(str(item.get("title") or "Пошук житла"))
    query.answer()
    query.edit_message_text(
        f"🗑 Видалити фільтр «{title}»? Це не можна скасувати.",
        parse_mode="HTML",
        reply_markup=_delete_confirm_keyboard(source, filter_id),
    )


def confirm_delete_filter(update: Update, context: CallbackContext, source: str, filter_id: int) -> None:
    query = update.callback_query
    user = update.effective_user
    if not query or not user or not is_allowed(user.id):
        return
    if not _own_filter(int(user.id), source, filter_id):
        query.answer("Цей фільтр вам не належить.", show_alert=True)
        return
    if source == "propotsdam":
        ok = propotsdam_store.delete_filter(filter_id, user_id=user.id)
        if ok:
            _sync_propot_filters()
    else:
        try:
            _request("DELETE", f"/api/housing/filters/{filter_id}")
            ok = True
        except Exception:
            logger.exception("Could not delete housing filter")
            ok = False
    if not ok:
        query.answer("Не вдалося видалити фільтр.", show_alert=True)
        return
    query.answer("Фільтр видалено.")
    show_self_manage(update, context, edit=True)


def start_edit_flow(update: Update, context: CallbackContext, filter_id: int) -> None:
    """Заводить майстер наново, заповнений поточними умовами фільтра.

    Змінюємо лише критерії — район, ціну, кімнати, площу; користувача й назву
    редагування не чіпає, це прибрало б потребу перебирати весь той самий
    майстер, що і при додаванні.
    """
    query = update.callback_query
    user = update.effective_user
    if not query or not user or not is_allowed(user.id):
        return
    item = _own_filter(int(user.id), "immowelt", filter_id)
    if not item:
        query.answer("Цей фільтр вам не належить.", show_alert=True)
        return
    districts_selected = list(item.get("districts") or [])
    context.user_data["housing_admin"] = {
        "mode": "immowelt", "step": "districts", "user_id": int(user.id),
        "title": str(item.get("title") or "Пошук житла"),
        "districts_selected": districts_selected,
        "min_price_eur": item.get("min_price_eur"),
        "max_price_eur": item.get("max_price_eur"),
        "min_rooms": item.get("min_rooms"),
        "max_rooms": item.get("max_rooms"),
        "min_area_m2": item.get("min_area_m2"),
        "max_area_m2": item.get("max_area_m2"),
        # Раніше задані умови показуємо вже позначеними — інакше редагування
        # виглядало б так, ніби всі попередні обмеження скинулися.
        "criteria_selected": [key for key in IMMOWELT_CRITERIA_KEYS if item.get(key) is not None],
        "edit_filter_id": filter_id,
    }
    query.answer()
    query.edit_message_text(
        _immowelt_district_text(districts_selected),
        parse_mode="HTML",
        reply_markup=_immowelt_district_keyboard(districts_selected),
    )


def _show_immowelt_preview(message, state: dict) -> None:
    criteria = _criteria_from_state(state)
    state["step"] = "preview"
    message.reply_text(
        _preview_text(str(state.get("title") or ""), criteria, _preview_criteria(criteria)),
        parse_mode="HTML",
        reply_markup=_preview_keyboard(),
        disable_web_page_preview=True,
    )


def _cross_source_suggestion(
    context: CallbackContext, chatter_id: int, filter_user_id: int, just_created: str, criteria: Dict[str, object]
) -> Optional[InlineKeyboardButton]:
    """Кнопка «заведіть і другий фільтр» — і сама переносить уже введене.

    Джерела ловлять різні сайти — той, хто стежить лише за Immowelt, легко
    забуває, що ProPotsdam треба заводити окремо (і навпаки). Показуємо
    підказку лише самому власнику: якщо адмін додає фільтр іншій людині,
    підказка адміну про чужий другий фільтр була б не до речі.

    Район, кімнати й площу переносимо без повторних питань — це ті самі
    одиниці й майже ті самі назви районів. Ціну свідомо не чіпаємо: Immowelt
    рахує холодну оренду, ProPotsdam — повну, тож перенесене число означало б
    зовсім іншу суму, а не ту саму умову.
    """
    if int(chatter_id) != int(filter_user_id):
        return None
    districts = list(criteria.get("districts") or [])
    title = str(criteria.get("title") or "Пошук житла")
    shared = {
        "user_id": int(filter_user_id),
        "title": title,
        "min_rooms": criteria.get("min_rooms"),
        "max_rooms": criteria.get("max_rooms"),
        "min_area_m2": criteria.get("min_area_m2"),
        "max_area_m2": criteria.get("max_area_m2"),
    }
    if just_created == "immowelt":
        if propotsdam_store.list_filters(user_id=int(filter_user_id), active_only=True):
            return None
        context.user_data["housing_clone_source"] = {
            "target": "propotsdam",
            "districts": _translate_districts(districts, IMMOWELT_TO_PROPOT_DISTRICT, set(PROPOT_DISTRICTS)),
            **shared,
        }
        return InlineKeyboardButton(
            "🏢 Створити такий самий фільтр ProPotsdam", callback_data="housing:clone_propot"
        )
    if just_created == "propotsdam":
        immowelt = [
            item for item in _all_immowelt_filters()
            if int(item.get("user_id") or 0) == int(filter_user_id) and item.get("active")
        ]
        if immowelt:
            return None
        context.user_data["housing_clone_source"] = {
            "target": "immowelt",
            "districts": _translate_districts(districts, PROPOT_TO_IMMOWELT_DISTRICT, set(IMMOWELT_DISTRICTS)),
            **shared,
        }
        return InlineKeyboardButton(
            "🏠 Створити такий самий фільтр Immowelt", callback_data="housing:clone_immo"
        )
    return None


def _clone_propot_from_immowelt(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    source = context.user_data.pop("housing_clone_source", None)
    if not source or source.get("target") != "propotsdam":
        query.answer()
        return
    filter_id = propotsdam_store.create_filter(
        user_id=source["user_id"],
        title=source["title"],
        districts=propotsdam_store.normalize_districts(",".join(source.get("districts") or [])),
        min_rooms=source.get("min_rooms"),
        max_rooms=source.get("max_rooms"),
        min_area_m2=source.get("min_area_m2"),
        max_area_m2=source.get("max_area_m2"),
    )
    _sync_propot_filters()
    query.answer("Фільтр ProPotsdam створено.")
    query.edit_message_text(
        f"✅ Фільтр ProPotsdam додано.\nID: P{filter_id}\nНазва: {html.escape(str(source['title']))}\n\n"
        "Район, кімнати й площу перенесено з Immowelt-фільтра. Оренду не переносив — "
        "Immowelt рахує холодну, ProPotsdam повну; за потреби задайте її окремо через «Мої фільтри»."
    )


def _clone_immowelt_from_propot(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    source = context.user_data.pop("housing_clone_source", None)
    if not source or source.get("target") != "immowelt":
        query.answer()
        return
    try:
        payload = _request("POST", "/api/housing/filters", json={
            "user_id": source["user_id"],
            "title": source["title"],
            "districts": source.get("districts") or [],
            "min_price_eur": None,
            "max_price_eur": None,
            "min_rooms": source.get("min_rooms"),
            "max_rooms": source.get("max_rooms"),
            "min_area_m2": source.get("min_area_m2"),
            "max_area_m2": source.get("max_area_m2"),
        })
    except Exception as exc:
        logger.exception("Could not clone Immowelt filter from ProPotsdam")
        query.answer("Не вдалося зберегти фільтр.", show_alert=True)
        query.edit_message_text(f"⚠️ Не вдалося зберегти фільтр: {html.escape(str(exc))}")
        return
    query.answer("Фільтр Immowelt створено.")
    query.edit_message_text(
        f"✅ Фільтр житла додано.\nID: {payload.get('filter_id')}\nНазва: {html.escape(str(source['title']))}\n\n"
        "Район, кімнати й площу перенесено з ProPotsdam-фільтра. Ціну не переносив — "
        "ProPotsdam рахує повну оренду, Immowelt холодну; за потреби задайте її окремо через «Мої фільтри»."
    )


def _save_immowelt_filter(update: Update, context: CallbackContext) -> None:
    """Зберігає зібраний майстром фільтр разом із його умовами.

    Раніше сюди йшли лише назва й посилання, а відбір іде за умовами в самому
    записі: фільтр без умов збігається з будь-якою квартирою Потсдама.
    Той самий майстер веде і редагування — відрізняє лише `edit_filter_id`
    у стані, з яким сюди приходять.
    """
    query = update.callback_query
    state = context.user_data.get("housing_admin") or {}
    if state.get("mode") != "immowelt" or state.get("step") != "preview":
        query.answer()
        return
    criteria = _criteria_from_state(state)
    edit_filter_id = state.get("edit_filter_id")
    try:
        if edit_filter_id:
            _request("PATCH", f"/api/housing/filters/{edit_filter_id}", json={
                "title": state["title"], **criteria,
            })
            filter_id = edit_filter_id
        else:
            payload = _request("POST", "/api/housing/filters", json={
                "user_id": state["user_id"], "title": state["title"], **criteria,
            })
            filter_id = payload.get("filter_id")
    except Exception as exc:
        logger.exception("Could not save housing filter")
        query.answer("Не вдалося зберегти фільтр.", show_alert=True)
        query.edit_message_text(f"⚠️ Не вдалося зберегти фільтр: {html.escape(str(exc))}")
        context.user_data.pop("housing_admin", None)
        return
    context.user_data.pop("housing_admin", None)
    heading = "Фільтр оновлено" if edit_filter_id else "Фільтр житла додано"
    query.answer("Фільтр оновлено." if edit_filter_id else "Фільтр збережено.")
    text_out = (
        f"✅ <b>{heading}</b>\n\n"
        f"ID: {filter_id}\n"
        f"Назва: {html.escape(str(state['title']))}\n"
        f"Умови: {_describe_criteria(criteria)}"
    )
    rows = [[InlineKeyboardButton("⬅ До моніторингу", callback_data="housing:menu")]]
    if not edit_filter_id:
        suggestion = _cross_source_suggestion(
            context, int(update.effective_user.id), int(state["user_id"]), "immowelt",
            {**criteria, "title": state["title"]},
        )
        if suggestion is not None:
            text_out += "\n\n💡 У вас ще немає фільтра ProPotsdam — можна завести такий самий."
            rows.insert(0, [suggestion])
    query.edit_message_text(text_out, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))


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
    if step == "criteria":
        update.message.reply_text(
            "Оберіть умови кнопками нижче і натисніть «Далі».",
            reply_markup=_immowelt_criteria_keyboard(state.get("criteria_selected")),
        )
        return True
    if step in IMMOWELT_CRITERIA_BY_KEY:
        spec = IMMOWELT_CRITERIA_BY_KEY[step]
        value = _parse_single_number(text)
        if value is _INVALID_NUMBER:
            update.message.reply_text("Потрібне число або «-», щоб пропустити.\n\n" + spec["prompt"])
            return True
        if _violates_sibling_bound(state, step, value):
            update.message.reply_text(
                "Мінімум не може бути більшим за максимум. Надішліть значення ще раз.\n\n" + spec["prompt"]
            )
            return True
        state[step] = value
        queue = list(state.get("criteria_queue") or [])
        if queue and queue[0] == step:
            queue = queue[1:]
        state["criteria_queue"] = queue
        if queue:
            state["step"] = queue[0]
            update.message.reply_text(IMMOWELT_CRITERIA_BY_KEY[queue[0]]["prompt"])
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
    state["step"] = "criteria"
    query.answer()
    query.edit_message_text(
        _immowelt_criteria_text(state.get("criteria_selected")),
        parse_mode="HTML",
        reply_markup=_immowelt_criteria_keyboard(state.get("criteria_selected")),
    )


def _toggle_immowelt_criteria(update: Update, context: CallbackContext, key: str) -> None:
    query = update.callback_query
    state = context.user_data.get("housing_admin") or {}
    if state.get("mode") != "immowelt" or state.get("step") != "criteria":
        query.answer()
        return
    selected = list(state.get("criteria_selected") or [])
    if key in selected:
        selected.remove(key)
    else:
        selected.append(key)
    state["criteria_selected"] = selected
    query.answer()
    query.edit_message_text(
        _immowelt_criteria_text(selected),
        parse_mode="HTML",
        reply_markup=_immowelt_criteria_keyboard(selected),
    )


def _finish_immowelt_criteria(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    state = context.user_data.get("housing_admin") or {}
    if state.get("mode") != "immowelt" or state.get("step") != "criteria":
        query.answer()
        return
    selected = set(state.get("criteria_selected") or [])
    # Канонічний порядок полів, а не порядок натискань — так «максимум» пари
    # завжди питається після «мінімуму», і звірка meж у _violates_sibling_bound
    # завжди має з чим звіряти.
    queue = [key for key in IMMOWELT_CRITERIA_KEYS if key in selected]
    query.answer()
    if not queue:
        _show_immowelt_preview(query.message, state)
        return
    state["criteria_queue"] = queue
    state["step"] = queue[0]
    query.edit_message_text(IMMOWELT_CRITERIA_BY_KEY[queue[0]]["prompt"])


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
    if step == "criteria":
        update.message.reply_text(
            "Оберіть умови кнопками нижче і натисніть «Далі».",
            reply_markup=_propot_criteria_keyboard(state.get("criteria_selected")),
        )
        return True
    if step in PROPOT_CRITERIA_BY_KEY:
        spec = PROPOT_CRITERIA_BY_KEY[step]
        value = _parse_single_number(text)
        if value is _INVALID_NUMBER:
            update.message.reply_text("Потрібне число або «-», щоб пропустити.\n\n" + spec["prompt"])
            return True
        if _violates_sibling_bound(state, step, value):
            update.message.reply_text(
                "Мінімум не може бути більшим за максимум. Надішліть значення ще раз.\n\n" + spec["prompt"]
            )
            return True
        state[step] = value
        queue = list(state.get("criteria_queue") or [])
        if queue and queue[0] == step:
            queue = queue[1:]
        state["criteria_queue"] = queue
        if queue:
            state["step"] = queue[0]
            update.message.reply_text(PROPOT_CRITERIA_BY_KEY[queue[0]]["prompt"])
            return True
        _finalize_propot_filter(update.message, int(update.effective_user.id), context, state)
        return True
    return False


def _finalize_propot_filter(message, chatter_id: int, context: CallbackContext, state: dict) -> None:
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
    text_out = (
        f"✅ Фільтр ProPotsdam додано.\nID: P{filter_id}\n"
        f"Користувач: {state['user_id']}\nНазва: {html.escape(str(state['title']))}"
    )
    suggestion = _cross_source_suggestion(
        context, chatter_id, int(state["user_id"]), "propotsdam",
        {
            "title": state["title"],
            "districts": [d for d in str(state.get("districts") or "").split(",") if d],
            "min_rooms": state.get("min_rooms"),
            "max_rooms": state.get("max_rooms"),
            "min_area_m2": state.get("min_area_m2"),
            "max_area_m2": state.get("max_area_m2"),
        },
    )
    if suggestion is None:
        message.reply_text(text_out)
    else:
        message.reply_text(
            text_out + "\n\n💡 У вас ще немає фільтра Immowelt — можна завести такий самий.",
            reply_markup=InlineKeyboardMarkup([[suggestion]]),
        )


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
    state["step"] = "criteria"
    query.answer()
    query.edit_message_text(
        _propot_criteria_text(state.get("criteria_selected")),
        parse_mode="HTML",
        reply_markup=_propot_criteria_keyboard(state.get("criteria_selected")),
    )


def _toggle_propot_criteria(update: Update, context: CallbackContext, key: str) -> None:
    query = update.callback_query
    state = context.user_data.get("housing_admin") or {}
    if state.get("mode") != "propotsdam" or state.get("step") != "criteria":
        query.answer()
        return
    selected = list(state.get("criteria_selected") or [])
    if key in selected:
        selected.remove(key)
    else:
        selected.append(key)
    state["criteria_selected"] = selected
    query.answer()
    query.edit_message_text(
        _propot_criteria_text(selected),
        parse_mode="HTML",
        reply_markup=_propot_criteria_keyboard(selected),
    )


def _finish_propot_criteria(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    state = context.user_data.get("housing_admin") or {}
    if state.get("mode") != "propotsdam" or state.get("step") != "criteria":
        query.answer()
        return
    selected = set(state.get("criteria_selected") or [])
    queue = [key for key in PROPOT_CRITERIA_KEYS if key in selected]
    query.answer()
    if not queue:
        _finalize_propot_filter(query.message, int(update.effective_user.id), context, state)
        return
    state["criteria_queue"] = queue
    state["step"] = queue[0]
    query.edit_message_text(PROPOT_CRITERIA_BY_KEY[queue[0]]["prompt"])


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
    elif query.data.startswith("housing:imm_crit:"):
        _toggle_immowelt_criteria(update, context, query.data.split(":", 2)[2])
    elif query.data == "housing:imm_crit_done":
        _finish_immowelt_criteria(update, context)
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
    elif query.data.startswith("housing:propot_crit:"):
        _toggle_propot_criteria(update, context, query.data.split(":", 2)[2])
    elif query.data == "housing:propot_crit_done":
        _finish_propot_criteria(update, context)
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
    elif query.data == "housing:clone_propot":
        _clone_propot_from_immowelt(update, context)
    elif query.data == "housing:clone_immo":
        _clone_immowelt_from_propot(update, context)
    elif query.data == "housing:self_manage":
        query.answer()
        show_self_manage(update, context, edit=True)
    elif query.data == "housing:notify_settings":
        query.answer()
        show_notify_settings(update, context, edit=True)
    elif query.data.startswith("housing:notify_quiet:"):
        raw = query.data.split(":")[2]
        toggle_quiet_hours(update, context, raw == "1")
    elif query.data.startswith("housing:notify_digest:"):
        mode = query.data.split(":", 2)[2]
        set_digest_mode(update, context, mode)
    elif query.data.startswith("housing:toggle:"):
        _toggle_owned_filter(update, context)
    elif query.data.startswith("housing:edit:"):
        _, _, source, raw_id = query.data.split(":", 3)
        if source == "immowelt" and raw_id.isdigit():
            start_edit_flow(update, context, int(raw_id))
        else:
            query.answer()
    elif query.data.startswith("housing:delete_confirm:"):
        _, _, source, raw_id = query.data.split(":", 3)
        if raw_id.isdigit():
            confirm_delete_filter(update, context, source, int(raw_id))
        else:
            query.answer()
    elif query.data.startswith("housing:delete:"):
        _, _, source, raw_id = query.data.split(":", 3)
        if raw_id.isdigit():
            start_delete_flow(update, context, source, int(raw_id))
        else:
            query.answer()
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
