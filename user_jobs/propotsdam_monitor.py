"""Scheduled ProPotsdam collector integration for PotsdamBot."""

import html
import logging
import os
from datetime import timedelta
from threading import Lock
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
CHECK_INTERVAL_SECONDS = 30 * 60
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
ERROR_ALERT_COOLDOWN = timedelta(hours=6)
RELOGIN_HINT = (
    "Как чинить:\n"
    "1) Откройте на ПК Chrome-профиль сборщика ProPotsdam.\n"
    "2) Зайдите на https://propotsdam-kundenportal.easysquare.com/propotsdam-kundenportal/index.html\n"
    "3) Войдите в ProPotsdam/easysquare вручную и убедитесь, что открывается Immobiliensuche.\n"
    "4) После входа следующая проверка снова соберёт квартиры автоматически."
)
_scan_lock = Lock()


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


def _should_alert_error(previous_status: Dict) -> bool:
    if not previous_status or previous_status.get("last_status") != "error":
        return True
    last_checked = previous_status.get("last_checked_at")
    if last_checked is None:
        return True
    return propotsdam_store.utc_now() - last_checked >= ERROR_ALERT_COOLDOWN


def _notify_admin_error(bot, error: Exception, previous_status: Dict) -> bool:
    if not ADMIN_ID or not _should_alert_error(previous_status):
        return False
    text = (
        "⚠️ <b>ProPotsdam не смог собрать квартиры</b>\n\n"
        f"Причина: {html.escape(str(error))}\n\n"
        f"{html.escape(RELOGIN_HINT)}"
    )
    try:
        bot.send_message(chat_id=ADMIN_ID, text=text, parse_mode="HTML", disable_web_page_preview=True)
        return True
    except Exception:
        logger.exception("Could not notify admin about ProPotsdam collector error")
        return False


def check_job(context) -> Dict[str, int]:
    if not PROPOTSDAM_CHECK_ENABLED:
        propotsdam_store.record_status("disabled", listings_count=0)
        return {"ok": 1, "enabled": 0, "sent": 0}
    if not _scan_lock.acquire(blocking=False):
        logger.warning("ProPotsdam scan is already running; skipping overlapping invocation")
        return {"ok": 1, "enabled": 1, "skipped": 1, "sent": 0}
    try:
        return _check_job_locked(context)
    finally:
        _scan_lock.release()


def _check_job_locked(context) -> Dict[str, int]:
    bot = context.bot
    try:
        listings = _request_scan()
    except Exception as exc:
        previous_status = propotsdam_store.latest_status()
        alerted = _notify_admin_error(bot, exc, previous_status)
        propotsdam_store.record_status("error", listings_count=0, error=str(exc))
        logger.warning("ProPotsdam scan failed; admin_alerted=%s: %s", alerted, exc)
        return {"ok": 0, "enabled": 1, "sent": 0, "admin_alerted": int(alerted)}
    stored = propotsdam_store.upsert_listings(listings)
    propotsdam_store.record_status("ok", listings_count=stored)
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
