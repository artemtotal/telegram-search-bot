"""Open the known ProPotsdam formlist route and capture listing responses."""

import json
import os
from pathlib import Path

from playwright.sync_api import sync_playwright

PROFILE_DIR = Path(os.getenv("PROPOTSDAM_PROFILE_DIR", str(Path.home() / "AppData" / "Local" / "PotsdamBot" / "propotsdam-browser")))
ARTIFACT_DIR = Path(os.getenv("PROPOTSDAM_ARTIFACT_DIR", str(Path.home() / "AppData" / "Local" / "PotsdamBot" / "propotsdam-artifacts")))
URLS = [
    "https://propotsdam-kundenportal.easysquare.com/propotsdam-kundenportal/index.html#/formlist/%252Fsection%252F0%252Fbox%252F0",
    "https://propotsdam-kundenportal.easysquare.com/propotsdam-kundenportal/index.html#/formlist/%2Fsection%2F0%2Fbox%2F0",
]


def main():
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    captured = []
    steps = []
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
            if any(x in url.lower() for x in ["xmlforms", "esq_ia_reobj", "accndocs", "formlist", "realestate"]):
                item = {"status": response.status, "url": url, "content_type": response.headers.get("content-type", "")}
                try:
                    ctype = item["content_type"].lower()
                    if "xml" in ctype or "json" in ctype or "text" in ctype:
                        item["body_sample"] = response.text()[:5000]
                except Exception as exc:
                    item["body_error"] = repr(exc)
                captured.append(item)
        page.on("response", on_response)
        for idx, url in enumerate(URLS):
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(10000)
            text = page.locator("body").inner_text(timeout=5000)
            screenshot = ARTIFACT_DIR / f"propotsdam-route-{idx}.png"
            page.screenshot(path=str(screenshot), full_page=True)
            steps.append({"idx": idx, "url": page.url, "text_sample": text[:5000], "screenshot": str(screenshot)})
        out = ARTIFACT_DIR / "propotsdam-route-recon.json"
        out.write_text(json.dumps({"steps": steps, "captured": captured}, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"artifact": str(out), "captured": len(captured), "last_url": page.url}, ensure_ascii=False, indent=2))
        context.close()

if __name__ == "__main__":
    main()
