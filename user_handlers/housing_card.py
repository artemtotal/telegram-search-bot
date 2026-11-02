"""Картка фільтра житла (27.10.2026), за зразком берлінського бота XATA.

Замість майстра «портали → райони → тип → Jobcenter → про що запитати →
питання по черзі» людина бачить одне повідомлення з усіма умовами й кнопками
з поточними значеннями. Тап по кнопці відкриває вибір значення (кнопки або
своє число в чат), і зміна одразу зберігається - без «Готово» в кінці.

Під капотом це той самий пошук (housing_search) із записами на кожній
площадці: картка оновлює їх на місці через update_filter / PATCH приймача,
не пересоздаючи. Тож старі фільтри відкриваються в картці без міграції.

Новий фільтр - одне питання (Jobcenter, скільки осіб), далі картка. Він
створюється на паузі: людина перевіряє умови й тисне «▶️ Почати пошук»,
інакше фільтр без жодної межі одразу засипав би її всіма квартирами міста.

Місце (02.11.2026, етап 1 «Берлін і Бранденбург», docs/housing-region-plan.md):
рядок «📍 Де» — місто чи індекс і радіус (`criteria["area"]`, housing_area).
Новий фільтр спершу питає місце, Jobcenter — для Потсдама й Берліна (етап 5,
jobcenter_tables); в округах Бранденбургу таблиць поки немає. Поза Потсдамом працюють площадки з REGION_SOURCES, а коли в
зону потрапляє частина Потсдама — ще й потсдамські агентства (їхні квартири
звіряються за індексом, етап 2). Immowelt (етап 3) отримує місце в тілі
запиту до check-Wohnung і шукає там само. Що людина сама вимкнула,
пошук пам'ятає в `criteria["sources_off"]` і повертає, щойно місце знову Потсдам. Поки — лише для адміна, усім —
HOUSING_PLACE_FOR_ALL=1.

WBS (02.11.2026): рядок «🪪 WBS» і питання в новому фільтрі (після Jobcenter) —
`criteria["wbs"]`, див. housing_wbs. Видно разом із місцем (place_enabled).
"""

import html
import logging
import os
from typing import Dict, List, Optional, Tuple

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackContext, CallbackQueryHandler

import i18n
from user_handlers import housing_monitor as hm
from user_jobs import (
    coop_offers, housing_area, housing_journey_store, housing_search_store, housing_wbs, jobcenter_tables,
    propotsdam_store,
)

logger = logging.getLogger(__name__)

PREFIX = "hcard:"
FIELDS = ("max_price_eur", "max_price_warm_eur", "min_rooms", "max_rooms", "min_area_m2", "max_area_m2")
PRESETS = {
    "max_price_eur": (500, 600, 700, 800, 900, 1000, 1200, 1500, 2000),
    "max_price_warm_eur": (700, 800, 900, 1000, 1100, 1200, 1400, 1600, 2000),
    "min_rooms": (1, 2, 3, 4, 5),
    "max_rooms": (1, 2, 3, 4, 5, 6),
    "min_area_m2": (30, 40, 50, 60, 70, 80),
    "max_area_m2": (50, 60, 70, 80, 90, 100, 120),
}
ICONS = {
    "max_price_eur": "❄️", "max_price_warm_eur": "🔥", "min_rooms": "🛏", "max_rooms": "🛏",
    "min_area_m2": "📐", "max_area_m2": "📐",
}
UNITS = {"max_price_eur": "€", "max_price_warm_eur": "€", "min_area_m2": "м²", "max_area_m2": "м²"}
# Рядок картки, на який вказує 👉, коли людина змінює поле.
FIELD_LINE = {
    "max_price_eur": "kalt", "max_price_warm_eur": "warm", "min_rooms": "rooms", "max_rooms": "rooms",
    "min_area_m2": "area", "max_area_m2": "area",
}
OTHER_END = {"min_rooms": "max_rooms", "max_rooms": "min_rooms", "min_area_m2": "max_area_m2", "max_area_m2": "min_area_m2"}
# Нижня межа ціни відсікала дешеві квартири, яких ніхто не просив прибирати
# (див. CRITERIA_PICKER_OPTIONS у housing_monitor): картка ставить лише
# «до», а стару «від» знімає, щойно людина змінює цю ціну.
PRICE_MIN_OF = {"max_price_eur": "min_price_eur", "max_price_warm_eur": "min_price_warm_eur"}
MAX_VALUE = {"min_rooms": 20, "max_rooms": 20, "min_area_m2": 1000, "max_area_m2": 1000}
JOBCENTER_CHOICES = (1, 2, 3, 4, 5, 6)
# Площадки, що шукають і поза Потсдамом. Решта поки збирає лише Потсдам.
# Immowelt — з етапу 3 (02.11.2026): check-Wohnung читає Берлін і Бранденбург
# без браузера й звіряє місце фільтра (копія housing_area там).
REGION_SOURCES = {"immoscout", "kleinanzeigen", "vonovia", "immowelt", "ohnemakler"}
# Площадки, що не знають місця. Поки таких немає.
POTSDAM_ONLY_SOURCES: set = set()
# Лише Берлін: міські компанії з inberlinwohnen.de (етап 8).
BERLIN_ONLY_SOURCES = {"inberlin"}
PLACE_FOR_ALL = os.getenv("HOUSING_PLACE_FOR_ALL", "0") == "1"


def place_enabled(user_id: int) -> bool:
    return PLACE_FOR_ALL or int(user_id) == hm.ADMIN_ID


def covering_sources(area: Optional[Dict]) -> List[str]:
    """Площадки, які шукають у цьому місці."""
    result = []
    for source in hm.AVAILABLE_SOURCE_KEYS:
        if source in BERLIN_ONLY_SOURCES:
            works = housing_area.touches_berlin(area)
        elif source in REGION_SOURCES:
            works = True
        elif source in POTSDAM_ONLY_SOURCES:
            works = housing_area.covers_potsdam(area)
        else:  # потсдамські агентства: звіряють індекс, тож досить частини Потсдама
            works = housing_area.touches_potsdam(area)
        if works:
            result.append(source)
    return result


def jobcenter_for(place: Optional[Dict], persons: int) -> Optional[Tuple[float, float]]:
    """(холодна, тепла) межа Jobcenter для місця; None — таблиці для нього немає.

    Потсдам — потсдамська таблиця (і з радіусом: людина живе в Потсдамі),
    Берлін — AV-Wohnen (етап 5), округи Бранденбургу — поки ні."""
    if housing_area.is_potsdam(place):
        _area, price, warm = hm.jobcenter_limits(persons)
        return price, warm
    return jobcenter_tables.for_place(place, persons)


def place_sources(criteria: Dict) -> List[str]:
    off = set(criteria.get("sources_off") or [])
    wanted = [source for source in covering_sources(criteria.get("area")) if source not in off]
    # Людина колись вимкнула ImmoScout24, а тепер шукає в Берліні: без нього
    # пошук був би порожнім.
    return wanted or covering_sources(criteria.get("area"))


def _t(key: str, lang: str, **kwargs) -> str:
    return i18n.t(f"housing.card.{key}", lang, **kwargs)


def _value(value, unit: str = "") -> str:
    return hm._preset_label(value, unit)


def _pairs(buttons: list, per_row: int = 2) -> list:
    """Кнопки рядами - щоб картка вміщалась на екран телефона."""
    return [buttons[i:i + per_row] for i in range(0, len(buttons), per_row)]


def _state(context: CallbackContext) -> dict:
    return context.user_data.setdefault("hcard", {})


# --- записи площадок ------------------------------------------------------------

def _member_kwargs(source: str, criteria: Dict, sources) -> Dict:
    """Ті самі поля, що їх кожній площадці дає майстер (_finalize_multi_filter)."""
    c = criteria
    rooms_area = {
        "min_rooms": c.get("min_rooms"), "max_rooms": c.get("max_rooms"),
        "min_area_m2": c.get("min_area_m2"), "max_area_m2": c.get("max_area_m2"),
    }
    kalt = {"min_price_eur": c.get("min_price_eur"), "max_price_eur": c.get("max_price_eur")}
    warm = {"min_price_warm_eur": c.get("min_price_warm_eur"), "max_price_warm_eur": c.get("max_price_warm_eur")}
    districts = [str(item) for item in (c.get("districts") or [])]
    return dict(_source_kwargs(source, c, rooms_area, kalt, warm, districts), area=c.get("area"), wbs=c.get("wbs"))


def _source_kwargs(source: str, c: Dict, rooms_area: Dict, kalt: Dict, warm: Dict, districts: List[str]) -> Dict:
    if source == "propotsdam":
        propot = hm._translate_districts(districts, hm.IMMOWELT_TO_PROPOT_DISTRICT, set(hm.PROPOT_DISTRICTS))
        return {
            "districts": propotsdam_store.normalize_districts(",".join(propot)), **rooms_area, **kalt,
            # Gesamtmiete порталу - та сама повна оренда.
            "min_total_rent_eur": warm["min_price_warm_eur"], "max_total_rent_eur": warm["max_price_warm_eur"],
        }
    if source == "immoscout":
        return {"districts": ",".join(districts), **rooms_area, **kalt, **warm}
    if source in ("semmelhaack", "regiomakler", "vonovia", "inberlin"):
        return {**rooms_area, **kalt, **warm}
    if source == "karlmarx":
        return {**rooms_area, "max_price_eur": kalt["max_price_eur"], **warm}
    return {**rooms_area, **kalt}  # schoba, locals, kleinanzeigen: лише Kaltmiete


def _immowelt_payload(criteria: Dict) -> Dict:
    districts = hm._translate_districts(
        [str(item) for item in (criteria.get("districts") or [])],
        hm.PROPOT_TO_IMMOWELT_DISTRICT, set(hm.IMMOWELT_DISTRICTS),
    )
    payload = {key: criteria.get(key) for key in (
        "min_price_eur", "max_price_eur", "min_price_warm_eur", "max_price_warm_eur",
        "min_rooms", "max_rooms", "min_area_m2", "max_area_m2",
    )}
    payload["districts"] = districts
    # Завжди явно: PATCH без поля лишив би старий звужений вибір.
    payload["property_types"] = list(hm.normalize_property_types(criteria.get("property_types")))
    # Місце й WBS — завжди явно, як і типи: PATCH без них лишив би старе.
    payload["area"] = criteria.get("area")
    payload["wbs"] = criteria.get("wbs")
    return payload


def _update_member(source: str, filter_id: int, owner: int, criteria: Dict, sources) -> bool:
    title = hm._auto_title(source, criteria)
    try:
        if source == "immowelt":
            hm._request("PATCH", f"/api/housing/filters/{int(filter_id)}", json={"title": title, **_immowelt_payload(criteria)})
            return True
        store = hm._LOCAL_SOURCE_MODULES[source][0]
        return bool(store.update_filter(int(filter_id), owner, title, **_member_kwargs(source, criteria, sources)))
    except Exception:
        logger.exception("Could not update %s filter %s from the card", source, filter_id)
        return False


def _create_member(source: str, owner: int, criteria: Dict, sources) -> Optional[int]:
    title = hm._auto_title(source, criteria)
    try:
        if source == "immowelt":
            payload = hm._request("POST", "/api/housing/filters", json={
                "user_id": owner, "title": title, **_immowelt_payload(criteria),
            })
            return int(payload["filter_id"]) if payload.get("filter_id") is not None else None
        store = hm._LOCAL_SOURCE_MODULES[source][0]
        return int(store.create_filter(user_id=owner, title=title, **_member_kwargs(source, criteria, sources)))
    except Exception:
        logger.exception("Could not create %s filter from the card", source)
        return None


def apply_criteria(search: Dict, criteria: Dict) -> List[str]:
    """Нові умови - в усі записи пошуку й у сам пошук. Повертає площадки,
    які оновити не вдалося (приймач Immowelt міг не відповісти)."""
    owner = int(search["user_id"])
    sources = [source for source, _ in search["members"]]
    failed = [
        source for source, filter_id in search["members"]
        if not _update_member(source, filter_id, owner, criteria, sources)
    ]
    housing_search_store.update(search["search_id"], criteria=criteria)
    if "propotsdam" in sources:
        hm._sync_propot_filters()
    return failed


def create_search(owner: int, criteria: Dict, sources, coops, active: bool) -> Optional[int]:
    members = []
    for source in sources:
        filter_id = _create_member(source, owner, criteria, sources)
        if filter_id is None:
            continue
        if not active:
            hm._set_one_filter_active(source, filter_id, False, owner)
        members.append((source, filter_id))
    if not members:
        return None
    if "propotsdam" in sources:
        hm._sync_propot_filters()
    search_id = housing_search_store.create(owner, criteria, members, coops)
    hm._sync_coop_subscriptions(owner)
    return search_id


# --- пошук для картки -------------------------------------------------------------

def _load(user_id: int, search_id: int) -> Tuple[Optional[int], Optional[Dict]]:
    """(номер, пошук із записами) або (None, None). Адмін бачить будь-який."""
    stored = housing_search_store.get(search_id, user_id)
    if stored is None and int(user_id) == hm.ADMIN_ID:
        stored = housing_search_store.get_any(search_id)
    if stored is None:
        return None, None
    for number, search in enumerate(hm.user_searches(int(stored["user_id"])), 1):
        if search["search_id"] == int(search_id):
            return number, search
    return None, None


def _paused(search: Dict) -> bool:
    return hm._search_paused(search)


def _sources(search: Dict) -> List[str]:
    present = set(hm._search_sources(search))
    return [source for source in hm.AVAILABLE_SOURCE_KEYS if source in present]


# --- картка -------------------------------------------------------------------------

def _range(criteria: Dict, low_key: str, high_key: str, unit: str, lang: str) -> str:
    low, high = criteria.get(low_key), criteria.get(high_key)
    if low is None and high is None:
        return _t("any", lang)
    if low is not None and high is not None:
        return f"{_value(low)}–{_value(high, unit)}"
    if low is not None:
        return _t("from", lang, value=_value(low, unit))
    return _t("to", lang, value=_value(high, unit))


def card_text(number: int, search: Dict, lang: str, pointer: Optional[str] = None, note: str = "",
              show_place: bool = False) -> str:
    criteria = search["criteria"]
    status = _t("status_off", lang) if _paused(search) else _t("status_on", lang)
    sources = _sources(search)
    districts = [str(item) for item in (criteria.get("districts") or [])]
    if districts:
        shown = ", ".join(districts[:3]) + (f" +{len(districts) - 3}" if len(districts) > 3 else "")
    else:
        shown = _t("all_districts", lang)
    total = len(covering_sources(criteria.get("area")))
    portals = (
        _t("all_sources", lang, n=total) if len(sources) == total
        else _t("some_sources", lang, n=len(sources), total=total)
    )
    if search.get("coops"):
        portals += _t("coops", lang, n=len(search["coops"]))
    if housing_area.touches_berlin(criteria.get("area")) and criteria.get("berlin_coops") is not False:
        portals += _t("coops", lang, n=coop_offers.count())
    place = criteria.get("area")
    if not housing_area.covers_potsdam(place):
        # Поза Потсдамом — не «1 з 11», а які саме площадки шукають.
        portals = ", ".join(hm.SOURCE_LABEL.get(source, source) for source in sources)
    lines = []
    if show_place or place is not None:
        lines.append(("place", _t("line.place", lang, value=html.escape(housing_area.label(place, lang)))))
    lines += [
        ("kalt", _t("line.kalt", lang, value=_range(criteria, "min_price_eur", "max_price_eur", "€", lang))),
        ("warm", _t("line.warm", lang, value=_range(criteria, "min_price_warm_eur", "max_price_warm_eur", "€", lang))),
        ("rooms", _t("line.rooms", lang, value=_range(criteria, "min_rooms", "max_rooms", "", lang))),
        ("area", _t("line.area", lang, value=_range(criteria, "min_area_m2", "max_area_m2", "м²", lang))),
        ("districts", _t("line.districts", lang, value=html.escape(shown))),
        ("sources", _t("line.sources", lang, value=portals)),
    ]
    if not housing_area.is_default(place):
        lines = [line for line in lines if line[0] != "districts"]
    if show_place or criteria.get("wbs"):
        lines.append(("wbs", _t("line.wbs", lang, value=wbs_label(criteria.get("wbs"), lang))))
    types = hm.normalize_property_types(criteria.get("property_types"))
    if "immowelt" in sources and types and len(types) < len(hm.PROPERTY_TYPE_KEYS):
        lines.append(("ptypes", _t("line.ptypes", lang, value=", ".join(
            i18n.t(f"housing.ptype.{key}", lang) for key in types
        ))))
    body = "\n".join(("👉 " if key == pointer else "▪️ ") + text for key, text in lines)
    head = _t("title", lang, n=number, status=status)
    parts = [head, "", body]
    # Що площа чи ціна без межі, видно в рядку картки. Окремо попереджаємо
    # лише про кімнати: без «від» приходять і однокімнатні (як і в старому
    # списку фільтрів, див. hm._unbounded_note).
    if pointer is None and not hm._has_room_bound(criteria):
        parts += ["", _t("no_rooms", lang)]
    if pointer is None and not housing_area.is_default(place):
        parts += ["", _t("place_note_outside" if not housing_area.covers_potsdam(place) else "place_note_radius", lang)]
    if note:
        parts += ["", note]
    return "\n".join(parts)


def _field_button(field: str, criteria: Dict, lang: str) -> InlineKeyboardButton:
    value = criteria.get(field)
    if value is None:
        label = _t(f"btn.{field}", lang)
    else:
        sign = "≤" if field.startswith("max") else "≥"
        label = f"{ICONS[field]} {sign} {_value(value, UNITS.get(field, ''))}"
    return InlineKeyboardButton(label, callback_data=f"{PREFIX}f:{field}")


def card_keyboard(search: Dict, lang: str, viewer_id: int = 0) -> InlineKeyboardMarkup:
    criteria = search["criteria"]
    rows = []
    paused = _paused(search)
    if paused:
        rows.append([InlineKeyboardButton(_t("btn.start", lang), callback_data=f"{PREFIX}on")])
    place = criteria.get("area")
    if place_enabled(viewer_id) or place is not None:
        rows.append([InlineKeyboardButton(
            _t("btn.place", lang, value=housing_area.label(place, lang)), callback_data=f"{PREFIX}pl",
        )])
    rows += [
        [_field_button("max_price_eur", criteria, lang), _field_button("max_price_warm_eur", criteria, lang)],
        [_field_button("min_rooms", criteria, lang), _field_button("max_rooms", criteria, lang)],
        [_field_button("min_area_m2", criteria, lang), _field_button("max_area_m2", criteria, lang)],
        # Райони — у самому Потсдамі, а округи — у самому Берліні (етап 7).
        ([InlineKeyboardButton(_t("btn.districts", lang), callback_data=f"{PREFIX}dist")]
         if housing_area.is_default(place) else
         [InlineKeyboardButton(_t("btn.districts", lang), callback_data=f"{PREFIX}bz")]
         if housing_area.is_berlin_city(place) else [])
        + [InlineKeyboardButton(_t("btn.sources", lang), callback_data=f"{PREFIX}src")],
    ]
    if place_enabled(viewer_id) or criteria.get("wbs"):
        rows.append([InlineKeyboardButton(
            _t("btn.wbs", lang, value=wbs_label(criteria.get("wbs"), lang)), callback_data=f"{PREFIX}wb",
        )])
    owner = int(search["user_id"])
    admin_view = int(viewer_id) == hm.ADMIN_ID and owner != hm.ADMIN_ID
    delete = (
        f"housing:as:{owner}:del:{search['search_id']}" if admin_view
        else f"housing:s:del:{search['search_id']}"
    )
    rows.append(
        ([] if paused else [InlineKeyboardButton(_t("btn.pause", lang), callback_data=f"{PREFIX}off")])
        + [InlineKeyboardButton(_t("btn.delete", lang), callback_data=delete)]
    )
    rows.append([InlineKeyboardButton(_t("btn.done", lang), callback_data=f"{PREFIX}done")])
    return InlineKeyboardMarkup(rows)


def _show(update: Update, context: CallbackContext, note: str = "", edit: bool = True) -> None:
    st = _state(context)
    user = update.effective_user
    lang = i18n.get_lang(user.id)
    number, search = _load(int(user.id), int(st.get("sid") or 0))
    if search is None:
        st.clear()
        hm._edit_or_reply(update, _t("not_found", lang), InlineKeyboardMarkup([[
            InlineKeyboardButton(i18n.t("housing.btn.back_to_monitor", lang), callback_data="housing:menu"),
        ]]), edit)
        return
    st.pop("input", None)
    hm._edit_or_reply(
        update, card_text(number, search, lang, note=note, show_place=place_enabled(int(user.id))),
        card_keyboard(search, lang, int(user.id)), edit,
    )


def open_card(update: Update, context: CallbackContext, search_id: int, edit: bool = True) -> None:
    """Відповідь на натискання (query.answer) - справа того, хто викликає."""
    context.user_data["hcard"] = {"sid": int(search_id)}
    _show(update, context, edit=edit)


# --- новий фільтр ----------------------------------------------------------------

def start_new(update: Update, context: CallbackContext, edit: bool = False) -> None:
    user = update.effective_user
    if place_enabled(int(user.id)):
        # Спершу місце; Jobcenter — після нього і лише для Потсдама.
        context.user_data["hcard"] = {"new_flow": True}
        query = getattr(update, "callback_query", None)
        if query:
            query.answer()
        _place_screen(update, context, edit=edit and bool(query))
        return
    _jobcenter_screen(update, context, edit)


def _jobcenter_screen(update: Update, context: CallbackContext, edit: bool = False) -> None:
    user = update.effective_user
    lang = i18n.get_lang(user.id)
    buttons = [
        InlineKeyboardButton(i18n.t("housing.jobcenter.btn_persons", lang, n=n), callback_data=f"{PREFIX}new:{n}")
        for n in JOBCENTER_CHOICES
    ]
    rows = _pairs(buttons, 3)
    rows.append([
        InlineKeyboardButton(i18n.t("housing.jobcenter.btn_no", lang), callback_data=f"{PREFIX}new:no"),
        InlineKeyboardButton(hm.BTN_CANCEL, callback_data="housing:menu"),
    ])
    query = getattr(update, "callback_query", None)
    if query:
        query.answer()
    hm._edit_or_reply(update, _t("new_question", lang), InlineKeyboardMarkup(rows), edit and bool(query))


def wbs_label(value, lang: str) -> str:
    choice = housing_wbs.normalize(value)
    if choice is None:
        return _t("wbs.any", lang)
    if choice == "none":
        return _t("wbs.none", lang)
    return f"WBS {choice}"


def _wbs_keyboard(action: str, lang: str, back: str) -> InlineKeyboardMarkup:
    """Вибір WBS: `action` — «wv» (картка) чи «nw» (новий фільтр)."""
    def button(value: str, label: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(label, callback_data=f"{PREFIX}{action}:{value}")

    rows = [
        [button("any", _t("wbs.any", lang)), button("none", _t("wbs.none", lang))],
        [button(level, f"WBS {level}") for level in ("100", "140", "160")],
        [button(level, f"WBS {level}") for level in ("180", "220")],
        [InlineKeyboardButton(_t("btn.back", lang) if back != "housing:menu" else hm.BTN_CANCEL, callback_data=back)],
    ]
    return InlineKeyboardMarkup(rows)


def _wbs_question(update: Update, context: CallbackContext) -> None:
    """Новий фільтр: останнє питання перед створенням."""
    query = getattr(update, "callback_query", None)
    if query:
        query.answer()
    lang = i18n.get_lang(update.effective_user.id)
    hm._edit_or_reply(update, _t("new_wbs_question", lang), _wbs_keyboard("nw", lang, "housing:menu"), bool(query))


def _create_new(update: Update, context: CallbackContext, raw: str) -> None:
    query = update.callback_query
    user = update.effective_user
    lang = i18n.get_lang(user.id)
    st = context.user_data.get("hcard") or {}
    place = st.get("new_area")
    criteria: Dict = {"districts": [], "property_types": []}
    if place is not None:
        criteria["area"] = place
    if st.get("new_wbs"):
        criteria["wbs"] = st["new_wbs"]
    persons = int(raw) if raw.isdigit() else None
    limits = jobcenter_for(place, persons) if persons else None
    if limits:
        price, warm = limits
        # Холодна межа = сам ліміт Bruttokaltmiete, як і в майстрі.
        criteria.update(max_price_eur=price, max_price_warm_eur=warm)
    try:
        housing_journey_store.set_household(int(user.id), persons)
    except Exception:
        logger.exception("Could not save the Jobcenter household of %s", user.id)
    query.answer()
    coops = list(hm.COOP_KEYS) if housing_area.covers_potsdam(place) else []
    search_id = create_search(int(user.id), criteria, place_sources(criteria), coops, active=False)
    if search_id is None:
        hm._edit_or_reply(update, i18n.t("housing.finalize.nothing_saved", lang), InlineKeyboardMarkup([[
            InlineKeyboardButton(i18n.t("housing.btn.back_to_monitor", lang), callback_data="housing:menu"),
        ]]), True)
        return
    context.user_data["hcard"] = {"sid": search_id, "new": True}
    _show(update, context, note=_t("new_hint", lang))


# --- поле ----------------------------------------------------------------------------

def _field_keyboard(field: str, owner: int, lang: str, place: Optional[Dict] = None) -> InlineKeyboardMarkup:
    unit = UNITS.get(field, "")
    buttons = [
        InlineKeyboardButton(_value(value, unit), callback_data=f"{PREFIX}v:{field}:{value}")
        for value in PRESETS[field]
    ]
    rows = _pairs(buttons, 3)
    persons = None
    # Ліміти Jobcenter — лише там, де бот знає таблицю (Потсдам, Берлін).
    if field in PRICE_MIN_OF and jobcenter_for(place, 1) is not None:
        try:
            persons = housing_journey_store.get_household(owner)
        except Exception:
            persons = None
    if persons:
        price, warm = jobcenter_for(place, persons)
        limit = price if field == "max_price_eur" else warm
        rows.insert(0, [InlineKeyboardButton(
            _t("jobcenter_btn", lang, n=persons, value=_value(limit, "€")), callback_data=f"{PREFIX}v:{field}:{limit}",
        )])
    rows.append([
        InlineKeyboardButton(_t("btn.no_limit", lang), callback_data=f"{PREFIX}v:{field}:-"),
        InlineKeyboardButton(_t("btn.back", lang), callback_data=f"{PREFIX}open"),
    ])
    return InlineKeyboardMarkup(rows)


def _ask_field(update: Update, context: CallbackContext, field: str) -> None:
    query = update.callback_query
    st = _state(context)
    lang = i18n.get_lang(update.effective_user.id)
    number, search = _load(int(update.effective_user.id), int(st.get("sid") or 0))
    if search is None or field not in FIELDS:
        query.answer()
        _show(update, context)
        return
    query.answer()
    st["input"] = field
    text = card_text(number, search, lang, pointer=FIELD_LINE[field]) + "\n\n" + _t(f"ask.{field}", lang) + "\n" + _t("ask_hint", lang)
    keyboard = _field_keyboard(field, int(search["user_id"]), lang, search["criteria"].get("area"))
    hm._edit_or_reply(update, text, keyboard, True)


def _set_value(update: Update, context: CallbackContext, field: str, value: Optional[float]) -> Optional[str]:
    """Зберігає одне поле. Повертає текст помилки для людини або None."""
    st = _state(context)
    lang = i18n.get_lang(update.effective_user.id)
    _number, search = _load(int(update.effective_user.id), int(st.get("sid") or 0))
    if search is None:
        return _t("not_found", lang)
    if value is not None and (value <= 0 or value > MAX_VALUE.get(field, 100000)):
        return _t("bad_number", lang)
    criteria = dict(search["criteria"])
    other = OTHER_END.get(field)
    if value is not None and other and criteria.get(other) is not None:
        low, high = (value, criteria[other]) if field.startswith("min") else (criteria[other], value)
        if low > high:
            return _t("min_over_max", lang)
    criteria[field] = value
    if field in PRICE_MIN_OF:
        criteria[PRICE_MIN_OF[field]] = None
    failed = apply_criteria(search, criteria)
    st["dirty"] = True
    st.pop("input", None)
    if failed:
        return _t("save_failed", lang, sources=", ".join(hm.SOURCE_LABEL.get(s, s) for s in failed))
    return None


def _parse_number(text: str) -> Optional[float]:
    raw = (text or "").strip().replace("€", "").replace("m²", "").replace("м²", "").replace(" ", "").replace(",", ".")
    try:
        value = float(raw)
    except ValueError:
        return None
    return int(value) if value.is_integer() else round(value, 2)


def handle_text(update: Update, context: CallbackContext) -> bool:
    """Своє число після тапу по полю. Не число - віддаємо тексту далі (людина
    могла натиснути кнопку нижнього меню) і забуваємо про очікування."""
    st = context.user_data.get("hcard") or {}
    field = st.get("input")
    if not field or not update.message or not update.message.text:
        return False
    text = update.message.text.strip()
    if field == "place":
        return _place_text(update, context, text)
    value = None if text == "-" else _parse_number(text)
    if value is None and text != "-":
        st.pop("input", None)
        return False
    lang = i18n.get_lang(update.effective_user.id)
    error = _set_value(update, context, field, value)
    if error:
        st["input"] = field
        update.message.reply_text(error)
        return True
    _show(update, context, note=_t("saved", lang), edit=False)
    return True


# --- райони й портали --------------------------------------------------------------

def _districts_screen(update: Update, context: CallbackContext) -> None:
    st = _state(context)
    lang = i18n.get_lang(update.effective_user.id)
    names = hm._canonical_districts(st.get("draft_sources") or [])
    chosen = set(st.get("draft_districts") or [])
    rows = _pairs([
        InlineKeyboardButton(("✅ " if name in chosen else "☐ ") + name, callback_data=f"{PREFIX}dt:{index}")
        for index, name in enumerate(names)
    ])
    rows.append([
        InlineKeyboardButton(_t("btn.all_districts", lang), callback_data=f"{PREFIX}dall"),
        InlineKeyboardButton(_t("btn.no_districts", lang), callback_data=f"{PREFIX}dnone"),
    ])
    rows.append([
        InlineKeyboardButton(_t("btn.save", lang), callback_data=f"{PREFIX}dok"),
        InlineKeyboardButton(_t("btn.back", lang), callback_data=f"{PREFIX}open"),
    ])
    hm._edit_or_reply(update, _t("districts_text", lang), InlineKeyboardMarkup(rows), True)


def _bezirke_screen(update: Update, context: CallbackContext) -> None:
    """Округи Берліна (етап 7): ті самі кнопки, що й райони Потсдама."""
    st = _state(context)
    lang = i18n.get_lang(update.effective_user.id)
    names = housing_area.berlin_bezirke()
    chosen = set(st.get("draft_bezirke") or [])
    rows = _pairs([
        InlineKeyboardButton(("✅ " if name in chosen else "☐ ") + name, callback_data=f"{PREFIX}bt:{index}")
        for index, name in enumerate(names)
    ])
    rows.append([
        InlineKeyboardButton(_t("btn.all_districts", lang), callback_data=f"{PREFIX}ball"),
        InlineKeyboardButton(_t("btn.no_districts", lang), callback_data=f"{PREFIX}bnone"),
    ])
    rows.append([
        InlineKeyboardButton(_t("btn.save", lang), callback_data=f"{PREFIX}bok"),
        InlineKeyboardButton(_t("btn.back", lang), callback_data=f"{PREFIX}open"),
    ])
    hm._edit_or_reply(update, _t("districts_text", lang), InlineKeyboardMarkup(rows), True)


def _sources_screen(update: Update, context: CallbackContext) -> None:
    st = _state(context)
    lang = i18n.get_lang(update.effective_user.id)
    sources = set(st.get("draft_sources") or [])
    coops = set(st.get("draft_coops") or [])
    place = st.get("draft_place")
    covering = set(covering_sources(place))
    rows = _pairs([
        InlineKeyboardButton(("✅ " if spec["key"] in sources else "☐ ") + spec["label"], callback_data=f"{PREFIX}st:{spec['key']}")
        for spec in hm.AVAILABLE_SOURCES if spec["key"] in covering
    ])
    # Кооперативи — потсдамські.
    rows += [] if not housing_area.covers_potsdam(place) else _pairs([
        InlineKeyboardButton(
            ("✅ " if coop["key"] in coops else "☐ ") + coop["label"], callback_data=f"{PREFIX}ct:{coop['key']}",
        )
        for coop in hm.coop_watchdog.COOPERATIVES
    ])
    # Кооперативи Берліна (етап 9) — одним перемикачем, коли в зоні є Берлін.
    if housing_area.touches_berlin(place):
        rows.append([InlineKeyboardButton(
            ("✅ " if st.get("draft_berlin_coops", True) else "☐ ") + _t("btn.berlin_coops", lang, n=coop_offers.count()),
            callback_data=f"{PREFIX}bc",
        )])
    if "immowelt" in sources and "immowelt" in covering:
        types = set(st.get("draft_ptypes") or [])
        rows.append([
            InlineKeyboardButton(
                ("✅ " if key in types else "☐ ") + i18n.t(f"housing.ptype.{key}", lang), callback_data=f"{PREFIX}pt:{key}",
            )
            for key in hm.PROPERTY_TYPE_KEYS
        ])
    rows.append([
        InlineKeyboardButton(_t("btn.save", lang), callback_data=f"{PREFIX}sok"),
        InlineKeyboardButton(_t("btn.back", lang), callback_data=f"{PREFIX}open"),
    ])
    text = _t("sources_text", lang)
    if not housing_area.covers_potsdam(place):
        text += "\n\n" + _t("place_note_outside", lang)
    hm._edit_or_reply(update, text, InlineKeyboardMarkup(rows), True)


def _load_drafts(context: CallbackContext, search: Dict) -> None:
    st = _state(context)
    st["draft_sources"] = _sources(search)
    st["draft_coops"] = list(search.get("coops") or [])
    st["draft_place"] = search["criteria"].get("area")
    st["draft_berlin_coops"] = search["criteria"].get("berlin_coops") is not False
    # Порожній список у фільтрі - «усі райони». На екрані це всі галочки, а
    # не жодної: інакше людина бачила «нічого не вибрано» й думала, що
    # квартир не буде ніде (27.10.2026).
    st["draft_districts"] = (
        [str(item) for item in search["criteria"].get("districts") or []]
        or list(hm._canonical_districts(st["draft_sources"]))
    )
    types = hm.normalize_property_types(search["criteria"].get("property_types"))
    st["draft_ptypes"] = list(types) if types else list(hm.PROPERTY_TYPE_KEYS)


def _toggle(items: list, key: str) -> list:
    return [item for item in items if item != key] if key in items else items + [key]


def _save_sources(update: Update, context: CallbackContext, search: Dict) -> Optional[str]:
    st = _state(context)
    lang = i18n.get_lang(update.effective_user.id)
    covering = covering_sources(search["criteria"].get("area"))
    wanted = [source for source in covering if source in set(st.get("draft_sources") or [])]
    if not wanted:
        return i18n.t("housing.toast.pick_a_source", lang)
    failed = change_sources(
        search, wanted, coops=st.get("draft_coops") or [], ptypes=st.get("draft_ptypes") or [],
        berlin_coops=bool(st.get("draft_berlin_coops", True)),
    )
    st["dirty"] = True
    if failed:
        return _t("save_failed", lang, sources=", ".join(hm.SOURCE_LABEL.get(s, s) for s in sorted(set(failed))))
    return None


def change_sources(search: Dict, wanted: List[str], coops, ptypes, berlin_coops: bool) -> List[str]:
    """Площадки, кооперативи й типи житла пошуку. Спільне для бота й сайту."""
    covering = covering_sources(search["criteria"].get("area"))
    wanted = [source for source in covering if source in set(wanted)]
    owner = int(search["user_id"])
    criteria = dict(search["criteria"])
    # Що людина вимкнула сама — пам'ятаємо й для інших місць (див. place_sources).
    off = set(criteria.get("sources_off") or []) - set(covering)
    criteria["sources_off"] = sorted(off | {source for source in covering if source not in wanted})
    types = [key for key in hm.PROPERTY_TYPE_KEYS if key in set(ptypes or [])]
    criteria["property_types"] = [] if len(types) == len(hm.PROPERTY_TYPE_KEYS) else types
    active = not _paused(search)
    current = {source: filter_id for source, filter_id in search["members"]}
    failed = []
    for source, filter_id in search["members"]:
        if source not in wanted:
            if hm._delete_one_filter(source, filter_id, owner):
                housing_search_store.remove_member(search["search_id"], source, filter_id)
            else:
                failed.append(source)
    for source in wanted:
        if source in current:
            continue
        filter_id = _create_member(source, owner, criteria, wanted)
        if filter_id is None:
            failed.append(source)
            continue
        if not active:
            hm._set_one_filter_active(source, filter_id, False, owner)
        housing_search_store.add_member(search["search_id"], source, filter_id)
    criteria["berlin_coops"] = bool(berlin_coops)
    coops = [key for key in hm.COOP_KEYS if key in set(coops or [])]
    if not housing_area.covers_potsdam(criteria.get("area")):
        coops = []
    housing_search_store.update(search["search_id"], coops=coops)
    hm._sync_coop_subscriptions(owner)
    refreshed = housing_search_store.get_any(search["search_id"]) or search
    failed += apply_criteria(refreshed, criteria)
    return failed


# --- місце ---------------------------------------------------------------------------

QUICK_PLACES = {"potsdam": housing_area.POTSDAM, "berlin": housing_area.BERLIN}


def _place_screen(update: Update, context: CallbackContext, edit: bool = True, note: str = "") -> None:
    """Де шукати: дві кнопки або своє місто/індекс у чат."""
    st = _state(context)
    lang = i18n.get_lang(update.effective_user.id)
    st["input"] = "place"
    new_flow = bool(st.get("new_flow"))
    rows = [[
        InlineKeyboardButton("📍 Potsdam", callback_data=f"{PREFIX}pc:potsdam"),
        InlineKeyboardButton("📍 Berlin", callback_data=f"{PREFIX}pc:berlin"),
    ], [
        InlineKeyboardButton(hm.BTN_CANCEL, callback_data="housing:menu") if new_flow
        else InlineKeyboardButton(_t("btn.back", lang), callback_data=f"{PREFIX}open"),
    ]]
    text = _t("new_place_question" if new_flow else "place_text", lang)
    if note:
        text += "\n\n" + note
    hm._edit_or_reply(update, text, InlineKeyboardMarkup(rows), edit)


def _option_label(option: Dict) -> str:
    """«📍 Werder (Märkisch-Oderland)» — два Werder різняться округом."""
    name = option["name"] if option.get("kind") == "city" else f"{option['plz']} {option['name']}"
    kreis = option.get("kreis") or ""
    return f"📍 {name}" + (f" ({kreis})" if kreis and kreis != option["name"] else "")


def _choices_screen(update: Update, context: CallbackContext, options: List[Dict], edit: bool) -> None:
    st = _state(context)
    lang = i18n.get_lang(update.effective_user.id)
    st["place_options"] = options
    rows = [[InlineKeyboardButton(_option_label(option), callback_data=f"{PREFIX}pc:{index}")]
            for index, option in enumerate(options)]
    rows.append([InlineKeyboardButton(_t("btn.back", lang), callback_data=f"{PREFIX}pl")])
    hm._edit_or_reply(update, _t("place_choose", lang), InlineKeyboardMarkup(rows), edit)


def _radius_screen(update: Update, context: CallbackContext, place: Dict, edit: bool) -> None:
    st = _state(context)
    lang = i18n.get_lang(update.effective_user.id)
    st["place_draft"] = place
    st.pop("input", None)
    km = "km" if lang == "de" else "км"
    buttons = [InlineKeyboardButton(_t("btn.radius_city", lang), callback_data=f"{PREFIX}pr:0")] + [
        InlineKeyboardButton(f"+ {radius} {km}", callback_data=f"{PREFIX}pr:{radius}")
        for radius in housing_area.RADII if radius
    ]
    rows = _pairs(buttons, 3)
    rows.append([InlineKeyboardButton(_t("btn.back", lang), callback_data=f"{PREFIX}pl")])
    name = housing_area.label(dict(place, radius_km=0), lang)
    text = _t("radius_text", lang, place=html.escape(name))
    if housing_area.is_potsdam(place):
        text += "\n\n" + _t("radius_potsdam_hint", lang)
    hm._edit_or_reply(update, text, InlineKeyboardMarkup(rows), edit)


def _picked(update: Update, context: CallbackContext, options: List[Dict], edit: bool) -> None:
    if len(options) == 1:
        _radius_screen(update, context, options[0], edit)
    else:
        _choices_screen(update, context, options, edit)


def _place_text(update: Update, context: CallbackContext, text: str) -> bool:
    """Місто чи індекс, написані в чат після «📍»."""
    lang = i18n.get_lang(update.effective_user.id)
    options = housing_area.find(text)
    if not options and not text[:1].isalnum():
        # Кнопка нижнього меню («🏠 …»), а не місто — віддаємо далі.
        _state(context).pop("input", None)
        return False
    if not options:
        _place_screen(update, context, edit=False, note=_t("place_not_found", lang, text=html.escape(text[:60])))
        return True
    _picked(update, context, options, edit=False)
    return True


def _sync_members(search: Dict, criteria: Dict) -> List[str]:
    """Записи площадок — під місце: зайві видаляє, потрібні створює (на паузі,
    якщо пошук на паузі). Повертає площадки, з якими не вдалося."""
    owner = int(search["user_id"])
    wanted = place_sources(criteria)
    active = not _paused(search)
    present = {source for source, _ in search["members"]}
    failed = []
    for source, filter_id in search["members"]:
        if source in wanted:
            continue
        if hm._delete_one_filter(source, filter_id, owner):
            housing_search_store.remove_member(search["search_id"], source, filter_id)
        else:
            failed.append(source)
    for source in wanted:
        if source in present:
            continue
        filter_id = _create_member(source, owner, criteria, wanted)
        if filter_id is None:
            failed.append(source)
            continue
        if not active:
            hm._set_one_filter_active(source, filter_id, False, owner)
        housing_search_store.add_member(search["search_id"], source, filter_id)
    return failed


def _save_place(update: Update, context: CallbackContext, search: Dict, place: Optional[Dict]) -> Optional[str]:
    lang = i18n.get_lang(update.effective_user.id)
    failed = change_place(search, place)
    _state(context)["dirty"] = True
    if failed:
        return _t("save_failed", lang, sources=", ".join(hm.SOURCE_LABEL.get(s, s) for s in sorted(set(failed))))
    return None


def change_place(search: Dict, place: Optional[Dict]) -> List[str]:
    """Нове місце пошуку: записи площадок під нього, кооперативи, умови.
    Спільне для картки в боті й сайту (housing_web). Повертає невдалі площадки."""
    criteria = dict(search["criteria"])
    old = criteria.get("area")
    if "sources_off" not in criteria and housing_area.covers_potsdam(old):
        # Старий пошук: вимкнене людиною — те, чого серед записів немає.
        present = set(_sources(search))
        criteria["sources_off"] = [source for source in hm.AVAILABLE_SOURCE_KEYS if source not in present]
    criteria["area"] = place
    if not housing_area.is_default(place):
        criteria["districts"] = []
    coops = list(search.get("coops") or [])
    if not housing_area.covers_potsdam(place):
        coops = []
    elif not housing_area.covers_potsdam(old):
        coops = list(hm.COOP_KEYS)  # повернулись у Потсдам — кооперативи, як у нового пошуку
    housing_search_store.update(search["search_id"], criteria=criteria, coops=coops)
    hm._sync_coop_subscriptions(int(search["user_id"]))
    refreshed = housing_search_store.get_any(search["search_id"]) or search
    failed = _sync_members(dict(refreshed, items=search.get("items", [])), criteria)
    refreshed = housing_search_store.get_any(search["search_id"]) or refreshed
    failed += apply_criteria(refreshed, criteria)
    return failed


def _place_callback(update: Update, context: CallbackContext, action: str, arg: str) -> None:
    query = update.callback_query
    st = _state(context)
    lang = i18n.get_lang(update.effective_user.id)
    if action == "pl":
        query.answer()
        _place_screen(update, context)
        return
    if action == "pc":
        if arg in QUICK_PLACES:
            options = housing_area.find(QUICK_PLACES[arg])
        else:
            try:
                options = [list(st.get("place_options") or [])[int(arg)]]
            except (IndexError, ValueError):
                options = []
        query.answer()
        if not options:
            _place_screen(update, context)
            return
        _picked(update, context, options, edit=True)
        return
    # pr — радіус обрано, місце готове.
    draft = st.get("place_draft")
    if not draft:
        query.answer()
        _place_screen(update, context)
        return
    try:
        radius = int(arg)
    except ValueError:
        radius = 0
    place = housing_area.make(draft, radius)
    if st.get("new_flow"):
        st["new_area"] = place
        if jobcenter_for(place, 1) is not None:
            _jobcenter_screen(update, context, edit=True)  # сам відповідає на натискання
        else:
            st["new_persons"] = "no"
            _wbs_question(update, context)
        return
    _number, search = _load(int(update.effective_user.id), int(st.get("sid") or 0))
    if search is None:
        query.answer()
        _show(update, context)
        return
    error = _save_place(update, context, search, place)
    query.answer(error or _t("saved", lang), show_alert=bool(error))
    _show(update, context)


# --- пауза й готово -------------------------------------------------------------------

def _set_active(update: Update, context: CallbackContext, search: Dict, active: bool) -> None:
    owner = int(search["user_id"])
    for item in search["items"]:
        source, filter_id = hm.housing_search_grouping.item_key(item)
        hm._set_one_filter_active(source, filter_id, active, owner)
    st = _state(context)
    if active and st.pop("new", False):
        # Перший запуск нового фільтра - як збереження в майстрі: перший
        # фільтр людини, звіт «за 30 днів підійшло б N», подія для адміна.
        saved = [(source, filter_id, search["criteria"]) for source, filter_id in search["members"]]
        hm._after_filter_saved(context, owner, saved, search_id=search["search_id"])
        hm._maybe_send_first_filter_congrats(context, owner)
        st.pop("dirty", None)


def _done(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    st = _state(context)
    user = update.effective_user
    _number, search = _load(int(user.id), int(st.get("sid") or 0))
    query.answer()
    if search is not None and st.get("dirty") and not _paused(search):
        saved = [(source, filter_id, search["criteria"]) for source, filter_id in search["members"]]
        hm._after_filter_saved(context, int(search["user_id"]), saved, edited=True, search_id=search["search_id"])
    context.user_data.pop("hcard", None)
    if search is not None and int(user.id) == hm.ADMIN_ID and int(search["user_id"]) != hm.ADMIN_ID:
        hm.show_admin_user_filters(update, context, int(search["user_id"]), edit=True)
    else:
        hm.show_menu(update, context, edit=True)


# --- маршрутизація ---------------------------------------------------------------------

def handle_callback(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    if not query or not query.data:
        return
    user = update.effective_user
    if not user or not hm.is_allowed(user.id):
        query.answer()
        return
    lang = i18n.get_lang(user.id)
    parts = query.data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    st = _state(context)

    if action in ("pl", "pc", "pr"):
        _place_callback(update, context, action, parts[2] if len(parts) > 2 else "")
        return
    if action == "new":
        raw = parts[2] if len(parts) > 2 else "no"
        if st.get("new_flow"):
            # Після Jobcenter — ще питання про WBS, тоді створюємо.
            st["new_persons"] = raw
            _wbs_question(update, context)
            return
        _create_new(update, context, raw)
        return
    if action == "nw":
        value = parts[2] if len(parts) > 2 else "any"
        st["new_wbs"] = housing_wbs.normalize(value)
        _create_new(update, context, str(st.get("new_persons") or "no"))
        return
    if action == "open":
        query.answer()
        _show(update, context)
        return
    if action == "done":
        _done(update, context)
        return
    if action == "f":
        _ask_field(update, context, parts[2] if len(parts) > 2 else "")
        return
    if action == "v":
        field = parts[2] if len(parts) > 2 else ""
        raw = parts[3] if len(parts) > 3 else "-"
        if field not in FIELDS:
            query.answer()
            return
        value = None if raw == "-" else _parse_number(raw)
        error = _set_value(update, context, field, value)
        if error:
            query.answer(error, show_alert=True)
            return
        query.answer(_t("saved", lang))
        _show(update, context)
        return

    _number, search = _load(int(user.id), int(st.get("sid") or 0))
    if search is None:
        query.answer()
        _show(update, context)
        return
    if action in ("on", "off"):
        _set_active(update, context, search, action == "on")
        query.answer(_t("started_toast" if action == "on" else "paused_toast", lang))
        _show(update, context)
    elif action == "dist":
        _load_drafts(context, search)
        query.answer()
        _districts_screen(update, context)
    elif action == "dt":
        names = hm._canonical_districts(st.get("draft_sources") or [])
        try:
            name = names[int(parts[2])]
        except (IndexError, ValueError):
            query.answer()
            return
        st["draft_districts"] = _toggle(list(st.get("draft_districts") or []), name)
        query.answer()
        _districts_screen(update, context)
    elif action in ("dall", "dnone"):
        names = hm._canonical_districts(st.get("draft_sources") or [])
        st["draft_districts"] = list(names) if action == "dall" else []
        query.answer()
        _districts_screen(update, context)
    elif action == "dok":
        chosen = list(st.get("draft_districts") or [])
        if not chosen:
            query.answer(_t("pick_a_district", lang), show_alert=True)
            return
        names = hm._canonical_districts(st.get("draft_sources") or [])
        # Усі позначені - зберігаємо як «усі»: тоді й нові райони порталу,
        # яких ще нема в списку, теж підходитимуть.
        districts = [] if set(names) <= set(chosen) else chosen
        criteria = dict(search["criteria"], districts=districts)
        failed = apply_criteria(search, criteria)
        st["dirty"] = True
        if failed:
            query.answer(_t("save_failed", lang, sources=", ".join(failed)), show_alert=True)
        else:
            query.answer(_t("saved", lang))
        _show(update, context)
    elif action == "src":
        _load_drafts(context, search)
        query.answer()
        _sources_screen(update, context)
    elif action == "bz":
        place = search["criteria"].get("area") or {}
        st["draft_bezirke"] = list(place.get("bezirke") or housing_area.berlin_bezirke())
        query.answer()
        _bezirke_screen(update, context)
    elif action == "bt":
        names = housing_area.berlin_bezirke()
        try:
            name = names[int(parts[2])]
        except (IndexError, ValueError):
            query.answer()
            return
        st["draft_bezirke"] = _toggle(list(st.get("draft_bezirke") or []), name)
        query.answer()
        _bezirke_screen(update, context)
    elif action in ("ball", "bnone"):
        st["draft_bezirke"] = housing_area.berlin_bezirke() if action == "ball" else []
        query.answer()
        _bezirke_screen(update, context)
    elif action == "bok":
        chosen = sorted(set(st.get("draft_bezirke") or []))
        place = dict(search["criteria"].get("area") or {})
        if not chosen or not housing_area.is_berlin_city(place):
            query.answer(_t("pick_a_district", lang), show_alert=True)
            return
        # Усі позначені — «увесь Берлін», без списку.
        if set(chosen) >= set(housing_area.berlin_bezirke()):
            place.pop("bezirke", None)
        else:
            place["bezirke"] = chosen
        failed = apply_criteria(search, dict(search["criteria"], area=place))
        st["dirty"] = True
        query.answer(_t("save_failed", lang, sources=", ".join(failed)) if failed else _t("saved", lang),
                     show_alert=bool(failed))
        _show(update, context)
    elif action == "wb":
        query.answer()
        current = wbs_label(search["criteria"].get("wbs"), lang)
        hm._edit_or_reply(update, _t("wbs_text", lang, value=current), _wbs_keyboard("wv", lang, f"{PREFIX}open"), True)
    elif action == "wv":
        value = housing_wbs.normalize(parts[2] if len(parts) > 2 else "any")
        failed = apply_criteria(search, dict(search["criteria"], wbs=value))
        st["dirty"] = True
        if failed:
            query.answer(_t("save_failed", lang, sources=", ".join(failed)), show_alert=True)
        else:
            query.answer(_t("saved", lang))
        _show(update, context)
    elif action == "bc":
        st["draft_berlin_coops"] = not st.get("draft_berlin_coops", True)
        query.answer()
        _sources_screen(update, context)
    elif action in ("st", "ct", "pt"):
        key = parts[2] if len(parts) > 2 else ""
        field = {"st": "draft_sources", "ct": "draft_coops", "pt": "draft_ptypes"}[action]
        valid = {"st": hm.AVAILABLE_SOURCE_KEYS, "ct": hm.COOP_KEYS, "pt": hm.PROPERTY_TYPE_KEYS}[action]
        if key in valid:
            st[field] = _toggle(list(st.get(field) or []), key)
        query.answer()
        _sources_screen(update, context)
    elif action == "sok":
        error = _save_sources(update, context, search)
        if error:
            query.answer(error, show_alert=True)
            if error == i18n.t("housing.toast.pick_a_source", lang):
                return
        else:
            query.answer(_t("saved", lang))
        _show(update, context)
    else:
        query.answer()


callback_handler = CallbackQueryHandler(handle_callback, pattern=r"^hcard:")
