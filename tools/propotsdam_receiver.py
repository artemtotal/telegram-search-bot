"""Host-side ProPotsdam/easysquare collector HTTP service.

Credentials are read from environment variables only:
- PROPOTSDAM_USERNAME
- PROPOTSDAM_PASSWORD
"""

import json
import logging
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock

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
_last_result = {"ok": False, "error": "no scan yet", "listings": []}


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
    for pattern in ["Immobiliensuche", "mehr anzeigen", "^Immobilien$", "Immobilien"]:
        _click_text(page, [pattern], timeout=5000)
    try:
        page.wait_for_load_state("networkidle", timeout=20000)
    except PlaywrightTimeoutError:
        pass
    page.wait_for_timeout(3000)


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
        if self.path == "/health":
            self._json(200, {"ok": True, "last_ok": bool(_last_result.get("ok")), "count": len(_last_result.get("listings") or [])})
        elif self.path == "/api/propotsdam/listings":
            self._json(200, _last_result)
        else:
            self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        global _last_result
        if self.path == "/api/propotsdam/scan":
            try:
                _last_result = scan()
            except Exception as exc:
                logger.exception("scan failed")
                _last_result = {"ok": False, "error": str(exc), "listings": []}
            self._json(200 if _last_result.get("ok") else 500, _last_result)
        else:
            self._json(404, {"ok": False, "error": "not found"})


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    logger.info("ProPotsdam receiver listening on http://%s:%s", HOST, PORT)
    server.serve_forever()


if __name__ == "__main__":
    main()
