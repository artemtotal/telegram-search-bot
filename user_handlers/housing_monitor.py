"""Private housing monitoring menu backed by local housing receivers."""

import calendar
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

import i18n
from user_jobs import (
    coop_watchdog,
    coop_watchdog_store,
    housing_access_store,
    karlmarx_matching,
    karlmarx_store,
    kleinanzeigen_matching,
    kleinanzeigen_store,
    locals_matching,
    locals_store,
    propotsdam_matching,
    propotsdam_parser,
    propotsdam_store,
    regiomakler_matching,
    regiomakler_store,
    schoba_matching,
    schoba_store,
    semmelhaack_matching,
    semmelhaack_store,
    user_settings_store,
)

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
BTN_ADMIN_ADD = "➕ Додати користувача"
BTN_ADMIN_ACCESS_ADD = "👤 Додати доступ користувачу"
BTN_ADMIN_ACCESS_LIST = "👥 Доступ до моніторингу"
BTN_CANCEL = "✖ Скасувати"
BTN_SELF_ADD = "➕ Додати фільтр"
BTN_SELF_MANAGE = "⚙️ Мої фільтри"
BTN_CURRENT_MATCHES = "🔍 Квартири, що підходять"
BTN_COOPS = "🏘 Кооперативи (Gewoba/WBG)"
ACCESS_MONTH_OPTIONS = [1, 3, 6, 12]
EXPIRY_WARNING_DAYS = 3
BERLIN_TZ = ZoneInfo("Europe/Berlin")
IMMOWELT_STALE_AFTER = timedelta(minutes=30)
# propotsdam/semmelhaack/schoba/regiomakler/locals/karlmarx all scan every 15
# minutes now (2026-08-21, was 30) — 20 minutes keeps the same ~1.5x margin
# so a normal successful check still reads green/yellow, not red.
PROPOTSDAM_STALE_AFTER = timedelta(minutes=20)
# Kleinanzeigen опитується раз на 30 хв, а не раз на 15, як решта джерел —
# з тим самим порогом свіжості він завжди показував би 🔴 одразу після успіху.
KLEINANZEIGEN_STALE_AFTER = timedelta(minutes=45)
# coop_watchdog.CHECK_INTERVAL_SECONDS is 30 minutes - same ~1.5x margin.
COOP_STALE_AFTER = timedelta(minutes=45)
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
def _numeric_prompt(question: str, example: str) -> str:
    """Formats a wizard numeric question in a consistent, hand-holding way —
    people were skimming past the terse one-liner ("Мінімальна кількість
    кімнат: Або «-», щоб пропустити.") and not noticing they could just type
    a number and hit send, or skip with a dash."""
    return (
        f"🔢 <b>{question}</b>\n\n"
        f"Напишіть <b>одну цифру</b> внизу в чаті й натисніть «Надіслати» "
        f"(наприклад, <code>{example}</code>).\n\n"
        "Не хочете обмежувати цей показник — просто надішліть «-»."
    )


IMMOWELT_CRITERIA_FIELDS = [
    {"key": "min_price_eur", "label": "Ціна: мінімум (від)", "prompt": _numeric_prompt("Мінімальна холодна оренда (Kaltmiete), €", "600"), "label_key": "housing.field.label.price_min", "question_key": "housing.field.q.kaltmiete_min", "example": "600"},
    {"key": "max_price_eur", "label": "Ціна: максимум (до)", "prompt": _numeric_prompt("Максимальна холодна оренда (Kaltmiete), €", "1200"), "label_key": "housing.field.label.price_max", "question_key": "housing.field.q.kaltmiete_max", "example": "1200"},
    {"key": "min_rooms", "label": "Кімнати: мінімум (від)", "prompt": _numeric_prompt("Мінімальна кількість кімнат", "2"), "label_key": "housing.field.label.rooms_min", "question_key": "housing.field.q.rooms_min", "example": "2"},
    {"key": "max_rooms", "label": "Кімнати: максимум (до)", "prompt": _numeric_prompt("Максимальна кількість кімнат", "4"), "label_key": "housing.field.label.rooms_max", "question_key": "housing.field.q.rooms_max", "example": "4"},
    {"key": "min_area_m2", "label": "Площа: мінімум (від)", "prompt": _numeric_prompt("Мінімальна площа, м²", "50"), "label_key": "housing.field.label.area_min", "question_key": "housing.field.q.area_min", "example": "50"},
    {"key": "max_area_m2", "label": "Площа: максимум (до)", "prompt": _numeric_prompt("Максимальна площа, м²", "90"), "label_key": "housing.field.label.area_max", "question_key": "housing.field.q.area_max", "example": "90"},
]
IMMOWELT_CRITERIA_KEYS = [spec["key"] for spec in IMMOWELT_CRITERIA_FIELDS]
IMMOWELT_CRITERIA_BY_KEY = {spec["key"]: spec for spec in IMMOWELT_CRITERIA_FIELDS}
ADMIN_PAGE_SIZE = 20
PROPOT_CRITERIA_FIELDS = [
    {"key": "min_rooms", "label": "Кімнати: мінімум (від)", "prompt": _numeric_prompt("Мінімальна кількість кімнат", "2"), "label_key": "housing.field.label.rooms_min", "question_key": "housing.field.q.rooms_min", "example": "2"},
    {"key": "max_rooms", "label": "Кімнати: максимум (до)", "prompt": _numeric_prompt("Максимальна кількість кімнат", "4"), "label_key": "housing.field.label.rooms_max", "question_key": "housing.field.q.rooms_max", "example": "4"},
    {"key": "min_area_m2", "label": "Площа: мінімум (від)", "prompt": _numeric_prompt("Мінімальна площа, м²", "50"), "label_key": "housing.field.label.area_min", "question_key": "housing.field.q.area_min", "example": "50"},
    {"key": "max_area_m2", "label": "Площа: максимум (до)", "prompt": _numeric_prompt("Максимальна площа, м²", "90"), "label_key": "housing.field.label.area_max", "question_key": "housing.field.q.area_max", "example": "90"},
    {"key": "min_total_rent_eur", "label": "Оренда: мінімум (від)", "prompt": _numeric_prompt("Мінімальна загальна оренда (Gesamtmiete), €", "700"), "label_key": "housing.field.label.rent_min", "question_key": "housing.field.q.gesamtmiete_min", "example": "700"},
    {"key": "max_total_rent_eur", "label": "Оренда: максимум (до)", "prompt": _numeric_prompt("Максимальна загальна оренда (Gesamtmiete), €", "1400"), "label_key": "housing.field.label.rent_max", "question_key": "housing.field.q.gesamtmiete_max", "example": "1400"},
]
PROPOT_CRITERIA_KEYS = [spec["key"] for spec in PROPOT_CRITERIA_FIELDS]
PROPOT_CRITERIA_BY_KEY = {spec["key"]: spec for spec in PROPOT_CRITERIA_FIELDS}
# SEMMELHAACK не показує райони взагалі — фільтр тут лише кімнати/площа/ціна,
# і ціна там теж холодна оренда (Kaltmiete), як у Immowelt.
SEMM_CRITERIA_FIELDS = [
    {"key": "min_rooms", "label": "Кімнати: мінімум (від)", "prompt": _numeric_prompt("Мінімальна кількість кімнат", "2"), "label_key": "housing.field.label.rooms_min", "question_key": "housing.field.q.rooms_min", "example": "2"},
    {"key": "max_rooms", "label": "Кімнати: максимум (до)", "prompt": _numeric_prompt("Максимальна кількість кімнат", "4"), "label_key": "housing.field.label.rooms_max", "question_key": "housing.field.q.rooms_max", "example": "4"},
    {"key": "min_area_m2", "label": "Площа: мінімум (від)", "prompt": _numeric_prompt("Мінімальна площа, м²", "50"), "label_key": "housing.field.label.area_min", "question_key": "housing.field.q.area_min", "example": "50"},
    {"key": "max_area_m2", "label": "Площа: максимум (до)", "prompt": _numeric_prompt("Максимальна площа, м²", "90"), "label_key": "housing.field.label.area_max", "question_key": "housing.field.q.area_max", "example": "90"},
    {"key": "min_price_eur", "label": "Ціна: мінімум (від)", "prompt": _numeric_prompt("Мінімальна холодна оренда (Kaltmiete), €", "600"), "label_key": "housing.field.label.price_min", "question_key": "housing.field.q.kaltmiete_min", "example": "600"},
    {"key": "max_price_eur", "label": "Ціна: максимум (до)", "prompt": _numeric_prompt("Максимальна холодна оренда (Kaltmiete), €", "1200"), "label_key": "housing.field.label.price_max", "question_key": "housing.field.q.kaltmiete_max", "example": "1200"},
]
SEMM_CRITERIA_KEYS = [spec["key"] for spec in SEMM_CRITERIA_FIELDS]
SEMM_CRITERIA_BY_KEY = {spec["key"]: spec for spec in SEMM_CRITERIA_FIELDS}
# SCHOBA теж не показує райони надійно — фільтр лише кімнати/площа/ціна.
# Nettokaltmiete — та сама холодна оренда, що й у Immowelt/SEMMELHAACK.
SCHOBA_CRITERIA_FIELDS = [
    {"key": "min_rooms", "label": "Кімнати: мінімум (від)", "prompt": _numeric_prompt("Мінімальна кількість кімнат", "2"), "label_key": "housing.field.label.rooms_min", "question_key": "housing.field.q.rooms_min", "example": "2"},
    {"key": "max_rooms", "label": "Кімнати: максимум (до)", "prompt": _numeric_prompt("Максимальна кількість кімнат", "4"), "label_key": "housing.field.label.rooms_max", "question_key": "housing.field.q.rooms_max", "example": "4"},
    {"key": "min_area_m2", "label": "Площа: мінімум (від)", "prompt": _numeric_prompt("Мінімальна площа, м²", "50"), "label_key": "housing.field.label.area_min", "question_key": "housing.field.q.area_min", "example": "50"},
    {"key": "max_area_m2", "label": "Площа: максимум (до)", "prompt": _numeric_prompt("Максимальна площа, м²", "90"), "label_key": "housing.field.label.area_max", "question_key": "housing.field.q.area_max", "example": "90"},
    {"key": "min_price_eur", "label": "Ціна: мінімум (від)", "prompt": _numeric_prompt("Мінімальна холодна оренда (Nettokaltmiete), €", "600"), "label_key": "housing.field.label.price_min", "question_key": "housing.field.q.nettokaltmiete_min", "example": "600"},
    {"key": "max_price_eur", "label": "Ціна: максимум (до)", "prompt": _numeric_prompt("Максимальна холодна оренда (Nettokaltmiete), €", "1200"), "label_key": "housing.field.label.price_max", "question_key": "housing.field.q.nettokaltmiete_max", "example": "1200"},
]
SCHOBA_CRITERIA_KEYS = [spec["key"] for spec in SCHOBA_CRITERIA_FIELDS]
SCHOBA_CRITERIA_BY_KEY = {spec["key"]: spec for spec in SCHOBA_CRITERIA_FIELDS}
# ImmoTeam Potsdam і alpha Immobilien публікують одну спільну стрічку (плагін
# immomakler) — теж без надійного словника районів, теж Kaltmiete.
REGIOMAKLER_CRITERIA_FIELDS = [
    {"key": "min_rooms", "label": "Кімнати: мінімум (від)", "prompt": _numeric_prompt("Мінімальна кількість кімнат", "2"), "label_key": "housing.field.label.rooms_min", "question_key": "housing.field.q.rooms_min", "example": "2"},
    {"key": "max_rooms", "label": "Кімнати: максимум (до)", "prompt": _numeric_prompt("Максимальна кількість кімнат", "4"), "label_key": "housing.field.label.rooms_max", "question_key": "housing.field.q.rooms_max", "example": "4"},
    {"key": "min_area_m2", "label": "Площа: мінімум (від)", "prompt": _numeric_prompt("Мінімальна площа, м²", "50"), "label_key": "housing.field.label.area_min", "question_key": "housing.field.q.area_min", "example": "50"},
    {"key": "max_area_m2", "label": "Площа: максимум (до)", "prompt": _numeric_prompt("Максимальна площа, м²", "90"), "label_key": "housing.field.label.area_max", "question_key": "housing.field.q.area_max", "example": "90"},
    {"key": "min_price_eur", "label": "Ціна: мінімум (від)", "prompt": _numeric_prompt("Мінімальна холодна оренда (Kaltmiete), €", "600"), "label_key": "housing.field.label.price_min", "question_key": "housing.field.q.kaltmiete_min", "example": "600"},
    {"key": "max_price_eur", "label": "Ціна: максимум (до)", "prompt": _numeric_prompt("Максимальна холодна оренда (Kaltmiete), €", "1200"), "label_key": "housing.field.label.price_max", "question_key": "housing.field.q.kaltmiete_max", "example": "1200"},
]
REGIOMAKLER_CRITERIA_KEYS = [spec["key"] for spec in REGIOMAKLER_CRITERIA_FIELDS]
REGIOMAKLER_CRITERIA_BY_KEY = {spec["key"]: spec for spec in REGIOMAKLER_CRITERIA_FIELDS}
# Kleinanzeigen — оголошення від приватних осіб і дрібних агентств упереміш,
# без надійної мітки Kalt/Warm на ціні, тож у спільне запитання Kaltmiete не
# приєднується — питає ціну окремо, як ProPotsdam.
KLEINANZEIGEN_CRITERIA_FIELDS = [
    {"key": "min_rooms", "label": "Кімнати: мінімум (від)", "prompt": _numeric_prompt("Мінімальна кількість кімнат", "2"), "label_key": "housing.field.label.rooms_min", "question_key": "housing.field.q.rooms_min", "example": "2"},
    {"key": "max_rooms", "label": "Кімнати: максимум (до)", "prompt": _numeric_prompt("Максимальна кількість кімнат", "4"), "label_key": "housing.field.label.rooms_max", "question_key": "housing.field.q.rooms_max", "example": "4"},
    {"key": "min_area_m2", "label": "Площа: мінімум (від)", "prompt": _numeric_prompt("Мінімальна площа, м²", "50"), "label_key": "housing.field.label.area_min", "question_key": "housing.field.q.area_min", "example": "50"},
    {"key": "max_area_m2", "label": "Площа: максимум (до)", "prompt": _numeric_prompt("Максимальна площа, м²", "90"), "label_key": "housing.field.label.area_max", "question_key": "housing.field.q.area_max", "example": "90"},
    {"key": "min_price_eur", "label": "Ціна: мінімум (від)", "prompt": _numeric_prompt("Мінімальна ціна, €", "600"), "label_key": "housing.field.label.price_min", "question_key": "housing.field.q.price_min", "example": "600"},
    {"key": "max_price_eur", "label": "Ціна: максимум (до)", "prompt": _numeric_prompt("Максимальна ціна, €", "1200"), "label_key": "housing.field.label.price_max", "question_key": "housing.field.q.price_max", "example": "1200"},
]
KLEINANZEIGEN_CRITERIA_KEYS = [spec["key"] for spec in KLEINANZEIGEN_CRITERIA_FIELDS]
KLEINANZEIGEN_CRITERIA_BY_KEY = {spec["key"]: spec for spec in KLEINANZEIGEN_CRITERIA_FIELDS}
# locals® теж без районів; ціна — Kaltmiete, як у Immowelt/SEMMELHAACK/SCHOBA/regiomakler.
LOCALS_CRITERIA_FIELDS = [
    {"key": "min_rooms", "label": "Кімнати: мінімум (від)", "prompt": _numeric_prompt("Мінімальна кількість кімнат", "2"), "label_key": "housing.field.label.rooms_min", "question_key": "housing.field.q.rooms_min", "example": "2"},
    {"key": "max_rooms", "label": "Кімнати: максимум (до)", "prompt": _numeric_prompt("Максимальна кількість кімнат", "4"), "label_key": "housing.field.label.rooms_max", "question_key": "housing.field.q.rooms_max", "example": "4"},
    {"key": "min_area_m2", "label": "Площа: мінімум (від)", "prompt": _numeric_prompt("Мінімальна площа, м²", "50"), "label_key": "housing.field.label.area_min", "question_key": "housing.field.q.area_min", "example": "50"},
    {"key": "max_area_m2", "label": "Площа: максимум (до)", "prompt": _numeric_prompt("Максимальна площа, м²", "90"), "label_key": "housing.field.label.area_max", "question_key": "housing.field.q.area_max", "example": "90"},
    {"key": "min_price_eur", "label": "Ціна: мінімум (від)", "prompt": _numeric_prompt("Мінімальна холодна оренда (Kaltmiete), €", "600"), "label_key": "housing.field.label.price_min", "question_key": "housing.field.q.kaltmiete_min", "example": "600"},
    {"key": "max_price_eur", "label": "Ціна: максимум (до)", "prompt": _numeric_prompt("Максимальна холодна оренда (Kaltmiete), €", "1200"), "label_key": "housing.field.label.price_max", "question_key": "housing.field.q.kaltmiete_max", "example": "1200"},
]
LOCALS_CRITERIA_KEYS = [spec["key"] for spec in LOCALS_CRITERIA_FIELDS]
LOCALS_CRITERIA_BY_KEY = {spec["key"]: spec for spec in LOCALS_CRITERIA_FIELDS}
# Karl Marx теж без районів; ціна — Warmmiete (тепла оренда), не Kaltmiete,
# тож у спільне запитання Kaltmiete не приєднується — питає ціну окремо, як
# Kleinanzeigen/ProPotsdam.
KARLMARX_CRITERIA_FIELDS = [
    {"key": "min_rooms", "label": "Кімнати: мінімум (від)", "prompt": _numeric_prompt("Мінімальна кількість кімнат", "2"), "label_key": "housing.field.label.rooms_min", "question_key": "housing.field.q.rooms_min", "example": "2"},
    {"key": "max_rooms", "label": "Кімнати: максимум (до)", "prompt": _numeric_prompt("Максимальна кількість кімнат", "4"), "label_key": "housing.field.label.rooms_max", "question_key": "housing.field.q.rooms_max", "example": "4"},
    {"key": "min_area_m2", "label": "Площа: мінімум (від)", "prompt": _numeric_prompt("Мінімальна площа, м²", "50"), "label_key": "housing.field.label.area_min", "question_key": "housing.field.q.area_min", "example": "50"},
    {"key": "max_area_m2", "label": "Площа: максимум (до)", "prompt": _numeric_prompt("Максимальна площа, м²", "90"), "label_key": "housing.field.label.area_max", "question_key": "housing.field.q.area_max", "example": "90"},
    {"key": "min_price_eur", "label": "Ціна: мінімум (від)", "prompt": _numeric_prompt("Мінімальна тепла оренда (Warmmiete), €", "700"), "label_key": "housing.field.label.price_min", "question_key": "housing.field.q.warmmiete_min", "example": "700"},
    {"key": "max_price_eur", "label": "Ціна: максимум (до)", "prompt": _numeric_prompt("Максимальна тепла оренда (Warmmiete), €", "1400"), "label_key": "housing.field.label.price_max", "question_key": "housing.field.q.warmmiete_max", "example": "1400"},
]
KARLMARX_CRITERIA_KEYS = [spec["key"] for spec in KARLMARX_CRITERIA_FIELDS]
KARLMARX_CRITERIA_BY_KEY = {spec["key"]: spec for spec in KARLMARX_CRITERIA_FIELDS}

# Портали, серед яких можна обирати одразу при створенні фільтра. Список коротко —
# додавати нове джерело буде такий самий однорядковий запис, а не окремий майстер.
AVAILABLE_SOURCES = [
    {"key": "immowelt", "icon": "🏠", "label": "Immowelt"},
    {"key": "propotsdam", "icon": "🏢", "label": "ProPotsdam"},
    {"key": "semmelhaack", "icon": "🏘", "label": "SEMMELHAACK"},
    {"key": "schoba", "icon": "🏡", "label": "SCHOBA"},
    {"key": "regiomakler", "icon": "🤝", "label": "ImmoTeam/alpha"},
    {"key": "kleinanzeigen", "icon": "📋", "label": "Kleinanzeigen"},
    {"key": "locals", "icon": "🔑", "label": "locals®"},
    {"key": "karlmarx", "icon": "🧱", "label": "Karl Marx"},
]
AVAILABLE_SOURCE_KEYS = [spec["key"] for spec in AVAILABLE_SOURCES]
# Immowelt і ProPotsdam мають райони (Stadtteil); SEMMELHAACK — ні. Спільний крок
# вибору району в майстрі показуємо, лише якщо серед обраних джерел є хоч одне звідси.
DISTRICT_AWARE_SOURCES = {"immowelt", "propotsdam"}

# Район, кімнати й площу той самий фільтр питає лише раз — і Immowelt, і ProPotsdam
# розуміють ці умови однаково. Ціну натомість питає окремо для кожного обраного
# джерела: Immowelt рахує холодну оренду (Kaltmiete), ProPotsdam — повну (Gesamtmiete).
SHARED_CRITERIA_FIELDS = [
    {"key": "min_rooms", "label": "Кімнати: мінімум (від)", "prompt": _numeric_prompt("Мінімальна кількість кімнат", "2"), "label_key": "housing.field.label.rooms_min", "question_key": "housing.field.q.rooms_min", "example": "2"},
    {"key": "max_rooms", "label": "Кімнати: максимум (до)", "prompt": _numeric_prompt("Максимальна кількість кімнат", "4"), "label_key": "housing.field.label.rooms_max", "question_key": "housing.field.q.rooms_max", "example": "4"},
    {"key": "min_area_m2", "label": "Площа: мінімум (від)", "prompt": _numeric_prompt("Мінімальна площа, м²", "50"), "label_key": "housing.field.label.area_min", "question_key": "housing.field.q.area_min", "example": "50"},
    {"key": "max_area_m2", "label": "Площа: максимум (до)", "prompt": _numeric_prompt("Максимальна площа, м²", "90"), "label_key": "housing.field.label.area_max", "question_key": "housing.field.q.area_max", "example": "90"},
]
SHARED_CRITERIA_KEYS = [spec["key"] for spec in SHARED_CRITERIA_FIELDS]
SHARED_CRITERIA_BY_KEY = {spec["key"]: spec for spec in SHARED_CRITERIA_FIELDS}
PRICE_STEP_FIELDS = {
    "min_price_eur": IMMOWELT_CRITERIA_BY_KEY["min_price_eur"],
    "max_price_eur": IMMOWELT_CRITERIA_BY_KEY["max_price_eur"],
    "min_total_rent_eur": PROPOT_CRITERIA_BY_KEY["min_total_rent_eur"],
    "max_total_rent_eur": PROPOT_CRITERIA_BY_KEY["max_total_rent_eur"],
    # Kleinanzeigen дістає власні ключі стану (не "min_price_eur") — його ціна
    # не той самий Kaltmiete, що в Immowelt/SEMMELHAACK/SCHOBA/regiomakler, і
    # об'єднувати їх у спільне запитання було б помилково.
    "min_ka_price_eur": {
        "key": "min_ka_price_eur", "label": "Ціна: мінімум (від)",
        "prompt": _numeric_prompt("Мінімальна ціна, €", "600"),
        "label_key": "housing.field.label.price_min", "question_key": "housing.field.q.price_min", "example": "600",
    },
    "max_ka_price_eur": {
        "key": "max_ka_price_eur", "label": "Ціна: максимум (до)",
        "prompt": _numeric_prompt("Максимальна ціна, €", "1200"),
        "label_key": "housing.field.label.price_max", "question_key": "housing.field.q.price_max", "example": "1200",
    },
    # Karl Marx рахує теплу оренду (Warmmiete), а не Kaltmiete — теж окремі
    # ключі стану, щоб не змішувати з холодною орендою інших джерел.
    "min_km_price_eur": {
        "key": "min_km_price_eur", "label": "Ціна: мінімум (від)",
        "prompt": _numeric_prompt("Мінімальна тепла оренда (Warmmiete), €", "700"),
        "label_key": "housing.field.label.price_min", "question_key": "housing.field.q.warmmiete_min", "example": "700",
    },
    "max_km_price_eur": {
        "key": "max_km_price_eur", "label": "Ціна: максимум (до)",
        "prompt": _numeric_prompt("Максимальна тепла оренда (Warmmiete), €", "1400"),
        "label_key": "housing.field.label.price_max", "question_key": "housing.field.q.warmmiete_max", "example": "1400",
    },
}
PRICE_STEP_PROMPTS = {key: spec["prompt"] for key, spec in PRICE_STEP_FIELDS.items()}
# Одна кнопка на всі майстри: куди саме веде «Назад» вирішує сам обробник,
# дивлячись на `mode`/`step` у стані — окремий callback на кожен крок був би
# зайвим, бо крок завжди один: попереднє поле того самого списку.
BACK_CALLBACK = "housing:field_back"


def _field_keyboard() -> InlineKeyboardMarkup:
    # Просто «⬅ Назад» губилося серед тексту питання — люди не помічали, що
    # можна виправити попередню відповідь, і кидали майстер на середині.
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад — виправити відповідь", callback_data=BACK_CALLBACK)]])


def _reply_field_prompt(message, text: str) -> None:
    """All wizard field prompts go through here — otherwise the bold/code
    formatting in `_numeric_prompt` shows up as literal `<b>` tags instead of
    rendering, since `reply_text` defaults to no parse mode."""
    message.reply_text(text, parse_mode="HTML", reply_markup=_field_keyboard())


def _edit_field_prompt(query, text: str) -> None:
    query.edit_message_text(text, parse_mode="HTML", reply_markup=_field_keyboard())


def _format_answer(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, (int, float)):
        return f"{value:g}"
    return str(value)


def _fields_recap(state: dict, fields: list, exclude_key: Optional[str] = None) -> str:
    """Показує вже введені відповіді над наступним запитанням.

    Люди намагалися виправити помилку, редагуючи своє старе повідомлення в
    Telegram — бот такі редагування не бачить, тож правка тихо нікуди не
    діла. Коли перед очима видно «Мінімум кімнат: 3», людина хоча б розуміє,
    що саме вже зафіксовано, і може виправити це кнопкою «Назад» замість
    марної спроби відредагувати старий текст.
    """
    lines = []
    for spec in fields:
        key = spec["key"]
        if key == exclude_key or key not in state:
            continue
        lines.append(f"✅ {spec.get('label', key)}: {_format_answer(state[key])}")
    return "\n".join(lines)


def _localized_field(spec: dict, lang: str = "uk") -> dict:
    """spec["label"]/["prompt"] are frozen Ukrainian at import time (see
    _numeric_prompt) - kept as-is for uk so every existing call site that
    still reads them directly needs no changes. For ru/de, resolves the
    translated variant per-request from label_key/question_key."""
    if lang == "uk" or "question_key" not in spec:
        return spec
    question = i18n.t(spec["question_key"], lang)
    return {
        **spec,
        "label": i18n.t(spec["label_key"], lang),
        "prompt": i18n.t("housing.field.numeric_prompt", lang, question=question, example=spec["example"]),
    }


def _field_prompt(state: dict, fields: list, next_key: str, lang: str = "uk") -> str:
    resolved = [_localized_field(spec, lang) for spec in fields]
    recap = _fields_recap(state, resolved, exclude_key=next_key)
    prompt = next(spec["prompt"] for spec in resolved if spec["key"] == next_key)
    return f"{recap}\n\n{prompt}" if recap else prompt


KALTMIETE_SOURCES = {"immowelt", "semmelhaack", "schoba", "regiomakler", "locals"}


def _price_steps_for(sources_selected) -> list:
    steps = []
    # Immowelt, SEMMELHAACK і SCHOBA всі рахують холодну оренду (Kaltmiete/
    # Nettokaltmiete) — те саме число підходить для всіх трьох, тож питання
    # одне на всіх, а не окремо для кожного джерела.
    if any(source in KALTMIETE_SOURCES for source in sources_selected):
        steps += ["min_price_eur", "max_price_eur"]
    if "propotsdam" in sources_selected:
        steps += ["min_total_rent_eur", "max_total_rent_eur"]
    if "kleinanzeigen" in sources_selected:
        steps += ["min_ka_price_eur", "max_ka_price_eur"]
    if "karlmarx" in sources_selected:
        steps += ["min_km_price_eur", "max_km_price_eur"]
    return steps


def _canonical_districts(sources_selected) -> list:
    """Список районів для спільного кроку вибору: словник Immowelt, якщо він
    серед обраних джерел (є переклад в ProPotsdam), інакше — власний ProPotsdam."""
    return IMMOWELT_DISTRICTS if "immowelt" in (sources_selected or []) else PROPOT_DISTRICTS


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


def _describe_range(min_val, max_val, *, unit: str, is_int: bool = False, lang: str = "uk") -> Optional[str]:
    if min_val is None and max_val is None:
        return None
    fmt = (lambda v: str(int(round(v)))) if is_int else (lambda v: f"{v:g}")
    if min_val is not None and max_val is not None:
        return f"{fmt(min_val)}–{fmt(max_val)}{unit}"
    if min_val is not None:
        return i18n.t("housing.describe.from", lang, value=f"{fmt(min_val)}{unit}")
    return i18n.t("housing.describe.to", lang, value=f"{fmt(max_val)}{unit}")


def _describe_criteria(criteria: Dict[str, object], lang: str = "uk") -> str:
    districts = criteria.get("districts") or []
    parts = [
        ", ".join(str(item) for item in districts) if districts
        else i18n.t("housing.describe.all_districts", lang)
    ]
    price = _describe_range(
        criteria.get("min_price_eur"), criteria.get("max_price_eur"),
        unit=i18n.t("housing.unit.eur", lang), is_int=True, lang=lang,
    )
    if price:
        parts.append(price)
    rooms = _describe_range(
        criteria.get("min_rooms"), criteria.get("max_rooms"), unit=i18n.t("housing.unit.rooms", lang), lang=lang,
    )
    if rooms:
        parts.append(rooms)
    area = _describe_range(
        criteria.get("min_area_m2"), criteria.get("max_area_m2"), unit=i18n.t("housing.unit.m2", lang), lang=lang,
    )
    if area:
        parts.append(area)
    return html.escape(" · ".join(parts))


def _auto_title(source: str, criteria: Dict[str, object]) -> str:
    """Назва фільтра тепер сама — умови й так видно всюди в списку, тож окреме
    питання «як назвати фільтр» було зайвим кроком майстра."""
    summary = html.unescape(_describe_criteria(criteria))
    label = SOURCE_LABEL.get(source, source)
    return (f"{label}: {summary}" if summary else label)[:120]


def _sync_propot_filters() -> None:
    try:
        _request("POST", "/api/propotsdam/filters", json={"filters": propotsdam_store.list_filters()})
    except Exception:
        logger.exception("Could not sync ProPotsdam filters to shared browser receiver")


# Джерела з власним сховищем у цьому боті — Immowelt керується окремим
# check-Wohnung приймачем без API для «нещодавніх» вибірок, тож пропозиція
# «показати за годину/добу» після створення фільтра охоплює лише ці сім.
_LOCAL_SOURCE_MODULES = {
    "propotsdam": (propotsdam_store, propotsdam_matching),
    "semmelhaack": (semmelhaack_store, semmelhaack_matching),
    "schoba": (schoba_store, schoba_matching),
    "regiomakler": (regiomakler_store, regiomakler_matching),
    "kleinanzeigen": (kleinanzeigen_store, kleinanzeigen_matching),
    "locals": (locals_store, locals_matching),
    "karlmarx": (karlmarx_store, karlmarx_matching),
}
RECENT_WINDOWS = [("останню годину", 1), ("останню добу", 24)]


def _recent_offer_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(f"🕐 Показати за {label}", callback_data=f"housing:recent:{hours}")]
        for label, hours in RECENT_WINDOWS
    ]
    rows.append([InlineKeyboardButton("Не треба", callback_data="housing:recent_skip")])
    return InlineKeyboardMarkup(rows)


def _offer_recent_matches(context: CallbackContext, created) -> None:
    """Stashes just-created (source, filter_id) pairs for the two follow-up
    buttons ('за годину'/'за добу') to search, instead of embedding them in
    callback_data — Telegram's 64-byte limit makes that unsafe once more than
    a couple of sources are created together in the multi-source wizard."""
    context.user_data["recent_offer_filters"] = list(created)


FIRST_FILTER_CONGRATS_TEXT = (
    "🎉 Ви впорались, молодець!\n\n"
    "Тепер усю рутину ми візьмемо на себе. Вам залишилось тільки чекати, "
    "а ми будемо шукати квартири за вашими параметрами і, щойно знайдемо — "
    "одразу пришлемо сюди. Терпіння! 🍀"
)


def _maybe_send_first_filter_congrats(context: CallbackContext, user_id: Optional[int]) -> None:
    """One-time encouragement the first time someone creates a filter.

    Tracked in bot_data rather than by asking "how many filters does this
    person already have" (user_filters(), which goes through
    _all_immowelt_filters()/_request()) - that extra call on every single
    creation would collide with the exact call-count assertions the
    filter-creation tests already make on those same mocks. bot_data
    resets on a container restart, so in theory someone could see this
    message again if they add a new filter shortly after one - a harmless
    repeat for an encouraging one-liner, not worth that collision.
    """
    if not user_id:
        return
    bot_data = getattr(context, "bot_data", None)
    if bot_data is None:
        return
    uid = int(user_id)
    seen = bot_data.setdefault("housing_first_filter_congratulated", set())
    if uid in seen:
        return
    seen.add(uid)
    bot = getattr(context, "bot", None)
    if bot is None:
        return
    try:
        bot.send_message(chat_id=uid, text=FIRST_FILTER_CONGRATS_TEXT)
    except Exception:
        logger.exception("Could not send the first-filter congrats message to user %s", user_id)


def _clear_recent_offer_keyboard(query) -> None:
    try:
        query.edit_message_reply_markup(reply_markup=None)
    except BadRequest as exc:
        if "Message is not modified" not in str(exc):
            raise
    except Exception:
        pass


def _send_recent_matches(update: Update, context: CallbackContext, hours: int) -> None:
    """Searches the just-created filter(s) against listings first seen within
    the chosen window, bypassing the create-time baseline that otherwise
    hides everything already in the catalog at filter-creation time — this is
    an explicit one-off request, not the regular scheduled delivery path."""
    query = update.callback_query
    if not query:
        return
    created = context.user_data.pop("recent_offer_filters", None) or []
    query.answer()
    _clear_recent_offer_keyboard(query)
    if not created:
        return
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    sent_any = False
    for source, filter_id in created:
        modules = _LOCAL_SOURCE_MODULES.get(source)
        if not modules:
            continue
        store, matching = modules
        filt = next(
            (f for f in store.list_filters() if int(f["filter_id"]) == int(filter_id)), None,
        )
        if not filt:
            continue
        owner_id = int(filt["user_id"])
        for listing in store.list_active_listings_since(cutoff):
            if not matching.matches_filter(listing, filt):
                continue
            text = matching.format_notification(listing)
            context.bot.send_message(
                chat_id=owner_id, text=text, parse_mode="HTML", disable_web_page_preview=False,
            )
            store.mark_delivered(int(filter_id), str(listing["listing_key"]))
            sent_any = True
    if not sent_any and update.effective_user:
        context.bot.send_message(
            chat_id=int(update.effective_user.id), text="За цей період підходящих оголошень немає.",
        )


def _current_matches(source: str, filt: Dict[str, object]) -> list:
    """Listings that satisfy this filter's criteria right now - a live
    check, not a delivery. Used by show_current_matches() so a person can
    see for themselves whether their filter is actually finding anything,
    instead of guessing from the crawl-freshness traffic lights."""
    if source == "immowelt":
        criteria = {"districts": list(filt.get("districts") or [])}
        for key in IMMOWELT_CRITERIA_KEYS:
            criteria[key] = filt.get(key)
        preview = _preview_criteria(criteria)
        return list(preview.get("matches") or [])
    modules = _LOCAL_SOURCE_MODULES.get(source)
    if not modules:
        return []
    store, matching = modules
    try:
        listings = store.list_active_listings()
    except Exception:
        logger.exception("Could not load active listings for %s while checking current matches", source)
        return []
    return [listing for listing in listings if matching.matches_filter(listing, filt)]


def _match_line(source: str, listing: Dict[str, object]) -> str:
    title = html.escape(str(listing.get("title") or "Wohnung"))
    bits = []
    district = listing.get("district")
    if district:
        bits.append(str(district))
    rooms = listing.get("rooms")
    if rooms:
        bits.append(f"{rooms:g} кімн.")
    area = listing.get("area_m2")
    if area:
        bits.append(f"{area:g} м²")
    price = listing.get("price_eur")
    if price is None:
        price = listing.get("total_rent_eur")
    if price:
        bits.append(f"{price:g} €")
    suffix = f" · {' · '.join(bits)}" if bits else ""
    url = listing.get("url") or listing.get("detail_url")
    if not url and source == "propotsdam":
        url = propotsdam_parser.PORTAL_URL
    link = f' — <a href="{html.escape(str(url))}">Відкрити</a>' if url else ""
    return f"• {title}{suffix}{link}"


MAX_MATCHES_SHOWN_PER_FILTER = 15
COOP_SOURCE_KEYS = [coop["key"] for coop in coop_watchdog.COOPERATIVES]
# Explicit, not derived from the key - "wbg1903" and "wbg_daheim" would both
# collide on "W" if this were auto-generated from the first letter.
COOP_PREFIXES = {"gewoba": "G", "wbg1903": "W", "wbg_daheim": "D"}
ALL_HOUSING_SOURCES = [
    "immowelt", "propotsdam", "semmelhaack", "schoba",
    "regiomakler", "kleinanzeigen", "locals", "karlmarx",
    *COOP_SOURCE_KEYS,
]


def show_current_matches(update: Update, context: CallbackContext) -> None:
    """«🔍 Квартири, що підходять» — an on-demand, real check across every
    active filter, instead of the freshness traffic lights people kept
    misreading as "there's an apartment for you"."""
    query = update.callback_query
    user = update.effective_user
    if not user or not is_allowed(user.id):
        if query:
            query.answer()
        return
    if query:
        query.answer()
    chat_id = int(user.id)
    back_keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("⬅ До моніторингу", callback_data="housing:menu")]])
    filters = user_filters(user.id)
    if not filters:
        context.bot.send_message(
            chat_id=chat_id,
            text="У вас поки немає жодного фільтра. Спочатку додайте його кнопкою «➕ Додати фільтр».",
            reply_markup=back_keyboard,
        )
        return

    total = 0
    blocks = []
    for item in filters:
        source = item.get("source") or "immowelt"
        matches = _current_matches(source, item)
        total += len(matches)
        icon = SOURCE_ICON.get(source, "🏠")
        label = SOURCE_LABEL.get(source, source)
        filter_id = item.get("filter_id")
        lines = [f"{icon} <b>{html.escape(str(label))} #{filter_id}</b>: {html.escape(str(item.get('title') or ''))}"]
        if not matches:
            lines.append("Поки нічого не підходить під ці умови.")
        else:
            for listing in matches[:MAX_MATCHES_SHOWN_PER_FILTER]:
                lines.append(_match_line(source, listing))
            if len(matches) > MAX_MATCHES_SHOWN_PER_FILTER:
                lines.append(f"…і ще {len(matches) - MAX_MATCHES_SHOWN_PER_FILTER}.")
        blocks.append("\n".join(lines))

    context.bot.send_message(
        chat_id=chat_id, text=f"🔍 <b>Квартири, що підходять зараз: {total}</b>", parse_mode="HTML",
    )
    for block in blocks:
        context.bot.send_message(chat_id=chat_id, text=block, parse_mode="HTML", disable_web_page_preview=True)

    # People kept reading "some sources listed, others silent" as a bug -
    # it was just that they'd never filtered those sources at all. Say so
    # explicitly instead of letting the report look selective/incomplete.
    covered = {item.get("source") or "immowelt" for item in filters}
    missing = [s for s in ALL_HOUSING_SOURCES if s not in covered]
    footer_lines = ["Якщо з'явиться нова квартира під ваші умови — ми одразу напишемо вам сюди."]
    if missing:
        missing_labels = ", ".join(SOURCE_LABEL.get(s, s) for s in missing)
        footer_lines.append(f"\nФільтра ще немає на: {missing_labels}. Додати можна кнопкою «➕ Додати фільтр».")
    context.bot.send_message(
        chat_id=chat_id, text="\n".join(footer_lines), reply_markup=back_keyboard,
    )


def user_filters(user_id: Optional[int]) -> list:
    if not user_id:
        return []
    immowelt = [
        item
        for item in _all_immowelt_filters()
        if int(item.get("user_id") or 0) == int(user_id) and item.get("active")
    ]
    propot = propotsdam_store.list_filters(user_id=int(user_id), active_only=True)
    for item in propot:
        item.setdefault("source", "propotsdam")
    semm = semmelhaack_store.list_filters(user_id=int(user_id), active_only=True)
    for item in semm:
        item.setdefault("source", "semmelhaack")
    schoba = schoba_store.list_filters(user_id=int(user_id), active_only=True)
    for item in schoba:
        item.setdefault("source", "schoba")
    regio = regiomakler_store.list_filters(user_id=int(user_id), active_only=True)
    for item in regio:
        item.setdefault("source", "regiomakler")
    kanz = kleinanzeigen_store.list_filters(user_id=int(user_id), active_only=True)
    for item in kanz:
        item.setdefault("source", "kleinanzeigen")
    loc = locals_store.list_filters(user_id=int(user_id), active_only=True)
    for item in loc:
        item.setdefault("source", "locals")
    km = karlmarx_store.list_filters(user_id=int(user_id), active_only=True)
    for item in km:
        item.setdefault("source", "karlmarx")
    coops = coop_watchdog_store.list_filters(user_id=int(user_id), active_only=True)
    for item in coops:
        item["source"] = item["coop_key"]
    return immowelt + propot + semm + schoba + regio + kanz + loc + km + coops


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
    semm = semmelhaack_store.list_filters(user_id=int(user_id))
    for item in semm:
        item.setdefault("source", "semmelhaack")
    schoba = schoba_store.list_filters(user_id=int(user_id))
    for item in schoba:
        item.setdefault("source", "schoba")
    regio = regiomakler_store.list_filters(user_id=int(user_id))
    for item in regio:
        item.setdefault("source", "regiomakler")
    kanz = kleinanzeigen_store.list_filters(user_id=int(user_id))
    for item in kanz:
        item.setdefault("source", "kleinanzeigen")
    loc = locals_store.list_filters(user_id=int(user_id))
    for item in loc:
        item.setdefault("source", "locals")
    km = karlmarx_store.list_filters(user_id=int(user_id))
    for item in km:
        item.setdefault("source", "karlmarx")
    return immowelt + propot + semm + schoba + regio + kanz + loc + km


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


def _menu_keyboard(user_id: Optional[int] = None, lang: str = "uk") -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(i18n.t("housing.btn.refresh_status", lang), callback_data="housing:menu")],
        [InlineKeyboardButton(i18n.t("housing.btn.faq", lang), callback_data="housing:faq")],
    ]
    if user_id and int(user_id) == ADMIN_ID:
        rows.insert(0, [InlineKeyboardButton(i18n.t("housing.btn.admin", lang), callback_data="housing:admin")])
        rows.insert(1, [InlineKeyboardButton(i18n.t("housing.btn.current_matches", lang), callback_data="housing:current_matches")])
        rows.insert(2, [InlineKeyboardButton(i18n.t("housing.btn.coops", lang), callback_data="housing:coops")])
        rows.insert(3, [InlineKeyboardButton(i18n.t("housing.btn.notify_settings", lang), callback_data="housing:notify_settings")])
    elif is_allowed(user_id):
        rows.insert(0, [InlineKeyboardButton(i18n.t("housing.btn.self_add", lang), callback_data="housing:self_add")])
        rows.insert(1, [InlineKeyboardButton(i18n.t("housing.btn.self_manage", lang), callback_data="housing:self_manage")])
        rows.insert(2, [InlineKeyboardButton(i18n.t("housing.btn.current_matches", lang), callback_data="housing:current_matches")])
        rows.insert(3, [InlineKeyboardButton(i18n.t("housing.btn.coops", lang), callback_data="housing:coops")])
        rows.insert(4, [InlineKeyboardButton(i18n.t("housing.btn.notify_settings", lang), callback_data="housing:notify_settings")])
    rows.append([InlineKeyboardButton("🌐 Мова / Язык / Sprache", callback_data="housing:lang:menu")])
    rows.append([InlineKeyboardButton(i18n.t("housing.btn.back_home", lang), callback_data="anon:home")])
    return InlineKeyboardMarkup(rows)


def _lang_picker_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(i18n.LANG_LABELS[code], callback_data=f"housing:lang:set:{code}")]
        for code in i18n.SUPPORTED_LANGS
    ]
    rows.append([InlineKeyboardButton("⬅ Назад / Назад / Zurück", callback_data="housing:menu")])
    return InlineKeyboardMarkup(rows)


def _coop_subscription_state(user_id: int) -> Dict[str, bool]:
    subs = coop_watchdog_store.list_filters(user_id=int(user_id))
    return {row["coop_key"]: bool(row["active"]) for row in subs}


def _coops_text(user_id: int, lang: str = "uk") -> str:
    state = _coop_subscription_state(user_id)
    lines = [
        i18n.t("housing.coops.title", lang),
        i18n.t("housing.coops.explain", lang),
    ]
    for coop in coop_watchdog.COOPERATIVES:
        on = state.get(coop["key"], False)
        mark = i18n.t("housing.coops.on", lang) if on else i18n.t("housing.coops.off", lang)
        lines.append(f"{mark} — {html.escape(coop['label'])}")
    return "\n".join(lines)


def _coops_keyboard(user_id: int, lang: str = "uk") -> InlineKeyboardMarkup:
    state = _coop_subscription_state(user_id)
    rows = []
    for coop in coop_watchdog.COOPERATIVES:
        on = state.get(coop["key"], False)
        mark = "✅" if on else "⬜"
        rows.append([InlineKeyboardButton(
            f"{mark} {coop['label']}", callback_data=f"housing:coop_toggle:{coop['key']}",
        )])
    rows.append([InlineKeyboardButton(i18n.t("housing.btn.back_to_monitor", lang), callback_data="housing:menu")])
    return InlineKeyboardMarkup(rows)


def show_coop_subscriptions(update: Update, context: CallbackContext, edit: bool = False) -> None:
    user = update.effective_user
    if not user or not is_allowed(user.id):
        if update.callback_query:
            update.callback_query.answer()
        return
    lang = i18n.get_lang(user.id)
    text = _coops_text(user.id, lang)
    keyboard = _coops_keyboard(user.id, lang)
    if edit and update.callback_query:
        try:
            update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)
        except BadRequest as exc:
            if "Message is not modified" not in str(exc):
                raise
    else:
        update.effective_message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)


def _toggle_coop_subscription(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    user = update.effective_user
    if not query or not user or not is_allowed(user.id):
        if query:
            query.answer()
        return
    coop_key = query.data.split(":", 2)[2]
    coop = next((c for c in coop_watchdog.COOPERATIVES if c["key"] == coop_key), None)
    if coop is None:
        query.answer("Невідомий кооператив.", show_alert=True)
        return
    currently_on = _coop_subscription_state(user.id).get(coop_key, False)
    if currently_on:
        coop_watchdog_store.set_filter_active(int(user.id), coop_key, False)
        query.answer(f"Вимкнено: {coop['label']}")
    else:
        coop_watchdog_store.create_filter(int(user.id), coop_key, coop["label"])
        query.answer(f"Стежимо за: {coop['label']}")
    show_coop_subscriptions(update, context, edit=True)


FAQ_TEXT = (
    "❓ <b>Довідка та часті питання</b>\n\n"
    "<b>Що робить цей розділ?</b>\n"
    "Стежить одразу за 8 порталами оренди житла в Потсдамі (Immowelt, ProPotsdam, "
    "SEMMELHAACK, SCHOBA, ImmoTeam/alpha, Kleinanzeigen, locals®, Karl Marx) і надсилає "
    "вам повідомлення, щойно з'являється квартира під ваші умови — самому нічого "
    "перевіряти не треба.\n\n"
    "<b>Як працює фільтр?</b>\n"
    "Ви задаєте кімнати, площу і ціну (де можна — ще й район). Бот шукає за цими "
    "умовами на всіх обраних порталах одночасно. Можна створити кілька фільтрів з "
    "різними умовами — наприклад, один вужчий і один запасний, ширший.\n\n"
    "<b>Чому іноді питає ціну двічі?</b>\n"
    "Портали рахують орендну плату по-різному: більшість показує <b>Kaltmiete</b> "
    "(холодну оренду, без комунальних) — це одне спільне питання. ProPotsdam показує "
    "повну <b>Gesamtmiete</b>, Karl Marx — <b>Warmmiete</b> (з комунальними), а на "
    "Kleinanzeigen ціна взагалі без чіткої мітки. Це різні числа за суттю, тож питання "
    "теж окремі — так фільтр рахує правильно для кожного порталу.\n\n"
    "<b>Що таке кнопки «за останню годину/добу» після створення фільтра?</b>\n"
    "Одразу після створення фільтра бот запам'ятовує вже наявні квартири мовчки, щоб "
    "не завалити вас усім архівом одразу. Якщо хочете все ж побачити, що зʼявилось "
    "нещодавно — саме для цього ці дві кнопки.\n\n"
    "<b>Як змінити або вимкнути фільтр?</b>\n"
    f"«{BTN_SELF_MANAGE}» у головному меню — там можна поставити фільтр на паузу, "
    "відредагувати умови або видалити зовсім.\n\n"
    "<b>Скільки це коштує і як отримати доступ?</b>\n"
    "10 €/місяць, відкриває адміністратор особисто після запиту через кнопку "
    "«📩 Запросити доступ».\n\n"
    "<b>Щось не працює або є питання?</b>\n"
    "Напишіть про це прямо адміністратору."
)


def show_faq(update: Update, context: CallbackContext, edit: bool = False) -> None:
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("⬅ До моніторингу", callback_data="housing:menu")]])
    if edit and update.callback_query:
        try:
            update.callback_query.edit_message_text(FAQ_TEXT, parse_mode="HTML", reply_markup=keyboard)
        except BadRequest as exc:
            if "Message is not modified" not in str(exc):
                raise
    else:
        update.effective_message.reply_text(FAQ_TEXT, parse_mode="HTML", reply_markup=keyboard)


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


def _relative_time(value, lang: str = "uk") -> str:
    """Час словами замість дати-часу, яку людина йде перевіряти вручну.

    Технічна відмітка «13.08.2026 02:25» вимагає рахувати самому, скільки це
    було тому. «3 хв тому» відповідає на реальне питання одразу.
    """
    checked_at = _as_berlin_datetime(value)
    if checked_at is None:
        return i18n.t("housing.time.never", lang)
    now = _now_berlin()
    seconds = max(0.0, (now - checked_at).total_seconds())
    if seconds < 60:
        return i18n.t("housing.time.just_now", lang)
    minutes = int(seconds // 60)
    if minutes < 60:
        return i18n.t("housing.time.minutes_ago", lang, n=minutes)
    hours = int(minutes // 60)
    if hours < 24:
        return i18n.t("housing.time.hours_ago", lang, n=hours)
    if checked_at.date() == (now - timedelta(days=1)).date():
        return i18n.t("housing.time.yesterday_at", lang, time=checked_at.strftime('%H:%M'))
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


def _immowelt_status_lines(lang: str = "uk") -> list:
    """Рядки про стан обходу Immowelt.

    Час беремо з `/api/status`: обхід один на всіх, тому власної відмітки у
    фільтрів немає. Поки приймач її не віддає, відкочуємось на
    `last_checked_at` самих фільтрів, інакше панель мовчала б про перевірку.
    """
    filters = [item for item in _all_immowelt_filters() if item.get("active")]
    if not filters:
        return [i18n.t("housing.status.immowelt_no_filters", lang)]

    status = _receiver_status()
    checked_at = str(status.get("immowelt_last_check_at") or "")
    if not checked_at:
        checked_at = max((str(item.get("last_checked_at") or "") for item in filters), default="")
    seen_total = sum(int(item.get("seen_count") or 0) for item in filters)
    error = str(status.get("immowelt_last_error") or "")
    skip_reason = str(status.get("immowelt_last_skip_reason") or "")

    light = _traffic_light(checked_at, IMMOWELT_STALE_AFTER)
    if not checked_at:
        lines = [i18n.t("housing.status.never_checked", lang, name="Immowelt")]
    else:
        lines = [i18n.t(
            "housing.status.immowelt_checking", lang,
            light=light, relative=_relative_time(checked_at, lang), count=seen_total,
        )]
    # Мовчазна поломка виглядала як звичайна перевірка без новин, тож причину
    # показуємо окремим рядком, а не ховаємо за старою відміткою часу.
    if error:
        lines.append(i18n.t("housing.status.error", lang, name="Immowelt", error=html.escape(error)))
    elif skip_reason:
        lines.append(i18n.t("housing.status.immowelt_skipped", lang, reason=html.escape(skip_reason)))
    return lines


# (store, display name, staleness threshold, count-phrase key) - SEMMELHAACK is
# the one source that phrases its count as "у Потсдамі" instead of plain
# "квартир", everything else here is byte-for-byte identical in shape.
_GENERIC_STATUS_SOURCES = [
    (semmelhaack_store, "SEMMELHAACK", PROPOTSDAM_STALE_AFTER, "housing.status.checking_potsdam_count"),
    (schoba_store, "SCHOBA", PROPOTSDAM_STALE_AFTER, "housing.status.checking"),
    (regiomakler_store, "ImmoTeam/alpha", PROPOTSDAM_STALE_AFTER, "housing.status.checking"),
    (kleinanzeigen_store, "Kleinanzeigen", KLEINANZEIGEN_STALE_AFTER, "housing.status.checking"),
    (locals_store, "locals®", PROPOTSDAM_STALE_AFTER, "housing.status.checking"),
    (karlmarx_store, "Karl Marx", PROPOTSDAM_STALE_AFTER, "housing.status.checking"),
]


def _status_lines(lang: str = "uk") -> list:
    lines = []
    try:
        lines.extend(_immowelt_status_lines(lang))
    except Exception:
        logger.exception("Could not load Immowelt status")

    status = propotsdam_store.latest_status()
    if status:
        light = _traffic_light(status.get("last_checked_at"), PROPOTSDAM_STALE_AFTER)
        label = status.get("last_status") or "unknown"
        count = status.get("listings_count") or 0
        lines.append(i18n.t(
            "housing.status.propotsdam_checking", lang,
            light=light, relative=_relative_time(status.get("last_checked_at"), lang),
            status=html.escape(str(label)), count=count,
        ))
        if status.get("last_error"):
            lines.append(i18n.t("housing.status.error", lang, name="ProPotsdam", error=html.escape(str(status.get("last_error")))))
    else:
        lines.append(i18n.t("housing.status.never_checked", lang, name="ProPotsdam"))

    for store, name, stale_after, count_key in _GENERIC_STATUS_SOURCES:
        source_status = store.latest_status()
        if source_status:
            light = _traffic_light(source_status.get("last_checked_at"), stale_after)
            count = source_status.get("listings_count") or 0
            lines.append(i18n.t(
                count_key, lang, light=light, name=name,
                relative=_relative_time(source_status.get("last_checked_at"), lang), count=count,
            ))
            if source_status.get("last_error"):
                lines.append(i18n.t(
                    "housing.status.error", lang, name=name,
                    error=html.escape(str(source_status.get("last_error"))),
                ))
        else:
            lines.append(i18n.t("housing.status.never_checked", lang, name=name))

    # Cooperatives have no per-listing scrape yet (see CoopWatchdogFilter's
    # docstring) - the light here reflects crawl freshness same as the rest,
    # but "state" is just empty/not-empty, never a listing count.
    for coop in coop_watchdog.COOPERATIVES:
        coop_status = coop_watchdog_store.get_status(coop["key"])
        label = html.escape(coop["label"])
        if coop_status:
            light = _traffic_light(coop_status.get("last_checked_at"), COOP_STALE_AFTER)
            was_empty = coop_status.get("was_empty")
            if was_empty is False:
                state = i18n.t("housing.status.coop.available", lang)
            elif was_empty is True:
                state = i18n.t("housing.status.coop.empty", lang)
            else:
                state = i18n.t("housing.status.coop.first_check", lang)
            lines.append(i18n.t(
                "housing.status.coop_checking", lang, light=light, name=label,
                relative=_relative_time(coop_status.get("last_checked_at"), lang), state=state,
            ))
            if coop_status.get("last_error"):
                lines.append(i18n.t(
                    "housing.status.error", lang, name=label,
                    error=html.escape(str(coop_status.get("last_error"))),
                ))
        else:
            lines.append(i18n.t("housing.status.never_checked", lang, name=label))
    return lines


def _render_menu(user_id: int, lang: str = "uk") -> str:
    filters = user_filters(user_id)
    lines = [
        i18n.t("housing.menu.title", lang),
        "",
        i18n.t("housing.menu.intro", lang),
        "",
        i18n.t("housing.menu.coops_intro", lang, btn=i18n.t("housing.btn.coops", lang)),
        "",
        i18n.t("housing.menu.status_header", lang),
        *_status_lines(lang),
        "",
    ]
    if not filters:
        lines.append(i18n.t("housing.menu.no_filters", lang))
    else:
        lines.append(i18n.t("housing.menu.your_filters", lang))
        prefixes = {
            "immowelt": "", "propotsdam": "P", "semmelhaack": "S", "schoba": "C",
            "regiomakler": "R", "kleinanzeigen": "K", "locals": "L", "karlmarx": "M",
            **COOP_PREFIXES,
        }
        for item in filters:
            prefix = prefixes.get(_item_source(item), "")
            title = html.escape(str(item.get('title') or i18n.t("housing.filter.default_title", lang)))
            lines.append(f"• #{prefix}{int(item.get('filter_id'))}: {title}")
    return "\n".join(lines)


def _locked_keyboard(lang: str = "uk") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(i18n.t("housing.btn.request_access", lang), callback_data="housing:access_request")],
        [InlineKeyboardButton(i18n.t("housing.btn.faq", lang), callback_data="housing:faq")],
        [InlineKeyboardButton(i18n.t("housing.btn.back_home", lang), callback_data="anon:home")],
    ])


def show_menu(update: Update, context: CallbackContext, edit: bool = False) -> None:
    user = update.effective_user
    if not user or not is_allowed(user.id):
        # Доступ видавався лише тим, що адмін вручну вбивав Telegram ID, і людині
        # не було чим про нього попросити.
        lang = i18n.get_lang(user.id) if user else "uk"
        text = i18n.t("housing.locked.text", lang)
        if edit and update.callback_query:
            update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=_locked_keyboard(lang))
        else:
            update.effective_message.reply_text(text, parse_mode="HTML", reply_markup=_locked_keyboard(lang))
        return
    lang = i18n.get_lang(user.id)
    text = _render_menu(user.id, lang)
    if edit and update.callback_query:
        try:
            update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=_menu_keyboard(user.id, lang))
        except BadRequest as exc:
            if "Message is not modified" not in str(exc):
                raise
    else:
        update.effective_message.reply_text(text, parse_mode="HTML", reply_markup=_menu_keyboard(user.id, lang))


def _admin_rows() -> list:
    """Плаский перелік фільтрів (один запис на фільтр), готовий для групування за user_id."""
    # Раніше сюди йшов `_tasks()`, а зведене завдання браузера не має
    # `filter_id`: `int(None)` валив колбек, і адмінка просто не відкривалась.
    rows = []
    for item in _all_immowelt_filters():
        filter_id = _filter_id(item)
        label = f"#{filter_id}" if filter_id is not None else "#?"
        title = html.escape(str(item.get("title") or "Пошук житла"))
        if not item.get("active"):
            title += " · вимкнено"
        rows.append({"user_id": int(item.get("user_id") or 0), "label": label, "title": title})
    for item in propotsdam_store.list_filters():
        rows.append({
            "user_id": int(item.get("user_id")), "label": f"P#{int(item.get('filter_id'))}",
            "title": html.escape(str(item.get("title") or "ProPotsdam")),
        })
    for item in semmelhaack_store.list_filters():
        rows.append({
            "user_id": int(item.get("user_id")), "label": f"S#{int(item.get('filter_id'))}",
            "title": html.escape(str(item.get("title") or "SEMMELHAACK")),
        })
    for item in schoba_store.list_filters():
        rows.append({
            "user_id": int(item.get("user_id")), "label": f"C#{int(item.get('filter_id'))}",
            "title": html.escape(str(item.get("title") or "SCHOBA")),
        })
    for item in regiomakler_store.list_filters():
        rows.append({
            "user_id": int(item.get("user_id")), "label": f"R#{int(item.get('filter_id'))}",
            "title": html.escape(str(item.get("title") or "ImmoTeam/alpha")),
        })
    for item in kleinanzeigen_store.list_filters():
        rows.append({
            "user_id": int(item.get("user_id")), "label": f"K#{int(item.get('filter_id'))}",
            "title": html.escape(str(item.get("title") or "Kleinanzeigen")),
        })
    for item in locals_store.list_filters():
        rows.append({
            "user_id": int(item.get("user_id")), "label": f"L#{int(item.get('filter_id'))}",
            "title": html.escape(str(item.get("title") or "locals®")),
        })
    for item in karlmarx_store.list_filters():
        rows.append({
            "user_id": int(item.get("user_id")), "label": f"M#{int(item.get('filter_id'))}",
            "title": html.escape(str(item.get("title") or "Karl Marx")),
        })
    for item in coop_watchdog_store.list_filters():
        prefix = COOP_PREFIXES.get(item["coop_key"], "?")
        title = html.escape(str(item.get("title") or item["coop_key"]))
        if not item.get("active"):
            title += " · призупинено"
        rows.append({
            "user_id": int(item.get("user_id")), "label": f"{prefix}#{int(item.get('filter_id'))}",
            "title": title,
        })
    return rows


def _group_admin_rows(rows: list) -> list:
    """Групує плаский перелік фільтрів за user_id, зберігаючи порядок першої появи."""
    groups: Dict[int, list] = {}
    order: list = []
    for row in rows:
        uid = row["user_id"]
        if uid not in groups:
            groups[uid] = []
            order.append(uid)
        groups[uid].append(row)
    return [(uid, groups[uid]) for uid in order]


def _paginate_admin_groups(groups: list, page_size: int = ADMIN_PAGE_SIZE) -> list:
    """Пакує групи по сторінках, ніколи не розриваючи фільтри одного користувача навпіл."""
    pages: list = []
    current: list = []
    current_size = 0
    for uid, items in groups:
        if current and current_size + len(items) > page_size:
            pages.append(current)
            current = []
            current_size = 0
        current.append((uid, items))
        current_size += len(items)
    if current:
        pages.append(current)
    return pages or [[]]


def _admin_group_header(user_id: int, names: Dict[int, str]) -> str:
    name = names.get(user_id)
    if user_id == ADMIN_ID:
        label = f"{user_id} · адмін"
    elif name:
        label = f"{user_id} · {html.escape(name)}"
    else:
        label = str(user_id)
    return f"👤 <b>{label}</b>"


def _render_admin(page: int = 0) -> str:
    """Показує сторінку переліку фільтрів, згрупованих за користувачем.

    Раніше перелік друкувався одним плоским списком без прив'язки фільтрів
    один до одного — коли в людини їх набиралось вісім упереміш із чужими,
    розібратись, що кому належить, було майже неможливо. Групування за
    user_id це вирішує; пагінація й далі потрібна — Telegram усе одно ріже
    повідомлення на 4096 символах.
    """
    rows = _admin_rows()
    lines = ["⚙️ <b>Адмінка житла</b>", ""]
    if not rows:
        lines.append("Фільтрів поки немає.")
        return "\n".join(lines)
    groups = _group_admin_rows(rows)
    pages = _paginate_admin_groups(groups)
    total_pages = max(1, len(pages))
    page = max(0, min(int(page), total_pages - 1))
    names = {
        int(u["user_id"]): str(u["display_name"])
        for u in housing_access_store.list_users()
        if u.get("display_name")
    }
    lines.append(
        f"Усього фільтрів: {len(rows)} · користувачів: {len(groups)} · "
        f"сторінка {page + 1} з {total_pages}"
    )
    lines.append("")
    for uid, items in pages[page]:
        lines.append(f"{_admin_group_header(uid, names)} · фільтрів: {len(items)}")
        for item in items:
            lines.append(f"   • {item['label']} · {item['title']}")
        lines.append("")
    return "\n".join(lines).rstrip()


def _admin_page_count() -> int:
    groups = _group_admin_rows(_admin_rows())
    return max(1, len(_paginate_admin_groups(groups)))


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


def _add_months(dt: datetime, months: int) -> datetime:
    """Calendar-accurate `dt + N months` (no external dependency needed).

    Clamps to the last day of the target month, so granting on Jan 31 for
    one month lands on Feb 28/29 instead of raising on an invalid date.
    """
    month_index = dt.month - 1 + months
    year = dt.year + month_index // 12
    month = month_index % 12 + 1
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


def _access_months_keyboard(target_id: int) -> InlineKeyboardMarkup:
    row = [
        InlineKeyboardButton(f"{months} міс.", callback_data=f"housing:access_months:{target_id}:{months}")
        for months in ACCESS_MONTH_OPTIONS
    ]
    return InlineKeyboardMarkup([row])


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
    # Pending state lives in bot_data (keyed by user id), not user_data:
    # the admin's grant/deny click runs in the ADMIN's own context, which
    # can't reach into the requester's separate user_data to clear it there.
    # That mismatch was why a denied user stayed stuck until the container
    # restarted and wiped the in-memory user_data store.
    if context.bot_data.get("housing_access_pending", {}).get(int(user.id)):
        query.answer("Запит вже надіслано, зачекайте на відповідь.", show_alert=True)
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
                InlineKeyboardButton("💌 Написати в ЛС", url=f"tg://user?id={int(user.id)}"),
            ]]),
        )
    except Exception:
        logger.exception("Could not deliver housing access request to the admin")
        query.answer("Не вдалося надіслати запит. Спробуйте пізніше.", show_alert=True)
        return
    context.bot_data.setdefault("housing_access_pending", {})[int(user.id)] = True
    context.bot_data.setdefault("housing_access_names", {})[int(user.id)] = name
    query.answer("Запит надіслано.")
    query.edit_message_text(
        "📩 Запит надіслано адміністратору.\n\nМи повідомимо, щойно доступ відкриють."
    )


def _notify_user_access_granted(bot, user_id: int, expires_at: Optional[datetime] = None) -> None:
    until = f" до {expires_at.strftime('%d.%m.%Y')}" if expires_at else ""
    try:
        bot.send_message(
            chat_id=user_id,
            text=(
                f"✅ Доступ до моніторингу житла відкрито{until}. Натисніть «🏠 Моніторинг житла», "
                "щоб додати фільтр."
            ),
        )
    except Exception:
        # Людина могла заблокувати бота — рішення адміна від цього не залежить.
        logger.exception("Could not notify user %s about granted housing access", user_id)


def _notify_user_access_revoked(bot, user_id: int) -> None:
    try:
        bot.send_message(
            chat_id=user_id,
            text=(
                "⛔ Доступ до моніторингу житла закінчився. Якщо він знадобиться знову — "
                "натисніть «🏠 Моніторинг житла» і запросіть доступ ще раз."
            ),
        )
    except Exception:
        logger.exception("Could not notify user %s about revoked housing access", user_id)


GOODBYE_TEXT = (
    "Дякуємо, що були з нами! 🙏\n\n"
    "У майбутньому завжди можете написати @artemtotal, якщо моніторинг житла "
    "знадобиться знову. Сподіваємось, ви вже знайшли житло. 🏡"
)


def _close_access(bot, user_id: int, notify_admin: bool = True) -> None:
    """Revokes access and deletes the person's filters (see
    `_delete_all_filters_for_user` for why deletion, not just deactivation,
    is required to actually stop notifications), then says goodbye."""
    housing_access_store.revoke_access(user_id)
    removed = _delete_all_filters_for_user(user_id)
    try:
        bot.send_message(chat_id=user_id, text=GOODBYE_TEXT)
    except Exception:
        logger.exception("Could not send the goodbye message to user %s", user_id)
    if notify_admin and ADMIN_ID:
        try:
            bot.send_message(
                chat_id=ADMIN_ID,
                text=f"⛔ Доступ користувача {user_id} закрито (фільтрів прибрано: {removed}).",
            )
        except Exception:
            logger.exception("Could not notify admin about closing access for user %s", user_id)


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
    if grant:
        # Duration is picked on the next step (see _finalize_access_grant) -
        # the request stays "pending" in bot_data until then.
        query.answer()
        name = str(context.bot_data.get("housing_access_names", {}).get(target_id, ""))
        query.edit_message_text(
            f"На скільки місяців відкрити доступ?\n\nКористувач: {html.escape(name or str(target_id))}\n"
            f"Telegram ID: <code>{target_id}</code>",
            parse_mode="HTML",
            reply_markup=_access_months_keyboard(target_id),
        )
        return
    # Clear the pending flag for THIS user so they can submit a new request
    # after this decision (see the comment in request_access for why this
    # has to be bot_data, keyed by user id, rather than user_data).
    context.bot_data.get("housing_access_pending", {}).pop(target_id, None)
    name = str(context.bot_data.get("housing_access_names", {}).pop(target_id, ""))
    query.answer("✖ У доступі відмовлено")
    query.edit_message_text(
        f"✖ У доступі відмовлено\n\nКористувач: {html.escape(name or str(target_id))}\n"
        f"Telegram ID: <code>{target_id}</code>",
        parse_mode="HTML",
    )
    try:
        context.bot.send_message(
            chat_id=target_id,
            text="На жаль, доступ до моніторингу житла зараз не відкрито.",
        )
    except Exception:
        logger.exception("Could not notify user %s about the housing access denial", target_id)


def _finalize_access_grant(update: Update, context: CallbackContext) -> None:
    """Handles the `housing:access_months:{user_id}:{months}` tap from either
    a fresh request (_resolve_access_request) or a manual/renewal grant
    (start_access_add_flow's name step, or the renew shortcut)."""
    query = update.callback_query
    user = update.effective_user
    if not query or not user or int(user.id) != ADMIN_ID:
        return
    try:
        parts = query.data.split(":")
        target_id = int(parts[2])
        months = int(parts[3])
    except (IndexError, ValueError):
        query.answer("Некоректна команда.", show_alert=True)
        return
    context.bot_data.get("housing_access_pending", {}).pop(target_id, None)
    name = str(context.bot_data.get("housing_access_names", {}).pop(target_id, ""))
    expires_at = _add_months(datetime.utcnow(), months)
    housing_access_store.grant_access(target_id, name, expires_at=expires_at)
    expires_str = expires_at.strftime("%d.%m.%Y")
    query.answer("✅ Доступ надано")
    query.edit_message_text(
        f"✅ Доступ надано на {months} міс. (до {expires_str})\n\n"
        f"Користувач: {html.escape(name or str(target_id))}\n"
        f"Telegram ID: <code>{target_id}</code>",
        parse_mode="HTML",
    )
    _notify_user_access_granted(context.bot, target_id, expires_at)


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


def _access_users_keyboard() -> InlineKeyboardMarkup:
    """Кнопка 🗑 на кожного користувача — раніше цей екран був лише списком
    без жодної дії над записом, і прибрати чийсь доступ можна було тільки
    вручну в базі."""
    rows = []
    for item in housing_access_store.list_users():
        target_id = int(item["user_id"])
        name = str(item.get("display_name") or "без назви")[:30]
        rows.append([InlineKeyboardButton(
            f"🗑 {target_id} · {name}", callback_data=f"housing:access_delete:{target_id}",
        )])
    rows.append([InlineKeyboardButton(BTN_ADMIN_ACCESS_ADD, callback_data="housing:access_add")])
    rows.append([InlineKeyboardButton("⬅ До адмінки", callback_data="housing:admin")])
    return InlineKeyboardMarkup(rows)


def show_access_users(update: Update, context: CallbackContext, edit: bool = False) -> None:
    user = update.effective_user
    if not user or int(user.id) != ADMIN_ID:
        return
    keyboard = _access_users_keyboard()
    if edit and update.callback_query:
        try:
            update.callback_query.edit_message_text(
                _render_access_users(), parse_mode="HTML", reply_markup=keyboard
            )
        except BadRequest as exc:
            if "Message is not modified" not in str(exc):
                raise
    else:
        update.effective_message.reply_text(
            _render_access_users(), parse_mode="HTML", reply_markup=keyboard
        )


def start_access_delete_flow(update: Update, context: CallbackContext, target_id: int) -> None:
    query = update.callback_query
    user = update.effective_user
    if not query or not user or int(user.id) != ADMIN_ID:
        return
    query.answer()
    query.edit_message_text(
        f"🗑 Прибрати доступ для Telegram ID <code>{target_id}</code>?\n\n"
        "Разом із доступом видаляться і всі його фільтри по всіх джерелах — "
        "інакше сповіщення й далі надходили б за старими фільтрами. "
        "Це не можна скасувати.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🗑 Так, прибрати", callback_data=f"housing:access_delete_confirm:{target_id}"),
            InlineKeyboardButton(BTN_CANCEL, callback_data="housing:access_list"),
        ]]),
    )


def _delete_all_filters_for_user(user_id: int) -> int:
    """Прибирає геть усі фільтри людини по кожному джерелу.

    Розсилка сповіщень (`check_job` кожного джерела) не звіряється зі списком
    доступу — вона просто бере всі активні фільтри й шле по них. Тож саме
    лише видалення з `housing_access_store` нічого не змінило б: старі
    фільтри лишались би активними, і повідомлення й далі надходили б.
    """
    removed = 0
    for item in _all_immowelt_filters():
        if int(item.get("user_id") or 0) != int(user_id):
            continue
        filter_id = _filter_id(item)
        if filter_id is None:
            continue
        try:
            _request("DELETE", f"/api/housing/filters/{filter_id}")
            removed += 1
        except Exception:
            logger.exception("Could not delete Immowelt filter %s while revoking access", filter_id)

    propot_filters = propotsdam_store.list_filters(user_id=user_id)
    for filt in propot_filters:
        if propotsdam_store.delete_filter(int(filt["filter_id"]), user_id=user_id):
            removed += 1
    if propot_filters:
        _sync_propot_filters()

    for store in (semmelhaack_store, schoba_store, regiomakler_store, kleinanzeigen_store, locals_store, karlmarx_store):
        for filt in store.list_filters(user_id=user_id):
            if store.delete_filter(int(filt["filter_id"]), user_id=user_id):
                removed += 1
    return removed


def confirm_access_delete(update: Update, context: CallbackContext, target_id: int) -> None:
    query = update.callback_query
    user = update.effective_user
    if not query or not user or int(user.id) != ADMIN_ID:
        return
    ok = housing_access_store.revoke_access(target_id)
    removed = _delete_all_filters_for_user(target_id)
    if ok or removed:
        suffix = f" Видалено фільтрів: {removed}." if removed else ""
        query.answer(f"Доступ прибрано.{suffix}")
        _notify_user_access_revoked(context.bot, target_id)
    else:
        query.answer("Користувача вже немає в списку.")
    show_access_users(update, context, edit=True)


def _start_access_renewal(update: Update, context: CallbackContext) -> None:
    """Admin-side shortcut from the "user wants to renew" notice - skips
    retyping the Telegram ID and straight into the months picker."""
    query = update.callback_query
    user = update.effective_user
    if not query or not user or int(user.id) != ADMIN_ID:
        return
    try:
        target_id = int(query.data.split(":")[2])
    except (IndexError, ValueError):
        query.answer("Некоректна команда.", show_alert=True)
        return
    query.answer()
    existing_name = ""
    for row in housing_access_store.list_users():
        if int(row["user_id"]) == target_id:
            existing_name = str(row.get("display_name") or "")
            break
    context.bot_data.setdefault("housing_access_names", {})[target_id] = existing_name
    query.edit_message_text(
        f"На скільки місяців продовжити доступ?\n\nКористувач: {html.escape(existing_name or str(target_id))}\n"
        f"Telegram ID: <code>{target_id}</code>",
        parse_mode="HTML",
        reply_markup=_access_months_keyboard(target_id),
    )


def _handle_access_continue(update: Update, context: CallbackContext) -> None:
    """The user tapped "✅ Продовжити підписку" on the 3-day expiry warning."""
    query = update.callback_query
    user = update.effective_user
    if not query or not user:
        return
    try:
        target_id = int(query.data.split(":")[2])
    except (IndexError, ValueError):
        query.answer("Некоректна команда.", show_alert=True)
        return
    if int(user.id) != target_id:
        query.answer()
        return
    query.answer("Дякуємо!")
    query.edit_message_text("Дякуємо! Ми повідомили адміністратора — він зв'яжеться з вами щодо продовження.")
    if not ADMIN_ID:
        return
    name = _display_name(user)
    context.bot_data.setdefault("housing_access_names", {})[target_id] = name
    try:
        context.bot.send_message(
            chat_id=ADMIN_ID,
            text=(
                "🔄 <b>Користувач хоче продовжити підписку</b>\n\n"
                f"Користувач: {html.escape(name)}\n"
                f"Telegram ID: <code>{target_id}</code>"
            ),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄 Продовжити зараз", callback_data=f"housing:access_renew:{target_id}"),
            ]]),
        )
    except Exception:
        logger.exception("Could not notify admin about a renewal request from user %s", target_id)


def _handle_access_stop(update: Update, context: CallbackContext) -> None:
    """The user tapped "❌ Не продовжувати" on the 3-day expiry warning.

    Access is NOT closed here - it stays open for the remaining days the
    person already paid for and closes itself automatically on the actual
    expiry date, same as if they'd never answered the warning at all (see
    check_access_expiry's list_expired() pass). This just turns off the
    "do you want to continue" question and confirms what's coming.
    """
    query = update.callback_query
    user = update.effective_user
    if not query or not user:
        return
    try:
        target_id = int(query.data.split(":")[2])
    except (IndexError, ValueError):
        query.answer("Некоректна команда.", show_alert=True)
        return
    if int(user.id) != target_id:
        query.answer()
        return
    query.answer()
    query.edit_message_text(
        "Гаразд. Через 3 дні підписку та доступ до моніторингу житла буде автоматично закрито."
    )


def check_access_expiry(context) -> None:
    """Daily job: warns 3 days before expiry, then auto-closes access once
    the date actually passes (whether or not anyone answered the warning)."""
    bot = context.bot
    for row in housing_access_store.list_expiring_soon(within_days=EXPIRY_WARNING_DAYS):
        target_id = int(row["user_id"])
        name = str(row.get("display_name") or "")
        expires_at = row.get("expires_at")
        expires_str = expires_at.strftime("%d.%m.%Y") if expires_at else "?"
        try:
            bot.send_message(
                chat_id=target_id,
                text=(
                    "⏳ Через 3 дні закінчується ваша підписка на моніторинг житла "
                    "в Потсдамі. Бажаєте продовжити?"
                ),
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Продовжити підписку", callback_data=f"housing:access_continue:{target_id}"),
                    InlineKeyboardButton("❌ Не продовжувати", callback_data=f"housing:access_stop:{target_id}"),
                ]]),
            )
        except Exception:
            logger.exception("Could not send the expiry warning to user %s", target_id)
        if ADMIN_ID:
            try:
                bot.send_message(
                    chat_id=ADMIN_ID,
                    text=(
                        f"⏳ У користувача {html.escape(name) or target_id} ({target_id}) "
                        f"через 3 дні закінчується доступ до моніторингу житла (до {expires_str})."
                    ),
                    parse_mode="HTML",
                )
            except Exception:
                logger.exception("Could not notify admin about expiring access for user %s", target_id)
        housing_access_store.mark_notice_sent(target_id)

    for row in housing_access_store.list_expired():
        _close_access(bot, int(row["user_id"]))


def start_admin_add_flow(update: Update, context: CallbackContext, edit: bool = False) -> None:
    """Єдиний вхід для адміна: раніше Immowelt і ProPotsdam додавались двома
    різними кнопками з двома різними майстрами — тепер один майстер, той
    самий, що й самообслуговування, лише перший крок питає «для кого», а не
    «для себе»."""
    user = update.effective_user
    if not user or int(user.id) != ADMIN_ID:
        return
    context.user_data["housing_admin"] = {
        "mode": "multi", "step": "admin_target_user_id", "sources_selected": [],
    }
    text = "➕ <b>Додати користувача</b>\n\nНадішліть Telegram ID користувача."
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
        f"Поточний вибір: <b>{html.escape(suffix)}</b>"
    )



def _preview_text(criteria: Dict[str, object], preview: Dict[str, object]) -> str:
    lines = [
        "🔍 <b>Перевірка фільтра</b>",
        "",
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
        [InlineKeyboardButton("⬅️ Назад — виправити умови", callback_data="housing:imm_back")],
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
    return f"🏢 <b>Райони ProPotsdam</b>\n\nОберіть райони галочками.\nПоточний вибір: <b>{html.escape(suffix)}</b>"




def _sources_keyboard(selected) -> InlineKeyboardMarkup:
    selected = set(selected or [])
    rows = []
    for spec in AVAILABLE_SOURCES:
        mark = "✅" if spec["key"] in selected else "☐"
        rows.append([InlineKeyboardButton(
            f"{mark} {spec['icon']} {spec['label']}", callback_data=f"housing:src:{spec['key']}"
        )])
    rows.append([InlineKeyboardButton("➡ Далі", callback_data="housing:src_done")])
    rows.append([InlineKeyboardButton(BTN_CANCEL, callback_data="housing:src_cancel")])
    return InlineKeyboardMarkup(rows)


def _sources_text(selected) -> str:
    selected = selected or []
    names = ", ".join(SOURCE_LABEL.get(key, key) for key in selected) if selected else "не обрано"
    return (
        "🔎 <b>Новий фільтр</b>\n\nОберіть, де шукати — можна кілька порталів одразу. "
        "Список порталів згодом поповнюватиметься.\n"
        f"Поточний вибір: <b>{html.escape(names)}</b>"
    )


def start_self_add_flow(update: Update, context: CallbackContext, edit: bool = False) -> None:
    """Один вхід для всіх порталів: спершу питає ГДЕ шукати, а не заводить окремий
    майстер на кожне джерело — раніше Immowelt і ProPotsdam треба було додавати
    двома різними кнопками, не підозрюючи навіть, що це той самий пошук."""
    user = update.effective_user
    if not user or not is_allowed(user.id):
        return
    context.user_data["housing_admin"] = {
        "step": "sources", "user_id": int(user.id), "sources_selected": [],
    }
    text = _sources_text([])
    keyboard = _sources_keyboard([])
    if edit and update.callback_query:
        update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)
    else:
        update.effective_message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)


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


SOURCE_ICON = {
    "immowelt": "🏠", "propotsdam": "🏢", "semmelhaack": "🏘", "schoba": "🏡",
    "regiomakler": "🤝", "kleinanzeigen": "📋", "locals": "🔑", "karlmarx": "🧱",
    "gewoba": "🏗", "wbg1903": "🏚", "wbg_daheim": "🛖",
}
SOURCE_LABEL = {
    "immowelt": "Immowelt", "propotsdam": "ProPotsdam", "semmelhaack": "SEMMELHAACK",
    "schoba": "SCHOBA", "regiomakler": "ImmoTeam/alpha", "kleinanzeigen": "Kleinanzeigen",
    "locals": "locals®", "karlmarx": "Karl Marx",
    "all": "усі джерела",
    # Labels come straight from coop_watchdog.COOPERATIVES - one source of
    # truth for the human-readable name of each cooperative.
    **{coop["key"]: coop["label"] for coop in coop_watchdog.COOPERATIVES},
}


def _item_criteria_summary(item: Dict[str, object], source: str) -> str:
    """Стислий опис умов фільтра для кнопки.

    Назва каже лише «чий це пошук» («Пошук Артема»), а не «який» — двоє
    фільтрів на одну людину виглядали в списку однаково, поки не відкриєш
    кожен окремо. ProPotsdam зберігає район рядком через кому й ціну як
    «загальну оренду» під іншою назвою поля — приводимо до одного вигляду,
    яким уже вміє оперувати `_describe_criteria`.
    """
    if source == "propotsdam":
        districts = [d for d in str(item.get("districts") or "").split(",") if d]
        criteria = {
            "districts": districts,
            "min_price_eur": item.get("min_total_rent_eur"),
            "max_price_eur": item.get("max_total_rent_eur"),
            "min_rooms": item.get("min_rooms"),
            "max_rooms": item.get("max_rooms"),
            "min_area_m2": item.get("min_area_m2"),
            "max_area_m2": item.get("max_area_m2"),
        }
    else:
        criteria = item
    # _describe_criteria готує текст для HTML-повідомлення; кнопки HTML не
    # розбирають, тож &, < і подібне мають лишитися як є, а не як сутності.
    summary = html.unescape(_describe_criteria(criteria))
    if len(summary) > 40:
        summary = summary[:39].rstrip(" ,·") + "…"
    return summary


def _self_manage_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Список фільтрів, розбитий на розділи за джерелом.

    Immowelt і ProPotsdam малювали однакові на вигляд кнопки в одному
    суцільному списку — назва фільтра каже, чий це пошук, а не який портал
    він дивиться, тож два фільтри однієї людини були невідрізненні. Заголовок
    розділу — теж кнопка (Telegram не має нейтральних рядків у клавіатурі),
    але веде в нікуди: `housing:noop` лише гасить «годинник» на кнопці.
    """
    rows = []
    items = manageable_filters(user_id)
    for source in ("immowelt", "propotsdam", "semmelhaack", "schoba", "regiomakler", "kleinanzeigen", "locals", "karlmarx"):
        group = [item for item in items if _item_source(item) == source]
        if not group:
            continue
        rows.append([InlineKeyboardButton(
            f"── {SOURCE_ICON[source]} Фільтри {SOURCE_LABEL[source]} ──", callback_data="housing:noop"
        )])
        for item in group:
            filter_id = int(item.get("filter_id"))
            active = bool(item.get("active", True))
            mark = "✅" if active else "⏸"
            summary = _item_criteria_summary(item, source)
            rows.append([InlineKeyboardButton(
                f"{mark} {summary}",
                callback_data=f"housing:toggle:{source}:{filter_id}:{0 if active else 1}",
            )])
            # Раніше ProPotsdam-фільтр можна було лише поставити на паузу чи
            # видалити: одруківся в районі — і заводь новий фільтр з нуля.
            rows.append([
                InlineKeyboardButton("✏️ Редагувати", callback_data=f"housing:edit:{source}:{filter_id}"),
                InlineKeyboardButton("🗑 Видалити", callback_data=f"housing:delete:{source}:{filter_id}"),
            ])
    rows.append([InlineKeyboardButton("⬅ До моніторингу", callback_data="housing:menu")])
    return InlineKeyboardMarkup(rows)


def show_self_manage(update: Update, context: CallbackContext, edit: bool = False) -> None:
    user = update.effective_user
    if not user or not is_allowed(user.id):
        return
    filters = manageable_filters(user.id)
    text = (
        "⚙️ <b>Мої фільтри</b>\n\nНатисніть фільтр, щоб призупинити/увімкнути, "
        "а «✏️ Редагувати»/«🗑 Видалити» поруч — щоб змінити умови або прибрати зовсім."
        if filters else "⚙️ <b>Мої фільтри</b>\n\nУ вас ще немає фільтрів. Натисніть «➕ Додати фільтр» у меню."
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
    elif source == "semmelhaack":
        ok = semmelhaack_store.set_filter_active(filter_id, active, user_id=user.id)
    elif source == "schoba":
        ok = schoba_store.set_filter_active(filter_id, active, user_id=user.id)
    elif source == "regiomakler":
        ok = regiomakler_store.set_filter_active(filter_id, active, user_id=user.id)
    elif source == "kleinanzeigen":
        ok = kleinanzeigen_store.set_filter_active(filter_id, active, user_id=user.id)
    elif source == "locals":
        ok = locals_store.set_filter_active(filter_id, active, user_id=user.id)
    elif source == "karlmarx":
        ok = karlmarx_store.set_filter_active(filter_id, active, user_id=user.id)
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
    elif source == "semmelhaack":
        ok = semmelhaack_store.delete_filter(filter_id, user_id=user.id)
    elif source == "schoba":
        ok = schoba_store.delete_filter(filter_id, user_id=user.id)
    elif source == "regiomakler":
        ok = regiomakler_store.delete_filter(filter_id, user_id=user.id)
    elif source == "kleinanzeigen":
        ok = kleinanzeigen_store.delete_filter(filter_id, user_id=user.id)
    elif source == "locals":
        ok = locals_store.delete_filter(filter_id, user_id=user.id)
    elif source == "karlmarx":
        ok = karlmarx_store.delete_filter(filter_id, user_id=user.id)
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
        "districts_selected": districts_selected,
        "min_price_eur": item.get("min_price_eur"),
        "max_price_eur": item.get("max_price_eur"),
        "min_rooms": item.get("min_rooms"),
        "max_rooms": item.get("max_rooms"),
        "min_area_m2": item.get("min_area_m2"),
        "max_area_m2": item.get("max_area_m2"),
        "edit_filter_id": filter_id,
    }
    query.answer()
    query.edit_message_text(
        _immowelt_district_text(districts_selected),
        parse_mode="HTML",
        reply_markup=_immowelt_district_keyboard(districts_selected),
    )


def start_propot_edit_flow(update: Update, context: CallbackContext, filter_id: int) -> None:
    """Той самий підхід, що й у `start_edit_flow` для Immowelt.

    Раніше «Мої фільтри» вміло для ProPotsdam лише паузу й видалення —
    одруківся в районі чи площі, і єдиний вихід був завести фільтр з нуля.
    """
    query = update.callback_query
    user = update.effective_user
    if not query or not user or not is_allowed(user.id):
        return
    item = _own_filter(int(user.id), "propotsdam", filter_id)
    if not item:
        query.answer("Цей фільтр вам не належить.", show_alert=True)
        return
    districts_selected = [d for d in str(item.get("districts") or "").split(",") if d]
    context.user_data["housing_admin"] = {
        "mode": "propotsdam", "step": "districts", "user_id": int(user.id),
        "districts_selected": districts_selected,
        "min_rooms": item.get("min_rooms"),
        "max_rooms": item.get("max_rooms"),
        "min_area_m2": item.get("min_area_m2"),
        "max_area_m2": item.get("max_area_m2"),
        "min_total_rent_eur": item.get("min_total_rent_eur"),
        "max_total_rent_eur": item.get("max_total_rent_eur"),
        "edit_filter_id": filter_id,
    }
    query.answer()
    query.edit_message_text(
        _district_text(districts_selected),
        parse_mode="HTML",
        reply_markup=_district_keyboard(districts_selected),
    )


def start_semmelhaack_edit_flow(update: Update, context: CallbackContext, filter_id: int) -> None:
    """Той самий підхід, що й у `start_propot_edit_flow`, але без районів — їх
    у SEMMELHAACK немає, тож майстер одразу починає з кімнат."""
    query = update.callback_query
    user = update.effective_user
    if not query or not user or not is_allowed(user.id):
        return
    item = _own_filter(int(user.id), "semmelhaack", filter_id)
    if not item:
        query.answer("Цей фільтр вам не належить.", show_alert=True)
        return
    first_key = SEMM_CRITERIA_KEYS[0]
    context.user_data["housing_admin"] = {
        "mode": "semmelhaack", "step": first_key, "user_id": int(user.id),
        "min_rooms": item.get("min_rooms"),
        "max_rooms": item.get("max_rooms"),
        "min_area_m2": item.get("min_area_m2"),
        "max_area_m2": item.get("max_area_m2"),
        "min_price_eur": item.get("min_price_eur"),
        "max_price_eur": item.get("max_price_eur"),
        "edit_filter_id": filter_id,
    }
    query.answer()
    query.edit_message_text(_field_prompt({}, SEMM_CRITERIA_FIELDS, first_key, i18n.get_lang(user.id)), parse_mode="HTML")


def _finalize_semmelhaack_filter(message, context: CallbackContext, state: dict) -> None:
    """Той самий майстер веде і редагування — відрізняє лише `edit_filter_id`."""
    edit_filter_id = state.get("edit_filter_id")
    criteria = {
        "min_price_eur": state.get("min_price_eur"),
        "max_price_eur": state.get("max_price_eur"),
        "min_rooms": state.get("min_rooms"),
        "max_rooms": state.get("max_rooms"),
        "min_area_m2": state.get("min_area_m2"),
        "max_area_m2": state.get("max_area_m2"),
    }
    title = _auto_title("semmelhaack", criteria)
    common = dict(
        title=title,
        min_rooms=state.get("min_rooms"), max_rooms=state.get("max_rooms"),
        min_area_m2=state.get("min_area_m2"), max_area_m2=state.get("max_area_m2"),
        min_price_eur=state.get("min_price_eur"), max_price_eur=state.get("max_price_eur"),
    )
    if edit_filter_id:
        ok = semmelhaack_store.update_filter(
            filter_id=int(edit_filter_id), user_id=int(state["user_id"]), **common
        )
        filter_id = int(edit_filter_id)
    else:
        filter_id = semmelhaack_store.create_filter(user_id=state["user_id"], **common)
        ok = True
    context.user_data.pop("housing_admin", None)
    lang = i18n.get_lang(int(state["user_id"]))
    if not ok:
        message.reply_text("⚠️ Не вдалося зберегти фільтр — можливо, його вже видалено.")
        return
    heading = (
        i18n.t("housing.finalize.updated", lang, source="SEMMELHAACK") if edit_filter_id
        else i18n.t("housing.finalize.added", lang, source="SEMMELHAACK")
    )
    if not edit_filter_id:
        _offer_recent_matches(context, [("semmelhaack", filter_id)])
        _maybe_send_first_filter_congrats(context, state["user_id"])
    message.reply_text(
        i18n.t("housing.finalize.summary", lang, heading=heading, id=f"S{filter_id}", criteria=_describe_criteria(criteria, lang)),
        parse_mode="HTML",
        reply_markup=_recent_offer_keyboard() if not edit_filter_id else None,
    )


def _handle_semmelhaack_flow(update: Update, context: CallbackContext, state: dict, text: str) -> bool:
    step = state.get("step")
    if step in SEMM_CRITERIA_BY_KEY:
        spec = SEMM_CRITERIA_BY_KEY[step]
        value = _parse_single_number(text)
        if value is _INVALID_NUMBER:
            update.message.reply_text("Незрозуміле значення.\n\n" + spec["prompt"], parse_mode="HTML")
            return True
        if _violates_sibling_bound(state, step, value):
            update.message.reply_text(
                "Мінімум не може бути більшим за максимум. Надішліть значення ще раз.\n\n" + spec["prompt"], parse_mode="HTML"
            )
            return True
        state[step] = value
        index = SEMM_CRITERIA_KEYS.index(step)
        if index < len(SEMM_CRITERIA_KEYS) - 1:
            next_key = SEMM_CRITERIA_KEYS[index + 1]
            state["step"] = next_key
            _reply_field_prompt(update.message, _field_prompt(state, SEMM_CRITERIA_FIELDS, next_key, i18n.get_lang(update.effective_user.id)))
            return True
        _finalize_semmelhaack_filter(update.message, context, state)
        return True
    return False


def start_schoba_edit_flow(update: Update, context: CallbackContext, filter_id: int) -> None:
    """Той самий підхід, що й у `start_semmelhaack_edit_flow` — без районів."""
    query = update.callback_query
    user = update.effective_user
    if not query or not user or not is_allowed(user.id):
        return
    item = _own_filter(int(user.id), "schoba", filter_id)
    if not item:
        query.answer("Цей фільтр вам не належить.", show_alert=True)
        return
    first_key = SCHOBA_CRITERIA_KEYS[0]
    context.user_data["housing_admin"] = {
        "mode": "schoba", "step": first_key, "user_id": int(user.id),
        "min_rooms": item.get("min_rooms"),
        "max_rooms": item.get("max_rooms"),
        "min_area_m2": item.get("min_area_m2"),
        "max_area_m2": item.get("max_area_m2"),
        "min_price_eur": item.get("min_price_eur"),
        "max_price_eur": item.get("max_price_eur"),
        "edit_filter_id": filter_id,
    }
    query.answer()
    query.edit_message_text(_field_prompt({}, SCHOBA_CRITERIA_FIELDS, first_key, i18n.get_lang(user.id)), parse_mode="HTML")


def _finalize_schoba_filter(message, context: CallbackContext, state: dict) -> None:
    """Той самий майстер веде і редагування — відрізняє лише `edit_filter_id`."""
    edit_filter_id = state.get("edit_filter_id")
    criteria = {
        "min_price_eur": state.get("min_price_eur"),
        "max_price_eur": state.get("max_price_eur"),
        "min_rooms": state.get("min_rooms"),
        "max_rooms": state.get("max_rooms"),
        "min_area_m2": state.get("min_area_m2"),
        "max_area_m2": state.get("max_area_m2"),
    }
    title = _auto_title("schoba", criteria)
    common = dict(
        title=title,
        min_rooms=state.get("min_rooms"), max_rooms=state.get("max_rooms"),
        min_area_m2=state.get("min_area_m2"), max_area_m2=state.get("max_area_m2"),
        min_price_eur=state.get("min_price_eur"), max_price_eur=state.get("max_price_eur"),
    )
    if edit_filter_id:
        ok = schoba_store.update_filter(
            filter_id=int(edit_filter_id), user_id=int(state["user_id"]), **common
        )
        filter_id = int(edit_filter_id)
    else:
        filter_id = schoba_store.create_filter(user_id=state["user_id"], **common)
        ok = True
    context.user_data.pop("housing_admin", None)
    lang = i18n.get_lang(int(state["user_id"]))
    if not ok:
        message.reply_text("⚠️ Не вдалося зберегти фільтр — можливо, його вже видалено.")
        return
    heading = (
        i18n.t("housing.finalize.updated", lang, source="SCHOBA") if edit_filter_id
        else i18n.t("housing.finalize.added", lang, source="SCHOBA")
    )
    if not edit_filter_id:
        _offer_recent_matches(context, [("schoba", filter_id)])
        _maybe_send_first_filter_congrats(context, state["user_id"])
    message.reply_text(
        i18n.t("housing.finalize.summary", lang, heading=heading, id=f"C{filter_id}", criteria=_describe_criteria(criteria, lang)),
        parse_mode="HTML",
        reply_markup=_recent_offer_keyboard() if not edit_filter_id else None,
    )


def _handle_schoba_flow(update: Update, context: CallbackContext, state: dict, text: str) -> bool:
    step = state.get("step")
    if step in SCHOBA_CRITERIA_BY_KEY:
        spec = SCHOBA_CRITERIA_BY_KEY[step]
        value = _parse_single_number(text)
        if value is _INVALID_NUMBER:
            update.message.reply_text("Незрозуміле значення.\n\n" + spec["prompt"], parse_mode="HTML")
            return True
        if _violates_sibling_bound(state, step, value):
            update.message.reply_text(
                "Мінімум не може бути більшим за максимум. Надішліть значення ще раз.\n\n" + spec["prompt"], parse_mode="HTML"
            )
            return True
        state[step] = value
        index = SCHOBA_CRITERIA_KEYS.index(step)
        if index < len(SCHOBA_CRITERIA_KEYS) - 1:
            next_key = SCHOBA_CRITERIA_KEYS[index + 1]
            state["step"] = next_key
            _reply_field_prompt(update.message, _field_prompt(state, SCHOBA_CRITERIA_FIELDS, next_key, i18n.get_lang(update.effective_user.id)))
            return True
        _finalize_schoba_filter(update.message, context, state)
        return True
    return False


def start_regiomakler_edit_flow(update: Update, context: CallbackContext, filter_id: int) -> None:
    """Той самий підхід, що й у `start_schoba_edit_flow` — без районів."""
    query = update.callback_query
    user = update.effective_user
    if not query or not user or not is_allowed(user.id):
        return
    item = _own_filter(int(user.id), "regiomakler", filter_id)
    if not item:
        query.answer("Цей фільтр вам не належить.", show_alert=True)
        return
    first_key = REGIOMAKLER_CRITERIA_KEYS[0]
    context.user_data["housing_admin"] = {
        "mode": "regiomakler", "step": first_key, "user_id": int(user.id),
        "min_rooms": item.get("min_rooms"),
        "max_rooms": item.get("max_rooms"),
        "min_area_m2": item.get("min_area_m2"),
        "max_area_m2": item.get("max_area_m2"),
        "min_price_eur": item.get("min_price_eur"),
        "max_price_eur": item.get("max_price_eur"),
        "edit_filter_id": filter_id,
    }
    query.answer()
    query.edit_message_text(_field_prompt({}, REGIOMAKLER_CRITERIA_FIELDS, first_key, i18n.get_lang(user.id)), parse_mode="HTML")


def _finalize_regiomakler_filter(message, context: CallbackContext, state: dict) -> None:
    """Той самий майстер веде і редагування — відрізняє лише `edit_filter_id`."""
    edit_filter_id = state.get("edit_filter_id")
    criteria = {
        "min_price_eur": state.get("min_price_eur"),
        "max_price_eur": state.get("max_price_eur"),
        "min_rooms": state.get("min_rooms"),
        "max_rooms": state.get("max_rooms"),
        "min_area_m2": state.get("min_area_m2"),
        "max_area_m2": state.get("max_area_m2"),
    }
    title = _auto_title("regiomakler", criteria)
    common = dict(
        title=title,
        min_rooms=state.get("min_rooms"), max_rooms=state.get("max_rooms"),
        min_area_m2=state.get("min_area_m2"), max_area_m2=state.get("max_area_m2"),
        min_price_eur=state.get("min_price_eur"), max_price_eur=state.get("max_price_eur"),
    )
    if edit_filter_id:
        ok = regiomakler_store.update_filter(
            filter_id=int(edit_filter_id), user_id=int(state["user_id"]), **common
        )
        filter_id = int(edit_filter_id)
    else:
        filter_id = regiomakler_store.create_filter(user_id=state["user_id"], **common)
        ok = True
    context.user_data.pop("housing_admin", None)
    lang = i18n.get_lang(int(state["user_id"]))
    if not ok:
        message.reply_text("⚠️ Не вдалося зберегти фільтр — можливо, його вже видалено.")
        return
    heading = (
        i18n.t("housing.finalize.updated", lang, source="ImmoTeam/alpha") if edit_filter_id
        else i18n.t("housing.finalize.added", lang, source="ImmoTeam/alpha")
    )
    if not edit_filter_id:
        _offer_recent_matches(context, [("regiomakler", filter_id)])
        _maybe_send_first_filter_congrats(context, state["user_id"])
    message.reply_text(
        i18n.t("housing.finalize.summary", lang, heading=heading, id=f"R{filter_id}", criteria=_describe_criteria(criteria, lang)),
        parse_mode="HTML",
        reply_markup=_recent_offer_keyboard() if not edit_filter_id else None,
    )


def _handle_regiomakler_flow(update: Update, context: CallbackContext, state: dict, text: str) -> bool:
    step = state.get("step")
    if step in REGIOMAKLER_CRITERIA_BY_KEY:
        spec = REGIOMAKLER_CRITERIA_BY_KEY[step]
        value = _parse_single_number(text)
        if value is _INVALID_NUMBER:
            update.message.reply_text("Незрозуміле значення.\n\n" + spec["prompt"], parse_mode="HTML")
            return True
        if _violates_sibling_bound(state, step, value):
            update.message.reply_text(
                "Мінімум не може бути більшим за максимум. Надішліть значення ще раз.\n\n" + spec["prompt"], parse_mode="HTML"
            )
            return True
        state[step] = value
        index = REGIOMAKLER_CRITERIA_KEYS.index(step)
        if index < len(REGIOMAKLER_CRITERIA_KEYS) - 1:
            next_key = REGIOMAKLER_CRITERIA_KEYS[index + 1]
            state["step"] = next_key
            _reply_field_prompt(update.message, _field_prompt(state, REGIOMAKLER_CRITERIA_FIELDS, next_key, i18n.get_lang(update.effective_user.id)))
            return True
        _finalize_regiomakler_filter(update.message, context, state)
        return True
    return False


def start_kleinanzeigen_edit_flow(update: Update, context: CallbackContext, filter_id: int) -> None:
    """Той самий підхід, що й у `start_regiomakler_edit_flow` — без районів."""
    query = update.callback_query
    user = update.effective_user
    if not query or not user or not is_allowed(user.id):
        return
    item = _own_filter(int(user.id), "kleinanzeigen", filter_id)
    if not item:
        query.answer("Цей фільтр вам не належить.", show_alert=True)
        return
    first_key = KLEINANZEIGEN_CRITERIA_KEYS[0]
    context.user_data["housing_admin"] = {
        "mode": "kleinanzeigen", "step": first_key, "user_id": int(user.id),
        "min_rooms": item.get("min_rooms"),
        "max_rooms": item.get("max_rooms"),
        "min_area_m2": item.get("min_area_m2"),
        "max_area_m2": item.get("max_area_m2"),
        "min_price_eur": item.get("min_price_eur"),
        "max_price_eur": item.get("max_price_eur"),
        "edit_filter_id": filter_id,
    }
    query.answer()
    query.edit_message_text(_field_prompt({}, KLEINANZEIGEN_CRITERIA_FIELDS, first_key, i18n.get_lang(user.id)), parse_mode="HTML")


def _finalize_kleinanzeigen_filter(message, context: CallbackContext, state: dict) -> None:
    """Той самий майстер веде і редагування — відрізняє лише `edit_filter_id`."""
    edit_filter_id = state.get("edit_filter_id")
    criteria = {
        "min_price_eur": state.get("min_price_eur"),
        "max_price_eur": state.get("max_price_eur"),
        "min_rooms": state.get("min_rooms"),
        "max_rooms": state.get("max_rooms"),
        "min_area_m2": state.get("min_area_m2"),
        "max_area_m2": state.get("max_area_m2"),
    }
    title = _auto_title("kleinanzeigen", criteria)
    common = dict(
        title=title,
        min_rooms=state.get("min_rooms"), max_rooms=state.get("max_rooms"),
        min_area_m2=state.get("min_area_m2"), max_area_m2=state.get("max_area_m2"),
        min_price_eur=state.get("min_price_eur"), max_price_eur=state.get("max_price_eur"),
    )
    if edit_filter_id:
        ok = kleinanzeigen_store.update_filter(
            filter_id=int(edit_filter_id), user_id=int(state["user_id"]), **common
        )
        filter_id = int(edit_filter_id)
    else:
        filter_id = kleinanzeigen_store.create_filter(user_id=state["user_id"], **common)
        ok = True
    context.user_data.pop("housing_admin", None)
    lang = i18n.get_lang(int(state["user_id"]))
    if not ok:
        message.reply_text("⚠️ Не вдалося зберегти фільтр — можливо, його вже видалено.")
        return
    heading = (
        i18n.t("housing.finalize.updated", lang, source="Kleinanzeigen") if edit_filter_id
        else i18n.t("housing.finalize.added", lang, source="Kleinanzeigen")
    )
    if not edit_filter_id:
        _offer_recent_matches(context, [("kleinanzeigen", filter_id)])
        _maybe_send_first_filter_congrats(context, state["user_id"])
    message.reply_text(
        i18n.t("housing.finalize.summary", lang, heading=heading, id=f"K{filter_id}", criteria=_describe_criteria(criteria, lang)),
        parse_mode="HTML",
        reply_markup=_recent_offer_keyboard() if not edit_filter_id else None,
    )


def _handle_kleinanzeigen_flow(update: Update, context: CallbackContext, state: dict, text: str) -> bool:
    step = state.get("step")
    if step in KLEINANZEIGEN_CRITERIA_BY_KEY:
        spec = KLEINANZEIGEN_CRITERIA_BY_KEY[step]
        value = _parse_single_number(text)
        if value is _INVALID_NUMBER:
            update.message.reply_text("Незрозуміле значення.\n\n" + spec["prompt"], parse_mode="HTML")
            return True
        if _violates_sibling_bound(state, step, value):
            update.message.reply_text(
                "Мінімум не може бути більшим за максимум. Надішліть значення ще раз.\n\n" + spec["prompt"], parse_mode="HTML"
            )
            return True
        state[step] = value
        index = KLEINANZEIGEN_CRITERIA_KEYS.index(step)
        if index < len(KLEINANZEIGEN_CRITERIA_KEYS) - 1:
            next_key = KLEINANZEIGEN_CRITERIA_KEYS[index + 1]
            state["step"] = next_key
            _reply_field_prompt(update.message, _field_prompt(state, KLEINANZEIGEN_CRITERIA_FIELDS, next_key, i18n.get_lang(update.effective_user.id)))
            return True
        _finalize_kleinanzeigen_filter(update.message, context, state)
        return True
    return False


def start_locals_edit_flow(update: Update, context: CallbackContext, filter_id: int) -> None:
    """Той самий підхід, що й у `start_schoba_edit_flow` — без районів."""
    query = update.callback_query
    user = update.effective_user
    if not query or not user or not is_allowed(user.id):
        return
    item = _own_filter(int(user.id), "locals", filter_id)
    if not item:
        query.answer("Цей фільтр вам не належить.", show_alert=True)
        return
    first_key = LOCALS_CRITERIA_KEYS[0]
    context.user_data["housing_admin"] = {
        "mode": "locals", "step": first_key, "user_id": int(user.id),
        "min_rooms": item.get("min_rooms"),
        "max_rooms": item.get("max_rooms"),
        "min_area_m2": item.get("min_area_m2"),
        "max_area_m2": item.get("max_area_m2"),
        "min_price_eur": item.get("min_price_eur"),
        "max_price_eur": item.get("max_price_eur"),
        "edit_filter_id": filter_id,
    }
    query.answer()
    query.edit_message_text(_field_prompt({}, LOCALS_CRITERIA_FIELDS, first_key, i18n.get_lang(user.id)), parse_mode="HTML")


def _finalize_locals_filter(message, context: CallbackContext, state: dict) -> None:
    """Той самий майстер веде і редагування — відрізняє лише `edit_filter_id`."""
    edit_filter_id = state.get("edit_filter_id")
    criteria = {
        "min_price_eur": state.get("min_price_eur"),
        "max_price_eur": state.get("max_price_eur"),
        "min_rooms": state.get("min_rooms"),
        "max_rooms": state.get("max_rooms"),
        "min_area_m2": state.get("min_area_m2"),
        "max_area_m2": state.get("max_area_m2"),
    }
    title = _auto_title("locals", criteria)
    common = dict(
        title=title,
        min_rooms=state.get("min_rooms"), max_rooms=state.get("max_rooms"),
        min_area_m2=state.get("min_area_m2"), max_area_m2=state.get("max_area_m2"),
        min_price_eur=state.get("min_price_eur"), max_price_eur=state.get("max_price_eur"),
    )
    if edit_filter_id:
        ok = locals_store.update_filter(
            filter_id=int(edit_filter_id), user_id=int(state["user_id"]), **common
        )
        filter_id = int(edit_filter_id)
    else:
        filter_id = locals_store.create_filter(user_id=state["user_id"], **common)
        ok = True
    context.user_data.pop("housing_admin", None)
    lang = i18n.get_lang(int(state["user_id"]))
    if not ok:
        message.reply_text("⚠️ Не вдалося зберегти фільтр — можливо, його вже видалено.")
        return
    heading = (
        i18n.t("housing.finalize.updated", lang, source="locals®") if edit_filter_id
        else i18n.t("housing.finalize.added", lang, source="locals®")
    )
    if not edit_filter_id:
        _offer_recent_matches(context, [("locals", filter_id)])
        _maybe_send_first_filter_congrats(context, state["user_id"])
    message.reply_text(
        i18n.t("housing.finalize.summary", lang, heading=heading, id=f"L{filter_id}", criteria=_describe_criteria(criteria, lang)),
        parse_mode="HTML",
        reply_markup=_recent_offer_keyboard() if not edit_filter_id else None,
    )


def _handle_locals_flow(update: Update, context: CallbackContext, state: dict, text: str) -> bool:
    step = state.get("step")
    if step in LOCALS_CRITERIA_BY_KEY:
        spec = LOCALS_CRITERIA_BY_KEY[step]
        value = _parse_single_number(text)
        if value is _INVALID_NUMBER:
            update.message.reply_text("Незрозуміле значення.\n\n" + spec["prompt"], parse_mode="HTML")
            return True
        if _violates_sibling_bound(state, step, value):
            update.message.reply_text(
                "Мінімум не може бути більшим за максимум. Надішліть значення ще раз.\n\n" + spec["prompt"], parse_mode="HTML"
            )
            return True
        state[step] = value
        index = LOCALS_CRITERIA_KEYS.index(step)
        if index < len(LOCALS_CRITERIA_KEYS) - 1:
            next_key = LOCALS_CRITERIA_KEYS[index + 1]
            state["step"] = next_key
            _reply_field_prompt(update.message, _field_prompt(state, LOCALS_CRITERIA_FIELDS, next_key, i18n.get_lang(update.effective_user.id)))
            return True
        _finalize_locals_filter(update.message, context, state)
        return True
    return False


def start_karlmarx_edit_flow(update: Update, context: CallbackContext, filter_id: int) -> None:
    """Той самий підхід, що й у `start_schoba_edit_flow` — без районів."""
    query = update.callback_query
    user = update.effective_user
    if not query or not user or not is_allowed(user.id):
        return
    item = _own_filter(int(user.id), "karlmarx", filter_id)
    if not item:
        query.answer("Цей фільтр вам не належить.", show_alert=True)
        return
    first_key = KARLMARX_CRITERIA_KEYS[0]
    context.user_data["housing_admin"] = {
        "mode": "karlmarx", "step": first_key, "user_id": int(user.id),
        "min_rooms": item.get("min_rooms"),
        "max_rooms": item.get("max_rooms"),
        "min_area_m2": item.get("min_area_m2"),
        "max_area_m2": item.get("max_area_m2"),
        "min_price_eur": item.get("min_price_eur"),
        "max_price_eur": item.get("max_price_eur"),
        "edit_filter_id": filter_id,
    }
    query.answer()
    query.edit_message_text(_field_prompt({}, KARLMARX_CRITERIA_FIELDS, first_key, i18n.get_lang(user.id)), parse_mode="HTML")


def _finalize_karlmarx_filter(message, context: CallbackContext, state: dict) -> None:
    """Той самий майстер веде і редагування — відрізняє лише `edit_filter_id`."""
    edit_filter_id = state.get("edit_filter_id")
    criteria = {
        "min_price_eur": state.get("min_price_eur"),
        "max_price_eur": state.get("max_price_eur"),
        "min_rooms": state.get("min_rooms"),
        "max_rooms": state.get("max_rooms"),
        "min_area_m2": state.get("min_area_m2"),
        "max_area_m2": state.get("max_area_m2"),
    }
    title = _auto_title("karlmarx", criteria)
    common = dict(
        title=title,
        min_rooms=state.get("min_rooms"), max_rooms=state.get("max_rooms"),
        min_area_m2=state.get("min_area_m2"), max_area_m2=state.get("max_area_m2"),
        min_price_eur=state.get("min_price_eur"), max_price_eur=state.get("max_price_eur"),
    )
    if edit_filter_id:
        ok = karlmarx_store.update_filter(
            filter_id=int(edit_filter_id), user_id=int(state["user_id"]), **common
        )
        filter_id = int(edit_filter_id)
    else:
        filter_id = karlmarx_store.create_filter(user_id=state["user_id"], **common)
        ok = True
    context.user_data.pop("housing_admin", None)
    lang = i18n.get_lang(int(state["user_id"]))
    if not ok:
        message.reply_text("⚠️ Не вдалося зберегти фільтр — можливо, його вже видалено.")
        return
    heading = (
        i18n.t("housing.finalize.updated", lang, source="Karl Marx") if edit_filter_id
        else i18n.t("housing.finalize.added", lang, source="Karl Marx")
    )
    if not edit_filter_id:
        _offer_recent_matches(context, [("karlmarx", filter_id)])
        _maybe_send_first_filter_congrats(context, state["user_id"])
    message.reply_text(
        i18n.t("housing.finalize.summary", lang, heading=heading, id=f"M{filter_id}", criteria=_describe_criteria(criteria, lang)),
        parse_mode="HTML",
        reply_markup=_recent_offer_keyboard() if not edit_filter_id else None,
    )


def _handle_karlmarx_flow(update: Update, context: CallbackContext, state: dict, text: str) -> bool:
    step = state.get("step")
    if step in KARLMARX_CRITERIA_BY_KEY:
        spec = KARLMARX_CRITERIA_BY_KEY[step]
        value = _parse_single_number(text)
        if value is _INVALID_NUMBER:
            update.message.reply_text("Незрозуміле значення.\n\n" + spec["prompt"], parse_mode="HTML")
            return True
        if _violates_sibling_bound(state, step, value):
            update.message.reply_text(
                "Мінімум не може бути більшим за максимум. Надішліть значення ще раз.\n\n" + spec["prompt"], parse_mode="HTML"
            )
            return True
        state[step] = value
        index = KARLMARX_CRITERIA_KEYS.index(step)
        if index < len(KARLMARX_CRITERIA_KEYS) - 1:
            next_key = KARLMARX_CRITERIA_KEYS[index + 1]
            state["step"] = next_key
            _reply_field_prompt(update.message, _field_prompt(state, KARLMARX_CRITERIA_FIELDS, next_key, i18n.get_lang(update.effective_user.id)))
            return True
        _finalize_karlmarx_filter(update.message, context, state)
        return True
    return False


def _show_immowelt_preview(message, state: dict) -> None:
    criteria = _criteria_from_state(state)
    state["title"] = _auto_title("immowelt", criteria)
    state["step"] = "preview"
    message.reply_text(
        _preview_text(criteria, _preview_criteria(criteria)),
        parse_mode="HTML",
        reply_markup=_preview_keyboard(),
        disable_web_page_preview=True,
    )


def _back_from_immowelt_preview(update: Update, context: CallbackContext) -> None:
    """«Назад» із перевірки фільтра — раніше єдиним виходом було «Скасувати» й почати з нуля.

    Повертає до вибору районів, зберігаючи вже введені назву й райони; умови
    доведеться відповісти заново — той самий компроміс простоти, що й у
    решті майстра без чекбоксів.
    """
    query = update.callback_query
    state = context.user_data.get("housing_admin") or {}
    if state.get("mode") != "immowelt" or state.get("step") != "preview":
        query.answer()
        return
    state["step"] = "districts"
    query.answer()
    query.edit_message_text(
        _immowelt_district_text(state.get("districts_selected")),
        parse_mode="HTML",
        reply_markup=_immowelt_district_keyboard(state.get("districts_selected")),
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
    """Переносить район/кімнати/площу й питає лише оренду — двома питаннями.

    Instant-копія без питань раніше просто пропускала оренду мовчки: Immowelt
    рахує холодну (Kaltmiete), ProPotsdam — повну (Gesamtmiete), тож перенесене
    число означало б інакшу умову, а не ту саму.
    """
    query = update.callback_query
    source = context.user_data.pop("housing_clone_source", None)
    if not source or source.get("target") != "propotsdam":
        query.answer()
        return
    context.user_data["housing_admin"] = {
        "mode": "propotsdam", "step": "clone_price_min",
        "user_id": source["user_id"], "title": source["title"],
        "districts": propotsdam_store.normalize_districts(",".join(source.get("districts") or [])),
        "min_rooms": source.get("min_rooms"), "max_rooms": source.get("max_rooms"),
        "min_area_m2": source.get("min_area_m2"), "max_area_m2": source.get("max_area_m2"),
    }
    query.answer()
    query.edit_message_text(
        "Район, кімнати й площу перенесено з Immowelt-фільтра. Лишилось уточнити оренду — "
        "ProPotsdam рахує її як повну (Gesamtmiete), а Immowelt як холодну (Kaltmiete).\n\n"
        + PROPOT_CRITERIA_BY_KEY["min_total_rent_eur"]["prompt"],
        parse_mode="HTML",
    )


def _clone_immowelt_from_propot(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    source = context.user_data.pop("housing_clone_source", None)
    if not source or source.get("target") != "immowelt":
        query.answer()
        return
    context.user_data["housing_admin"] = {
        "mode": "immowelt", "step": "clone_price_min",
        "user_id": source["user_id"], "title": source["title"],
        "districts_selected": list(source.get("districts") or []),
        "min_rooms": source.get("min_rooms"), "max_rooms": source.get("max_rooms"),
        "min_area_m2": source.get("min_area_m2"), "max_area_m2": source.get("max_area_m2"),
    }
    query.answer()
    query.edit_message_text(
        "Район, кімнати й площу перенесено з ProPotsdam-фільтра. Лишилось уточнити оренду — "
        "Immowelt рахує її як холодну (Kaltmiete), а ProPotsdam як повну (Gesamtmiete).\n\n"
        + IMMOWELT_CRITERIA_BY_KEY["min_price_eur"]["prompt"],
        parse_mode="HTML",
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
    lang = i18n.get_lang(int(state["user_id"]))
    heading = (
        i18n.t("housing.finalize.immowelt_updated", lang) if edit_filter_id
        else i18n.t("housing.finalize.immowelt_added", lang)
    )
    query.answer("Фільтр оновлено." if edit_filter_id else "Фільтр збережено.")
    text_out = i18n.t(
        "housing.finalize.summary_bold", lang, heading=heading, id=filter_id, criteria=_describe_criteria(criteria, lang),
    )
    rows = [[InlineKeyboardButton("⬅ До моніторингу", callback_data="housing:menu")]]
    if not edit_filter_id:
        _maybe_send_first_filter_congrats(context, state.get("user_id"))
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
    if step == "districts":
        update.message.reply_text(
            "Оберіть райони кнопками нижче і натисніть «Готово».",
            reply_markup=_immowelt_district_keyboard(state.get("districts_selected")),
        )
        return True
    if step == "preview":
        update.message.reply_text("Натисніть «Зберегти фільтр», «Назад» або «Скасувати».", reply_markup=_preview_keyboard())
        return True
    if step == "clone_price_min" or step == "clone_price_max":
        return _handle_immowelt_clone_price_step(update, context, state, step, text)
    if step in IMMOWELT_CRITERIA_BY_KEY:
        spec = IMMOWELT_CRITERIA_BY_KEY[step]
        value = _parse_single_number(text)
        if value is _INVALID_NUMBER:
            update.message.reply_text("Незрозуміле значення.\n\n" + spec["prompt"], parse_mode="HTML")
            return True
        if _violates_sibling_bound(state, step, value):
            update.message.reply_text(
                "Мінімум не може бути більшим за максимум. Надішліть значення ще раз.\n\n" + spec["prompt"], parse_mode="HTML"
            )
            return True
        state[step] = value
        index = IMMOWELT_CRITERIA_KEYS.index(step)
        if index < len(IMMOWELT_CRITERIA_KEYS) - 1:
            next_key = IMMOWELT_CRITERIA_KEYS[index + 1]
            state["step"] = next_key
            _reply_field_prompt(update.message, _field_prompt(state, IMMOWELT_CRITERIA_FIELDS, next_key, i18n.get_lang(update.effective_user.id)))
            return True
        _show_immowelt_preview(update.message, state)
        return True
    return False


def _handle_immowelt_clone_price_step(
    update: Update, context: CallbackContext, state: dict, step: str, text: str
) -> bool:
    """Два питання після клонування: район/кімнати/площу вже перенесено, лишилась ціна.

    Immowelt рахує холодну оренду (Kaltmiete), а джерело клонування —
    ProPotsdam — загальну (Gesamtmiete), тож перенести число напряму
    означало б інакшу умову. Питаємо наново, окремо від звичайної
    послідовності полів — вона пройшла б і кімнати з площею вдруге.
    """
    field = "min_price_eur" if step == "clone_price_min" else "max_price_eur"
    spec = IMMOWELT_CRITERIA_BY_KEY[field]
    value = _parse_single_number(text)
    if value is _INVALID_NUMBER:
        update.message.reply_text("Незрозуміле значення.\n\n" + spec["prompt"], parse_mode="HTML")
        return True
    if step == "clone_price_max" and _violates_sibling_bound(state, field, value):
        update.message.reply_text(
            "Мінімум не може бути більшим за максимум. Надішліть значення ще раз.\n\n" + spec["prompt"], parse_mode="HTML"
        )
        return True
    state[field] = value
    if step == "clone_price_min":
        state["step"] = "clone_price_max"
        price_fields = [IMMOWELT_CRITERIA_BY_KEY["min_price_eur"], IMMOWELT_CRITERIA_BY_KEY["max_price_eur"]]
        _reply_field_prompt(update.message, _field_prompt(state, price_fields, "max_price_eur", i18n.get_lang(update.effective_user.id)))
        return True
    _show_immowelt_preview(update.message, state)
    return True


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
    first_key = IMMOWELT_CRITERIA_KEYS[0]
    state["step"] = first_key
    query.answer()
    _edit_field_prompt(query, _field_prompt(state, IMMOWELT_CRITERIA_FIELDS, first_key, i18n.get_lang(update.effective_user.id)))


def _handle_propot_flow(update: Update, context: CallbackContext, state: dict, text: str) -> bool:
    step = state.get("step")
    if step == "districts":
        update.message.reply_text("Оберіть райони кнопками нижче і натисніть 'Готово'.", reply_markup=_district_keyboard(state.get("districts_selected")))
        return True
    if step == "clone_price_min" or step == "clone_price_max":
        return _handle_propot_clone_price_step(update, context, state, step, text)
    if step in PROPOT_CRITERIA_BY_KEY:
        spec = PROPOT_CRITERIA_BY_KEY[step]
        value = _parse_single_number(text)
        if value is _INVALID_NUMBER:
            update.message.reply_text("Незрозуміле значення.\n\n" + spec["prompt"], parse_mode="HTML")
            return True
        if _violates_sibling_bound(state, step, value):
            update.message.reply_text(
                "Мінімум не може бути більшим за максимум. Надішліть значення ще раз.\n\n" + spec["prompt"], parse_mode="HTML"
            )
            return True
        state[step] = value
        index = PROPOT_CRITERIA_KEYS.index(step)
        if index < len(PROPOT_CRITERIA_KEYS) - 1:
            next_key = PROPOT_CRITERIA_KEYS[index + 1]
            state["step"] = next_key
            _reply_field_prompt(update.message, _field_prompt(state, PROPOT_CRITERIA_FIELDS, next_key, i18n.get_lang(update.effective_user.id)))
            return True
        _finalize_propot_filter(update.message, int(update.effective_user.id), context, state)
        return True
    return False


def _handle_propot_clone_price_step(
    update: Update, context: CallbackContext, state: dict, step: str, text: str
) -> bool:
    """Два питання після клонування — див. пояснення в _handle_immowelt_clone_price_step."""
    field = "min_total_rent_eur" if step == "clone_price_min" else "max_total_rent_eur"
    spec = PROPOT_CRITERIA_BY_KEY[field]
    value = _parse_single_number(text)
    if value is _INVALID_NUMBER:
        update.message.reply_text("Незрозуміле значення.\n\n" + spec["prompt"], parse_mode="HTML")
        return True
    if step == "clone_price_max" and _violates_sibling_bound(state, field, value):
        update.message.reply_text(
            "Мінімум не може бути більшим за максимум. Надішліть значення ще раз.\n\n" + spec["prompt"], parse_mode="HTML"
        )
        return True
    state[field] = value
    if step == "clone_price_min":
        state["step"] = "clone_price_max"
        rent_fields = [PROPOT_CRITERIA_BY_KEY["min_total_rent_eur"], PROPOT_CRITERIA_BY_KEY["max_total_rent_eur"]]
        _reply_field_prompt(update.message, _field_prompt(state, rent_fields, "max_total_rent_eur", i18n.get_lang(update.effective_user.id)))
        return True
    _finalize_propot_filter(update.message, int(update.effective_user.id), context, state)
    return True


def _finalize_propot_filter(message, chatter_id: int, context: CallbackContext, state: dict) -> None:
    """Той самий майстер веде і редагування — відрізняє лише `edit_filter_id`."""
    edit_filter_id = state.get("edit_filter_id")
    state["title"] = _auto_title("propotsdam", {
        "districts": [d for d in str(state.get("districts") or "").split(",") if d],
        "min_price_eur": state.get("min_total_rent_eur"),
        "max_price_eur": state.get("max_total_rent_eur"),
        "min_rooms": state.get("min_rooms"),
        "max_rooms": state.get("max_rooms"),
        "min_area_m2": state.get("min_area_m2"),
        "max_area_m2": state.get("max_area_m2"),
    })
    common = dict(
        title=state["title"],
        districts=state.get("districts", ""),
        min_rooms=state.get("min_rooms"),
        max_rooms=state.get("max_rooms"),
        min_area_m2=state.get("min_area_m2"),
        max_area_m2=state.get("max_area_m2"),
        min_total_rent_eur=state.get("min_total_rent_eur"),
        max_total_rent_eur=state.get("max_total_rent_eur"),
    )
    if edit_filter_id:
        ok = propotsdam_store.update_filter(
            filter_id=int(edit_filter_id), user_id=int(state["user_id"]), **common
        )
        filter_id = int(edit_filter_id)
    else:
        filter_id = propotsdam_store.create_filter(user_id=state["user_id"], **common)
        ok = True
    _sync_propot_filters()
    context.user_data.pop("housing_admin", None)
    lang = i18n.get_lang(chatter_id)
    if not ok:
        message.reply_text("⚠️ Не вдалося зберегти фільтр — можливо, його вже видалено.")
        return
    heading = (
        i18n.t("housing.finalize.updated", lang, source="ProPotsdam") if edit_filter_id
        else i18n.t("housing.finalize.added", lang, source="ProPotsdam")
    )
    text_out = i18n.t("housing.finalize.propot_summary", lang, heading=heading, id=filter_id, user_id=state['user_id'])
    if edit_filter_id:
        message.reply_text(text_out)
        return
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
    _offer_recent_matches(context, [("propotsdam", filter_id)])
    _maybe_send_first_filter_congrats(context, state["user_id"])
    rows = list(_recent_offer_keyboard().inline_keyboard)
    if suggestion is not None:
        text_out += "\n\n💡 У вас ще немає фільтра Immowelt — можна завести такий самий."
        rows.insert(0, [suggestion])
    message.reply_text(text_out, reply_markup=InlineKeyboardMarkup(rows))


def handle_private_text(update: Update, context: CallbackContext) -> bool:
    if not update.message or not update.message.text or not update.effective_user:
        return False
    user_id = int(update.effective_user.id)
    if user_id != ADMIN_ID and not is_allowed(user_id):
        return False
    text = update.message.text.strip()
    if user_id != ADMIN_ID:
        lang = i18n.get_lang(user_id)
        if text == i18n.t("housing.btn.self_add", lang):
            start_self_add_flow(update, context)
            return True
        if text == i18n.t("housing.btn.self_manage", lang):
            show_self_manage(update, context)
            return True
    if text == BTN_ADMIN_ADD:
        if user_id != ADMIN_ID:
            return False
        start_admin_add_flow(update, context)
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
            target_id = access_state["user_id"]
            display_name = text[:120]
            context.user_data.pop("housing_access_admin", None)
            context.bot_data.setdefault("housing_access_names", {})[target_id] = display_name
            update.message.reply_text(
                f"На скільки місяців відкрити доступ?\n\nКористувач: {html.escape(display_name)}\n"
                f"Telegram ID: <code>{target_id}</code>",
                parse_mode="HTML",
                reply_markup=_access_months_keyboard(target_id),
            )
            return True
    state = context.user_data.get("housing_admin")
    if not state:
        return False
    if text == BTN_CANCEL:
        context.user_data.pop("housing_admin", None)
        update.message.reply_text("Скасовано.")
        return True
    if state.get("step") == "admin_target_user_id":
        if not text.lstrip("-").isdigit():
            update.message.reply_text("Telegram ID має бути числом. Надішліть ID ще раз.")
            return True
        state["user_id"] = int(text)
        state["step"] = "sources"
        update.message.reply_text(
            _sources_text([]), parse_mode="HTML", reply_markup=_sources_keyboard([]),
        )
        return True
    if state.get("step") == "sources":
        update.message.reply_text(
            "Оберіть портали кнопками нижче і натисніть «Далі».",
            reply_markup=_sources_keyboard(state.get("sources_selected")),
        )
        return True
    if state.get("mode") == "propotsdam":
        return _handle_propot_flow(update, context, state, text)
    if state.get("mode") == "semmelhaack":
        return _handle_semmelhaack_flow(update, context, state, text)
    if state.get("mode") == "schoba":
        return _handle_schoba_flow(update, context, state, text)
    if state.get("mode") == "regiomakler":
        return _handle_regiomakler_flow(update, context, state, text)
    if state.get("mode") == "kleinanzeigen":
        return _handle_kleinanzeigen_flow(update, context, state, text)
    if state.get("mode") == "locals":
        return _handle_locals_flow(update, context, state, text)
    if state.get("mode") == "karlmarx":
        return _handle_karlmarx_flow(update, context, state, text)
    if state.get("mode") == "multi":
        return _handle_multi_flow(update, context, state, text)
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
    first_key = PROPOT_CRITERIA_KEYS[0]
    state["step"] = first_key
    query.answer()
    _edit_field_prompt(query, _field_prompt(state, PROPOT_CRITERIA_FIELDS, first_key, i18n.get_lang(update.effective_user.id)))


def _toggle_source(update: Update, context: CallbackContext, source_key: str) -> None:
    query = update.callback_query
    state = context.user_data.get("housing_admin") or {}
    if state.get("step") != "sources" or source_key not in AVAILABLE_SOURCE_KEYS:
        query.answer()
        return
    selected = list(state.get("sources_selected") or [])
    if source_key in selected:
        selected.remove(source_key)
    else:
        selected.append(source_key)
    state["sources_selected"] = selected
    query.answer()
    query.edit_message_text(_sources_text(selected), parse_mode="HTML", reply_markup=_sources_keyboard(selected))


def _multi_district_keyboard(state: dict, selected) -> InlineKeyboardMarkup:
    selected = set(selected or [])
    districts = _canonical_districts(state.get("sources_selected") or [])
    rows = []
    for district in districts:
        mark = "✅" if district in selected else "☐"
        rows.append([InlineKeyboardButton(f"{mark} {district}", callback_data=f"housing:multi_district:{district}")])
    rows.append([InlineKeyboardButton("✅ Готово", callback_data="housing:multi_district_done")])
    rows.append([InlineKeyboardButton("🌍 Усі райони", callback_data="housing:multi_district_all")])
    rows.append([InlineKeyboardButton(BTN_CANCEL, callback_data="housing:multi_cancel")])
    return InlineKeyboardMarkup(rows)


def _multi_district_text(selected) -> str:
    selected = selected or []
    suffix = ", ".join(selected) if selected else "усі райони"
    return f"🏙 <b>Райони</b>\n\nОберіть райони галочками.\nПоточний вибір: <b>{html.escape(suffix)}</b>"


def _finish_sources(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    state = context.user_data.get("housing_admin") or {}
    if state.get("step") != "sources":
        query.answer()
        return
    selected = list(state.get("sources_selected") or [])
    if not selected:
        query.answer("Оберіть хоча б один портал.", show_alert=True)
        return
    state["mode"] = "multi"
    state["districts_selected"] = []
    query.answer()
    if any(source in DISTRICT_AWARE_SOURCES for source in selected):
        state["step"] = "districts"
        query.edit_message_text(
            _multi_district_text([]), parse_mode="HTML", reply_markup=_multi_district_keyboard(state, [])
        )
        return
    # Жодне обране джерело не знає районів (лише SEMMELHAACK) — крок вибору
    # району тут нема сенсу показувати, він однаково нічого не відфільтрує.
    first_key = SHARED_CRITERIA_KEYS[0]
    state["step"] = first_key
    _edit_field_prompt(query, _field_prompt(state, SHARED_CRITERIA_FIELDS, first_key, i18n.get_lang(update.effective_user.id)))


def _toggle_multi_district(update: Update, context: CallbackContext, district: str) -> None:
    query = update.callback_query
    state = context.user_data.get("housing_admin") or {}
    if state.get("mode") != "multi" or state.get("step") != "districts":
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
        _multi_district_text(selected), parse_mode="HTML", reply_markup=_multi_district_keyboard(state, selected)
    )


def _finish_multi_districts(update: Update, context: CallbackContext, all_districts: bool = False) -> None:
    query = update.callback_query
    state = context.user_data.get("housing_admin") or {}
    if state.get("mode") != "multi" or state.get("step") != "districts":
        query.answer()
        return
    if all_districts:
        state["districts_selected"] = []
    state["step"] = SHARED_CRITERIA_KEYS[0]
    query.answer()
    _edit_field_prompt(query, _field_prompt(state, SHARED_CRITERIA_FIELDS, SHARED_CRITERIA_KEYS[0], i18n.get_lang(update.effective_user.id)))


def _finalize_multi_filter(message, context: CallbackContext, state: dict) -> None:
    """Зберігає фільтр одразу в усіх обраних джерелах — окремим записом у кожному.

    Immowelt і ProPotsdam зберігають фільтри у різних сховищах (перший — через
    HTTP-приймач check-Wohnung, другий — у власній таблиці бота), тож єдиного
    «мультипортального» запису нема: район/кімнати/площу користувач ввів раз,
    а зберігаємо це двома незалежними фільтрами, кожен зі своєю оренду.
    """
    sources = state.get("sources_selected") or []
    districts = list(state.get("districts_selected") or [])
    results = []
    if "immowelt" in sources:
        criteria = {
            "districts": districts,
            "min_price_eur": state.get("min_price_eur"),
            "max_price_eur": state.get("max_price_eur"),
            "min_rooms": state.get("min_rooms"),
            "max_rooms": state.get("max_rooms"),
            "min_area_m2": state.get("min_area_m2"),
            "max_area_m2": state.get("max_area_m2"),
        }
        title = _auto_title("immowelt", criteria)
        try:
            payload = _request("POST", "/api/housing/filters", json={
                "user_id": state["user_id"], "title": title, **criteria,
            })
            results.append(("immowelt", payload.get("filter_id"), criteria, None))
        except Exception as exc:
            logger.exception("Could not save Immowelt filter from the multi-source wizard")
            results.append(("immowelt", None, criteria, str(exc)))
    if "propotsdam" in sources:
        propot_districts = (
            _translate_districts(districts, IMMOWELT_TO_PROPOT_DISTRICT, set(PROPOT_DISTRICTS))
            if "immowelt" in sources else districts
        )
        criteria = {
            "districts": propot_districts,
            "min_price_eur": state.get("min_total_rent_eur"),
            "max_price_eur": state.get("max_total_rent_eur"),
            "min_rooms": state.get("min_rooms"),
            "max_rooms": state.get("max_rooms"),
            "min_area_m2": state.get("min_area_m2"),
            "max_area_m2": state.get("max_area_m2"),
        }
        title = _auto_title("propotsdam", criteria)
        filter_id = propotsdam_store.create_filter(
            user_id=state["user_id"], title=title,
            districts=propotsdam_store.normalize_districts(",".join(propot_districts)),
            min_rooms=state.get("min_rooms"), max_rooms=state.get("max_rooms"),
            min_area_m2=state.get("min_area_m2"), max_area_m2=state.get("max_area_m2"),
            min_total_rent_eur=state.get("min_total_rent_eur"), max_total_rent_eur=state.get("max_total_rent_eur"),
        )
        _sync_propot_filters()
        results.append(("propotsdam", filter_id, criteria, None))
    if "semmelhaack" in sources:
        # Без районів — фільтр тут лише кімнати/площа/ціна, яку могли вже
        # запитати спільно з Immowelt (обидва рахують Kaltmiete).
        criteria = {
            "min_price_eur": state.get("min_price_eur"),
            "max_price_eur": state.get("max_price_eur"),
            "min_rooms": state.get("min_rooms"),
            "max_rooms": state.get("max_rooms"),
            "min_area_m2": state.get("min_area_m2"),
            "max_area_m2": state.get("max_area_m2"),
        }
        title = _auto_title("semmelhaack", criteria)
        filter_id = semmelhaack_store.create_filter(
            user_id=state["user_id"], title=title,
            min_rooms=state.get("min_rooms"), max_rooms=state.get("max_rooms"),
            min_area_m2=state.get("min_area_m2"), max_area_m2=state.get("max_area_m2"),
            min_price_eur=state.get("min_price_eur"), max_price_eur=state.get("max_price_eur"),
        )
        results.append(("semmelhaack", filter_id, criteria, None))
    if "schoba" in sources:
        # Так само без районів; ціна теж холодна оренда — могла піти в те
        # саме спільне запитання, що й Immowelt/SEMMELHAACK.
        criteria = {
            "min_price_eur": state.get("min_price_eur"),
            "max_price_eur": state.get("max_price_eur"),
            "min_rooms": state.get("min_rooms"),
            "max_rooms": state.get("max_rooms"),
            "min_area_m2": state.get("min_area_m2"),
            "max_area_m2": state.get("max_area_m2"),
        }
        title = _auto_title("schoba", criteria)
        filter_id = schoba_store.create_filter(
            user_id=state["user_id"], title=title,
            min_rooms=state.get("min_rooms"), max_rooms=state.get("max_rooms"),
            min_area_m2=state.get("min_area_m2"), max_area_m2=state.get("max_area_m2"),
            min_price_eur=state.get("min_price_eur"), max_price_eur=state.get("max_price_eur"),
        )
        results.append(("schoba", filter_id, criteria, None))
    if "regiomakler" in sources:
        # Так само без районів; ціна теж холодна оренда (Kaltmiete).
        criteria = {
            "min_price_eur": state.get("min_price_eur"),
            "max_price_eur": state.get("max_price_eur"),
            "min_rooms": state.get("min_rooms"),
            "max_rooms": state.get("max_rooms"),
            "min_area_m2": state.get("min_area_m2"),
            "max_area_m2": state.get("max_area_m2"),
        }
        title = _auto_title("regiomakler", criteria)
        filter_id = regiomakler_store.create_filter(
            user_id=state["user_id"], title=title,
            min_rooms=state.get("min_rooms"), max_rooms=state.get("max_rooms"),
            min_area_m2=state.get("min_area_m2"), max_area_m2=state.get("max_area_m2"),
            min_price_eur=state.get("min_price_eur"), max_price_eur=state.get("max_price_eur"),
        )
        results.append(("regiomakler", filter_id, criteria, None))
    if "locals" in sources:
        # Так само без районів; ціна теж холодна оренда (Kaltmiete).
        criteria = {
            "min_price_eur": state.get("min_price_eur"),
            "max_price_eur": state.get("max_price_eur"),
            "min_rooms": state.get("min_rooms"),
            "max_rooms": state.get("max_rooms"),
            "min_area_m2": state.get("min_area_m2"),
            "max_area_m2": state.get("max_area_m2"),
        }
        title = _auto_title("locals", criteria)
        filter_id = locals_store.create_filter(
            user_id=state["user_id"], title=title,
            min_rooms=state.get("min_rooms"), max_rooms=state.get("max_rooms"),
            min_area_m2=state.get("min_area_m2"), max_area_m2=state.get("max_area_m2"),
            min_price_eur=state.get("min_price_eur"), max_price_eur=state.get("max_price_eur"),
        )
        results.append(("locals", filter_id, criteria, None))
    if "kleinanzeigen" in sources:
        # Власні ключі ціни (min_ka_price_eur), не спільне запитання Kaltmiete.
        criteria = {
            "min_price_eur": state.get("min_ka_price_eur"),
            "max_price_eur": state.get("max_ka_price_eur"),
            "min_rooms": state.get("min_rooms"),
            "max_rooms": state.get("max_rooms"),
            "min_area_m2": state.get("min_area_m2"),
            "max_area_m2": state.get("max_area_m2"),
        }
        title = _auto_title("kleinanzeigen", criteria)
        filter_id = kleinanzeigen_store.create_filter(
            user_id=state["user_id"], title=title,
            min_rooms=state.get("min_rooms"), max_rooms=state.get("max_rooms"),
            min_area_m2=state.get("min_area_m2"), max_area_m2=state.get("max_area_m2"),
            min_price_eur=state.get("min_ka_price_eur"), max_price_eur=state.get("max_ka_price_eur"),
        )
        results.append(("kleinanzeigen", filter_id, criteria, None))
    if "karlmarx" in sources:
        # Власні ключі ціни (min_km_price_eur) — Warmmiete, не Kaltmiete.
        criteria = {
            "min_price_eur": state.get("min_km_price_eur"),
            "max_price_eur": state.get("max_km_price_eur"),
            "min_rooms": state.get("min_rooms"),
            "max_rooms": state.get("max_rooms"),
            "min_area_m2": state.get("min_area_m2"),
            "max_area_m2": state.get("max_area_m2"),
        }
        title = _auto_title("karlmarx", criteria)
        filter_id = karlmarx_store.create_filter(
            user_id=state["user_id"], title=title,
            min_rooms=state.get("min_rooms"), max_rooms=state.get("max_rooms"),
            min_area_m2=state.get("min_area_m2"), max_area_m2=state.get("max_area_m2"),
            min_price_eur=state.get("min_km_price_eur"), max_price_eur=state.get("max_km_price_eur"),
        )
        results.append(("karlmarx", filter_id, criteria, None))
    context.user_data.pop("housing_admin", None)
    lang = i18n.get_lang(int(state["user_id"]))
    lines = [i18n.t("housing.finalize.multi_title", lang), ""]
    for source, filter_id, criteria, error in results:
        icon = SOURCE_ICON[source]
        label = SOURCE_LABEL[source]
        if error:
            lines.append(i18n.t("housing.finalize.multi_error", lang, icon=icon, label=label, error=html.escape(error)))
        else:
            lines.append(i18n.t(
                "housing.finalize.multi_line", lang, icon=icon, label=label, id=filter_id,
                criteria=_describe_criteria(criteria, lang),
            ))
    created = [
        (source, filter_id) for source, filter_id, _criteria, error in results
        if not error and source in _LOCAL_SOURCE_MODULES
    ]
    rows = [[InlineKeyboardButton("⬅ До моніторингу", callback_data="housing:menu")]]
    if created:
        _offer_recent_matches(context, created)
        rows = list(_recent_offer_keyboard().inline_keyboard) + rows
    if any(not error for _source, _filter_id, _criteria, error in results):
        _maybe_send_first_filter_congrats(context, state["user_id"])
    message.reply_text(
        "\n".join(lines), parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(rows),
    )


def _step_back(update: Update, context: CallbackContext) -> None:
    """Одна кнопка «⬅ Назад» на всі майстри числових питань.

    Куди саме веде «назад» — вирішує сам обробник, а не сам callback: крок
    завжди один і той самий (попереднє поле того самого списку, або, для
    першого поля списку, попередній екран — райони чи портали).
    """
    query = update.callback_query
    state = context.user_data.get("housing_admin") or {}
    mode = state.get("mode")
    step = state.get("step")
    query.answer()
    lang = i18n.get_lang(update.effective_user.id)

    if mode == "immowelt" and step in IMMOWELT_CRITERIA_KEYS:
        idx = IMMOWELT_CRITERIA_KEYS.index(step)
        if idx == 0:
            state["step"] = "districts"
            query.edit_message_text(
                _immowelt_district_text(state.get("districts_selected")), parse_mode="HTML",
                reply_markup=_immowelt_district_keyboard(state.get("districts_selected")),
            )
            return
        prev_key = IMMOWELT_CRITERIA_KEYS[idx - 1]
        state["step"] = prev_key
        _edit_field_prompt(query, _field_prompt(state, IMMOWELT_CRITERIA_FIELDS, prev_key, lang))
        return
    if mode == "propotsdam" and step in PROPOT_CRITERIA_KEYS:
        idx = PROPOT_CRITERIA_KEYS.index(step)
        if idx == 0:
            state["step"] = "districts"
            query.edit_message_text(
                _district_text(state.get("districts_selected")), parse_mode="HTML",
                reply_markup=_district_keyboard(state.get("districts_selected")),
            )
            return
        prev_key = PROPOT_CRITERIA_KEYS[idx - 1]
        state["step"] = prev_key
        _edit_field_prompt(query, _field_prompt(state, PROPOT_CRITERIA_FIELDS, prev_key, lang))
        return
    if mode == "semmelhaack" and step in SEMM_CRITERIA_KEYS:
        idx = SEMM_CRITERIA_KEYS.index(step)
        if idx == 0:
            # Перше поле цього майстра — редагування вже відкрите на
            # конкретному фільтрі, повертатись до вибору джерел нема куди.
            return
        prev_key = SEMM_CRITERIA_KEYS[idx - 1]
        state["step"] = prev_key
        _edit_field_prompt(query, _field_prompt(state, SEMM_CRITERIA_FIELDS, prev_key, lang))
        return
    if mode == "schoba" and step in SCHOBA_CRITERIA_KEYS:
        idx = SCHOBA_CRITERIA_KEYS.index(step)
        if idx == 0:
            return
        prev_key = SCHOBA_CRITERIA_KEYS[idx - 1]
        state["step"] = prev_key
        _edit_field_prompt(query, _field_prompt(state, SCHOBA_CRITERIA_FIELDS, prev_key, lang))
        return
    if mode == "regiomakler" and step in REGIOMAKLER_CRITERIA_KEYS:
        idx = REGIOMAKLER_CRITERIA_KEYS.index(step)
        if idx == 0:
            return
        prev_key = REGIOMAKLER_CRITERIA_KEYS[idx - 1]
        state["step"] = prev_key
        _edit_field_prompt(query, _field_prompt(state, REGIOMAKLER_CRITERIA_FIELDS, prev_key, lang))
        return
    if mode == "kleinanzeigen" and step in KLEINANZEIGEN_CRITERIA_KEYS:
        idx = KLEINANZEIGEN_CRITERIA_KEYS.index(step)
        if idx == 0:
            return
        prev_key = KLEINANZEIGEN_CRITERIA_KEYS[idx - 1]
        state["step"] = prev_key
        _edit_field_prompt(query, _field_prompt(state, KLEINANZEIGEN_CRITERIA_FIELDS, prev_key, lang))
        return
    if mode == "locals" and step in LOCALS_CRITERIA_KEYS:
        idx = LOCALS_CRITERIA_KEYS.index(step)
        if idx == 0:
            return
        prev_key = LOCALS_CRITERIA_KEYS[idx - 1]
        state["step"] = prev_key
        _edit_field_prompt(query, _field_prompt(state, LOCALS_CRITERIA_FIELDS, prev_key, lang))
        return
    if mode == "karlmarx" and step in KARLMARX_CRITERIA_KEYS:
        idx = KARLMARX_CRITERIA_KEYS.index(step)
        if idx == 0:
            return
        prev_key = KARLMARX_CRITERIA_KEYS[idx - 1]
        state["step"] = prev_key
        _edit_field_prompt(query, _field_prompt(state, KARLMARX_CRITERIA_FIELDS, prev_key, lang))
        return
    if mode in ("immowelt", "propotsdam") and step in ("clone_price_min", "clone_price_max"):
        # Клон переносить район/кімнати/площу без питань — до них нема куди
        # повертатись, лишились тільки самі два питання про оренду.
        if step == "clone_price_max":
            state["step"] = "clone_price_min"
            fields = (
                [IMMOWELT_CRITERIA_BY_KEY["min_price_eur"]] if mode == "immowelt"
                else [PROPOT_CRITERIA_BY_KEY["min_total_rent_eur"]]
            )
            query.edit_message_text(_field_prompt(state, fields, fields[0]["key"], lang), parse_mode="HTML")
        return
    if mode == "multi":
        if step in SHARED_CRITERIA_KEYS:
            idx = SHARED_CRITERIA_KEYS.index(step)
            if idx == 0:
                sources = state.get("sources_selected") or []
                if any(source in DISTRICT_AWARE_SOURCES for source in sources):
                    state["step"] = "districts"
                    query.edit_message_text(
                        _multi_district_text(state.get("districts_selected")), parse_mode="HTML",
                        reply_markup=_multi_district_keyboard(state, state.get("districts_selected")),
                    )
                else:
                    state["step"] = "sources"
                    query.edit_message_text(
                        _sources_text(state.get("sources_selected")), parse_mode="HTML",
                        reply_markup=_sources_keyboard(state.get("sources_selected")),
                    )
                return
            prev_key = SHARED_CRITERIA_KEYS[idx - 1]
            state["step"] = prev_key
            _edit_field_prompt(query, _field_prompt(state, SHARED_CRITERIA_FIELDS, prev_key, lang))
            return
        if step in PRICE_STEP_PROMPTS:
            price_steps = state.get("_price_steps") or []
            idx = price_steps.index(step) if step in price_steps else 0
            price_fields = SHARED_CRITERIA_FIELDS + [PRICE_STEP_FIELDS[key] for key in price_steps]
            if idx == 0:
                prev_key = SHARED_CRITERIA_KEYS[-1]
                state["step"] = prev_key
                _edit_field_prompt(query, _field_prompt(state, SHARED_CRITERIA_FIELDS, prev_key, lang))
                return
            prev_key = price_steps[idx - 1]
            state["step"] = prev_key
            _edit_field_prompt(query, _field_prompt(state, price_fields, prev_key, lang))
            return
        if step == "districts":
            state["step"] = "sources"
            query.edit_message_text(
                _sources_text(state.get("sources_selected")), parse_mode="HTML",
                reply_markup=_sources_keyboard(state.get("sources_selected")),
            )
            return


def _handle_multi_flow(update: Update, context: CallbackContext, state: dict, text: str) -> bool:
    step = state.get("step")
    if step == "districts":
        update.message.reply_text(
            "Оберіть райони кнопками нижче і натисніть «Готово».",
            reply_markup=_multi_district_keyboard(state, state.get("districts_selected")),
        )
        return True
    if step in SHARED_CRITERIA_BY_KEY:
        spec = SHARED_CRITERIA_BY_KEY[step]
        value = _parse_single_number(text)
        if value is _INVALID_NUMBER:
            update.message.reply_text("Незрозуміле значення.\n\n" + spec["prompt"], parse_mode="HTML")
            return True
        if _violates_sibling_bound(state, step, value):
            update.message.reply_text(
                "Мінімум не може бути більшим за максимум. Надішліть значення ще раз.\n\n" + spec["prompt"], parse_mode="HTML"
            )
            return True
        state[step] = value
        index = SHARED_CRITERIA_KEYS.index(step)
        if index < len(SHARED_CRITERIA_KEYS) - 1:
            next_key = SHARED_CRITERIA_KEYS[index + 1]
            state["step"] = next_key
            _reply_field_prompt(update.message, _field_prompt(state, SHARED_CRITERIA_FIELDS, next_key, i18n.get_lang(update.effective_user.id)))
            return True
        price_steps = _price_steps_for(state.get("sources_selected") or [])
        if not price_steps:
            _finalize_multi_filter(update.message, context, state)
            return True
        state["_price_steps"] = price_steps
        state["step"] = price_steps[0]
        price_fields = SHARED_CRITERIA_FIELDS + [PRICE_STEP_FIELDS[key] for key in price_steps]
        _reply_field_prompt(update.message, _field_prompt(state, price_fields, price_steps[0], i18n.get_lang(update.effective_user.id)))
        return True
    if step in PRICE_STEP_PROMPTS:
        value = _parse_single_number(text)
        if value is _INVALID_NUMBER:
            update.message.reply_text("Незрозуміле значення.\n\n" + PRICE_STEP_PROMPTS[step], parse_mode="HTML")
            return True
        if _violates_sibling_bound(state, step, value):
            update.message.reply_text(
                "Мінімум не може бути більшим за максимум. Надішліть значення ще раз.\n\n" + PRICE_STEP_PROMPTS[step], parse_mode="HTML"
            )
            return True
        state[step] = value
        price_steps = state.get("_price_steps") or []
        idx = price_steps.index(step)
        if idx < len(price_steps) - 1:
            next_step = price_steps[idx + 1]
            state["step"] = next_step
            price_fields = SHARED_CRITERIA_FIELDS + [PRICE_STEP_FIELDS[key] for key in price_steps]
            _reply_field_prompt(update.message, _field_prompt(state, price_fields, next_step, i18n.get_lang(update.effective_user.id)))
            return True
        _finalize_multi_filter(update.message, context, state)
        return True
    return False


def handle_callback(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    if not query or not query.data:
        return
    if query.data == "housing:menu":
        query.answer()
        show_menu(update, context, edit=True)
    elif query.data == "housing:lang:menu":
        query.answer()
        query.edit_message_text(
            "🌐 Оберіть мову / Выберите язык / Sprache wählen",
            reply_markup=_lang_picker_keyboard(),
        )
    elif query.data.startswith("housing:lang:set:"):
        lang = query.data.split(":")[3]
        if lang in i18n.SUPPORTED_LANGS:
            user_settings_store.set_language(update.effective_user.id, lang)
            query.answer(i18n.LANG_LABELS[lang])
        else:
            query.answer()
        show_menu(update, context, edit=True)
    elif query.data == "housing:admin":
        query.answer()
        show_admin(update, context, edit=True)
    elif query.data == "housing:add":
        query.answer()
        start_admin_add_flow(update, context, edit=True)
    elif query.data == "housing:faq":
        query.answer()
        show_faq(update, context, edit=True)
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
    elif query.data.startswith("housing:access_delete_confirm:"):
        target_id = int(query.data.split(":")[2])
        confirm_access_delete(update, context, target_id)
    elif query.data.startswith("housing:access_delete:"):
        target_id = int(query.data.split(":")[2])
        start_access_delete_flow(update, context, target_id)
    elif query.data.startswith("housing:access_months:"):
        _finalize_access_grant(update, context)
    elif query.data.startswith("housing:access_renew:"):
        _start_access_renewal(update, context)
    elif query.data.startswith("housing:access_continue:"):
        _handle_access_continue(update, context)
    elif query.data.startswith("housing:access_stop:"):
        _handle_access_stop(update, context)
    elif query.data.startswith("housing:recent:"):
        raw_hours = query.data.split(":", 2)[2]
        if raw_hours.isdigit():
            _send_recent_matches(update, context, int(raw_hours))
        else:
            query.answer()
    elif query.data == "housing:recent_skip":
        query.answer()
        context.user_data.pop("recent_offer_filters", None)
        _clear_recent_offer_keyboard(query)
    elif query.data.startswith("housing:imm_district:"):
        _toggle_immowelt_district(update, context, query.data.split(":", 2)[2])
    elif query.data == "housing:imm_district_done":
        _finish_immowelt_districts(update, context)
    elif query.data == "housing:imm_district_all":
        _finish_immowelt_districts(update, context, all_districts=True)
    elif query.data == "housing:imm_save":
        _save_immowelt_filter(update, context)
    elif query.data == "housing:imm_back":
        _back_from_immowelt_preview(update, context)
    elif query.data == "housing:imm_cancel":
        context.user_data.pop("housing_admin", None)
        query.answer()
        query.edit_message_text(
            "Скасовано.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅ До моніторингу", callback_data="housing:menu")]]),
        )
    elif query.data.startswith("housing:propot_district:"):
        _toggle_district(update, context, query.data.split(":", 2)[2])
    elif query.data == "housing:propot_district_done":
        _finish_districts(update, context)
    elif query.data == "housing:propot_district_all":
        _finish_districts(update, context, all_districts=True)
    elif query.data == "housing:propot_cancel":
        context.user_data.pop("housing_admin", None)
        query.answer()
        query.edit_message_text(
            "Скасовано.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅ До моніторингу", callback_data="housing:menu")]]),
        )
    elif query.data == BACK_CALLBACK:
        _step_back(update, context)
    elif query.data == "housing:self_add":
        query.answer()
        start_self_add_flow(update, context, edit=True)
    elif query.data.startswith("housing:src:"):
        _toggle_source(update, context, query.data.split(":", 2)[2])
    elif query.data == "housing:src_done":
        _finish_sources(update, context)
    elif query.data == "housing:src_cancel":
        context.user_data.pop("housing_admin", None)
        query.answer()
        query.edit_message_text(
            "Скасовано.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅ До моніторингу", callback_data="housing:menu")]]),
        )
    elif query.data.startswith("housing:multi_district:"):
        _toggle_multi_district(update, context, query.data.split(":", 2)[2])
    elif query.data == "housing:multi_district_done":
        _finish_multi_districts(update, context)
    elif query.data == "housing:multi_district_all":
        _finish_multi_districts(update, context, all_districts=True)
    elif query.data == "housing:multi_cancel":
        context.user_data.pop("housing_admin", None)
        query.answer()
        query.edit_message_text(
            "Скасовано.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅ До моніторингу", callback_data="housing:menu")]]),
        )
    elif query.data == "housing:clone_propot":
        _clone_propot_from_immowelt(update, context)
    elif query.data == "housing:clone_immo":
        _clone_immowelt_from_propot(update, context)
    elif query.data == "housing:self_manage":
        query.answer()
        show_self_manage(update, context, edit=True)
    elif query.data == "housing:current_matches":
        show_current_matches(update, context)
    elif query.data == "housing:coops":
        show_coop_subscriptions(update, context, edit=True)
    elif query.data.startswith("housing:coop_toggle:"):
        _toggle_coop_subscription(update, context)
    elif query.data == "housing:noop":
        query.answer()
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
        if not raw_id.isdigit():
            query.answer()
        elif source == "immowelt":
            start_edit_flow(update, context, int(raw_id))
        elif source == "propotsdam":
            start_propot_edit_flow(update, context, int(raw_id))
        elif source == "semmelhaack":
            start_semmelhaack_edit_flow(update, context, int(raw_id))
        elif source == "schoba":
            start_schoba_edit_flow(update, context, int(raw_id))
        elif source == "regiomakler":
            start_regiomakler_edit_flow(update, context, int(raw_id))
        elif source == "kleinanzeigen":
            start_kleinanzeigen_edit_flow(update, context, int(raw_id))
        elif source == "locals":
            start_locals_edit_flow(update, context, int(raw_id))
        elif source == "karlmarx":
            start_karlmarx_edit_flow(update, context, int(raw_id))
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
    _maybe_send_first_filter_congrats(context, user_id)


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
