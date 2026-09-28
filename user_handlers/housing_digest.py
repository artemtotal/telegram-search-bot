"""Weekly housing digest: every new flat on the market next to the ones that
matched the person's own filters.

The point is for people to see for themselves that the bot is working and
whether their filter is any good: "97 new flats this week, 2 for you" with a
chart of where the filter cuts says more than any advice from the admin.

Immowelt's full catalogue lives in check-Wohnung (the bot only ever sees the
Immowelt flats it delivered), so its side comes from the receiver's preview
with `since_days`/`include_rows`; the other eight sources are read from the
bot's own tables.
"""

import logging
import time
from datetime import datetime, timedelta, timezone
from datetime import time as dtime
from typing import Dict, List, Optional, Set

import pytz
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import i18n
from database import DBSession, HousingSendLog
from user_handlers import housing_monitor as hm
from user_jobs import housing_journey_store, housing_stats_chart, housing_tier

logger = logging.getLogger(__name__)

DIGEST_DAYS = 7
# Monday morning: 07:00 UTC is 09:00 in Berlin in summer, 08:00 in winter. The
# job queue (APScheduler 3 under PTB 13) only accepts pytz zones, so no
# ZoneInfo here.
DIGEST_WEEKDAY = 0
DIGEST_TIME = dtime(7, 0, tzinfo=pytz.utc)
SEND_PAUSE_SECONDS = 0.1


def _price(listing: Dict) -> Optional[float]:
    for key in ("price_eur", "price_warm_eur", "total_rent_eur"):
        value = listing.get(key)
        if value not in (None, ""):
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
    return None


def _num(value) -> Optional[float]:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _as_row(listing: Dict) -> tuple:
    return (_num(listing.get("rooms")), _num(listing.get("area_m2")), _price(listing))


class WeekCatalog:
    """Every listing first seen in the window, loaded once per run."""

    def __init__(self, cutoff: datetime) -> None:
        self.cutoff = cutoff
        # source -> [(listing dict, gone within the free delay?)]
        self.local: Dict[str, List[tuple]] = {}
        session = DBSession()
        try:
            for source, model in hm._LISTING_MODELS.items():
                store, _matching = hm._LOCAL_SOURCE_MODULES[source]
                entries = []
                for row in session.query(model).filter(model.first_seen_at >= cutoff).all():
                    last_seen = getattr(row, "last_seen_at", None)
                    gone_fast = bool(
                        getattr(row, "is_active", True) is False
                        and last_seen is not None
                        and last_seen - row.first_seen_at < housing_tier.FREE_DELAY
                    )
                    entries.append((store.listing_to_dict(row), gone_fast))
                self.local[source] = entries
        finally:
            session.close()
        preview = hm._preview_criteria({"since_days": DIGEST_DAYS, "include_rows": True})
        self.immowelt_rows = list(preview.get("rows") or []) if isinstance(preview, dict) else []

    def all_rows(self) -> List[tuple]:
        rows = [_as_row(listing) for entries in self.local.values() for listing, _gone in entries]
        rows.extend(_as_row(row) for row in self.immowelt_rows)
        return rows


def _immowelt_matches(filters: List[Dict]) -> Dict[str, Dict]:
    """listing_key -> row, for Immowelt flats any of these filters caught."""
    matched = {}
    for filt in filters:
        criteria = {"districts": list(filt.get("districts") or [])}
        for key in hm.IMMOWELT_CRITERIA_KEYS:
            criteria[key] = filt.get(key)
        preview = hm._preview_criteria({**criteria, "since_days": DIGEST_DAYS, "include_rows": True})
        for row in (preview.get("rows") or []) if isinstance(preview, dict) else []:
            if row.get("match"):
                matched[str(row.get("listing_key"))] = row
    return matched


def _user_filters(immowelt_filters: List[Dict], user_id: int) -> Dict[str, List[Dict]]:
    by_source = {}
    for source, (store, _matching) in hm._LOCAL_SOURCE_MODULES.items():
        filters = store.list_filters(user_id=user_id, active_only=True)
        if filters:
            by_source[source] = filters
    immowelt = [f for f in immowelt_filters if int(f.get("user_id") or 0) == user_id and f.get("active")]
    if immowelt:
        by_source["immowelt"] = immowelt
    return by_source


def _sent_since(user_id: int, cutoff: datetime) -> int:
    session = DBSession()
    try:
        return session.query(HousingSendLog).filter(
            HousingSendLog.user_id == int(user_id), HousingSendLog.sent_at >= cutoff,
        ).count()
    finally:
        session.close()


def user_week(catalog: WeekCatalog, filters: Dict[str, List[Dict]], user_id: int) -> Dict:
    mine_rows = []
    missed = 0
    for source, source_filters in filters.items():
        if source == "immowelt":
            mine_rows.extend(_as_row(row) for row in _immowelt_matches(source_filters).values())
            continue
        _store, matching = hm._LOCAL_SOURCE_MODULES[source]
        for listing, gone_fast in catalog.local.get(source, []):
            if any(matching.matches_filter(listing, f) for f in source_filters):
                mine_rows.append(_as_row(listing))
                missed += int(gone_fast)
    return {
        "mine_rows": mine_rows,
        "missed": missed,
        "sent": _sent_since(user_id, catalog.cutoff),
    }


def _caption(user_id: int, lang: str, total: int, week: Dict, start: datetime, end: datetime) -> str:
    mine = len(week["mine_rows"])
    parts = [i18n.t(
        "housing.digest.caption", lang,
        start=start.strftime("%d.%m"), end=end.strftime("%d.%m"),
        total=total, mine=mine, sent=week["sent"],
    )]
    if week["missed"] and not housing_tier.is_premium(user_id):
        parts.append(i18n.t(
            "housing.digest.missed", lang, missed=week["missed"],
            hours=housing_tier.free_delay_hours(), price=housing_tier.PREMIUM_PRICE_EUR,
        ))
    if mine <= hm.THIN_FILTER_MAX_HITS:
        parts.append(i18n.t("housing.digest.thin", lang))
    else:
        parts.append(i18n.t("housing.digest.legend_hint", lang))
    return "\n\n".join(parts)


def _keyboard(user_id: int, lang: str) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(i18n.t("housing.btn.self_manage", lang), callback_data="housing:self_manage")]]
    if not housing_tier.is_premium(user_id):
        rows.append([InlineKeyboardButton(i18n.t("housing.btn.request_access", lang), callback_data="housing:access_request")])
    rows.append([InlineKeyboardButton(i18n.t("housing.digest.btn_off", lang), callback_data="housing:digest_off")])
    return InlineKeyboardMarkup(rows)


def send_digest(bot, user_id: int, catalog: WeekCatalog, filters: Dict[str, List[Dict]], now: datetime) -> bool:
    lang = i18n.get_lang(user_id)
    week = user_week(catalog, filters, user_id)
    all_rows = catalog.all_rows()
    start = (catalog.cutoff.replace(tzinfo=timezone.utc)).astimezone(hm.BERLIN_TZ)
    end = now.replace(tzinfo=timezone.utc).astimezone(hm.BERLIN_TZ)
    chart = housing_stats_chart.render_comparison(
        all_rows, week["mine_rows"],
        i18n.t("housing.digest.chart_title", lang),
        {
            "area": i18n.t("housing.stats.axis_area", lang),
            "price": i18n.t("housing.stats.axis_price", lang),
            "rooms": i18n.t("housing.stats.axis_rooms", lang),
        },
        {"all": i18n.t("housing.digest.legend_all", lang), "mine": i18n.t("housing.digest.legend_mine", lang)},
    )
    bot.send_photo(
        chat_id=user_id, photo=chart,
        caption=_caption(user_id, lang, len(all_rows), week, start, end),
        parse_mode="HTML", reply_markup=_keyboard(user_id, lang),
    )
    return True


def recipients(immowelt_filters: List[Dict]) -> Set[int]:
    ids = set()
    for store, _matching in hm._LOCAL_SOURCE_MODULES.values():
        ids |= {int(f["user_id"]) for f in store.list_filters(active_only=True)}
    ids |= {int(f["user_id"]) for f in immowelt_filters if f.get("active") and f.get("user_id")}
    return ids - housing_journey_store.digest_off_ids()


def weekly_digest_job(context, only: Optional[Set[int]] = None) -> Dict[str, int]:
    """Monday morning: one digest per person with at least one active filter.
    `only` limits the run to these people (for trying it out by hand)."""
    bot = context.bot
    now = datetime.utcnow()
    catalog = WeekCatalog(now - timedelta(days=DIGEST_DAYS))
    immowelt_filters = hm._all_immowelt_filters()
    targets = recipients(immowelt_filters) if only is None else set(only)
    sent = failed = 0
    for user_id in sorted(targets):
        filters = _user_filters(immowelt_filters, user_id)
        if not filters:
            continue
        try:
            send_digest(bot, user_id, catalog, filters, now)
            sent += 1
        except Exception:
            failed += 1
            logger.warning("Could not send the weekly digest to %s", user_id, exc_info=True)
        time.sleep(SEND_PAUSE_SECONDS)
    logger.info("Weekly housing digest: sent=%s failed=%s", sent, failed)
    return {"sent": sent, "failed": failed}
