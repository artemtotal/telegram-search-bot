"""Scheduled ProPotsdam collector integration for PotsdamBot."""

import logging
import os
from typing import Dict, List

import requests

from user_jobs import propotsdam_matching, propotsdam_store

logger = logging.getLogger(__name__)

PROPOTSDAM_RECEIVER_URL = os.getenv(
    "PROPOTSDAM_RECEIVER_URL",
    "http://host.docker.internal:18766",
).rstrip("/")
PROPOTSDAM_PORTAL_URL = os.getenv(
    "PROPOTSDAM_PORTAL_URL",
    "https://propotsdam-kundenportal.easysquare.com/propotsdam-kundenportal/index.html#/formlist/%252Fsection%252F0%252Fbox%252F0",
)
PROPOTSDAM_CHECK_ENABLED = os.getenv("PROPOTSDAM_CHECK_ENABLED", "0") == "1"
PROPOTSDAM_TIMEOUT = int(os.getenv("PROPOTSDAM_TIMEOUT", "60") or 60)


def _request_scan() -> List[Dict]:
    response = requests.post(f"{PROPOTSDAM_RECEIVER_URL}/api/propotsdam/scan", timeout=PROPOTSDAM_TIMEOUT)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or not payload.get("ok"):
        raise RuntimeError(str(payload.get("error") if isinstance(payload, dict) else payload))
    listings = payload.get("listings") or []
    if not isinstance(listings, list):
        raise RuntimeError("collector returned non-list listings")
    return listings


def check_job(context) -> Dict[str, int]:
    if not PROPOTSDAM_CHECK_ENABLED:
        return {"ok": 1, "enabled": 0, "sent": 0}
    bot = context.bot
    listings = _request_scan()
    stored = propotsdam_store.upsert_listings(listings)
    active_listings = propotsdam_store.list_active_listings()
    filters = propotsdam_store.list_filters(active_only=True)
    matches = propotsdam_store.select_unsent_matches(active_listings, filters, propotsdam_store.delivered_pairs())
    sent = 0
    for filt, listing in matches:
        text = propotsdam_matching.format_notification(listing, PROPOTSDAM_PORTAL_URL)
        bot.send_message(chat_id=int(filt["user_id"]), text=text, parse_mode="HTML", disable_web_page_preview=False)
        propotsdam_store.mark_delivered(int(filt["filter_id"]), str(listing["listing_key"]))
        sent += 1
    logger.info("ProPotsdam scan stored=%s filters=%s matches=%s sent=%s", stored, len(filters), len(matches), sent)
    return {"ok": 1, "enabled": 1, "stored": stored, "filters": len(filters), "sent": sent}
