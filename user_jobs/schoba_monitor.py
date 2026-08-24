"""Scheduled SCHOBA scan: a plain HTTP GET, no login or browser needed.

The page is a portfolio showcase mixing vacant and already-rented units — only
`is_vacant` listings get stored and matched, so nobody gets notified about an
apartment that says "# vermietet" right there in the listing.
"""

import html
import logging
import os
import time
from datetime import timedelta
from typing import Dict, List

import requests

from user_jobs import schoba_matching, schoba_parser, schoba_store

logger = logging.getLogger(__name__)

CHECK_ENABLED = os.getenv("SCHOBA_CHECK_ENABLED", "1") == "1"
TIMEOUT = int(os.getenv("SCHOBA_TIMEOUT", "30") or 30)
CHECK_INTERVAL_SECONDS = 15 * 60
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
ERROR_ALERT_COOLDOWN = timedelta(hours=2)
_USER_AGENT = "Mozilla/5.0 (compatible; PotsdamHousingBot/1.0)"
# schoba.de періодично рве TLS-з'єднання на рівному місці
# (SSLZeroReturnError "TLS/SSL connection has been closed (EOF)") — повторний
# запит через секунду-другу проходить нормально. Без цих спроб кожен такий
# одиничний збій піднімав адмінський алерт про несправну перевірку, хоча сайт
# був цілком живий.
_FETCH_ATTEMPTS = 3
_RETRY_BACKOFF_SECONDS = (2, 5)


def _fetch_listings() -> List[Dict]:
    last_error = None
    for attempt in range(_FETCH_ATTEMPTS):
        try:
            response = requests.get(
                schoba_parser.LISTINGS_URL, timeout=TIMEOUT, headers={"User-Agent": _USER_AGENT},
            )
            response.raise_for_status()
            return schoba_parser.parse_listings(response.text)
        except requests.RequestException as exc:
            last_error = exc
            if attempt + 1 < _FETCH_ATTEMPTS:
                logger.info(
                    "SCHOBA fetch attempt %s/%s failed, retrying: %s",
                    attempt + 1, _FETCH_ATTEMPTS, exc,
                )
                time.sleep(_RETRY_BACKOFF_SECONDS[attempt])
    raise last_error


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


def _should_alert_fetch_error(previous_status: Dict) -> bool:
    if not previous_status or previous_status.get("last_status") != "error":
        return True
    last_checked = previous_status.get("last_checked_at")
    if last_checked is None:
        return True
    return schoba_store.utc_now() - last_checked >= ERROR_ALERT_COOLDOWN


def _notify_admin_fetch_failed(bot, error: Exception, previous_status: Dict) -> bool:
    if not ADMIN_ID or not _should_alert_fetch_error(previous_status):
        return False
    text = f"⚠️ <b>SCHOBA: не вдалося перевірити оголошення</b>\n\nПричина: {html.escape(str(error))}"
    try:
        bot.send_message(chat_id=ADMIN_ID, text=text, parse_mode="HTML", disable_web_page_preview=True)
        return True
    except Exception:
        logger.exception("Could not notify admin about a SCHOBA fetch failure")
        return False


def check_job(context) -> Dict[str, int]:
    if not CHECK_ENABLED:
        schoba_store.record_status("disabled", listings_count=0)
        return {"ok": 1, "enabled": 0, "sent": 0}
    bot = context.bot
    try:
        all_listings = _fetch_listings()
    except Exception as exc:
        previous_status = schoba_store.latest_status()
        alerted = _notify_admin_fetch_failed(bot, exc, previous_status)
        schoba_store.record_status("error", listings_count=0, error=str(exc))
        logger.warning("SCHOBA scan failed; admin_alerted=%s: %s", alerted, exc)
        return {"ok": 0, "enabled": 1, "sent": 0, "admin_alerted": int(alerted)}
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
