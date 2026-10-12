"""Scheduled ImmoScout24 scan: the mobile app's JSON API, no browser.

The website sits behind an AWS WAF image CAPTCHA; the app's API serves the
same Potsdam results to a plain POST (see `immoscout_parser`). Each scan
reads every result page (≈5 of 50, newest first), then each new listing's
own page once — for the full rent, the exact district and the gallery — and
only listings whose page has been read are matched: before that the warm
bound and the district would be guesses.

When the source went live (12.10.2026) every filter people had already made
got an ImmoScout24 part too (`_backfill_existing_searches`) — after the first
scan had stored the current listings, so that the flats already online on
that day count as seen and nobody gets two hundred messages at once.
"""

import html
import logging
import os
import time
from datetime import timedelta
from typing import Dict, List

import requests
from telegram import InputMediaPhoto

import i18n
from database import ImmoscoutListing
from user_jobs import (
    alert_cooldown, delivery_dedup, housing_search_store, housing_tier,
    immoscout_matching, immoscout_parser, immoscout_store,
)

logger = logging.getLogger(__name__)

CHECK_ENABLED = os.getenv("IMMOSCOUT_CHECK_ENABLED", "1") == "1"
TIMEOUT = int(os.getenv("IMMOSCOUT_TIMEOUT", "30") or 30)
CHECK_INTERVAL_SECONDS = 15 * 60
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
ERROR_ALERT_COOLDOWN = timedelta(hours=2)
# 50 оголошень на сторінку; у Потсдамі їх ≈200 — ліміт лише від нескінченного циклу.
MAX_PAGES = 10
# Скільки карток оголошень читати за один обхід. Першого дня їх ≈200 — решту
# дочитають наступні обходи, а нові оголошення зазвичай укладаються в один.
DETAILS_PER_SCAN = 40
DETAIL_PAUSE_SECONDS = 0.5
GALLERY_ALBUM_MAX = 10
CAPTION_LIMIT = 1024
# Позначка в immoscout_status: існуючим фільтрам частину ImmoScout24 вже додано.
BACKFILL_KEY = "backfill_existing_searches"
SOURCE = "immoscout"


def _headers() -> Dict[str, str]:
    return {"User-Agent": immoscout_parser.USER_AGENT, "Accept": "application/json"}


def _fetch_listings() -> List[Dict]:
    listings: List[Dict] = []
    seen = set()
    page = 1
    while page <= MAX_PAGES:
        response = requests.post(
            immoscout_parser.LIST_URL,
            params=dict(immoscout_parser.LIST_PARAMS, pagenumber=page),
            json=immoscout_parser.LIST_BODY,
            headers=_headers(),
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()
        for listing in immoscout_parser.parse_listings(payload):
            if listing["listing_key"] not in seen:
                seen.add(listing["listing_key"])
                listings.append(listing)
        if page >= immoscout_parser.page_count(payload):
            break
        page += 1
    return listings


def _add_details(listings: List[Dict]) -> int:
    """Дочитує картки оголошень, яких ще не читали (найновіші — першими)."""
    known = immoscout_store.keys_with_details()
    filled = 0
    for listing in listings:
        if filled >= DETAILS_PER_SCAN:
            break
        key = listing["listing_key"]
        if key in known:
            continue
        try:
            response = requests.get(
                immoscout_parser.EXPOSE_API_URL.format(listing_id=key), headers=_headers(), timeout=TIMEOUT,
            )
            response.raise_for_status()
            details = immoscout_parser.parse_expose(response.json())
        except Exception as exc:
            logger.warning("Could not read ImmoScout24 listing %s: %s", key, exc)
            continue
        for field in ("price_eur", "price_warm_eur", "address", "district"):
            if details.get(field) not in (None, ""):
                listing[field] = details[field]
        if details.get("gallery_urls"):
            listing["gallery_urls"] = details["gallery_urls"]
        listing["detail_fetched"] = True
        filled += 1
        time.sleep(DETAIL_PAUSE_SECONDS)
    return filled


def _filter_kwargs(criteria: Dict) -> Dict:
    return {
        "districts": ",".join(str(d) for d in (criteria.get("districts") or [])),
        **{key: criteria.get(key) for key in (
            "min_rooms", "max_rooms", "min_area_m2", "max_area_m2",
            "min_price_eur", "max_price_eur", "min_price_warm_eur", "max_price_warm_eur",
        )},
    }


def _backfill_existing_searches() -> int:
    """Раз за весь час: додати ImmoScout24 до кожного вже створеного фільтра."""
    if immoscout_store.latest_status_for(BACKFILL_KEY):
        return 0
    added = 0
    for search in housing_search_store.list_all():
        if any(source == SOURCE for source, _ in search["members"]):
            continue
        criteria = search["criteria"]
        filter_id = immoscout_store.create_filter(
            user_id=search["user_id"], title="ImmoScout24", **_filter_kwargs(criteria),
        )
        housing_search_store.add_member(search["search_id"], SOURCE, filter_id)
        added += 1
    immoscout_store.record_status_for(BACKFILL_KEY, "done", listings_count=added)
    logger.info("ImmoScout24 added to %s existing filters", added)
    return added


def _send_listing(bot, chat_id: int, listing: Dict, text: str) -> bool:
    """Галерея з текстом підписом; False — текст треба надіслати окремо."""
    photos = [url for url in (listing.get("gallery_urls") or []) if str(url).strip()]
    if not photos:
        cover = str(listing.get("cover_image_url") or "").strip()
        photos = [cover] if cover else []
    photos = photos[:GALLERY_ALBUM_MAX]
    if not photos:
        return False
    caption = text if len(text) <= CAPTION_LIMIT else None
    try:
        if len(photos) == 1:
            bot.send_photo(chat_id=chat_id, photo=photos[0], caption=caption, parse_mode="HTML" if caption else None)
        else:
            media = []
            for index, url in enumerate(photos, start=1):
                item_kwargs = {"media": url}
                if index == 1 and caption:
                    item_kwargs["caption"] = caption
                    item_kwargs["parse_mode"] = "HTML"
                media.append(InputMediaPhoto(**item_kwargs))
            bot.send_media_group(chat_id=chat_id, media=media)
        return caption is not None
    except Exception:
        logger.exception("Could not send ImmoScout24 post to %s", chat_id)
        return False


def _should_alert_fetch_error(previous_status: Dict) -> bool:
    if not previous_status or previous_status.get("last_status") != "error":
        return True
    return alert_cooldown.due("immoscout:fetch", ERROR_ALERT_COOLDOWN)


def _notify_admin_fetch_failed(bot, error: Exception, previous_status: Dict) -> bool:
    if not ADMIN_ID or not _should_alert_fetch_error(previous_status):
        return False
    status_code = getattr(getattr(error, "response", None), "status_code", None)
    hint = ""
    if status_code in (401, 403, 429):
        hint = (
            f"\n\n🚫 Код {status_code} — схоже, API застосунку перестало пускати нас. "
            "Можливо, треба оновити версію в USER_AGENT (immoscout_parser)."
        )
    text = f"⚠️ <b>ImmoScout24: не вдалося перевірити оголошення</b>\n\nПричина: {html.escape(str(error))}{hint}"
    try:
        bot.send_message(chat_id=ADMIN_ID, text=text, parse_mode="HTML", disable_web_page_preview=True)
        alert_cooldown.mark("immoscout:fetch")
        return True
    except Exception:
        logger.exception("Could not notify admin about an ImmoScout24 fetch failure")
        return False


def check_job(context) -> Dict[str, int]:
    if not CHECK_ENABLED:
        immoscout_store.record_status("disabled", listings_count=0)
        return {"ok": 1, "enabled": 0, "sent": 0}
    bot = context.bot
    try:
        all_listings = _fetch_listings()
    except Exception as exc:
        previous_status = immoscout_store.latest_status()
        alerted = _notify_admin_fetch_failed(bot, exc, previous_status)
        immoscout_store.record_status("error", listings_count=0, error=str(exc))
        logger.warning("ImmoScout24 scan failed; admin_alerted=%s: %s", alerted, exc)
        return {"ok": 0, "enabled": 1, "sent": 0, "admin_alerted": int(alerted)}
    detailed = _add_details(all_listings)
    stored = immoscout_store.upsert_listings(all_listings)
    immoscout_store.record_status("ok", listings_count=stored)
    try:
        _backfill_existing_searches()
    except Exception:
        logger.exception("Could not add ImmoScout24 to existing filters")
    # Лише ті, чию картку прочитано: до того невідомі повна оренда й точний район.
    active_listings = [item for item in immoscout_store.list_active_listings() if item.get("detail_fetched")]
    filters = immoscout_store.list_filters(active_only=True)
    matches = immoscout_store.select_unsent_matches(active_listings, filters, immoscout_store.delivered_pairs())
    sent = 0
    dedup = delivery_dedup.PerUserDedup()
    gate = housing_tier.DeliveryGate(SOURCE, ImmoscoutListing)
    for filt, listing in matches:
        chat_id = int(filt["user_id"])
        listing_key = str(listing["listing_key"])
        if not gate.due(chat_id, listing):
            continue
        if not dedup.claim(chat_id, listing_key):
            immoscout_store.mark_delivered(int(filt["filter_id"]), listing_key)
            continue
        text = immoscout_matching.format_notification(listing, lang=i18n.get_lang(chat_id))
        text += gate.footer(chat_id)
        if not _send_listing(bot, chat_id, listing, text):
            bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML", disable_web_page_preview=False)
        immoscout_store.mark_delivered(int(filt["filter_id"]), listing_key)
        gate.sent(chat_id, listing_key, int(filt["filter_id"]))
        sent += 1
    logger.info(
        "ImmoScout24 scan total=%s stored=%s details=%s filters=%s sent=%s",
        len(all_listings), stored, detailed, len(filters), sent,
    )
    return {"ok": 1, "enabled": 1, "stored": stored, "filters": len(filters), "sent": sent}
