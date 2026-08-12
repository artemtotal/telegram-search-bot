"""Manual reconnaissance helper for the ProPotsdam/easysquare portal.

Keeps credentials outside source code. Set PROPOTSDAM_USERNAME and
PROPOTSDAM_PASSWORD in the environment before running.
"""

import json
import os
import re
from pathlib import Path
from urllib.parse import urljoin

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

PORTAL_URL = os.getenv(
    "PROPOTSDAM_URL",
    "https://propotsdam-kundenportal.easysquare.com/propotsdam-kundenportal/index.html#/formlist/%252Fsection%252F0%252Fbox%252F0",
)
PROFILE_DIR = Path(os.getenv(
    "PROPOTSDAM_PROFILE_DIR",
    str(Path.home() / "AppData" / "Local" / "PotsdamBot" / "propotsdam-browser"),
))
ARTIFACT_DIR = Path(os.getenv(
    "PROPOTSDAM_ARTIFACT_DIR",
    str(Path.home() / "AppData" / "Local" / "PotsdamBot" / "propotsdam-artifacts"),
))


def _redact(value: str) -> str:
    if not value:
        return ""
    if "@" in value:
        left, right = value.split("@", 1)
        return f"{left[:2]}***@{right}"
    return "***"


def _click_text(page, patterns, timeout=5000):
    for pattern in patterns:
        try:
            loc = page.get_by_text(re.compile(pattern, re.I)).first
            loc.click(timeout=timeout)
            return pattern
        except Exception:
            pass
    return None


def _fill_login(page, username: str, password: str) -> bool:
    try:
        email = page.locator("input[type='email'], input[name*='mail' i], input[name*='user' i], input[type='text']").first
        email.fill(username, timeout=5000)
        pwd = page.locator("input[type='password']").first
        pwd.fill(password, timeout=5000)
        clicked = _click_text(page, ["anmelden", "login", "einloggen"], timeout=3000)
        if not clicked:
            pwd.press("Enter")
        page.wait_for_load_state("networkidle", timeout=20000)
        return True
    except Exception:
        return False


def _navigate_to_list(page):
    _click_text(page, ["Immobiliensuche"], timeout=7000)
    page.wait_for_timeout(1000)
    _click_text(page, ["mehr anzeigen", "mehr"], timeout=5000)
    page.wait_for_timeout(1000)
    _click_text(page, ["^Immobilien$"], timeout=5000)
    try:
        page.wait_for_load_state("networkidle", timeout=20000)
    except PlaywrightTimeoutError:
        pass


def _extract_visible_cards(page):
    return page.evaluate(
        """
        () => {
          const textOf = (node) => (node?.innerText || '').replace(/\s+/g, ' ').trim();
          const nodes = [...document.querySelectorAll('mat-card, .mat-card, [class*=card], [class*=list-item], [class*=object], [class*=immobil]')];
          const cards = nodes.map((node) => ({
            tag: node.tagName,
            className: node.className || '',
            text: textOf(node),
            links: [...node.querySelectorAll('a[href]')].map(a => a.href),
            images: [...node.querySelectorAll('img[src]')].map(img => img.src),
          })).filter(x => x.text && /(Zimmer|Wohnfläche|Gesamt|Stadtteil|Verfügbar|m²|EUR)/i.test(x.text));
          return cards.slice(0, 30);
        }
        """
    )


def main():
    username = os.getenv("PROPOTSDAM_USERNAME", "")
    password = os.getenv("PROPOTSDAM_PASSWORD", "")
    if not username or not password:
        raise SystemExit("Set PROPOTSDAM_USERNAME and PROPOTSDAM_PASSWORD before running")

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    responses = []

    with sync_playwright() as p:
        executable_path = os.getenv("PROPOTSDAM_BROWSER") or r"C:\Program Files\Google\Chrome\Application\chrome.exe"
        context = p.chromium.launch_persistent_context(
            str(PROFILE_DIR),
            headless=False,
            viewport={"width": 1440, "height": 1000},
            executable_path=executable_path if Path(executable_path).exists() else None,
        )
        page = context.pages[0] if context.pages else context.new_page()

        def on_response(response):
            url = response.url
            ctype = response.headers.get("content-type", "")
            if any(token in url.lower() for token in ["immobil", "estate", "object", "form", "easy"]):
                responses.append({"status": response.status, "url": url, "content_type": ctype})

        page.on("response", on_response)
        page.goto(PORTAL_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_load_state("networkidle", timeout=30000)

        if page.locator("input[type='password']").count() or page.get_by_text(re.compile("anmelden", re.I)).count():
            _click_text(page, ["anmelden"], timeout=3000)
            _fill_login(page, username, password)

        error = None
        try:
            _navigate_to_list(page)
            page.wait_for_timeout(3000)
        except Exception as exc:
            error = repr(exc)
        cards = _extract_visible_cards(page)
        screenshot = ARTIFACT_DIR / "propotsdam-recon.png"
        page.screenshot(path=str(screenshot), full_page=True)
        html_path = ARTIFACT_DIR / "propotsdam-recon.html"
        html_path.write_text(page.content(), encoding="utf-8")
        payload = {
            "ok": error is None,
            "error": error,
            "url": page.url,
            "title": page.title(),
            "username": _redact(username),
            "cards_count": len(cards),
            "visible_text_sample": page.locator("body").inner_text(timeout=5000)[:5000],
            "cards": cards,
            "interesting_responses": responses[-80:],
            "screenshot": str(screenshot),
            "html": str(html_path),
        }
        out = ARTIFACT_DIR / "propotsdam-recon.json"
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"ok": error is None, "error": error, "cards_count": len(cards), "url": page.url, "artifact": str(out), "screenshot": str(screenshot)}, ensure_ascii=False, indent=2))
        context.close()


if __name__ == "__main__":
    main()
