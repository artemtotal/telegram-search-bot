"""Scheduled scan of the shared ImmoTeam/alpha (immomakler) feed.

Both sites run the same WordPress plugin, but only ImmoTeam's own query-string
filters (`vermarktungsart`/`ort`) actually work — alpha's return empty results
or 404 for the same parameters, confirmed by hand against the live sites. So
ImmoTeam is queried pre-filtered to rent+Potsdam (two requests, since "Potsdam"
and "Potsdam-Babelsberg" are separate taxonomy terms there), while alpha is
fetched unfiltered and narrowed client-side. Both feeds are then merged and
deduped by Objekt-ID — confirmed live that the exact same listing appears on
both domains under the same ID, so concatenating without dedup would double
every match.
"""

import html
import logging
import os
from typing import Dict, List

import requests

from user_jobs import regiomakler_matching, regiomakler_parser, regiomakler_store

logger = logging.getLogger(__name__)

CHECK_ENABLED = os.getenv("REGIOMAKLER_CHECK_ENABLED", "1") == "1"
TIMEOUT = int(os.getenv("REGIOMAKLER_TIMEOUT", "30") or 30)
CHECK_INTERVAL_SECONDS = 15 * 60
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
_USER_AGENT = "Mozilla/5.0 (compatible; PotsdamHousingBot/1.0)"

_IMMOTEAM_URLS = [
    "https://immoteam-potsdam.de/immobilienangebote/?vermarktungsart=miete&ort=potsdam",
    "https://immoteam-potsdam.de/immobilienangebote/?vermarktungsart=miete&ort=potsdam-babelsberg",
]
_ALPHA_URL = "https://potsdam-immobilien.de/immobilien/"


def _fetch(url: str) -> str:
    response = requests.get(url, timeout=TIMEOUT, headers={"User-Agent": _USER_AGENT})
    response.raise_for_status()
    return response.text


def _fetch_all_listings() -> List[Dict]:
    """One page per feed source; a single feed failing doesn't blank out the other."""
    listings: List[Dict] = []
    for url in _IMMOTEAM_URLS:
        html_text = _fetch(url)
        listings.extend(regiomakler_parser.parse_listings(html_text, "immoteam"))
    alpha_html = _fetch(_ALPHA_URL)
    listings.extend(regiomakler_parser.parse_listings(alpha_html, "alpha"))
    return listings


def _dedupe(listings: List[Dict]) -> List[Dict]:
    seen = set()
    unique = []
    for listing in listings:
        key = str(listing.get("listing_key") or "")
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(listing)
    return unique


def _is_relevant(listing: Dict) -> bool:
    city = str(listing.get("city") or "").casefold()
    return bool(listing.get("is_rental")) and listing.get("is_vacant") and city.startswith("potsdam")


def _notify_admin_parse_broke(bot) -> None:
    if not ADMIN_ID:
        return
    text = (
        "⚠️ <b>ImmoTeam/alpha: розбір сторінок повернув 0 оголошень з обох сайтів</b>\n\n"
        "Схоже, змінилась розмітка спільного плагіна immomakler — варто перевірити вручну."
    )
    try:
        bot.send_message(chat_id=ADMIN_ID, text=text, parse_mode="HTML", disable_web_page_preview=True)
    except Exception:
        logger.exception("Could not notify admin about a broken ImmoTeam/alpha parse")


def check_job(context) -> Dict[str, int]:
    if not CHECK_ENABLED:
        regiomakler_store.record_status("disabled", listings_count=0)
        return {"ok": 1, "enabled": 0, "sent": 0}
    bot = context.bot
    try:
        all_listings = _fetch_all_listings()
    except Exception as exc:
        regiomakler_store.record_status("error", listings_count=0, error=str(exc))
        logger.warning("ImmoTeam/alpha scan failed: %s", exc)
        return {"ok": 0, "enabled": 1, "sent": 0}
    if not all_listings:
        _notify_admin_parse_broke(bot)
    relevant = _dedupe([item for item in all_listings if _is_relevant(item)])
    stored = regiomakler_store.upsert_listings(relevant)
    regiomakler_store.record_status("ok", listings_count=stored)
    active_listings = regiomakler_store.list_active_listings()
    filters = regiomakler_store.list_filters(active_only=True)
    matches = regiomakler_store.select_unsent_matches(
        active_listings, filters, regiomakler_store.delivered_pairs()
    )
    sent = 0
    for filt, listing in matches:
        text = regiomakler_matching.format_notification(listing)
        bot.send_message(chat_id=int(filt["user_id"]), text=text, parse_mode="HTML", disable_web_page_preview=False)
        regiomakler_store.mark_delivered(int(filt["filter_id"]), str(listing["listing_key"]))
        sent += 1
    logger.info(
        "ImmoTeam/alpha scan total=%s relevant=%s stored=%s filters=%s sent=%s",
        len(all_listings), len(relevant), stored, len(filters), sent,
    )
    return {"ok": 1, "enabled": 1, "stored": stored, "filters": len(filters), "sent": sent}
