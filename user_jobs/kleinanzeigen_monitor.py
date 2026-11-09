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

Since 02.11.2026 (stage 2 of docs/housing-region-plan.md) the scan also
reads the first page of Berlin and of Brandenburg (a second one only when the
first is almost all new) for filters with a place. Those flats are matched by
postcode, not by "the city is Potsdam"; a listing page (full rent, photos) is
read only for regional flats that fit somebody — a few a day, not hundreds.

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
import re
import time
from datetime import timedelta
from typing import Dict, List

import requests
from telegram import InputMediaPhoto

import i18n
from database import KleinanzeigenListing
from user_jobs import (
    alert_cooldown, delivery_dedup, housing_journey_store, housing_tier, kleinanzeigen_block, kleinanzeigen_matching,
    kleinanzeigen_parser, kleinanzeigen_store,
)

logger = logging.getLogger(__name__)

CHECK_ENABLED = os.getenv("KLEINANZEIGEN_CHECK_ENABLED", "1") == "1"
TIMEOUT = int(os.getenv("KLEINANZEIGEN_TIMEOUT", "30") or 30)
CHECK_INTERVAL_SECONDS = 20 * 60
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
ERROR_ALERT_COOLDOWN = timedelta(hours=2)
_USER_AGENT = "Mozilla/5.0 (compatible; PotsdamHousingBot/1.0)"
# Подпись к фото Telegram обрезает жёстче обычного текста (1024 против 4096
# символов) — раньше этого лимита текст уходит подписью, дальше отдельным сообщением.
# Telegram кладе в один альбом не більше 10 фото, скільки б їх не було.
GALLERY_ALBUM_MAX = 10
CAPTION_LIMIT = 1024
# Пошук по Потсдаму ніколи не буває порожнім — сторінка стабільно віддає 25-26
# карток. Одиничний нуль (реально спостережений 2026-08-25 12:45 серед сусідніх
# перевірок із 25-26) означає не зміну розмітки, а миттєвий збій на боці сайту,
# і повторний запит одразу повертає нормальну сторінку.
_FETCH_ATTEMPTS = 3
_RETRY_BACKOFF_SECONDS = (2, 5)
# Етап 2 «Берлін і Бранденбург» (02.11.2026).
REGIONS = tuple(
    region.strip() for region in os.getenv("KLEINANZEIGEN_REGIONS", "berlin,brandenburg").split(",")
    if region.strip() in kleinanzeigen_parser.REGION_PATHS
)
REGION_MAX_PAGES = 2
# Друга сторінка — лише коли на першій стільки нових: інакше нового там нема.
REGION_MORE_PAGES_AT = 15
REGION_PAUSE_SECONDS = 2
# Зі сторінки квартира зникає за години, а знята вона — коли її давно не видно.
REGION_STALE_AFTER = timedelta(days=3)
# Сторінки оголошень регіону за обхід: лише тим, що комусь підходять.
REGION_DETAILS_PER_SCAN = 10


def _fetch_listings() -> List[Dict]:
    last_error = None
    for attempt in range(_FETCH_ATTEMPTS):
        try:
            # 08.11.2026: на 403/429 повтор і проксі ПК — усередині kleinanzeigen_block.get
            response = kleinanzeigen_block.get(
                kleinanzeigen_parser.LISTINGS_URL, timeout=TIMEOUT, headers={"User-Agent": _USER_AGENT},
            )
            response.raise_for_status()
            # Сторінка віддається як UTF-8, але з 2026-09-01 заголовок
            # Content-Type більше не називає кодування — requests за стандартом
            # відкочується на ISO-8859-1, і «m²» перетворюється на «mÂ²».
            # Розбір після цього не знаходить ані площі, ані ціни.
            response.encoding = response.apparent_encoding or "utf-8"
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
            if kleinanzeigen_block.is_block(exc):
                # Пауза, повтор і проксі вже були — ще дві спроби лише подражнять сайт.
                break
        if attempt + 1 < _FETCH_ATTEMPTS:
            time.sleep(_RETRY_BACKOFF_SECONDS[attempt])
    if last_error is not None:
        raise last_error
    # Порожньо і після всіх спроб — тоді це вже схоже на справжню зміну
    # розмітки, і про це має дізнатись адмін (_notify_admin_parse_broke).
    return []


def _send_listing(bot, chat_id: int, listing: Dict, text: str) -> bool:
    """Шле квартиру одним постом: галерея фото з текстом підписом знизу.

    Фотографії беруться з бази — їх забрали тим самим запитом, яким читали
    повну ціну, тож окремого походу на сторінку оголошення тут немає:
    Kleinanzeigen забороняє автоматичний збір в Умовах використання, і бот
    навмисне тримає для неї рідший інтервал і один запит на оголошення.
    Коли галереї немає (старі записи), лишається обкладинка зі сторінки
    пошуку — як і раніше.

    Повертає True, якщо текст пішов підписом; False означає, що фото немає
    або підпис не вліз у ліміт, і викликач має надіслати текст окремо.
    """
    photos = [url for url in (listing.get("gallery_urls") or []) if str(url).strip()]
    if not photos:
        cover = str(listing.get("cover_image_url") or "").strip()
        photos = [cover] if cover else []
    if not photos:
        return False
    caption = text if len(text) <= CAPTION_LIMIT else None
    try:
        if len(photos) == 1:
            bot.send_photo(
                chat_id=chat_id, photo=photos[0],
                caption=caption, parse_mode="HTML" if caption else None,
            )
            return caption is not None
        media = [InputMediaPhoto(media=photos[0], caption=caption, parse_mode="HTML" if caption else None)]
        media.extend(InputMediaPhoto(media=url) for url in photos[1:])
        bot.send_media_group(chat_id=chat_id, media=media)
        return caption is not None
    except Exception:
        logger.exception("Could not send Kleinanzeigen post to %s", chat_id)
        return False


# Some swap-listing posters (e.g. the commercial account "Wohnungsswap.de")
# title every ad with the English "swap" instead of the German "Tausch" -
# a real one slipped through notifications with only "tausch" excluded.
_SWAP_KEYWORDS = ("tausch", "swap")

# У розділі «Wohnung mieten» трапляються оголошення про кімнату в WG чи
# суборенду: «Möbliertes Zimmer in Potsdam zur Untermiete – 400 €» парсер
# прочитав як 2 кімнати й 56 м² (площа всієї квартири), і воно пішло людині
# з фільтром «2–3 кімнати» (опитування 13.10.2026, «приходять не ті квартири»).
# Кімната — це слово «Zimmer» без числа перед ним і без «Wohnung» після:
# «2-Zimmer-Wohnung», «3 Zimmer, 75 m²», «Ein-Zimmer-Apartment» лишаються.
_ZIMMER_RE = re.compile(r"\bzimmer\b", re.IGNORECASE)
_FLAT_AFTER_ZIMMER_RE = re.compile(r"[\s-]*(wohnung|whg|apartment|appartement|maisonette)", re.IGNORECASE)
_COUNT_BEFORE_ZIMMER_RE = re.compile(
    r"(\d(?:[.,]\d)?|½|\b(?:ein|eine|zwei|drei|vier|fünf|fuenf|sechs))[\s-]*$", re.IGNORECASE
)


# «1 Zimmer in einer 4-Zimmer-Wohnung / Zweck-WG» — теж кімната, хоч перед
# «Zimmer» і стоїть число (02.11.2026 таку отримали 42 людини, що шукають квартиру).
_ROOM_IN_FLAT_RE = re.compile(r"\bzimmer\s+in\s+(?:einer|der|unserer|meiner|einem)\b|\bzweck[\s!-]*wg\b", re.IGNORECASE)


def _is_room_offer(title: str) -> bool:
    if _ROOM_IN_FLAT_RE.search(title):
        return True
    for match in _ZIMMER_RE.finditer(title):
        if _FLAT_AFTER_ZIMMER_RE.match(title, match.end()):
            continue
        if _COUNT_BEFORE_ZIMMER_RE.search(title[:match.start()]):
            continue
        return True
    return False


# Самоперевірка 09.11.2026: «Möblierte Unterkünfte in Spandau – Für Firmen und
# Großunternehmen» і «Möblierte Unterkunft in Nauen» за «50 €» (ціна за ніч/ліжко
# для бригад) пішли 5 людям як квартира за 1 €/м². Житло для монтажників і фірм —
# не оренда для життя: «Unterkunft/Unterkünfte», «Monteur», «für Firmen», «pro Nacht».
_LODGING_RE = re.compile(
    r"unterk(?:u|ü|ue)nft|monteur|arbeiter[\s-]*(?:zimmer|wohnung|unterk)|f(?:ü|ue|u)r\s+firmen"
    r"|firmen[\s-]*(?:wohnung|unterk)|pro\s+(?:nacht|tag|person)|\bferien[\s-]*wohnung|\bfewo\b",
    re.IGNORECASE,
)
# «Nachmieter gesucht» за «1 €» (09.11.2026): справжню ціну автор у список не поставив,
# а квартира без правдивої ціни пройшла б будь-який фільтр. Оренда квартири дешевша за
# 100 € не буває — таке не розсилаємо, доки ціна не стане схожою на справжню.
MIN_REAL_RENT_EUR = 100.0


def _placeholder_price(listing: Dict) -> bool:
    price = listing.get("price_eur")
    return isinstance(price, (int, float)) and 0 < price < MIN_REAL_RENT_EUR


def _is_flat_offer(listing: Dict) -> bool:
    title = str(listing.get("title") or "").casefold()
    return (
        not any(keyword in title for keyword in _SWAP_KEYWORDS)
        and not _is_room_offer(title)
        and not _LODGING_RE.search(title)
        and not _placeholder_price(listing)
    )


def _is_relevant(listing: Dict) -> bool:
    city = str(listing.get("city") or "").strip().casefold()
    return city == "potsdam" and _is_flat_offer(listing)


def _fetch_region_page(url: str) -> List[Dict]:
    response = kleinanzeigen_block.get(url, timeout=TIMEOUT, headers={"User-Agent": _USER_AGENT})
    response.raise_for_status()
    response.encoding = response.apparent_encoding or "utf-8"
    return kleinanzeigen_parser.parse_listings(response.text)


def _scan_regions(bot, skip=frozenset()) -> List[Dict]:
    """Берлін і Бранденбург: перша сторінка (друга — коли на першій майже все
    нове). Помилка тут не зупиняє Потсдам. Повертає квартири цього обходу."""
    known = kleinanzeigen_store.known_keys()
    fetched: List[Dict] = []
    for region in REGIONS:
        listings: List[Dict] = []
        seen = set(skip)
        try:
            for page in range(1, REGION_MAX_PAGES + 1):
                if page > 1:
                    time.sleep(REGION_PAUSE_SECONDS)
                items = _fetch_region_page(kleinanzeigen_parser.region_url(region, page))
                fresh = 0
                for item in items:
                    key = str(item.get("listing_key") or "")
                    if not key or key in seen or not _is_flat_offer(item):
                        continue
                    seen.add(key)
                    fresh += key not in known
                    listings.append(dict(item, region=region))
                if fresh < REGION_MORE_PAGES_AT:
                    break
        except requests.RequestException as exc:
            # 08.11.2026: відмова 403/429 — лише у вечірньому зведенні, не миттєво
            alerted = False if kleinanzeigen_block.is_block(exc) else _notify_admin_fetch_failed(bot, exc, {})
            logger.warning("Kleinanzeigen %s scan failed; admin_alerted=%s: %s", region, alerted, exc)
            continue
        first_scan = not kleinanzeigen_store.has_region(region)
        kleinanzeigen_store.upsert_listings(listings, region=region, deactivate_missing=False)
        if first_scan:
            _baseline(listings)
        gone =kleinanzeigen_store.deactivate_stale(region, kleinanzeigen_store.utc_now() - REGION_STALE_AFTER)
        logger.info("Kleinanzeigen %s scan listings=%s new=%s gone=%s", region, len(listings),
                    sum(1 for item in listings if item["listing_key"] not in known), gone)
        fetched.extend(listings)
        time.sleep(REGION_PAUSE_SECONDS)
    return fetched


def _baseline(listings: List[Dict]) -> int:
    """Перший обхід регіону: усе, що вже висить, — не нове.

    02.11.2026 перша сторінка Бранденбургу принесла 4 потсдамські квартири,
    яких не було на потсдамській, і одну двотижневої давності двоє людей
    отримали як щойно знайдену."""
    marked = 0
    for filt in kleinanzeigen_store.list_filters():
        for listing in listings:
            if kleinanzeigen_matching.matches_filter(listing, filt):
                kleinanzeigen_store.mark_delivered(int(filt["filter_id"]), str(listing["listing_key"]))
                marked += 1
    return marked


def _wanted_regional(listings: List[Dict], filters: List[Dict]) -> List[Dict]:
    """Квартири регіону, що комусь підходять за тим, що видно на сторінці, і ще
    без повної ціни — лише їм читаємо сторінку оголошення."""
    enriched = kleinanzeigen_store.keys_already_enriched()
    wanted = [
        item for item in listings
        if item["listing_key"] not in enriched
        and any(kleinanzeigen_matching.matches_filter(item, filt) for filt in filters)
    ]
    return wanted[:REGION_DETAILS_PER_SCAN]


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
    # Не last_checked_at статусу: він оновлюється на кожній перевірці, тож
    # пауза від нього ніколи не спливала і тривога приходила лише раз.
    return alert_cooldown.due("kleinanzeigen:parse", ERROR_ALERT_COOLDOWN)


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
        alert_cooldown.mark("kleinanzeigen:parse")
        return True
    except Exception:
        logger.exception("Could not notify admin about a broken Kleinanzeigen parse")
        return False


def _should_alert_fetch_error(previous_status: Dict) -> bool:
    if not previous_status or previous_status.get("last_status") != "error":
        return True
    # Не last_checked_at статусу: він оновлюється на кожній перевірці, тож
    # пауза від нього ніколи не спливала і тривога приходила лише раз.
    return alert_cooldown.due("kleinanzeigen:fetch", ERROR_ALERT_COOLDOWN)


def _notify_admin_fetch_failed(bot, error: Exception, previous_status: Dict) -> bool:
    """The one thing to watch during the 20-minute-interval experiment: does
    Kleinanzeigen start refusing requests? A 403/429 here is the signal."""
    if not ADMIN_ID or not _should_alert_fetch_error(previous_status):
        return False
    status_code = getattr(getattr(error, "response", None), "status_code", None)
    hint = ""
    if status_code in kleinanzeigen_block.BLOCK_CODES:
        # 08.11.2026: сюди доходить лише довга відмова — повтор, проксі ПК і
        # подовжений інтервал бот уже спробував сам.
        hint = (
            f"\n\n🚫 Код {status_code}: Kleinanzeigen блокує запити вже понад "
            f"{int(kleinanzeigen_block.ALERT_AFTER.total_seconds() // 3600)} год поспіль, і через проксі ПК теж "
            "(чи увімкнені ПК і тунель?). Бот сам перевіряє рідше: раз на "
            f"{kleinanzeigen_block.interval_minutes(CHECK_INTERVAL_SECONDS)} хв."
        )
    text = f"⚠️ <b>Kleinanzeigen: не вдалося перевірити оголошення</b>\n\nПричина: {html.escape(str(error))}{hint}"
    try:
        bot.send_message(chat_id=ADMIN_ID, text=text, parse_mode="HTML", disable_web_page_preview=True)
        alert_cooldown.mark("kleinanzeigen:fetch")
        return True
    except Exception:
        logger.exception("Could not notify admin about a Kleinanzeigen fetch failure")
        return False


def _add_full_rent(listings: List[Dict]) -> int:
    """Дозабирає повну ціну зі сторінок оголошень.

    Площадка тримає її готовим числом серед атрибутів оголошення, поруч із
    Nebenkosten. Запит іде лише за тим оголошенням, чиєї повної ціни ще немає —
    один раз на оголошення, тобто одиниці на добу,
    тож це не перетворює обхід на систематичний збір, якого Умови
    використання не дозволяють; сам список і далі опитується рідше за решту
    джерел. Збій на одному оголошенні не псує обхід.
    """
    enriched_keys = kleinanzeigen_store.keys_already_enriched()
    filled = 0
    for listing in listings:
        if str(listing.get("listing_key") or "") in enriched_keys:
            continue
        detail_url = str(listing.get("detail_url") or "").strip()
        if not detail_url:
            continue
        try:
            response = kleinanzeigen_block.get(detail_url, timeout=TIMEOUT, headers={"User-Agent": _USER_AGENT})
            response.raise_for_status()
            response.encoding = response.apparent_encoding or "utf-8"
            prices = kleinanzeigen_parser.parse_detail_prices(response.text)
            # Фотографії — тим самим запитом: окремого походу на сторінку
            # оголошення заради них не робимо.
            gallery = kleinanzeigen_parser.parse_gallery_urls(response.text)[:GALLERY_ALBUM_MAX]
            if gallery:
                listing["gallery_urls"] = gallery
        except Exception as exc:
            logger.warning("Could not read Kleinanzeigen prices for %s: %s", detail_url, exc)
            continue
        # Холодна в нас — зі сторінки видачі, а вона може не збігатися з
        # «ExactPreis» картки, тож звіряємо повну ще й з нею.
        warm = kleinanzeigen_parser.consistent_warm(
            listing.get("price_eur") if isinstance(listing.get("price_eur"), (int, float)) else None,
            prices.get("price_warm_eur"),
            prices.get("nebenkosten_eur"),
        )
        if warm is not None:
            listing["price_warm_eur"] = warm
            filled += 1
    return filled


def check_job(context) -> Dict[str, int]:
    if not CHECK_ENABLED:
        kleinanzeigen_store.record_status("disabled", listings_count=0)
        return {"ok": 1, "enabled": 0, "sent": 0}
    bot = context.bot
    if not kleinanzeigen_block.begin_run():
        logger.info("Kleinanzeigen: interval widened to %s min after a 403/429, skipping this run",
                    kleinanzeigen_block.interval_minutes(CHECK_INTERVAL_SECONDS))
        return {"ok": 1, "enabled": 1, "sent": 0, "skipped": 1}
    try:
        all_listings = _fetch_listings()
    except Exception as exc:
        kleinanzeigen_block.end_run(False, CHECK_INTERVAL_SECONDS)
        previous_status = kleinanzeigen_store.latest_status()
        if kleinanzeigen_block.is_block(exc) and not kleinanzeigen_block.failing_too_long():
            alerted = False   # разова відмова — у зведенні 21:00, не миттєво
        else:
            alerted = _notify_admin_fetch_failed(bot, exc, previous_status)
        kleinanzeigen_store.record_status("error", listings_count=0, error=str(exc))
        logger.warning("Kleinanzeigen scan failed; admin_alerted=%s: %s", alerted, exc)
        return {"ok": 0, "enabled": 1, "sent": 0, "admin_alerted": int(alerted)}
    if not all_listings:
        _notify_admin_parse_broke(bot, kleinanzeigen_store.latest_status())
    relevant = [item for item in all_listings if _is_relevant(item)]
    regional = _scan_regions(bot, skip={str(item.get("listing_key")) for item in relevant})
    filters = kleinanzeigen_store.list_filters(active_only=True)
    wanted = _wanted_regional(regional, filters)
    enriched = _add_full_rent(relevant + wanted)
    stored = kleinanzeigen_store.upsert_listings(relevant)
    for region in REGIONS:
        read = [item for item in wanted if item.get("region") == region]
        if read:
            kleinanzeigen_store.upsert_listings(read, region=region, deactivate_missing=False)
    kleinanzeigen_store.record_status("ok", listings_count=stored)
    kleinanzeigen_block.end_run(True, CHECK_INTERVAL_SECONDS)
    # Уже збережені до нових правил (житло для бригад, ціна-заглушка) теж не розсилаємо.
    active_listings = [item for item in kleinanzeigen_store.list_active_listings() if _is_flat_offer(item)]
    matches = kleinanzeigen_store.select_unsent_matches(
        active_listings, filters, kleinanzeigen_store.delivered_pairs()
    )
    sent = 0
    dedup = delivery_dedup.PerUserDedup()
    gate = housing_tier.DeliveryGate("kleinanzeigen", KleinanzeigenListing)
    for filt, listing in matches:
        chat_id = int(filt["user_id"])
        listing_key = str(listing["listing_key"])
        if not gate.due(chat_id, listing):
            continue
        # Та сама квартира під двома фільтрами однієї людини — одне
        # повідомлення; позначку про доставку отримують обидва фільтри.
        if not dedup.claim(chat_id, listing_key):
            kleinanzeigen_store.mark_delivered(int(filt["filter_id"]), listing_key)
            continue
        text = kleinanzeigen_matching.format_notification(listing, lang=i18n.get_lang(chat_id))
        text += gate.footer(chat_id, listing_key)
        try:
            posted_as_caption = _send_listing(bot, chat_id, listing, text)
            if not posted_as_caption:
                bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML", disable_web_page_preview=False)
        except Exception as exc:
            # Людина заблокувала бота чи видалила акаунт: пробувати далі марно.
            # Раніше виняток тут обривав увесь обхід, і всі фільтри після неї
            # не отримували нічого (знайдено 12.10.2026).
            if housing_tier.recipient_gone(exc):
                kleinanzeigen_store.mark_delivered(int(filt["filter_id"]), listing_key)
            else:
                logger.exception("Could not send kleinanzeigen listing %s to %s", listing_key, chat_id)
            continue
        kleinanzeigen_store.mark_delivered(int(filt["filter_id"]), listing_key)
        gate.sent(chat_id, listing_key, int(filt["filter_id"]))
        sent += 1
    near_misses = _send_near_misses(bot, filters, gate)
    logger.info(
        "Kleinanzeigen scan total=%s relevant=%s regional=%s stored=%s full_rent=%s filters=%s sent=%s near_miss=%s",
        len(all_listings), len(relevant), len(regional), stored, enriched, len(filters), sent, near_misses,
    )
    return {"ok": 1, "enabled": 1, "stored": stored, "filters": len(filters), "sent": sent, "near_miss": near_misses}


# «Трохи дорожче, але може підійти» — не частіше разу на 4 дні на людину і
# лише про свіжі оголошення: це підказка, а не друга стрічка.
NEAR_MISS_EVERY = timedelta(days=4)
NEAR_MISS_FRESH = timedelta(days=2)


def _send_near_misses(bot, filters: List[Dict], gate) -> int:
    """Квартири, що не пройшли лише через холодну межу, хоча показана ціна
    може бути повною орендою (див. kleinanzeigen_matching.near_miss_over).

    Кожній людині — щонайбільше одна, з найменшим перевищенням. Доставленою
    вона позначається всім фільтрам людини, для яких це той самий випадок,
    щоб за 4 дні та сама квартира не прийшла вдруге.
    """
    fresh = [item for item in kleinanzeigen_store.list_active_listings_since(
        kleinanzeigen_store.utc_now() - NEAR_MISS_FRESH) if _is_flat_offer(item)]
    if not fresh:
        return 0
    delivered = kleinanzeigen_store.delivered_pairs()
    not_since = kleinanzeigen_store.utc_now() - NEAR_MISS_EVERY
    allowed: Dict[int, bool] = {}
    best: Dict[int, tuple] = {}
    for filt in filters:
        if not filt.get("active", True):
            continue
        user_id = int(filt["user_id"])
        if user_id not in allowed:
            allowed[user_id] = housing_journey_store.near_miss_due(user_id, not_since)
        if not allowed[user_id]:
            continue
        for listing in fresh:
            key = str(listing.get("listing_key") or "")
            if not key or (int(filt["filter_id"]), key) in delivered:
                continue
            over = kleinanzeigen_matching.near_miss_over(listing, filt)
            if over is None or not gate.due(user_id, listing, remember=False):
                continue
            if user_id not in best or over < best[user_id][0]:
                best[user_id] = (over, filt, listing)
    sent = 0
    for user_id, (over, filt, listing) in best.items():
        listing_key = str(listing["listing_key"])
        lang = i18n.get_lang(user_id)
        text = (
            i18n.t("housing.near_miss.header", lang, over=f"{over:.0f}", limit=f"{float(filt['max_price_eur']):.0f}")
            + "\n\n" + kleinanzeigen_matching.format_notification(listing, lang=lang)
            + gate.footer(user_id, listing_key)
        )
        try:
            if not _send_listing(bot, user_id, listing, text):
                bot.send_message(chat_id=user_id, text=text, parse_mode="HTML", disable_web_page_preview=False)
        except Exception as exc:
            if not housing_tier.recipient_gone(exc):
                logger.exception("Could not send a Kleinanzeigen near miss %s to %s", listing_key, user_id)
                continue
        for other in filters:
            if int(other["user_id"]) == user_id and kleinanzeigen_matching.near_miss_over(listing, other) is not None:
                kleinanzeigen_store.mark_delivered(int(other["filter_id"]), listing_key)
        gate.sent(user_id, listing_key, int(filt["filter_id"]))
        housing_journey_store.mark_near_miss_sent(user_id)
        sent += 1
    return sent
