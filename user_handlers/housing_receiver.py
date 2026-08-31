"""Local HTTP receiver for Immowelt notifications from the shared browser service."""

import html
import json
import logging
import os
import re
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.parse import urlparse

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.error import NetworkError

from database import DBSession, HousingDelivery, ImmoweltListing


logger = logging.getLogger(__name__)
HOST = os.getenv("HOUSING_RECEIVER_HOST", "0.0.0.0")
PORT = int(os.getenv("HOUSING_RECEIVER_PORT", "5012") or 5012)
# Telegram кладёт в один альбом не больше 10 фото, сколько бы их ни было на карточке.
GALLERY_ALBUM_MAX = 10
# Подпись к фото Telegram обрезает жёстче обычного текста (1024 против 4096
# символов) — раньше этого лимита текст уходит подписью, дальше отдельным сообщением.
CAPTION_LIMIT = 1024
# Подписи ошибок, при которых запрос до Telegram заведомо не дошёл: соединение
# не открылось, значит сообщения человек не видел и повтор безопасен. Всё
# остальное (оборванный ответ, таймаут чтения) неоднозначно — там запрос уже
# ушёл, и повтор рискует прислать вторую копию одной квартиры.
_NOT_SENT_MARKERS = (
    "connect timeout",
    "connecttimeout",
    "timed out. (connect timeout",
    "failed to establish a new connection",
    "name or service not known",
    "temporary failure in name resolution",
)


def _text(value):
    return html.escape(str(value or "").strip())


def _already_delivered(user_id, listing_id):
    session = DBSession()
    try:
        return session.query(HousingDelivery).filter(
            HousingDelivery.user_id == int(user_id),
            HousingDelivery.listing_id == str(listing_id),
        ).first() is not None
    finally:
        session.close()


def _mark_delivered(user_id, listing_id):
    session = DBSession()
    try:
        exists = session.query(HousingDelivery).filter(
            HousingDelivery.user_id == int(user_id),
            HousingDelivery.listing_id == str(listing_id),
        ).first()
        if exists is None:
            session.add(HousingDelivery(
                user_id=int(user_id),
                listing_id=str(listing_id),
                sent_at=datetime.utcnow(),
            ))
            session.commit()
    finally:
        session.close()


def _request_never_left(exc):
    """Точно ли сообщение не ушло — только тогда повтор не создаст дубликат."""
    text = str(exc).casefold()
    return any(marker in text for marker in _NOT_SENT_MARKERS)


_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")


def _parse_number(raw):
    """Best-effort extraction of a number from the relay's free-text fields
    (e.g. "950 €", "3 Zimmer", "75 m²", "1.200,50 €") — German formatting uses
    '.' as a thousands separator and ',' as the decimal point."""
    if raw is None:
        return None
    text = str(raw).strip().replace(".", "").replace(",", ".")
    match = _NUMBER_RE.search(text)
    if not match:
        return None
    try:
        return float(match.group())
    except ValueError:
        return None


def _record_listing_for_stats(listing_id, listing):
    """Immowelt has no full-catalogue scan of its own in this bot — a
    separate relay service just forwards matches. So this is the only place
    we ever see an Immowelt listing; recording it here (once per listing_key)
    is what the housing stats dashboard (housing:stats) draws on."""
    session = DBSession()
    try:
        if session.query(ImmoweltListing).get(listing_id) is not None:
            return
        session.add(ImmoweltListing(
            listing_key=listing_id,
            title=str(listing.get("title") or "").strip() or None,
            address=str(listing.get("address") or "").strip() or None,
            rooms=_parse_number(listing.get("rooms")),
            area_m2=_parse_number(listing.get("area")),
            price_eur=_parse_number(listing.get("price")),
            detail_url=str(listing.get("url") or "").strip() or None,
            first_seen_at=datetime.utcnow(),
        ))
        session.commit()
    finally:
        session.close()


def handle_immowelt_result(bot, payload):
    if not isinstance(payload, dict):
        raise ValueError("JSON body must be an object")
    if payload.get("source") != "immowelt":
        raise ValueError("unsupported housing source")

    user_id = int(payload.get("user_id") or 0)
    if user_id <= 0:
        raise ValueError("user_id is required")
    filter_title = str(payload.get("filter_title") or "").strip()
    if not filter_title:
        raise ValueError("filter_title is required")

    listing = payload.get("listing")
    if not isinstance(listing, dict):
        raise ValueError("listing must be an object")
    url = str(listing.get("url") or "").strip()
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc.casefold().endswith("immowelt.de"):
        raise ValueError("listing URL must be an Immowelt HTTPS URL")

    listing_id = str(listing.get("listing_id") or "").strip() or url
    _record_listing_for_stats(listing_id, listing)
    if _already_delivered(user_id, listing_id):
        # Отправитель повторяет объявление, когда не дождался ответа. Само
        # сообщение при этом уже у человека, так что второй раз слать нечего.
        logger.info("Immowelt listing %s already delivered to %s; skipping", listing_id, user_id)
        return {"ok": True, "duplicate": True}

    lines = [
        "🏠 <b>Нове житло на Immowelt</b>",
        "",
        "🔎 Фільтр: " + _text(filter_title),
        "<b>" + (_text(listing.get("title")) or "Mietwohnung") + "</b>",
        "📍 " + (_text(listing.get("address")) or "Adresse unbekannt"),
    ]
    if listing.get("price"):
        lines.append("💶 " + _text(listing["price"]) + " Kaltmiete")
    details = [
        _text(listing.get("rooms")),
        _text(listing.get("area")),
        _text(listing.get("floor")),
    ]
    details = [item for item in details if item]
    if details:
        lines.append("📐 " + " · ".join(details))
    if listing.get("availability"):
        lines.append("📅 " + _text(listing["availability"]))

    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Відкрити на Immowelt", url=url)]]
    )
    text = "\n".join(lines)
    images = [str(u).strip() for u in (listing.get("images") or []) if str(u).strip()][:GALLERY_ALBUM_MAX]

    try:
        _send_immowelt_post(bot, user_id, images, text, keyboard, url)
    except NetworkError as exc:
        if _request_never_left(exc):
            raise
        # Запрос ушёл, но ответ потерялся. На практике Telegram в этом случае
        # сообщение доставляет, поэтому отмечаем его отправленным: повтор
        # прислал бы человеку вторую копию той же квартиры.
        _mark_delivered(user_id, listing_id)
        logger.warning(
            "Immowelt listing %s to %s: response lost, assuming delivered (%s)",
            listing_id, user_id, exc,
        )
        return {"ok": True, "assumed_delivered": True}

    _mark_delivered(user_id, listing_id)
    return {"ok": True}


def _send_text_with_button(bot, chat_id, text, keyboard):
    bot.send_message(
        chat_id=chat_id, text=text, parse_mode="HTML",
        reply_markup=keyboard, disable_web_page_preview=True,
    )


def _send_immowelt_post(bot, chat_id, images, text, keyboard, url):
    """Шлёт квартиру фото с подписью, если фото есть, и текстом с кнопкой — если нет.

    Кнопка "Відкрити на Immowelt" остаётся на одиночном фото и на голом тексте
    (`send_photo`/`send_message` её поддерживают), но на альбоме
    (`send_media_group`) Telegram кнопок вообще не показывает — ссылка тогда
    идёт строкой в подписи. Если подпись всё равно не влезает в лимит фото
    (1024 против 4096 у обычного текста), фото уходят без неё, а тот же текст
    с кнопкой — отдельным сообщением следом, иначе объявление осталось бы
    вовсе без текста.
    """
    if not images:
        _send_text_with_button(bot, chat_id, text, keyboard)
        return
    if len(images) == 1:
        if len(text) <= CAPTION_LIMIT:
            bot.send_photo(chat_id=chat_id, photo=images[0], caption=text, parse_mode="HTML", reply_markup=keyboard)
        else:
            bot.send_photo(chat_id=chat_id, photo=images[0])
            _send_text_with_button(bot, chat_id, text, keyboard)
        return
    album_caption = text + "\n\n🔗 " + _text(url)
    if len(album_caption) <= CAPTION_LIMIT:
        media = [InputMediaPhoto(media=images[0], caption=album_caption, parse_mode="HTML")]
        media.extend(InputMediaPhoto(media=photo_url) for photo_url in images[1:])
        bot.send_media_group(chat_id=chat_id, media=media)
    else:
        bot.send_media_group(chat_id=chat_id, media=[InputMediaPhoto(media=photo_url) for photo_url in images])
        _send_text_with_button(bot, chat_id, text, keyboard)


def handle_system_message(bot, payload):
    """Пересылает готовый HTML-текст одному человеку.

    Для heartbeat-алертов, денного дайджеста та зведення про перевищений
    ліміт — усе, що check-Wohnung раніше слав напряму зі свого окремого бота.
    Дедуплікації тут немає: на відміну від оголошень, ці повідомлення не
    мають стабільного ключа, а повторюються рідко й самі по собі ідемпотентні.
    """
    if not isinstance(payload, dict):
        raise ValueError("JSON body must be an object")
    user_id = int(payload.get("user_id") or 0)
    if user_id <= 0:
        raise ValueError("user_id is required")
    text = str(payload.get("text") or "").strip()
    if not text:
        raise ValueError("text is required")

    try:
        bot.send_message(
            chat_id=user_id,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except NetworkError as exc:
        if _request_never_left(exc):
            raise
        logger.warning("System message to %s: response lost, assuming delivered (%s)", user_id, exc)
        return {"ok": True, "assumed_delivered": True}

    return {"ok": True}


def start_receiver(bot):
    server = ThreadingHTTPServer((HOST, PORT), _handler_factory(bot))
    thread = Thread(target=server.serve_forever, name="Thread-housing-receiver", daemon=True)
    thread.start()
    logger.info("Housing receiver started at http://%s:%s/housing/immowelt", HOST, PORT)
    return server


def _handler_factory(bot):
    class HousingReceiverHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            logger.info("Housing receiver: " + fmt, *args)

        def _json_response(self, status, payload):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path != "/health":
                self._json_response(404, {"ok": False, "error": "not found"})
                return
            self._json_response(200, {"ok": True})

        def do_POST(self):
            if self.path == "/housing/immowelt":
                handler, label = handle_immowelt_result, "Immowelt notification"
            elif self.path == "/housing/system":
                handler, label = handle_system_message, "system message"
            else:
                self._json_response(404, {"ok": False, "error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(min(length, 1024 * 1024))
                payload = json.loads(raw.decode("utf-8"))
                result = handler(bot, payload)
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                logger.warning("Rejected %s: %s", label, exc)
                self._json_response(400, {"ok": False, "error": str(exc)})
                return
            except Exception as exc:
                logger.exception("Could not process %s", label)
                self._json_response(500, {"ok": False, "error": "internal error"})
                return
            self._json_response(200, result)

    return HousingReceiverHandler
