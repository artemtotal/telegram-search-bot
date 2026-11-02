"""Scheduled inberlinwohnen.de scan — Berlin's six municipal landlords in one list.

Stage 8 of docs/housing-region-plan.md (02.11.2026). Plain GET of the
"Wohnungsfinder" page, `?page=N`, ten flats a page, newest first; every card
carries its data as JSON (see `inberlin_parser`). Each scan reads pages until
one brings nothing new; once in six hours it reads all of them (≈95) to mark
the flats that were taken down. Only filters with a place in Berlin get
these flats — a filter without a place means Potsdam.

The very first scan, before the table has a single row, is a baseline: the
flats already online are marked as sent to every matching filter, so nobody
gets nine hundred messages at once.
"""

import html
import logging
import os
import time
from datetime import timedelta
from typing import Dict, List

import requests

import i18n
from database import InberlinListing
from user_jobs import (
    alert_cooldown, delivery_dedup, housing_tier, inberlin_matching, inberlin_parser, inberlin_store,
)

logger = logging.getLogger(__name__)

CHECK_ENABLED = os.getenv("INBERLIN_CHECK_ENABLED", "1") == "1"
TIMEOUT = int(os.getenv("INBERLIN_TIMEOUT", "30") or 30)
CHECK_INTERVAL_SECONDS = 15 * 60
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


def _fetch_page(page: int) -> List[Dict]:
    response = requests.get(
        inberlin_parser.page_url(page), timeout=TIMEOUT,
        headers={"User-Agent": _USER_AGENT, "Accept-Language": "de-DE,de;q=0.9"},
    )
    response.raise_for_status()
    return inberlin_parser.parse_listings(response.text)


def _full_sweep_due() -> bool:
    last = (inberlin_store.latest_status_for(FULL_KEY) or {}).get("last_checked_at")
    return last is None or inberlin_store.utc_now() - last >= FULL_SWEEP_EVERY


def _fetch(known: set, full: bool) -> List[Dict]:
    listings: List[Dict] = []
    seen = set()
    for page in range(1, (FULL_SWEEP_MAX_PAGES if full else NEW_PAGES_MAX) + 1):
        if page > 1:
            time.sleep(PAGE_PAUSE_SECONDS)
        items = _fetch_page(page)
        fresh = 0
        for item in items:
            if item["listing_key"] in seen:
                continue
            seen.add(item["listing_key"])
            fresh += item["listing_key"] not in known
            listings.append(item)
        if not items or (not full and not fresh):
            break
    return listings


def _baseline(listings: List[Dict]) -> int:
    marked = 0
    for filt in inberlin_store.list_filters():
        for listing in listings:
            if inberlin_matching.matches_filter(listing, filt):
                inberlin_store.mark_delivered(int(filt["filter_id"]), str(listing["listing_key"]))
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
        logger.exception("Could not send inberlinwohnen photo to %s", chat_id)
        return False


def _notify_admin_fetch_failed(bot, error: Exception, previous_status: Dict) -> bool:
    if not ADMIN_ID:
        return False
    if previous_status.get("last_status") == "error" and not alert_cooldown.due("inberlin:fetch", ERROR_ALERT_COOLDOWN):
        return False
    text = f"⚠️ <b>inberlinwohnen: не вдалося перевірити оголошення</b>\n\nПричина: {html.escape(str(error))}"
    try:
        bot.send_message(chat_id=ADMIN_ID, text=text, parse_mode="HTML", disable_web_page_preview=True)
        alert_cooldown.mark("inberlin:fetch")
        return True
    except Exception:
        logger.exception("Could not notify admin about an inberlinwohnen fetch failure")
        return False


def check_job(context) -> Dict[str, int]:
    if not CHECK_ENABLED:
        inberlin_store.record_status("disabled", listings_count=0)
        return {"ok": 1, "enabled": 0, "sent": 0}
    bot = context.bot
    known = inberlin_store.known_keys()
    first_ever = not known
    full = first_ever or _full_sweep_due()
    try:
        listings = _fetch(known, full)
    except Exception as exc:
        previous_status = inberlin_store.latest_status()
        alerted = _notify_admin_fetch_failed(bot, exc, previous_status)
        inberlin_store.record_status("error", listings_count=0, error=str(exc))
        logger.warning("inberlinwohnen scan failed; admin_alerted=%s: %s", alerted, exc)
        return {"ok": 0, "enabled": 1, "sent": 0, "admin_alerted": int(alerted)}
    stored = inberlin_store.upsert_listings(listings, deactivate_missing=full and bool(listings))
    inberlin_store.record_status("ok", listings_count=stored)
    if full:
        inberlin_store.record_status_for(FULL_KEY, "ok", listings_count=stored)
    if first_ever:
        logger.info("inberlinwohnen first scan: %s flats, baseline marks=%s", stored, _baseline(listings))
    filters = inberlin_store.list_filters(active_only=True)
    matches = inberlin_store.select_unsent_matches(
        inberlin_store.list_active_listings(), filters, inberlin_store.delivered_pairs(),
    )
    sent = 0
    dedup = delivery_dedup.PerUserDedup()
    gate = housing_tier.DeliveryGate("inberlin", InberlinListing)
    for filt, listing in matches:
        chat_id = int(filt["user_id"])
        listing_key = str(listing["listing_key"])
        if not gate.due(chat_id, listing):
            continue
        if not dedup.claim(chat_id, listing_key):
            inberlin_store.mark_delivered(int(filt["filter_id"]), listing_key)
            continue
        text = inberlin_matching.format_notification(listing, lang=i18n.get_lang(chat_id))
        text += gate.footer(chat_id, listing_key)
        try:
            if not _send_listing(bot, chat_id, listing, text):
                bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML", disable_web_page_preview=False)
        except Exception as exc:
            if housing_tier.recipient_gone(exc):
                inberlin_store.mark_delivered(int(filt["filter_id"]), listing_key)
            else:
                logger.exception("Could not send inberlinwohnen listing %s to %s", listing_key, chat_id)
            continue
        inberlin_store.mark_delivered(int(filt["filter_id"]), listing_key)
        gate.sent(chat_id, listing_key, int(filt["filter_id"]))
        sent += 1
    logger.info(
        "inberlinwohnen scan full=%s total=%s new=%s stored=%s filters=%s sent=%s",
        full, len(listings), sum(1 for item in listings if item["listing_key"] not in known), stored, len(filters), sent,
    )
    return {"ok": 1, "enabled": 1, "stored": stored, "filters": len(filters), "sent": sent}
