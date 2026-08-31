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
from datetime import timedelta
from typing import Dict, List

import requests
from telegram import InputMediaPhoto

from user_jobs import regiomakler_matching, regiomakler_parser, regiomakler_store

logger = logging.getLogger(__name__)

CHECK_ENABLED = os.getenv("REGIOMAKLER_CHECK_ENABLED", "1") == "1"
TIMEOUT = int(os.getenv("REGIOMAKLER_TIMEOUT", "30") or 30)
CHECK_INTERVAL_SECONDS = 15 * 60
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
ERROR_ALERT_COOLDOWN = timedelta(hours=2)
_USER_AGENT = "Mozilla/5.0 (compatible; PotsdamHousingBot/1.0)"
# Telegram кладёт в один альбом не больше 10 фото, сколько бы их ни было в галерее.
GALLERY_ALBUM_MAX = 10
# Подпись к фото Telegram обрезает жёстче обычного текста (1024 против 4096
# символов) — раньше этого лимита текст уходит подписью, дальше отдельным сообщением.
CAPTION_LIMIT = 1024

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


def _fetch_gallery(listing: Dict) -> List[str]:
    """Все фото объявления — со страницы самого объявления.

    Ни каталог immoteam, ни каталог alpha не показывают фото на карточке —
    только на странице конкретного объявления. Оба домена публичные, без
    логина, поэтому Telegram может забрать эти URL сам — скачивать и
    кешировать байты, как для ProPotsdam, не нужно.
    """
    detail_url = str(listing.get("detail_url") or "").strip()
    if not detail_url:
        return []
    try:
        html_text = _fetch(detail_url)
        return regiomakler_parser.parse_gallery_urls(html_text)[:GALLERY_ALBUM_MAX]
    except Exception as exc:
        logger.warning("Could not fetch ImmoTeam/alpha gallery for %s: %s", detail_url, exc)
        return []


def _send_listing(bot, chat_id: int, listing: Dict, text: str) -> bool:
    """Шлёт квартиру одним постом: галерея фото с текстом объявления подписью снизу.

    Возвращает True, если текст ушёл подписью — тогда отдельное текстовое
    сообщение отправлять не нужно. False означает, что фото не набралось или
    подпись не влезла в лимит: вызывающий обязан отправить тот же текст
    отдельным сообщением, иначе объявление осталось бы вовсе без текста.
    """
    photos = _fetch_gallery(listing)
    if not photos:
        return False
    caption = text if len(text) <= CAPTION_LIMIT else None
    try:
        if len(photos) == 1:
            bot.send_photo(
                chat_id=chat_id, photo=photos[0],
                caption=caption, parse_mode="HTML" if caption else None,
            )
        else:
            media = []
            for index, url in enumerate(photos, start=1):
                item_kwargs = {"media": url}
                # Telegram показывает подписью всей группы только подпись первого элемента.
                if index == 1 and caption:
                    item_kwargs["caption"] = caption
                    item_kwargs["parse_mode"] = "HTML"
                media.append(InputMediaPhoto(**item_kwargs))
            bot.send_media_group(chat_id=chat_id, media=media)
        return caption is not None
    except Exception:
        logger.exception("Could not send ImmoTeam/alpha post to %s", chat_id)
        return False


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


def _should_alert_fetch_error(previous_status: Dict) -> bool:
    if not previous_status or previous_status.get("last_status") != "error":
        return True
    last_checked = previous_status.get("last_checked_at")
    if last_checked is None:
        return True
    return regiomakler_store.utc_now() - last_checked >= ERROR_ALERT_COOLDOWN


def _notify_admin_fetch_failed(bot, error: Exception, previous_status: Dict) -> bool:
    if not ADMIN_ID or not _should_alert_fetch_error(previous_status):
        return False
    text = f"⚠️ <b>ImmoTeam/alpha: не вдалося перевірити оголошення</b>\n\nПричина: {html.escape(str(error))}"
    try:
        bot.send_message(chat_id=ADMIN_ID, text=text, parse_mode="HTML", disable_web_page_preview=True)
        return True
    except Exception:
        logger.exception("Could not notify admin about an ImmoTeam/alpha fetch failure")
        return False


def check_job(context) -> Dict[str, int]:
    if not CHECK_ENABLED:
        regiomakler_store.record_status("disabled", listings_count=0)
        return {"ok": 1, "enabled": 0, "sent": 0}
    bot = context.bot
    try:
        all_listings = _fetch_all_listings()
    except Exception as exc:
        previous_status = regiomakler_store.latest_status()
        alerted = _notify_admin_fetch_failed(bot, exc, previous_status)
        regiomakler_store.record_status("error", listings_count=0, error=str(exc))
        logger.warning("ImmoTeam/alpha scan failed; admin_alerted=%s: %s", alerted, exc)
        return {"ok": 0, "enabled": 1, "sent": 0, "admin_alerted": int(alerted)}
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
        chat_id = int(filt["user_id"])
        posted_as_caption = _send_listing(bot, chat_id, listing, text)
        if not posted_as_caption:
            bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML", disable_web_page_preview=False)
        regiomakler_store.mark_delivered(int(filt["filter_id"]), str(listing["listing_key"]))
        sent += 1
    logger.info(
        "ImmoTeam/alpha scan total=%s relevant=%s stored=%s filters=%s sent=%s",
        len(all_listings), len(relevant), stored, len(filters), sent,
    )
    return {"ok": 1, "enabled": 1, "stored": stored, "filters": len(filters), "sent": sent}
