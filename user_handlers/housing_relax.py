"""«Якщо трохи розширити фільтр — було б на N квартир більше».

Опитування жовтня 2026: більшість скарг — «приходить замало», і людина не
розуміє, чому бот мовчить. Мовчить він здебільшого чесно: під фільтр просто не
було квартир (сім'я з п'яти, від 4 кімнат до 1200 € — за два тижні жодної на
всіх площадках). Тому замість поради «спробуйте розширити» людина бачить, що
саме дала б кожна поступка, і застосовує її однією кнопкою:

- одразу після збереження чи редагування фільтра (звіт «за 30 днів підійшло б N»);
- коли фільтр тиждень нічого не приніс (quiet_filters_job, раз на день).

Рахуємо справжніми матчерами площадок по збережених записах фільтра, тож
райони, тепла/холодна ціна й особливості кожного джерела враховані так само,
як при розсилці. Застосування — той самий шлях, що й редагування майстром:
записи площадок перестворюються з новими умовами (_finalize_multi_filter).
"""

import html
import logging
import math
import time
from datetime import datetime, time as dtime, timedelta
from typing import Dict, List, Optional, Tuple

import pytz
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackContext

import i18n
from database import DBSession, HousingSendLog
from user_handlers import housing_monitor as hm
from user_jobs import housing_journey_store, housing_search_store

logger = logging.getLogger(__name__)

RELAX_DAYS = hm.FILTER_REPORT_DAYS
PRICE_STEP_EUR = 100
AREA_STEP_M2 = 10
MAX_OFFERS = 3
# Поступка варта кнопки, лише якщо додає хоч чверть до того, що вже є —
# інакше широкому фільтру пропонували б «+3 до 200».
MIN_GAIN_SHARE = 0.25

# Тихий фільтр: тиждень без жодної квартири; повтор не частіше ніж раз на два.
QUIET_DAYS = 7
QUIET_REPEAT_DAYS = 14
QUIET_SEARCHES_SHOWN = 3
QUIET_CHECK_TIME = dtime(11, 0, tzinfo=pytz.timezone("Europe/Berlin"))
SEND_PAUSE_SECONDS = 0.1

CALLBACK_PREFIX = "housing:relax:"

# Ключі цін у записах різних площадок: ProPotsdam тримає повну оренду як
# *_total_rent_eur, решта — як *_price_warm_eur.
MAX_PRICE_KEYS = ("max_price_eur", "max_price_warm_eur", "max_total_rent_eur")
MIN_PRICE_KEYS = ("min_price_eur", "min_price_warm_eur", "min_total_rent_eur")

# Порядок — від найзрозумілішої поступки.
RELAX_CODES = ("price", "rooms", "area", "districts", "minprice", "maxrooms", "maxarea")


def _num(value) -> Optional[float]:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def available(criteria: Dict) -> List[str]:
    """Які поступки взагалі мають сенс для цього фільтра."""
    codes = []
    if any(_num(criteria.get(key)) is not None for key in MAX_PRICE_KEYS):
        codes.append("price")
    if (_num(criteria.get("min_rooms")) or 0) >= 2:
        codes.append("rooms")
    if _num(criteria.get("min_area_m2")):
        codes.append("area")
    if criteria.get("districts"):
        codes.append("districts")
    if any(_num(criteria.get(key)) for key in MIN_PRICE_KEYS):
        codes.append("minprice")
    if _num(criteria.get("max_rooms")) is not None:
        codes.append("maxrooms")
    if _num(criteria.get("max_area_m2")) is not None:
        codes.append("maxarea")
    return codes


def relax(filt: Dict, code: str) -> Dict:
    """Копія умов (пошуку чи запису однієї площадки) з однією поступкою."""
    result = dict(filt)
    if code == "price":
        for key in MAX_PRICE_KEYS:
            value = _num(result.get(key))
            if value is not None:
                result[key] = value + PRICE_STEP_EUR
    elif code == "rooms":
        value = _num(result.get("min_rooms"))
        if value is not None:
            result["min_rooms"] = value - 1 if value - 1 >= 1 else None
    elif code == "area":
        value = _num(result.get("min_area_m2"))
        if value is not None:
            result["min_area_m2"] = value - AREA_STEP_M2 if value - AREA_STEP_M2 > 0 else None
    elif code == "districts":
        if "districts" in result:
            result["districts"] = "" if isinstance(result["districts"], str) else []
    elif code == "minprice":
        for key in MIN_PRICE_KEYS:
            if key in result:
                result[key] = None
    elif code == "maxrooms":
        value = _num(result.get("max_rooms"))
        if value is not None:
            result["max_rooms"] = value + 1
    elif code == "maxarea":
        value = _num(result.get("max_area_m2"))
        if value is not None:
            result["max_area_m2"] = value + AREA_STEP_M2
    return result


class Market:
    """Усі квартири за RELAX_DAYS днів, прочитані один раз на прохід."""

    def __init__(self, days: int = RELAX_DAYS, immowelt_filters: Optional[list] = None) -> None:
        self.days = days
        cutoff = datetime.utcnow() - timedelta(days=days)
        self.local: Dict[str, List[Dict]] = {}
        session = DBSession()
        try:
            for source, model in hm._LISTING_MODELS.items():
                store, _matching = hm._LOCAL_SOURCE_MODULES[source]
                rows = session.query(model).filter(model.first_seen_at >= cutoff).all()
                self.local[source] = [store.listing_to_dict(row) for row in rows]
        finally:
            session.close()
        self._immowelt_filters = immowelt_filters
        self._local_filters: Dict[str, Dict[int, Dict]] = {}

    def _immowelt_by_id(self) -> Dict[int, Dict]:
        if self._immowelt_filters is None:
            self._immowelt_filters = hm._all_immowelt_filters()
        return {
            fid: item for item in self._immowelt_filters
            for fid in [hm._filter_id(item)] if fid is not None
        }

    def member_filters(self, members) -> List[Tuple[str, Dict]]:
        """(джерело, збережений запис) для кожного члена пошуку, що ще існує."""
        result = []
        immowelt = None
        for source, filter_id in members:
            if source == "immowelt":
                if immowelt is None:
                    immowelt = self._immowelt_by_id()
                filt = immowelt.get(int(filter_id))
            elif source in hm._LOCAL_SOURCE_MODULES:
                if source not in self._local_filters:
                    store = hm._LOCAL_SOURCE_MODULES[source][0]
                    self._local_filters[source] = {int(f["filter_id"]): f for f in store.list_filters()}
                filt = self._local_filters[source].get(int(filter_id))
            else:
                filt = None
            if filt is not None:
                result.append((source, filt))
        return result

    def count(self, filters: List[Tuple[str, Dict]], code: Optional[str] = None) -> Optional[int]:
        total = 0
        counted = False
        for source, filt in filters:
            filt = relax(filt, code) if code else filt
            if source == "immowelt":
                hits = hm._immowelt_hits_since(filt, self.days)
            else:
                matching = hm._LOCAL_SOURCE_MODULES[source][1]
                hits = sum(1 for listing in self.local.get(source, []) if matching.matches_filter(listing, filt))
            if hits is not None:
                total += hits
                counted = True
        return total if counted else None


def offers(market: Market, criteria: Dict, filters: List[Tuple[str, Dict]]) -> Tuple[Optional[int], List[Tuple[str, int]]]:
    """(скільки зараз, [(поступка, скільки стало б)]) — найвигідніші спершу."""
    base = market.count(filters)
    if base is None:
        return None, []
    need = max(1, math.ceil(base * MIN_GAIN_SHARE))
    found = []
    for code in available(criteria):
        try:
            count = market.count(filters, code)
        except Exception:
            logger.exception("Could not count the %r relaxation", code)
            continue
        if count is not None and count - base >= need:
            found.append((code, count))
    found.sort(key=lambda item: (-item[1], RELAX_CODES.index(item[0])))
    return base, found[:MAX_OFFERS]


def _fmt(value) -> str:
    return hm._preset_label(value, "")


def offer_label(code: str, criteria: Dict, lang: str) -> str:
    if code == "price":
        return i18n.t("housing.relax.price", lang, step=PRICE_STEP_EUR)
    if code == "rooms":
        old = _num(criteria.get("min_rooms"))
        return i18n.t("housing.relax.rooms", lang, new=_fmt(old - 1), old=_fmt(old))
    if code == "area":
        old = _num(criteria.get("min_area_m2"))
        new = old - AREA_STEP_M2
        if new <= 0:
            return i18n.t("housing.relax.area_none", lang)
        return i18n.t("housing.relax.area", lang, new=_fmt(new), old=_fmt(old))
    if code == "districts":
        return i18n.t("housing.relax.districts", lang)
    if code == "minprice":
        return i18n.t("housing.relax.minprice", lang)
    if code == "maxrooms":
        old = _num(criteria.get("max_rooms"))
        return i18n.t("housing.relax.maxrooms", lang, new=_fmt(old + 1), old=_fmt(old))
    if code == "maxarea":
        old = _num(criteria.get("max_area_m2"))
        return i18n.t("housing.relax.maxarea", lang, new=_fmt(old + AREA_STEP_M2), old=_fmt(old))
    return code


def offers_block(criteria: Dict, base: int, found: List[Tuple[str, int]], lang: str) -> str:
    lines = [i18n.t("housing.relax.intro", lang, days=RELAX_DAYS)]
    for code, count in found:
        lines.append(i18n.t(
            "housing.relax.line", lang,
            label=html.escape(offer_label(code, criteria, lang)), count=count, gain=count - base,
        ))
    lines.append(i18n.t("housing.relax.tap", lang))
    return "\n".join(lines)


def offer_rows(search_id: int, criteria: Dict, base: int, found: List[Tuple[str, int]], lang: str) -> list:
    return [
        [InlineKeyboardButton(
            i18n.t("housing.relax.btn", lang, label=offer_label(code, criteria, lang), gain=count - base),
            callback_data=f"{CALLBACK_PREFIX}{int(search_id)}:{code}",
        )]
        for code, count in found
    ]


def report_for_search(user_id: int, search_id: int, market: Optional[Market] = None):
    """(пошук, скільки зараз, поступки) або None, якщо пошуку вже нема."""
    search = housing_search_store.get(int(search_id), int(user_id))
    if search is None:
        return None
    market = market or Market()
    filters = market.member_filters(search["members"])
    if not filters:
        return None
    base, found = offers(market, search["criteria"], filters)
    return search, base, found


def apply(update: Update, context: CallbackContext) -> None:
    """Кнопка поступки: перестворює фільтр із новими умовами."""
    query = update.callback_query
    user = update.effective_user
    if not query or not user:
        return
    try:
        _prefix, _relax, raw_id, code = query.data.split(":")
        search_id = int(raw_id)
    except ValueError:
        query.answer()
        return
    lang = i18n.get_lang(user.id)
    search = housing_search_store.get(search_id, int(user.id))
    if search is None or code not in RELAX_CODES or not hm.is_allowed(user.id):
        query.answer(i18n.t("housing.toast.not_your_filter", lang), show_alert=True)
        return
    criteria = relax(search["criteria"], code)
    query.answer(i18n.t("housing.relax.applying", lang))
    try:
        query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    state = {
        "mode": "multi", "step": "relax", "user_id": int(user.id), "dialog_lang": lang,
        "replace_search_id": search_id,
        "sources_selected": [source for source, _filter_id in search["members"]],
        "coops_selected": list(search["coops"]),
        "districts_selected": list(criteria.get("districts") or []),
        "property_types_selected": list(criteria.get("property_types") or []),
    }
    for key in (
        "min_rooms", "max_rooms", "min_area_m2", "max_area_m2",
        "min_price_eur", "max_price_eur", "min_price_warm_eur", "max_price_warm_eur",
    ):
        state[key] = criteria.get(key)
    hm._finalize_multi_filter(query.message, context, state)


def _search_number(user_id: int, search_id: int) -> int:
    return next(
        (index for index, search in enumerate(housing_search_store.list_for_user(user_id), 1)
         if search["search_id"] == search_id),
        1,
    )


def _active(filters: List[Tuple[str, Dict]]) -> bool:
    return any(bool(filt.get("active", True)) for _source, filt in filters)


def _users_with_flats_since(cutoff: datetime) -> set:
    session = DBSession()
    try:
        return {
            int(row.user_id)
            for row in session.query(HousingSendLog.user_id).filter(HousingSendLog.sent_at >= cutoff).distinct()
        }
    finally:
        session.close()


def quiet_message(user_id: int, search: Dict, base: int, found: List[Tuple[str, int]]) -> Tuple[str, InlineKeyboardMarkup]:
    lang = i18n.get_lang(user_id)
    number = _search_number(user_id, search["search_id"])
    text = i18n.t("housing.relax.quiet", lang, n=number, days=QUIET_DAYS, month=RELAX_DAYS, total=base)
    rows = []
    if found:
        text += "\n\n" + offers_block(search["criteria"], base, found, lang)
        rows = offer_rows(search["search_id"], search["criteria"], base, found, lang)
    else:
        text += "\n\n" + i18n.t("housing.relax.quiet_no_offers", lang)
    rows.append([InlineKeyboardButton(i18n.t("housing.btn.self_manage", lang), callback_data="housing:self_manage")])
    return text, InlineKeyboardMarkup(rows)


def quiet_filters_job(context, only: Optional[set] = None) -> Dict[str, int]:
    """Раз на день: тим, кому тиждень не прийшло жодної квартири, — чому і що змінити.

    `only` — обмежити прохід цими людьми (для ручної перевірки)."""
    bot = context.bot
    now = datetime.utcnow()
    since = now - timedelta(days=QUIET_DAYS)
    got_flats = _users_with_flats_since(since)
    by_user: Dict[int, List[Dict]] = {}
    for search in housing_search_store.list_all():
        by_user.setdefault(int(search["user_id"]), []).append(search)
    market = None
    sent = 0
    for user_id, searches in sorted(by_user.items()):
        if only is not None and user_id not in only:
            continue
        if user_id == hm.ADMIN_ID or user_id in got_flats:
            continue
        if not housing_journey_store.quiet_nudge_due(user_id, now - timedelta(days=QUIET_REPEAT_DAYS)):
            continue
        old_enough = [s for s in searches if s["created_at"] is not None and s["created_at"] <= since]
        if not old_enough:
            continue
        if market is None:
            immowelt_filters = hm._all_immowelt_filters()
            market = Market(RELAX_DAYS, immowelt_filters)
            week = Market(QUIET_DAYS, immowelt_filters)
        messages = []
        for search in old_enough:
            filters = market.member_filters(search["members"])
            if not filters or not _active(filters):
                continue
            try:
                # «На сайтах нічого не було» кажемо, лише якщо це правда. Якщо
                # за тиждень підхожі квартири з'являлись, а людині не прийшло
                # нічого, то це зникле за безкоштовну затримку або збій
                # доставки, а не порожній ринок.
                week_count = week.count(week.member_filters(search["members"]))
                if week_count is None or week_count > 0:
                    if week_count:
                        logger.warning(
                            "Quiet filter %s of %s: %s matching flats this week, none delivered",
                            search["search_id"], user_id, week_count,
                        )
                    continue
                base, found = offers(market, search["criteria"], filters)
            except Exception:
                logger.exception("Could not count relaxations for search %s", search["search_id"])
                continue
            if base is None:
                continue
            messages.append(quiet_message(user_id, search, base, found))
            if len(messages) >= QUIET_SEARCHES_SHOWN:
                break
        if not messages:
            continue
        for text, keyboard in messages:
            try:
                bot.send_message(chat_id=user_id, text=text, parse_mode="HTML", reply_markup=keyboard)
            except Exception:
                logger.warning("Could not send the quiet-filter message to %s", user_id, exc_info=True)
            time.sleep(SEND_PAUSE_SECONDS)
        housing_journey_store.mark_quiet_nudge_sent(user_id)
        sent += 1
    logger.info("Quiet housing filters: told %s people", sent)
    return {"sent": sent}
