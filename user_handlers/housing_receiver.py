"""Local HTTP receiver for Immowelt notifications from the shared browser service."""

import html
import json
import logging
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.parse import urlparse

from telegram import InlineKeyboardButton, InlineKeyboardMarkup


logger = logging.getLogger(__name__)
HOST = os.getenv("HOUSING_RECEIVER_HOST", "0.0.0.0")
PORT = int(os.getenv("HOUSING_RECEIVER_PORT", "5012") or 5012)


def _text(value):
    return html.escape(str(value or "").strip())


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
    bot.send_message(
        chat_id=user_id,
        text="\n".join(lines),
        parse_mode="HTML",
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )
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
            if self.path != "/housing/immowelt":
                self._json_response(404, {"ok": False, "error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(min(length, 1024 * 1024))
                payload = json.loads(raw.decode("utf-8"))
                result = handle_immowelt_result(bot, payload)
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                logger.warning("Rejected Immowelt notification: %s", exc)
                self._json_response(400, {"ok": False, "error": str(exc)})
                return
            except Exception as exc:
                logger.exception("Could not process Immowelt notification")
                self._json_response(500, {"ok": False, "error": "internal error"})
                return
            self._json_response(200, result)

    return HousingReceiverHandler
