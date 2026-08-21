"""Scheduled Kleinanzeigen scan — deliberately slower than the other sources.

Kleinanzeigen is a large classifieds platform, not a small local broker site,
and its Terms of Service prohibit automated scraping. A single manual-looking
GET succeeds without a CAPTCHA, but polling it on the same cadence as the
other sources would be systematic scraping the platform could detect and act
on — so this source stays capped to once every 30 minutes even though the
others were sped up to every 15, as an explicit user decision (2026-08-21:
tightened from 60 to 30 minutes, still deliberately the slowest of the
bunch).

Also filters out two kinds of noise the raw search page includes:
- Results outside Potsdam itself — the search radius pulls in nearby towns
  (Rathenow, Berlin-Schöneberg, ...), which the caller doesn't want.
- "TAUSCHWOHNUNG" (apartment-swap) listings — these aren't available to just
  apply for, the poster wants YOUR apartment in exchange for theirs, so
  matching them against a normal search filter would be misleading.
"""

import html
import logging
import os
from typing import Dict, List

import requests

from user_jobs import kleinanzeigen_matching, kleinanzeigen_parser, kleinanzeigen_store

logger = logging.getLogger(__name__)

CHECK_ENABLED = os.getenv("KLEINANZEIGEN_CHECK_ENABLED", "1") == "1"
TIMEOUT = int(os.getenv("KLEINANZEIGEN_TIMEOUT", "30") or 30)
CHECK_INTERVAL_SECONDS = 30 * 60
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
_USER_AGENT = "Mozilla/5.0 (compatible; PotsdamHousingBot/1.0)"


def _fetch_listings() -> List[Dict]:
    response = requests.get(
        kleinanzeigen_parser.LISTINGS_URL, timeout=TIMEOUT, headers={"User-Agent": _USER_AGENT},
    )
    response.raise_for_status()
    return kleinanzeigen_parser.parse_listings(response.text)


def _is_relevant(listing: Dict) -> bool:
    city = str(listing.get("city") or "").strip().casefold()
    title = str(listing.get("title") or "").casefold()
    return city == "potsdam" and "tausch" not in title


def _notify_admin_parse_broke(bot) -> None:
    if not ADMIN_ID:
        return
    text = (
        "⚠️ <b>Kleinanzeigen: розбір сторінки повернув 0 оголошень</b>\n\n"
        "Схоже, змінилась розмітка сторінки пошуку — варто перевірити вручну."
    )
    try:
        bot.send_message(chat_id=ADMIN_ID, text=text, parse_mode="HTML", disable_web_page_preview=True)
    except Exception:
        logger.exception("Could not notify admin about a broken Kleinanzeigen parse")


def check_job(context) -> Dict[str, int]:
    if not CHECK_ENABLED:
        kleinanzeigen_store.record_status("disabled", listings_count=0)
        return {"ok": 1, "enabled": 0, "sent": 0}
    bot = context.bot
    try:
        all_listings = _fetch_listings()
    except Exception as exc:
        kleinanzeigen_store.record_status("error", listings_count=0, error=str(exc))
        logger.warning("Kleinanzeigen scan failed: %s", exc)
        return {"ok": 0, "enabled": 1, "sent": 0}
    if not all_listings:
        _notify_admin_parse_broke(bot)
    relevant = [item for item in all_listings if _is_relevant(item)]
    stored = kleinanzeigen_store.upsert_listings(relevant)
    kleinanzeigen_store.record_status("ok", listings_count=stored)
    active_listings = kleinanzeigen_store.list_active_listings()
    filters = kleinanzeigen_store.list_filters(active_only=True)
    matches = kleinanzeigen_store.select_unsent_matches(
        active_listings, filters, kleinanzeigen_store.delivered_pairs()
    )
    sent = 0
    for filt, listing in matches:
        text = kleinanzeigen_matching.format_notification(listing)
        bot.send_message(chat_id=int(filt["user_id"]), text=text, parse_mode="HTML", disable_web_page_preview=False)
        kleinanzeigen_store.mark_delivered(int(filt["filter_id"]), str(listing["listing_key"]))
        sent += 1
    logger.info(
        "Kleinanzeigen scan total=%s relevant=%s stored=%s filters=%s sent=%s",
        len(all_listings), len(relevant), stored, len(filters), sent,
    )
    return {"ok": 1, "enabled": 1, "stored": stored, "filters": len(filters), "sent": sent}
