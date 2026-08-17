"""Scheduled locals® scan: a plain HTTP GET, no login or browser needed.

locals.de/wohnung-mieten-potsdam is a small, curated landing page — unlike
SCHOBA's "portfolio never goes fully empty" case, this market can legitimately
show zero current Potsdam rentals on a given day, so a zero-listings result is
not treated as a parser-broke signal worth alerting the admin about.
"""

import logging
import os
from typing import Dict, List

import requests

from user_jobs import locals_matching, locals_parser, locals_store

logger = logging.getLogger(__name__)

CHECK_ENABLED = os.getenv("LOCALS_CHECK_ENABLED", "1") == "1"
TIMEOUT = int(os.getenv("LOCALS_TIMEOUT", "30") or 30)
CHECK_INTERVAL_SECONDS = 30 * 60
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
_USER_AGENT = "Mozilla/5.0 (compatible; PotsdamHousingBot/1.0)"


def _fetch_listings() -> List[Dict]:
    response = requests.get(
        locals_parser.LISTINGS_URL, timeout=TIMEOUT, headers={"User-Agent": _USER_AGENT},
    )
    response.raise_for_status()
    return locals_parser.parse_listings(response.text)


def check_job(context) -> Dict[str, int]:
    if not CHECK_ENABLED:
        locals_store.record_status("disabled", listings_count=0)
        return {"ok": 1, "enabled": 0, "sent": 0}
    bot = context.bot
    try:
        all_listings = _fetch_listings()
    except Exception as exc:
        locals_store.record_status("error", listings_count=0, error=str(exc))
        logger.warning("locals® scan failed: %s", exc)
        return {"ok": 0, "enabled": 1, "sent": 0}
    stored = locals_store.upsert_listings(all_listings)
    locals_store.record_status("ok", listings_count=stored)
    active_listings = locals_store.list_active_listings()
    filters = locals_store.list_filters(active_only=True)
    matches = locals_store.select_unsent_matches(active_listings, filters, locals_store.delivered_pairs())
    sent = 0
    for filt, listing in matches:
        text = locals_matching.format_notification(listing)
        bot.send_message(chat_id=int(filt["user_id"]), text=text, parse_mode="HTML", disable_web_page_preview=False)
        locals_store.mark_delivered(int(filt["filter_id"]), str(listing["listing_key"]))
        sent += 1
    logger.info(
        "locals® scan total=%s stored=%s filters=%s sent=%s",
        len(all_listings), stored, len(filters), sent,
    )
    return {"ok": 1, "enabled": 1, "stored": stored, "filters": len(filters), "sent": sent}
