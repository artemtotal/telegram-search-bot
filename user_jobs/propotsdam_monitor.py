"""Scheduled ProPotsdam collector integration for PotsdamBot."""

import html
import logging
import os
import time
from datetime import timedelta
from threading import Lock
from typing import Dict, List, Optional

import requests
from telegram import InputMediaPhoto

import i18n
from user_jobs import propotsdam_matching, propotsdam_parser, propotsdam_store

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
# Telegram кладёт в один альбом не больше 10 фото, сколько бы их ни было в фиде.
PROPOTSDAM_ALBUM_MAX = 10
PROPOTSDAM_PHOTO_LIMIT = int(os.getenv("PROPOTSDAM_PHOTO_LIMIT", str(PROPOTSDAM_ALBUM_MAX)) or PROPOTSDAM_ALBUM_MAX)
PROPOTSDAM_PHOTO_TIMEOUT = int(os.getenv("PROPOTSDAM_PHOTO_TIMEOUT", "30") or 30)
# Подпись к фото Telegram обрезает жёстче, чем обычный текст (1024 против 4096
# символов). Оценка по сырой длине текста с HTML-тегами — с запасом в плюс,
# настоящий лимит считается уже после разбора тегов.
PROPOTSDAM_CAPTION_LIMIT = 1024
# How long a tick waits for a scan it just started before leaving the result to
# the next tick. Only bounds the wait — it is never reported as a failure.
PROPOTSDAM_SCAN_WAIT_SECONDS = int(os.getenv("PROPOTSDAM_SCAN_WAIT_SECONDS", "100") or 100)
PROPOTSDAM_POLL_SECONDS = int(os.getenv("PROPOTSDAM_POLL_SECONDS", "5") or 5)
CHECK_INTERVAL_SECONDS = 15 * 60
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
ERROR_ALERT_COOLDOWN = timedelta(hours=6)
EMPTY_ALERT_AFTER = timedelta(hours=int(os.getenv("PROPOTSDAM_EMPTY_ALERT_HOURS", "24") or 24))
EMPTY_ALERT_COOLDOWN = timedelta(hours=12)
EMPTY_HINT = (
    "Скорее всего на портале действительно нет предложений — это нормально.\n"
    "Но точно так же выглядит слетевшая сессия сборщика, поэтому стоит проверить:\n"
    "1) Откройте на ПК Chrome-профиль сборщика ProPotsdam.\n"
    "2) Зайдите на портал и убедитесь, что Immobiliensuche открывается без повторного входа.\n"
    "3) Если портал показывает квартиры, а бот их не видит — сессия сборщика умерла."
)
RELOGIN_HINT = (
    "Как чинить:\n"
    "1) Откройте на ПК Chrome-профиль сборщика ProPotsdam.\n"
    "2) Зайдите на https://propotsdam-kundenportal.easysquare.com/propotsdam-kundenportal/index.html\n"
    "3) Войдите в ProPotsdam/easysquare вручную и убедитесь, что открывается Immobiliensuche.\n"
    "4) После входа следующая проверка снова соберёт квартиры автоматически."
)
_scan_lock = Lock()
# `finished_at` of the newest collector result already stored. Kept in memory on
# purpose: after a restart the newest result is simply consumed once more, and
# `upsert_listings` plus the delivery keys make that a no-op instead of a resend.
_last_consumed_finished_at: Optional[str] = None


class ScanPending(Exception):
    """The collector is still scanning; a later tick will pick the result up."""


def _fetch_last_result() -> Dict:
    response = requests.get(
        f"{PROPOTSDAM_RECEIVER_URL}/api/propotsdam/listings",
        timeout=PROPOTSDAM_TIMEOUT,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("collector returned a non-object payload")
    return payload


def _request_scan() -> List[Dict]:
    """Start a scan and return the newest completed result.

    The collector runs a real browser, so a scan can outlast any HTTP timeout the
    bot is willing to hold a worker thread for. It therefore runs in the
    background: this starts one, waits a short while, and if it is not done yet
    raises `ScanPending` so the next tick consumes it. Previously the request
    blocked for the whole scan, and one slow run produced a bogus "collector is
    broken, re-login" alert even though the scan had actually succeeded.
    """
    global _last_consumed_finished_at
    started = requests.post(
        f"{PROPOTSDAM_RECEIVER_URL}/api/propotsdam/scan",
        timeout=PROPOTSDAM_TIMEOUT,
    )
    started.raise_for_status()

    deadline = time.monotonic() + PROPOTSDAM_SCAN_WAIT_SECONDS
    while True:
        payload = _fetch_last_result()
        finished_at = payload.get("finished_at")
        if finished_at and finished_at != _last_consumed_finished_at:
            if not payload.get("ok"):
                raise RuntimeError(str(payload.get("error") or "collector reported a failed scan"))
            listings = payload.get("listings") or []
            if not isinstance(listings, list):
                raise RuntimeError("collector returned non-list listings")
            _last_consumed_finished_at = finished_at
            return listings
        if not payload.get("running") and not finished_at:
            raise RuntimeError(str(payload.get("error") or "collector has produced no result yet"))
        if time.monotonic() >= deadline:
            raise ScanPending()
        time.sleep(PROPOTSDAM_POLL_SECONDS)


def _fetch_photos(listing: Dict) -> List[bytes]:
    """Байты всех фото квартиры — из кеша коллектора, по одному запросу на фото.

    Ходим за ними только здесь, для квартиры, которую прямо сейчас отправляем:
    качать фото всей выдачи ради пары совпадений незачем.
    """
    limit = max(0, min(PROPOTSDAM_PHOTO_LIMIT, PROPOTSDAM_ALBUM_MAX))
    photos: List[bytes] = []
    for resource_id in propotsdam_parser.image_resource_ids(listing)[:limit]:
        try:
            response = requests.get(
                f"{PROPOTSDAM_RECEIVER_URL}/api/propotsdam/photo/{resource_id}",
                timeout=PROPOTSDAM_PHOTO_TIMEOUT,
            )
            response.raise_for_status()
            if response.content:
                photos.append(response.content)
        except Exception as exc:
            logger.warning("Could not fetch ProPotsdam photo %s: %s", resource_id, exc)
    return photos


def _photo_filename(body: bytes, index: int) -> str:
    """Имя файла по сигнатуре самих байт.

    Без него python-telegram-bot угадывает тип через imghdr и на всём, что тот
    не опознал, подставляет application.octet-stream — Telegram такую «фотку»
    может и не принять.
    """
    if body.startswith(b"\x89PNG\r\n\x1a\n"):
        suffix = "png"
    elif body.startswith(b"RIFF") and body[8:12] == b"WEBP":
        suffix = "webp"
    else:
        suffix = "jpg"
    return f"propotsdam-{index}.{suffix}"


def _send_listing(bot, chat_id: int, listing: Dict, text: str) -> tuple[int, bool]:
    """Шлёт квартиру одним постом: альбом фото с текстом объявления подписью снизу.

    Возвращает (сколько фото ушло, ушёл ли текст подписью). Если подпись не
    ушла — из-за отсутствия фото или превышения лимита в 1024 символа —
    вызывающий обязан отправить тот же текст отдельным сообщением, как раньше;
    без этого длинное объявление осталось бы без текста вовсе.
    """
    photos = _fetch_photos(listing)
    if not photos:
        return 0, False
    caption = text if len(text) <= PROPOTSDAM_CAPTION_LIMIT else None
    try:
        if len(photos) == 1:
            bot.send_photo(
                chat_id=chat_id, photo=photos[0], filename=_photo_filename(photos[0], 1),
                caption=caption, parse_mode="HTML" if caption else None,
            )
        else:
            media = []
            for index, photo in enumerate(photos, start=1):
                item_kwargs = {"media": photo, "filename": _photo_filename(photo, index)}
                # Telegram показывает подписью всей группы только подпись первого элемента.
                if index == 1 and caption:
                    item_kwargs["caption"] = caption
                    item_kwargs["parse_mode"] = "HTML"
                media.append(InputMediaPhoto(**item_kwargs))
            bot.send_media_group(chat_id=chat_id, media=media)
        return len(photos), caption is not None
    except Exception:
        logger.exception("Could not send ProPotsdam post to %s", chat_id)
        return 0, False


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


def _maybe_alert_prolonged_empty(bot) -> bool:
    """Warn when the portal has been empty for implausibly long.

    A scan that returns zero listings is reported as "ok", because an empty portal is a
    normal state. The problem is that a dead collector session produces exactly the same
    result, so a silent breakage is indistinguishable from a quiet week. Once the portal
    has been empty longer than apartments realistically stay unavailable, say so.
    """
    last_seen = propotsdam_store.latest_listing_seen_at()
    if last_seen is None:
        return False
    now = propotsdam_store.utc_now()
    quiet_for = now - last_seen
    if quiet_for < EMPTY_ALERT_AFTER:
        return False
    last_alert = propotsdam_store.last_empty_alert_at()
    if last_alert is not None and now - last_alert < EMPTY_ALERT_COOLDOWN:
        return False
    if not ADMIN_ID:
        return False
    hours = int(quiet_for.total_seconds() // 3600)
    text = (
        "🟡 <b>ProPotsdam: ни одной квартиры уже %s ч</b>\n\n"
        "Проверки идут по расписанию и завершаются успешно, но портал "
        "возвращает пустой список.\n\n%s" % (hours, html.escape(EMPTY_HINT))
    )
    try:
        bot.send_message(chat_id=ADMIN_ID, text=text, parse_mode="HTML", disable_web_page_preview=True)
        propotsdam_store.record_empty_alert()
        return True
    except Exception:
        logger.exception("Could not notify admin about prolonged ProPotsdam emptiness")
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
    except ScanPending:
        # Not a failure: the browser is still working. Keep the previous status
        # so a slow scan cannot masquerade as a dead session.
        logger.info("ProPotsdam scan still running; the result will be consumed on a later tick")
        return {"ok": 1, "enabled": 1, "pending": 1, "sent": 0}
    except Exception as exc:
        previous_status = propotsdam_store.latest_status()
        alerted = _notify_admin_error(bot, exc, previous_status)
        propotsdam_store.record_status("error", listings_count=0, error=str(exc))
        logger.warning("ProPotsdam scan failed; admin_alerted=%s: %s", alerted, exc)
        return {"ok": 0, "enabled": 1, "sent": 0, "admin_alerted": int(alerted)}
    stored = propotsdam_store.upsert_listings(listings)
    propotsdam_store.record_status("ok", listings_count=stored)
    empty_alerted = _maybe_alert_prolonged_empty(bot) if stored == 0 else False
    active_listings = propotsdam_store.list_active_listings()
    filters = propotsdam_store.list_filters(active_only=True)
    matches = propotsdam_store.select_unsent_matches(active_listings, filters, propotsdam_store.delivered_pairs())
    sent = 0
    photos_sent = 0
    for filt, listing in matches:
        chat_id = int(filt["user_id"])
        text = propotsdam_matching.format_notification(listing, PROPOTSDAM_PORTAL_URL, lang=i18n.get_lang(chat_id))
        photos, posted_as_caption = _send_listing(bot, chat_id, listing, text)
        photos_sent += photos
        # Фото без подписи (нет фото вообще, или текст не влез в лимит подписи)
        # оставляют текст неотправленным — досылаем его отдельным сообщением.
        if not posted_as_caption:
            bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML", disable_web_page_preview=False)
        propotsdam_store.mark_delivered(int(filt["filter_id"]), str(listing["listing_key"]))
        sent += 1
    logger.info(
        "ProPotsdam scan stored=%s filters=%s matches=%s sent=%s photos=%s empty_alerted=%s",
        stored, len(filters), len(matches), sent, photos_sent, int(empty_alerted),
    )
    return {
        "ok": 1,
        "enabled": 1,
        "stored": stored,
        "filters": len(filters),
        "sent": sent,
        "photos": photos_sent,
        "empty_alerted": int(empty_alerted),
    }
