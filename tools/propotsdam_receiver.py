"""Host-side ProPotsdam/easysquare collector HTTP service.

Credentials are read from environment variables only:
- PROPOTSDAM_USERNAME
- PROPOTSDAM_PASSWORD

Run this with its own interpreter, never a bare ``python`` off PATH. It used to
resolve to Hermes' virtualenv, which left this service — and the Playwright
driver it spawns out of that venv's site-packages — holding files inside it, and
``hermes update`` aborts while anything locks them. The dedicated venv lives
outside this repository on purpose: the repo doubles as a Docker build context
with no .dockerignore, so a local .venv would be copied into the bot image.

    uv venv C:\\opt\\propotsdam-receiver\\.venv --python 3.11
    uv pip install --python C:\\opt\\propotsdam-receiver\\.venv\\Scripts\\python.exe "playwright>=1.54,<2"

Playwright's bundled browsers are not needed: ``scan`` drives the system Chrome
through ``PROPOTSDAM_BROWSER``.
"""

import json
import logging
import os
import re
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from user_jobs import propotsdam_parser

logger = logging.getLogger("propotsdam_receiver")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

HOST = os.getenv("PROPOTSDAM_RECEIVER_HOST", "127.0.0.1")
PORT = int(os.getenv("PROPOTSDAM_RECEIVER_PORT", "18766") or 18766)
PORTAL_URL = propotsdam_parser.PORTAL_URL
PROFILE_DIR = Path(os.getenv("PROPOTSDAM_PROFILE_DIR", str(Path.home() / "AppData" / "Local" / "PotsdamBot" / "propotsdam-browser")))
BROWSER = os.getenv("PROPOTSDAM_BROWSER", r"C:\Program Files\Google\Chrome\Application\chrome.exe")
_scan_lock = Lock()
_state_lock = Lock()
_last_result = {"ok": False, "error": "no scan yet", "listings": []}
_scan_running = False
_scan_started_at = None


def _click_text(page, patterns, timeout=5000):
    for pattern in patterns:
        try:
            page.get_by_text(re.compile(pattern, re.I)).first.click(timeout=timeout)
            page.wait_for_timeout(1200)
            return True
        except Exception:
            pass
    return False


def _login_if_needed(page):
    username = os.getenv("PROPOTSDAM_USERNAME", "")
    password = os.getenv("PROPOTSDAM_PASSWORD", "")
    if not username or not password:
        raise RuntimeError("PROPOTSDAM_USERNAME/PROPOTSDAM_PASSWORD are not set")
    if not (page.locator("input[type='password']").count() or page.get_by_text(re.compile("anmelden", re.I)).count()):
        return
    _click_text(page, ["anmelden"], timeout=3000)
    email = page.locator("input[type='email'], input[name*='mail' i], input[name*='user' i], input[type='text']").first
    email.fill(username, timeout=7000)
    page.locator("input[type='password']").first.fill(password, timeout=7000)
    if not _click_text(page, ["anmelden", "login", "einloggen"], timeout=3000):
        page.locator("input[type='password']").first.press("Enter")
    try:
        page.wait_for_load_state("networkidle", timeout=20000)
    except PlaywrightTimeoutError:
        pass


def _navigate_to_list(page):
    page.goto(PORTAL_URL, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(3000)
    _login_if_needed(page)
    for pattern in ["Immobiliensuche", "^Immobilien$", "Immobilien"]:
        _click_text(page, [pattern], timeout=5000)
    try:
        page.wait_for_load_state("networkidle", timeout=20000)
    except PlaywrightTimeoutError:
        pass
    page.wait_for_timeout(3000)
    _load_all_pages(page)


def _load_all_pages(page, max_clicks=50):
    """The listing view paginates behind a "mehr anzeigen" (show more)
    button instead of showing everything up front. A single click - which
    is all this used to do, as part of _navigate_to_list's one-shot pattern
    list - only ever loaded the first page, so listings on later pages
    (including the last one) were silently never scanned. Keeps clicking
    until the button is gone (or `max_clicks` as a safety cap against an
    unexpected always-present button) so every page's xmlforms response
    gets captured by `scan`'s response listener.
    """
    for _ in range(max_clicks):
        if not _click_text(page, ["mehr anzeigen"], timeout=3000):
            return
        try:
            page.wait_for_load_state("networkidle", timeout=20000)
        except PlaywrightTimeoutError:
            pass


def _extract_cards_from_dom(page):
    raw_cards = page.evaluate(
        """
        () => {
          const textOf = (node) => (node?.innerText || '').replace(/\s+/g, ' ').trim();
          const nodes = [...document.querySelectorAll('mat-card, .mat-card, [class*=card], [class*=list-item], [class*=object], [class*=immobil], li, article')];
          return nodes.map((node) => ({
            text: textOf(node),
            links: [...node.querySelectorAll('a[href]')].map(a => a.href),
            images: [...node.querySelectorAll('img[src]')].map(img => img.src),
          })).filter(x => x.text && /(Zimmer|Wohnfläche|Gesamt|Stadtteil|Verfügbar|m²|EUR)/i.test(x.text));
        }
        """
    )
    listings = []
    for card in raw_cards:
        text = card.get("text") or ""
        extra = {"raw_text": text}
        def after(label):
            match = re.search(label + r"\s*:?\s*([^|\n]+?)(?=\s+(?:Stadtteil|Zimmer|Wohnfläche|Gesamt|Verfügbar)|$)", text, re.I)
            return match.group(1).strip() if match else ""
        listings.append(propotsdam_parser.normalize_listing({
            "title": text.split(" Stadtteil ")[0].strip()[:160],
            "address": after("Adresse"),
            "district": after("Stadtteil"),
            "rooms": after("Zimmer"),
            "area": after("Wohnfläche"),
            "total_rent": after("Gesamtmiete|Gesamtmi(?:ete)?"),
            "available_from": after("Verfügbar(?: ab)?"),
            "detail_url": (card.get("links") or [""])[0],
            "image_url": (card.get("images") or [""])[0],
            "extra": extra,
        }))
    return listings


def scan():
    with _scan_lock:
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        xml_bodies = []
        responses = []
        with sync_playwright() as p:
            kwargs = {"headless": False, "viewport": {"width": 1440, "height": 1000}}
            if Path(BROWSER).exists():
                kwargs["executable_path"] = BROWSER
            context = p.chromium.launch_persistent_context(str(PROFILE_DIR), **kwargs)
            page = context.pages[0] if context.pages else context.new_page()

            def on_response(response):
                url = response.url
                if "xmlforms" not in url:
                    return
                responses.append(url)
                try:
                    xml_bodies.append(response.text())
                except Exception as exc:
                    logger.warning("Could not read xmlforms body from %s: %s", url, exc)

            page.on("response", on_response)
            _navigate_to_list(page)
            listings = []
            for xml_text in xml_bodies:
                listings.extend(propotsdam_parser.parse_boxlist_xml(xml_text))
            if not listings:
                listings = _extract_cards_from_dom(page)
            result = {"ok": True, "url": page.url, "listings": listings, "count": len(listings), "xmlforms": responses[-20:]}
            context.close()
            return result


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _store_result(result):
    global _last_result
    result["finished_at"] = _now_iso()
    with _state_lock:
        _last_result = result
    return result


def _scan_to_result():
    try:
        return scan()
    except Exception as exc:
        logger.exception("scan failed")
        return {"ok": False, "error": str(exc), "listings": []}


def _background_scan():
    global _scan_running
    try:
        _store_result(_scan_to_result())
    finally:
        with _state_lock:
            _scan_running = False


def _start_scan():
    """Kick off a scan in the background. False when one is already running.

    The scan drives a real browser: it usually finishes in about a minute, but a
    slow portal or a long "mehr anzeigen" pagination can push it past any timeout
    the caller is willing to wait. Blocking the caller for the whole scan meant a
    single slow run was reported as a failure — and, because `scan()` serialises
    on `_scan_lock`, the next 15-minute tick queued behind it and failed too.
    """
    global _scan_running, _scan_started_at
    with _state_lock:
        if _scan_running:
            return False
        _scan_running = True
        _scan_started_at = _now_iso()
    threading.Thread(target=_background_scan, name="propotsdam-scan", daemon=True).start()
    return True


def _snapshot():
    with _state_lock:
        payload = dict(_last_result)
        payload["running"] = _scan_running
        payload["started_at"] = _scan_started_at
    return payload


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        logger.info(fmt, *args)

    def _json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/health":
            snapshot = _snapshot()
            self._json(200, {
                "ok": True,
                "last_ok": bool(snapshot.get("ok")),
                "count": len(snapshot.get("listings") or []),
                "running": snapshot.get("running"),
                "finished_at": snapshot.get("finished_at"),
            })
        elif path == "/api/propotsdam/listings":
            self._json(200, _snapshot())
        else:
            self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        parts = urlsplit(self.path)
        if parts.path == "/api/propotsdam/scan":
            # ?wait=1 keeps the original blocking behaviour for manual runs.
            if parse_qs(parts.query).get("wait", ["0"])[0] == "1":
                result = _store_result(_scan_to_result())
                self._json(200 if result.get("ok") else 500, result)
                return
            started = _start_scan()
            snapshot = _snapshot()
            self._json(202, {
                "ok": True,
                "started": started,
                "running": True,
                "last_finished_at": snapshot.get("finished_at"),
                "last_ok": bool(snapshot.get("ok")),
                "last_count": len(snapshot.get("listings") or []),
            })
        else:
            self._json(404, {"ok": False, "error": "not found"})


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    logger.info("ProPotsdam receiver listening on http://%s:%s", HOST, PORT)
    server.serve_forever()


if __name__ == "__main__":
    main()
