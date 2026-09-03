"""Scheduled Vonovia scan: plain HTTP, no browser and no login.

Two things shape this module.

The first is the session cookie. `/api/real-estate/list` answers `406 Not
Acceptable` to a bare request; the same request from a session that has just
loaded the results page returns the JSON. So every scan opens the results page
once and then queries the API through that session — cheap, and it is the same
pair of requests a person's browser makes.

The second is that Vonovia currently lists **no** apartments in Potsdam at all
— their whole local inventory in the search is garages. Zero results is
therefore an ordinary day here, never a "the parser broke" signal, exactly as
with locals®. Only a failed request raises the alarm.
"""

import html
import logging
import os
from datetime import timedelta
from typing import Dict, List

import requests
from telegram import InputMediaPhoto

import i18n
from user_jobs import vonovia_matching, vonovia_parser, vonovia_store

logger = logging.getLogger(__name__)

CHECK_ENABLED = os.getenv("VONOVIA_CHECK_ENABLED", "1") == "1"
TIMEOUT = int(os.getenv("VONOVIA_TIMEOUT", "30") or 30)
CHECK_INTERVAL_SECONDS = 15 * 60
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
ERROR_ALERT_COOLDOWN = timedelta(hours=2)
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
# Скільки сторінок видачі гортаємо щонайбільше. Портал віддає по 15 на
# сторінку; у Потсдамі в них зараз 25 позицій разом із гаражами, тож ліміт —
# захист від нескінченного циклу, а не реальна межа.
MAX_PAGES = 10
# Telegram кладе в один альбом не більше 10 фото, скільки б їх не було в галереї.
GALLERY_ALBUM_MAX = 10
# Підпис до фото Telegram обрізає жорсткіше за звичайний текст (1024 проти 4096).
CAPTION_LIMIT = 1024


def _session() -> requests.Session:
    """Сесія з кукою сторінки видачі — без неї API відповідає 406."""
    session = requests.Session()
    session.headers.update({
        "User-Agent": _USER_AGENT,
        "Accept-Language": "de-DE,de;q=0.9",
    })
    response = session.get(
        vonovia_parser.SEARCH_PAGE_URL,
        params=dict(vonovia_parser.LIST_PARAMS, city=vonovia_parser.CITY),
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    return session


def _fetch_listings() -> List[Dict]:
    session = _session()
    listings: List[Dict] = []
    seen = set()
    offset = 0
    for _ in range(MAX_PAGES):
        response = session.get(
            vonovia_parser.LIST_URL,
            params=dict(
                vonovia_parser.LIST_PARAMS,
                city=vonovia_parser.CITY,
                limit=vonovia_parser.PAGE_SIZE,
                offset=offset,
            ),
            headers={
                "Accept": "application/json, text/plain, */*",
                "Referer": vonovia_parser.SEARCH_PAGE_URL,
            },
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()
        page = vonovia_parser.parse_listings(payload)
        for listing in page:
            key = str(listing.get("listing_key") or "")
            # Гаражі відсіюються ще в розборі, тому сторінка цілком може
            # виявитись порожньою, хоча оголошення на порталі ще є, — гортаємо
            # за загальною кількістю, а не за тим, чи щось лишилось після фільтра.
            if key and key not in seen:
                seen.add(key)
                listings.append(listing)
        offset += vonovia_parser.PAGE_SIZE
        if offset >= vonovia_parser.total_count(payload):
            break
    return listings


def _add_full_rent(listings: List[Dict]) -> int:
    """Дозабирає повну оренду зі сторінок оголошень.

    Каталог Vonovia друкує лише Kaltmiete, повну (`warmRent`) портал рахує на
    сторінці самого оголошення. Ходимо туди, доки повної ціни немає, — один
    раз на оголошення; збій на одному не має псувати весь обхід: без повної
    ціни фільтр просто не застосує до нього теплу межу.
    """
    priced = vonovia_store.keys_with_full_rent()
    filled = 0
    for listing in listings:
        if str(listing.get("listing_key") or "") in priced:
            continue
        detail_url = str(listing.get("detail_url") or "").strip()
        if not detail_url or detail_url == vonovia_parser.SEARCH_PAGE_URL:
            continue
        try:
            response = requests.get(detail_url, timeout=TIMEOUT, headers={"User-Agent": _USER_AGENT})
            response.raise_for_status()
            prices = vonovia_parser.parse_detail_prices(response.text)
            if not listing.get("gallery_urls"):
                # Каталог зазвичай віддає галерею сам; сюди доходить лише
                # оголошення, яке прийшло без фото.
                listing["gallery_urls"] = vonovia_parser.parse_gallery_urls(response.text)
        except Exception as exc:
            logger.warning("Could not read Vonovia prices for %s: %s", detail_url, exc)
            continue
        if prices.get("price_warm_eur") is not None:
            listing["price_warm_eur"] = prices["price_warm_eur"]
            filled += 1
    return filled


def _send_listing(bot, chat_id: int, listing: Dict, text: str) -> bool:
    """Шле квартиру одним постом: галерея фото з текстом оголошення підписом.

    Повертає True, якщо текст пішов підписом; False означає, що фото немає або
    підпис не вліз у ліміт, і викликач має надіслати текст окремо — інакше
    оголошення лишилось би зовсім без тексту.
    """
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
            bot.send_photo(
                chat_id=chat_id, photo=photos[0],
                caption=caption, parse_mode="HTML" if caption else None,
            )
        else:
            media = []
            for index, url in enumerate(photos, start=1):
                item_kwargs = {"media": url}
                # Telegram показує підписом усієї групи лише підпис першого елемента.
                if index == 1 and caption:
                    item_kwargs["caption"] = caption
                    item_kwargs["parse_mode"] = "HTML"
                media.append(InputMediaPhoto(**item_kwargs))
            bot.send_media_group(chat_id=chat_id, media=media)
        return caption is not None
    except Exception:
        logger.exception("Could not send Vonovia post to %s", chat_id)
        return False


def _should_alert_fetch_error(previous_status: Dict) -> bool:
    if not previous_status or previous_status.get("last_status") != "error":
        return True
    last_checked = previous_status.get("last_checked_at")
    if last_checked is None:
        return True
    return vonovia_store.utc_now() - last_checked >= ERROR_ALERT_COOLDOWN


def _notify_admin_fetch_failed(bot, error: Exception, previous_status: Dict) -> bool:
    if not ADMIN_ID or not _should_alert_fetch_error(previous_status):
        return False
    text = f"⚠️ <b>Vonovia: не вдалося перевірити оголошення</b>\n\nПричина: {html.escape(str(error))}"
    try:
        bot.send_message(chat_id=ADMIN_ID, text=text, parse_mode="HTML", disable_web_page_preview=True)
        return True
    except Exception:
        logger.exception("Could not notify admin about a Vonovia fetch failure")
        return False


def check_job(context) -> Dict[str, int]:
    if not CHECK_ENABLED:
        vonovia_store.record_status("disabled", listings_count=0)
        return {"ok": 1, "enabled": 0, "sent": 0}
    bot = context.bot
    try:
        all_listings = _fetch_listings()
    except Exception as exc:
        previous_status = vonovia_store.latest_status()
        alerted = _notify_admin_fetch_failed(bot, exc, previous_status)
        vonovia_store.record_status("error", listings_count=0, error=str(exc))
        logger.warning("Vonovia scan failed; admin_alerted=%s: %s", alerted, exc)
        return {"ok": 0, "enabled": 1, "sent": 0, "admin_alerted": int(alerted)}
    enriched = _add_full_rent(all_listings)
    stored = vonovia_store.upsert_listings(all_listings)
    vonovia_store.record_status("ok", listings_count=stored)
    active_listings = vonovia_store.list_active_listings()
    filters = vonovia_store.list_filters(active_only=True)
    matches = vonovia_store.select_unsent_matches(active_listings, filters, vonovia_store.delivered_pairs())
    sent = 0
    for filt, listing in matches:
        chat_id = int(filt["user_id"])
        text = vonovia_matching.format_notification(listing, lang=i18n.get_lang(chat_id))
        posted_as_caption = _send_listing(bot, chat_id, listing, text)
        if not posted_as_caption:
            bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML", disable_web_page_preview=False)
        vonovia_store.mark_delivered(int(filt["filter_id"]), str(listing["listing_key"]))
        sent += 1
    logger.info(
        "Vonovia scan total=%s stored=%s full_rent=%s filters=%s sent=%s",
        len(all_listings), stored, enriched, len(filters), sent,
    )
    return {"ok": 1, "enabled": 1, "stored": stored, "filters": len(filters), "sent": sent}
