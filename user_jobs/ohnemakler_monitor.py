"""Scheduled ohne-makler.net scan — private landlords, no agent's fee.

Stage 6 of docs/housing-region-plan.md (02.11.2026). Plain GET of the Berlin
and the Brandenburg search, `?page=N`, 24 a page. The order cannot be set by
link, so every scan reads all pages (≈8 + 5) once in 30 minutes and takes down
what is gone. The very first scan is a baseline. Places decide by postcode.
"""

import html
import logging
import math
import os
import time
from datetime import timedelta
from typing import Dict, List, Tuple

import requests

import i18n
from database import OhnemaklerListing
from user_jobs import (
    alert_cooldown, delivery_dedup, housing_tier, ohnemakler_matching, ohnemakler_parser, ohnemakler_store,
)

logger = logging.getLogger(__name__)

CHECK_ENABLED = os.getenv("OHNEMAKLER_CHECK_ENABLED", "1") == "1"
TIMEOUT = int(os.getenv("OHNEMAKLER_TIMEOUT", "30") or 30)
CHECK_INTERVAL_SECONDS = 30 * 60
REGIONS = ("berlin", "brandenburg")
MAX_PAGES = 30
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
ERROR_ALERT_COOLDOWN = timedelta(hours=2)
FULL_SWEEP_EVERY = timedelta(hours=6)
FULL_SWEEP_MAX_PAGES = 150
NEW_PAGES_MAX = 5
PAGE_PAUSE_SECONDS = 1.0
CAPTION_LIMIT = 1024
FULL_KEY = "full"
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)


def _fetch_page(region: str, page: int) -> Tuple[List[Dict], int]:
    """(оголошення сторінки, скільки всього в регіоні)."""
    response = requests.get(
        ohnemakler_parser.page_url(region, page), timeout=TIMEOUT,
        headers={"User-Agent": _USER_AGENT, "Accept-Language": "de-DE,de;q=0.9"},
    )
    if response.status_code == 404 and page > 1:
        return [], 0  # сторінки за останньою сайт віддає як 404 — це кінець видачі
    response.raise_for_status()
    return ohnemakler_parser.parse_listings(response.text), ohnemakler_parser.total_count(response.text)


def _full_sweep_due() -> bool:
    last = (ohnemakler_store.latest_status_for(FULL_KEY) or {}).get("last_checked_at")
    return last is None or ohnemakler_store.utc_now() - last >= FULL_SWEEP_EVERY


def _fetch(known: set, full: bool = True) -> List[Dict]:
    """Усі сторінки обох регіонів; сторінка без жодного нового id — кінець
    (за останньою сайт повертає знову перші)."""
    listings: List[Dict] = []
    seen = set()
    for region in REGIONS:
        pages = MAX_PAGES
        page = 1
        while page <= pages:
            time.sleep(PAGE_PAUSE_SECONDS)
            found, total = _fetch_page(region, page)
            if page == 1 and total:
                pages = min(MAX_PAGES, math.ceil(total / ohnemakler_parser.PAGE_SIZE))
            items = [item for item in found if item["listing_key"] not in seen]
            if not items:
                break
            for item in items:
                seen.add(item["listing_key"])
                listings.append(item)
            page += 1
    return listings


def _baseline(listings: List[Dict]) -> int:
    marked = 0
    for filt in ohnemakler_store.list_filters():
        for listing in listings:
            if ohnemakler_matching.matches_filter(listing, filt):
                ohnemakler_store.mark_delivered(int(filt["filter_id"]), str(listing["listing_key"]))
                marked += 1
    return marked


def _send_listing(bot, chat_id: int, listing: Dict, text: str) -> bool:
    photo = str(listing.get("cover_image_url") or "").strip()
    if not photo or len(text) > CAPTION_LIMIT:
        return False
    try:
        bot.send_photo(chat_id=chat_id, photo=photo, caption=text, parse_mode="HTML")
        return True
    except Exception:
        logger.exception("Could not send ohne-makler.net photo to %s", chat_id)
        return False


def _notify_admin_fetch_failed(bot, error: Exception, previous_status: Dict) -> bool:
    if not ADMIN_ID:
        return False
    if previous_status.get("last_status") == "error" and not alert_cooldown.due("ohnemakler:fetch", ERROR_ALERT_COOLDOWN):
        return False
    text = f"⚠️ <b>ohne-makler.net: не вдалося перевірити оголошення</b>\n\nПричина: {html.escape(str(error))}"
    try:
        bot.send_message(chat_id=ADMIN_ID, text=text, parse_mode="HTML", disable_web_page_preview=True)
        alert_cooldown.mark("ohnemakler:fetch")
        return True
    except Exception:
        logger.exception("Could not notify admin about an ohne-makler.net fetch failure")
        return False


def check_job(context) -> Dict[str, int]:
    if not CHECK_ENABLED:
        ohnemakler_store.record_status("disabled", listings_count=0)
        return {"ok": 1, "enabled": 0, "sent": 0}
    bot = context.bot
    known = ohnemakler_store.known_keys()
    first_ever = not known
    full = True
    try:
        listings = _fetch(known, full)
    except Exception as exc:
        previous_status = ohnemakler_store.latest_status()
        alerted = _notify_admin_fetch_failed(bot, exc, previous_status)
        ohnemakler_store.record_status("error", listings_count=0, error=str(exc))
        logger.warning("ohne-makler.net scan failed; admin_alerted=%s: %s", alerted, exc)
        return {"ok": 0, "enabled": 1, "sent": 0, "admin_alerted": int(alerted)}
    stored = ohnemakler_store.upsert_listings(listings, deactivate_missing=full and bool(listings))
    ohnemakler_store.record_status("ok", listings_count=stored)
    if full:
        ohnemakler_store.record_status_for(FULL_KEY, "ok", listings_count=stored)
    if first_ever:
        logger.info("ohne-makler.net first scan: %s flats, baseline marks=%s", stored, _baseline(listings))
    filters = ohnemakler_store.list_filters(active_only=True)
    matches = ohnemakler_store.select_unsent_matches(
        ohnemakler_store.list_active_listings(), filters, ohnemakler_store.delivered_pairs(),
    )
    sent = 0
    dedup = delivery_dedup.PerUserDedup()
    gate = housing_tier.DeliveryGate("ohnemakler", OhnemaklerListing)
    for filt, listing in matches:
        chat_id = int(filt["user_id"])
        listing_key = str(listing["listing_key"])
        if not gate.due(chat_id, listing):
            continue
        if not dedup.claim(chat_id, listing_key):
            ohnemakler_store.mark_delivered(int(filt["filter_id"]), listing_key)
            continue
        text = ohnemakler_matching.format_notification(listing, lang=i18n.get_lang(chat_id))
        text += gate.footer(chat_id, listing_key)
        try:
            if not _send_listing(bot, chat_id, listing, text):
                bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML", disable_web_page_preview=False)
        except Exception as exc:
            if housing_tier.recipient_gone(exc):
                ohnemakler_store.mark_delivered(int(filt["filter_id"]), listing_key)
            else:
                logger.exception("Could not send ohne-makler.net listing %s to %s", listing_key, chat_id)
            continue
        ohnemakler_store.mark_delivered(int(filt["filter_id"]), listing_key)
        gate.sent(chat_id, listing_key, int(filt["filter_id"]))
        sent += 1
    logger.info(
        "ohne-makler.net scan full=%s total=%s new=%s stored=%s filters=%s sent=%s",
        full, len(listings), sum(1 for item in listings if item["listing_key"] not in known), stored, len(filters), sent,
    )
    return {"ok": 1, "enabled": 1, "stored": stored, "filters": len(filters), "sent": sent}
