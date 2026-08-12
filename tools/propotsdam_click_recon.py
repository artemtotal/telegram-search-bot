"""Click all visible Immobilien candidates and capture ProPotsdam network responses."""

import json
import os
import re
from pathlib import Path

from playwright.sync_api import sync_playwright

PROFILE_DIR = Path(os.getenv("PROPOTSDAM_PROFILE_DIR", str(Path.home() / "AppData" / "Local" / "PotsdamBot" / "propotsdam-browser")))
ARTIFACT_DIR = Path(os.getenv("PROPOTSDAM_ARTIFACT_DIR", str(Path.home() / "AppData" / "Local" / "PotsdamBot" / "propotsdam-artifacts")))
URL = "https://propotsdam-kundenportal.easysquare.com/propotsdam-kundenportal/index.html#"


def main():
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    captured = []
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            str(PROFILE_DIR),
            headless=False,
            viewport={"width": 1440, "height": 1000},
            executable_path=r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        )
        page = context.pages[0] if context.pages else context.new_page()

        def on_response(response):
            url = response.url
            if "xmlforms" in url or "ESQ_IA_REOBJ" in url or "immobil" in url.lower():
                item = {"status": response.status, "url": url, "content_type": response.headers.get("content-type", "")}
                try:
                    txt = response.text(timeout=5000)
                    item["body_sample"] = txt[:4000]
                except Exception as exc:
                    item["body_error"] = repr(exc)
                captured.append(item)
        page.on("response", on_response)
        page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)
        candidates = page.locator("text=/^Immobilien$/")
        count = candidates.count()
        steps = []
        for i in range(count):
            try:
                candidates.nth(i).click(timeout=5000)
                page.wait_for_timeout(5000)
                cards_text = page.locator("body").inner_text(timeout=5000)
                steps.append({"index": i, "url": page.url, "text_sample": cards_text[:2000]})
                page.screenshot(path=str(ARTIFACT_DIR / f"propotsdam-click-{i}.png"), full_page=True)
            except Exception as exc:
                steps.append({"index": i, "error": repr(exc), "url": page.url})
        out = ARTIFACT_DIR / "propotsdam-click-recon.json"
        out.write_text(json.dumps({"count": count, "steps": steps, "captured": captured}, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"count": count, "artifact": str(out), "captured": len(captured)}, ensure_ascii=False, indent=2))
        context.close()


if __name__ == "__main__":
    main()
