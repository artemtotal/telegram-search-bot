"""Scheduled locals® scan: a plain HTTP GET, no login or browser needed.

locals.de/wohnung-mieten-potsdam is a small, curated landing page — unlike
SCHOBA's "portfolio never goes fully empty" case, this market can legitimately
show zero current Potsdam rentals on a given day, so a zero-listings result is
not treated as a parser-broke signal worth alerting the admin about.
"""

import html
import logging
import os
from datetime import timedelta
from typing import Dict, List

import requests
from telegram import InputMediaPhoto

import i18n
from user_jobs import locals_matching, locals_parser, locals_store

logger = logging.getLogger(__name__)

CHECK_ENABLED = os.getenv("LOCALS_CHECK_ENABLED", "1") == "1"
TIMEOUT = int(os.getenv("LOCALS_TIMEOUT", "30") or 30)
CHECK_INTERVAL_SECONDS = 15 * 60
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
ERROR_ALERT_COOLDOWN = timedelta(hours=2)
_USER_AGENT = "Mozilla/5.0 (compatible; PotsdamHousingBot/1.0)"
# Telegram кладёт в один альбом не больше 10 фото, сколько бы их ни было в галерее.
GALLERY_ALBUM_MAX = 10
# Подпись к фото Telegram обрезает жёстче обычного текста (1024 против 4096
# символов) — раньше этого лимита текст уходит подписью, дальше отдельным сообщением.
CAPTION_LIMIT = 1024


def _fetch_listings() -> List[Dict]:
    response = requests.get(
        locals_parser.LISTINGS_URL, timeout=TIMEOUT, headers={"User-Agent": _USER_AGENT},
    )
    response.raise_for_status()
    return locals_parser.parse_listings(response.text)


def _add_full_rent(listings: List[Dict]) -> int:
    """Дозабирає повну ціну зі сторінок оголошень.

    Каталог locals® показує лише Kaltmiete; поруч із нею всередині оголошення
    стоїть Nebenkosten, з яких і виходить повна. Ходимо туди, доки повної ціни
    немає, — один раз на оголошення; збій на одному не має псувати весь обхід — без повної ціни
    фільтр просто не застосує до нього теплу межу.
    """
    priced = locals_store.keys_with_full_rent()
    filled = 0
    for listing in listings:
        if str(listing.get("listing_key") or "") in priced:
            continue
        detail_url = str(listing.get("detail_url") or "").strip()
        if not detail_url:
            continue
        try:
            response = requests.get(detail_url, timeout=TIMEOUT, headers={"User-Agent": _USER_AGENT})
            response.raise_for_status()
            prices = locals_parser.parse_detail_prices(response.text)
        except Exception as exc:
            logger.warning("Could not read locals® prices for %s: %s", detail_url, exc)
            continue
        if prices.get("price_warm_eur") is not None:
            listing["price_warm_eur"] = prices["price_warm_eur"]
            filled += 1
    return filled


def _cover_only(listing: Dict) -> List[str]:
    cover = str(listing.get("cover_image_url") or "").strip()
    return [cover] if cover else []


def _fetch_gallery(listing: Dict) -> List[str]:
    """Все фото и планировки объявления — со страницы самого объявления.

    Каталожна картка вже несе одну обкладинку безкоштовно, тому вона ж і
    запасний варіант: якщо сторінка оголошення не відкрилась або на ній не
    знайшлось жодного фото glightbox, у сповіщення все одно піде обкладинка,
    а не голий текст.
    """
    detail_url = str(listing.get("detail_url") or "").strip()
    if not detail_url:
        return _cover_only(listing)
    try:
        response = requests.get(detail_url, timeout=TIMEOUT, headers={"User-Agent": _USER_AGENT})
        response.raise_for_status()
        urls = locals_parser.parse_gallery_urls(response.text)[:GALLERY_ALBUM_MAX]
    except Exception as exc:
        logger.warning("Could not fetch locals® gallery for %s: %s", detail_url, exc)
        urls = []
    return urls or _cover_only(listing)


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
        logger.exception("Could not send locals® post to %s", chat_id)
        return False


def _should_alert_fetch_error(previous_status: Dict) -> bool:
    if not previous_status or previous_status.get("last_status") != "error":
        return True
    last_checked = previous_status.get("last_checked_at")
    if last_checked is None:
        return True
    return locals_store.utc_now() - last_checked >= ERROR_ALERT_COOLDOWN


def _notify_admin_fetch_failed(bot, error: Exception, previous_status: Dict) -> bool:
    if not ADMIN_ID or not _should_alert_fetch_error(previous_status):
        return False
    text = f"⚠️ <b>locals®: не вдалося перевірити оголошення</b>\n\nПричина: {html.escape(str(error))}"
    try:
        bot.send_message(chat_id=ADMIN_ID, text=text, parse_mode="HTML", disable_web_page_preview=True)
        return True
    except Exception:
        logger.exception("Could not notify admin about a locals® fetch failure")
        return False


def check_job(context) -> Dict[str, int]:
    if not CHECK_ENABLED:
        locals_store.record_status("disabled", listings_count=0)
        return {"ok": 1, "enabled": 0, "sent": 0}
    bot = context.bot
    try:
        all_listings = _fetch_listings()
    except Exception as exc:
        previous_status = locals_store.latest_status()
        alerted = _notify_admin_fetch_failed(bot, exc, previous_status)
        locals_store.record_status("error", listings_count=0, error=str(exc))
        logger.warning("locals® scan failed; admin_alerted=%s: %s", alerted, exc)
        return {"ok": 0, "enabled": 1, "sent": 0, "admin_alerted": int(alerted)}
    enriched = _add_full_rent(all_listings)
    stored = locals_store.upsert_listings(all_listings)
    locals_store.record_status("ok", listings_count=stored)
    active_listings = locals_store.list_active_listings()
    filters = locals_store.list_filters(active_only=True)
    matches = locals_store.select_unsent_matches(active_listings, filters, locals_store.delivered_pairs())
    sent = 0
    for filt, listing in matches:
        chat_id = int(filt["user_id"])
        text = locals_matching.format_notification(listing, lang=i18n.get_lang(chat_id))
        posted_as_caption = _send_listing(bot, chat_id, listing, text)
        if not posted_as_caption:
            bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML", disable_web_page_preview=False)
        locals_store.mark_delivered(int(filt["filter_id"]), str(listing["listing_key"]))
        sent += 1
    logger.info(
        "locals® scan total=%s stored=%s full_rent=%s filters=%s sent=%s",
        len(all_listings), stored, enriched, len(filters), sent,
    )
    return {"ok": 1, "enabled": 1, "stored": stored, "filters": len(filters), "sent": sent}
