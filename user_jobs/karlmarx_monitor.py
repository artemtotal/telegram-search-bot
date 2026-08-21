"""Scheduled Karl Marx scan: a plain HTTP GET, no login or browser needed.

The offers page mixes commercial and residential cards — 0 residential matches
is a normal day (Karl Marx is mostly a commercial landlord right now), but 0
cards of ANY type would mean the markup broke, so that (not "0 residential")
is what triggers the admin alert.
"""

import html
import logging
import os
from datetime import timedelta
from typing import Dict, List

import requests

from user_jobs import karlmarx_matching, karlmarx_parser, karlmarx_store

logger = logging.getLogger(__name__)

CHECK_ENABLED = os.getenv("KARLMARX_CHECK_ENABLED", "1") == "1"
TIMEOUT = int(os.getenv("KARLMARX_TIMEOUT", "30") or 30)
CHECK_INTERVAL_SECONDS = 15 * 60
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
ERROR_ALERT_COOLDOWN = timedelta(hours=2)
_USER_AGENT = "Mozilla/5.0 (compatible; PotsdamHousingBot/1.0)"


def _fetch_html() -> str:
    response = requests.get(
        karlmarx_parser.LISTINGS_URL, timeout=TIMEOUT, headers={"User-Agent": _USER_AGENT},
    )
    response.raise_for_status()
    return response.text


def _notify_admin_parse_broke(bot) -> None:
    if not ADMIN_ID:
        return
    text = (
        "⚠️ <b>Karl Marx: розбір сторінки повернув 0 карток будь-якого типу</b>\n\n"
        "Схоже, змінилась розмітка wgkarlmarx.de/fuer-wohnungssucher і парсер "
        "більше не розпізнає картки оголошень — варто перевірити вручну."
    )
    try:
        bot.send_message(chat_id=ADMIN_ID, text=text, parse_mode="HTML", disable_web_page_preview=True)
    except Exception:
        logger.exception("Could not notify admin about a broken Karl Marx parse")


def _should_alert_fetch_error(previous_status: Dict) -> bool:
    if not previous_status or previous_status.get("last_status") != "error":
        return True
    last_checked = previous_status.get("last_checked_at")
    if last_checked is None:
        return True
    return karlmarx_store.utc_now() - last_checked >= ERROR_ALERT_COOLDOWN


def _notify_admin_fetch_failed(bot, error: Exception, previous_status: Dict) -> bool:
    if not ADMIN_ID or not _should_alert_fetch_error(previous_status):
        return False
    text = f"⚠️ <b>Karl Marx: не вдалося перевірити оголошення</b>\n\nПричина: {html.escape(str(error))}"
    try:
        bot.send_message(chat_id=ADMIN_ID, text=text, parse_mode="HTML", disable_web_page_preview=True)
        return True
    except Exception:
        logger.exception("Could not notify admin about a Karl Marx fetch failure")
        return False


def check_job(context) -> Dict[str, int]:
    if not CHECK_ENABLED:
        karlmarx_store.record_status("disabled", listings_count=0)
        return {"ok": 1, "enabled": 0, "sent": 0}
    bot = context.bot
    try:
        page_html = _fetch_html()
    except Exception as exc:
        previous_status = karlmarx_store.latest_status()
        alerted = _notify_admin_fetch_failed(bot, exc, previous_status)
        karlmarx_store.record_status("error", listings_count=0, error=str(exc))
        logger.warning("Karl Marx scan failed; admin_alerted=%s: %s", alerted, exc)
        return {"ok": 0, "enabled": 1, "sent": 0, "admin_alerted": int(alerted)}
    if karlmarx_parser.count_all_cards(page_html) == 0:
        _notify_admin_parse_broke(bot)
    listings = karlmarx_parser.parse_listings(page_html)
    stored = karlmarx_store.upsert_listings(listings)
    karlmarx_store.record_status("ok", listings_count=stored)
    active_listings = karlmarx_store.list_active_listings()
    filters = karlmarx_store.list_filters(active_only=True)
    matches = karlmarx_store.select_unsent_matches(active_listings, filters, karlmarx_store.delivered_pairs())
    sent = 0
    for filt, listing in matches:
        text = karlmarx_matching.format_notification(listing)
        bot.send_message(chat_id=int(filt["user_id"]), text=text, parse_mode="HTML", disable_web_page_preview=False)
        karlmarx_store.mark_delivered(int(filt["filter_id"]), str(listing["listing_key"]))
        sent += 1
    logger.info(
        "Karl Marx scan residential=%s stored=%s filters=%s sent=%s",
        len(listings), stored, len(filters), sent,
    )
    return {"ok": 1, "enabled": 1, "stored": stored, "filters": len(filters), "sent": sent}
