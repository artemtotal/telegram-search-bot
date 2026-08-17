"""Scheduled SEMMELHAACK scan: a plain HTTP GET, no login or browser needed.

Unlike ProPotsdam, there is no separate collector process — the page is public
and static, so the bot fetches and parses it directly on a timer.
"""

import html
import logging
import os
from typing import Dict, List

import requests

from user_jobs import semmelhaack_matching, semmelhaack_parser, semmelhaack_store

logger = logging.getLogger(__name__)

CHECK_ENABLED = os.getenv("SEMMELHAACK_CHECK_ENABLED", "1") == "1"
TIMEOUT = int(os.getenv("SEMMELHAACK_TIMEOUT", "30") or 30)
CHECK_INTERVAL_SECONDS = 30 * 60
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
_USER_AGENT = "Mozilla/5.0 (compatible; PotsdamHousingBot/1.0)"


def _fetch_listings() -> List[Dict]:
    response = requests.get(
        semmelhaack_parser.LISTINGS_URL, timeout=TIMEOUT, headers={"User-Agent": _USER_AGENT},
    )
    response.raise_for_status()
    return semmelhaack_parser.parse_listings(response.text)


def _notify_admin_parse_broke(bot, total_parsed: int) -> None:
    """Zero listings across ALL of Germany, not just Potsdam, means the page's
    markup likely changed under the parser — a broken scan looks identical to a
    portal that briefly went blank, so this is the only distinguishing signal."""
    if not ADMIN_ID:
        return
    text = (
        "⚠️ <b>SEMMELHAACK: розбір сторінки повернув 0 оголошень по всій Німеччині</b>\n\n"
        "Схоже, змінилась розмітка сторінки semmelhaack.de/mietangebote/ і парсер "
        "більше не розпізнає картки оголошень — варто перевірити вручну."
    )
    try:
        bot.send_message(chat_id=ADMIN_ID, text=text, parse_mode="HTML", disable_web_page_preview=True)
    except Exception:
        logger.exception("Could not notify admin about a broken SEMMELHAACK parse")


def check_job(context) -> Dict[str, int]:
    if not CHECK_ENABLED:
        semmelhaack_store.record_status("disabled", listings_count=0)
        return {"ok": 1, "enabled": 0, "sent": 0}
    bot = context.bot
    try:
        all_listings = _fetch_listings()
    except Exception as exc:
        semmelhaack_store.record_status("error", listings_count=0, error=str(exc))
        logger.warning("SEMMELHAACK scan failed: %s", exc)
        return {"ok": 0, "enabled": 1, "sent": 0}
    potsdam_listings = [item for item in all_listings if str(item.get("city") or "").strip().casefold() == "potsdam"]
    if not all_listings:
        _notify_admin_parse_broke(bot, len(all_listings))
    stored = semmelhaack_store.upsert_listings(potsdam_listings)
    semmelhaack_store.record_status("ok", listings_count=stored)
    active_listings = semmelhaack_store.list_active_listings()
    filters = semmelhaack_store.list_filters(active_only=True)
    matches = semmelhaack_store.select_unsent_matches(active_listings, filters, semmelhaack_store.delivered_pairs())
    sent = 0
    for filt, listing in matches:
        text = semmelhaack_matching.format_notification(listing)
        bot.send_message(chat_id=int(filt["user_id"]), text=text, parse_mode="HTML", disable_web_page_preview=False)
        semmelhaack_store.mark_delivered(int(filt["filter_id"]), str(listing["listing_key"]))
        sent += 1
    logger.info(
        "SEMMELHAACK scan total=%s potsdam=%s stored=%s filters=%s sent=%s",
        len(all_listings), len(potsdam_listings), stored, len(filters), sent,
    )
    return {"ok": 1, "enabled": 1, "stored": stored, "filters": len(filters), "sent": sent}
