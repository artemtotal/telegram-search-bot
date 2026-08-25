"""Scheduled Kleinanzeigen scan — deliberately slower than the other sources.

Kleinanzeigen is a large classifieds platform, not a small local broker site,
and its Terms of Service prohibit automated scraping. A single manual-looking
GET succeeds without a CAPTCHA, but polling it on the same cadence as the
other sources would be systematic scraping the platform could detect and act
on. History: capped at 60 min initially, tightened to 30 on 2026-08-21, then
to 20 the same day as a deliberate experiment — official Suchauftrag email
alerts turned out to run an hour or slower (worse than scraping) and would
have duplicated the bot's own delivery tracking, so the choice was "try a
tighter interval and watch for blocking" over "switch to a slower, harder to
track channel". _notify_admin_fetch_failed() below exists specifically to
surface that experiment's result — if Kleinanzeigen starts blocking, this is
what should be the first thing to notice it and to widen the interval back.

Also filters out two kinds of noise the raw search page includes:
- Results outside Potsdam itself — the search radius pulls in nearby towns
  (Rathenow, Berlin-Schöneberg, ...), which the caller doesn't want.
- Apartment-swap listings ("TAUSCHWOHNUNG", or "Wohnungsswap"-branded ones
  from commercial swap accounts) — these aren't available to just apply for,
  the poster wants YOUR apartment in exchange for theirs, so matching them
  against a normal search filter would be misleading.
"""

import html
import logging
import os
import time
from datetime import timedelta
from typing import Dict, List

import requests

from user_jobs import kleinanzeigen_matching, kleinanzeigen_parser, kleinanzeigen_store

logger = logging.getLogger(__name__)

CHECK_ENABLED = os.getenv("KLEINANZEIGEN_CHECK_ENABLED", "1") == "1"
TIMEOUT = int(os.getenv("KLEINANZEIGEN_TIMEOUT", "30") or 30)
CHECK_INTERVAL_SECONDS = 20 * 60
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
ERROR_ALERT_COOLDOWN = timedelta(hours=2)
_USER_AGENT = "Mozilla/5.0 (compatible; PotsdamHousingBot/1.0)"
# Пошук по Потсдаму ніколи не буває порожнім — сторінка стабільно віддає 25-26
# карток. Одиничний нуль (реально спостережений 2026-08-25 12:45 серед сусідніх
# перевірок із 25-26) означає не зміну розмітки, а миттєвий збій на боці сайту,
# і повторний запит одразу повертає нормальну сторінку.
_FETCH_ATTEMPTS = 3
_RETRY_BACKOFF_SECONDS = (2, 5)


def _fetch_listings() -> List[Dict]:
    last_error = None
    for attempt in range(_FETCH_ATTEMPTS):
        try:
            response = requests.get(
                kleinanzeigen_parser.LISTINGS_URL, timeout=TIMEOUT, headers={"User-Agent": _USER_AGENT},
            )
            response.raise_for_status()
            listings = kleinanzeigen_parser.parse_listings(response.text)
            if listings:
                return listings
            last_error = None
            logger.info(
                "Kleinanzeigen returned 0 listings on attempt %s/%s; retrying",
                attempt + 1, _FETCH_ATTEMPTS,
            )
        except requests.RequestException as exc:
            last_error = exc
            logger.info(
                "Kleinanzeigen fetch attempt %s/%s failed: %s", attempt + 1, _FETCH_ATTEMPTS, exc,
            )
        if attempt + 1 < _FETCH_ATTEMPTS:
            time.sleep(_RETRY_BACKOFF_SECONDS[attempt])
    if last_error is not None:
        raise last_error
    # Порожньо і після всіх спроб — тоді це вже схоже на справжню зміну
    # розмітки, і про це має дізнатись адмін (_notify_admin_parse_broke).
    return []


# Some swap-listing posters (e.g. the commercial account "Wohnungsswap.de")
# title every ad with the English "swap" instead of the German "Tausch" -
# a real one slipped through notifications with only "tausch" excluded.
_SWAP_KEYWORDS = ("tausch", "swap")


def _is_relevant(listing: Dict) -> bool:
    city = str(listing.get("city") or "").strip().casefold()
    title = str(listing.get("title") or "").casefold()
    return city == "potsdam" and not any(keyword in title for keyword in _SWAP_KEYWORDS)


def _should_alert_parse_broke(previous_status: Dict) -> bool:
    """Кулдаун для повідомлення про порожній розбір.

    На відміну від `_notify_admin_fetch_failed`, це повідомлення не мало
    жодного кулдауна: поки сторінка порожня, воно йшло щоперевірки, тобто
    кожні 20 хвилин. Попередній нульовий розбір записується як статус "ok"
    із listings_count=0, тож саме за цією парою й звіряємось.
    """
    if not previous_status:
        return True
    if previous_status.get("listings_count"):
        return True
    last_checked = previous_status.get("last_checked_at")
    if last_checked is None:
        return True
    return kleinanzeigen_store.utc_now() - last_checked >= ERROR_ALERT_COOLDOWN


def _notify_admin_parse_broke(bot, previous_status: Dict = None) -> bool:
    if not ADMIN_ID or not _should_alert_parse_broke(previous_status or {}):
        return False
    text = (
        "⚠️ <b>Kleinanzeigen: розбір сторінки повернув 0 оголошень</b>\n\n"
        f"Порожньо навіть після {_FETCH_ATTEMPTS} спроб поспіль — схоже, "
        "змінилась розмітка сторінки пошуку, варто перевірити вручну."
    )
    try:
        bot.send_message(chat_id=ADMIN_ID, text=text, parse_mode="HTML", disable_web_page_preview=True)
        return True
    except Exception:
        logger.exception("Could not notify admin about a broken Kleinanzeigen parse")
        return False


def _should_alert_fetch_error(previous_status: Dict) -> bool:
    if not previous_status or previous_status.get("last_status") != "error":
        return True
    last_checked = previous_status.get("last_checked_at")
    if last_checked is None:
        return True
    return kleinanzeigen_store.utc_now() - last_checked >= ERROR_ALERT_COOLDOWN


def _notify_admin_fetch_failed(bot, error: Exception, previous_status: Dict) -> bool:
    """The one thing to watch during the 20-minute-interval experiment: does
    Kleinanzeigen start refusing requests? A 403/429 here is the signal."""
    if not ADMIN_ID or not _should_alert_fetch_error(previous_status):
        return False
    status_code = getattr(getattr(error, "response", None), "status_code", None)
    hint = ""
    if status_code in (403, 429):
        hint = (
            f"\n\n🚫 Код {status_code} — схоже, Kleinanzeigen почав "
            "блокувати автоматичні запити. Варто повернути перевірку "
            "на рідший інтервал (KLEINANZEIGEN_CHECK_INTERVAL_SECONDS)."
        )
    text = f"⚠️ <b>Kleinanzeigen: не вдалося перевірити оголошення</b>\n\nПричина: {html.escape(str(error))}{hint}"
    try:
        bot.send_message(chat_id=ADMIN_ID, text=text, parse_mode="HTML", disable_web_page_preview=True)
        return True
    except Exception:
        logger.exception("Could not notify admin about a Kleinanzeigen fetch failure")
        return False


def check_job(context) -> Dict[str, int]:
    if not CHECK_ENABLED:
        kleinanzeigen_store.record_status("disabled", listings_count=0)
        return {"ok": 1, "enabled": 0, "sent": 0}
    bot = context.bot
    try:
        all_listings = _fetch_listings()
    except Exception as exc:
        previous_status = kleinanzeigen_store.latest_status()
        alerted = _notify_admin_fetch_failed(bot, exc, previous_status)
        kleinanzeigen_store.record_status("error", listings_count=0, error=str(exc))
        logger.warning("Kleinanzeigen scan failed; admin_alerted=%s: %s", alerted, exc)
        return {"ok": 0, "enabled": 1, "sent": 0, "admin_alerted": int(alerted)}
    if not all_listings:
        _notify_admin_parse_broke(bot, kleinanzeigen_store.latest_status())
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
