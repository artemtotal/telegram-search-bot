"""Scheduled SCHOBA scan: a plain HTTP GET, no login or browser needed.

The page is a portfolio showcase mixing vacant and already-rented units — only
`is_vacant` listings get stored and matched, so nobody gets notified about an
apartment that says "# vermietet" right there in the listing.
"""

import html
import logging
import os
from typing import Dict, List

import requests

from user_jobs import schoba_matching, schoba_parser, schoba_store

logger = logging.getLogger(__name__)

CHECK_ENABLED = os.getenv("SCHOBA_CHECK_ENABLED", "1") == "1"
TIMEOUT = int(os.getenv("SCHOBA_TIMEOUT", "30") or 30)
CHECK_INTERVAL_SECONDS = 15 * 60
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
_USER_AGENT = "Mozilla/5.0 (compatible; PotsdamHousingBot/1.0)"


def _fetch_listings() -> List[Dict]:
    response = requests.get(
        schoba_parser.LISTINGS_URL, timeout=TIMEOUT, headers={"User-Agent": _USER_AGENT},
    )
    response.raise_for_status()
    return schoba_parser.parse_listings(response.text)


def _notify_admin_parse_broke(bot) -> None:
    """Zero cards parsed at all (vacant or rented) means the page's markup
    likely changed under the parser — the portfolio never goes fully empty."""
    if not ADMIN_ID:
        return
    text = (
        "⚠️ <b>SCHOBA: розбір сторінки повернув 0 оголошень (навіть здані в оренду)</b>\n\n"
        "Схоже, змінилась розмітка сторінки schoba.de/immobilien/angebote/mieten.htm "
        "і парсер більше не розпізнає картки оголошень — варто перевірити вручну."
    )
    try:
        bot.send_message(chat_id=ADMIN_ID, text=text, parse_mode="HTML", disable_web_page_preview=True)
    except Exception:
        logger.exception("Could not notify admin about a broken SCHOBA parse")


def check_job(context) -> Dict[str, int]:
    if not CHECK_ENABLED:
        schoba_store.record_status("disabled", listings_count=0)
        return {"ok": 1, "enabled": 0, "sent": 0}
    bot = context.bot
    try:
        all_listings = _fetch_listings()
    except Exception as exc:
        schoba_store.record_status("error", listings_count=0, error=str(exc))
        logger.warning("SCHOBA scan failed: %s", exc)
        return {"ok": 0, "enabled": 1, "sent": 0}
    if not all_listings:
        _notify_admin_parse_broke(bot)
    vacant_listings = [item for item in all_listings if item.get("is_vacant")]
    stored = schoba_store.upsert_listings(vacant_listings)
    schoba_store.record_status("ok", listings_count=stored)
    active_listings = schoba_store.list_active_listings()
    filters = schoba_store.list_filters(active_only=True)
    matches = schoba_store.select_unsent_matches(active_listings, filters, schoba_store.delivered_pairs())
    sent = 0
    for filt, listing in matches:
        text = schoba_matching.format_notification(listing)
        bot.send_message(chat_id=int(filt["user_id"]), text=text, parse_mode="HTML", disable_web_page_preview=False)
        schoba_store.mark_delivered(int(filt["filter_id"]), str(listing["listing_key"]))
        sent += 1
    logger.info(
        "SCHOBA scan total=%s vacant=%s stored=%s filters=%s sent=%s",
        len(all_listings), len(vacant_listings), stored, len(filters), sent,
    )
    return {"ok": 1, "enabled": 1, "stored": stored, "filters": len(filters), "sent": sent}
