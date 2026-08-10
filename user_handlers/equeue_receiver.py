"""Tiny local HTTP receiver for browser-based DP Document checks."""

import json
import logging
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

from user_handlers import equeue_monitor


logger = logging.getLogger(__name__)
HOST = os.getenv("EQUEUE_RECEIVER_HOST", "0.0.0.0")
PORT = int(os.getenv("EQUEUE_RECEIVER_PORT", "5011") or 5011)


def start_receiver(bot):
    server = ThreadingHTTPServer((HOST, PORT), _handler_factory(bot))
    thread = Thread(target=server.serve_forever, name="Thread-equeue-receiver", daemon=True)
    thread.start()
    logger.info("E-queue browser receiver started at http://%s:%s/equeue", HOST, PORT)
    return server


def _handler_factory(bot):
    class EqueueReceiverHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            logger.info("E-queue receiver: " + fmt, *args)

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
            if self.path != "/equeue":
                self._json_response(404, {"ok": False, "error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(min(length, 1024 * 1024))
                payload = json.loads(raw.decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("JSON body must be an object")
                result = equeue_monitor.handle_browser_result(bot, payload)
            except Exception as exc:
                logger.exception("Could not process browser e-queue payload")
                self._json_response(400, {"ok": False, "error": str(exc)})
                return
            self._json_response(200 if result.get("ok") else 400, result)

    return EqueueReceiverHandler
