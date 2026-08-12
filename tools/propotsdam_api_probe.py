"""Probe authenticated ProPotsdam API responses and save XML bodies."""

import json
import os
import re
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

PORTAL_URL = "https://propotsdam-kundenportal.easysquare.com/propotsdam-kundenportal/index.html#/formlist/%252Fsection%252F0%252Fbox%252F0"
PROFILE_DIR = Path(os.getenv("PROPOTSDAM_PROFILE_DIR", str(Path.home() / "AppData" / "Local" / "PotsdamBot" / "propotsdam-browser")))
ARTIFACT_DIR = Path(os.getenv("PROPOTSDAM_ARTIFACT_DIR", str(Path.home() / "AppData" / "Local" / "PotsdamBot" / "propotsdam-artifacts")))
BROWSER = os.getenv("PROPOTSDAM_BROWSER", r"C:\Program Files\Google\Chrome\Application\chrome.exe")


def click_text(page, pattern, timeout=5000):
    try:
        page.get_by_text(re.compile(pattern, re.I)).first.click(timeout=timeout)
        page.wait_for_timeout(1500)
        return True
    except Exception as exc:
        return False


def main():
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    captured = []
    bodies = []
    with sync_playwright() as p:
        kwargs = {"headless": False, "viewport": {"width": 1440, "height": 1000}}
        if Path(BROWSER).exists():
            kwargs["executable_path"] = BROWSER
        context = p.chromium.launch_persistent_context(str(PROFILE_DIR), **kwargs)
        page = context.pages[0] if context.pages else context.new_page()

        def on_response(response):
            url = response.url
            if "xmlforms" not in url and "api5/services" not in url:
                return
            rec = {"status": response.status, "url": url, "content_type": response.headers.get("content-type", "")}
            try:
                text = response.text()
                idx = len(bodies)
                path = ARTIFACT_DIR / f"propotsdam-api-body-{idx}.txt"
                path.write_text(text, encoding="utf-8", errors="ignore")
                rec["body_path"] = str(path)
                rec["body_sample"] = text[:1000]
                bodies.append(str(path))
            except Exception as exc:
                rec["body_error"] = repr(exc)
            captured.append(rec)

        page.on("response", on_response)
        page.goto(PORTAL_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(5000)
        for pattern in ["Immobiliensuche", "mehr anzeigen", "^Immobilien$", "Immobilien"]:
            click_text(page, pattern)
        try:
            page.wait_for_load_state("networkidle", timeout=20000)
        except PlaywrightTimeoutError:
            pass
        page.wait_for_timeout(5000)
        page.screenshot(path=str(ARTIFACT_DIR / "propotsdam-api-probe.png"), full_page=True)
        payload = {
            "url": page.url,
            "text": page.locator("body").inner_text(timeout=5000)[:5000],
            "captured": captured,
            "bodies": bodies,
        }
        out = ARTIFACT_DIR / "propotsdam-api-probe.json"
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"out": str(out), "captured": len(captured), "bodies": bodies, "url": page.url}, ensure_ascii=False, indent=2))
        context.close()


if __name__ == "__main__":
    main()
