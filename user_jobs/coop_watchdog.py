"""Watchdog for housing cooperatives that currently have zero scrapeable listings.

Gewoba eG Babelsberg, WBG 1903 Potsdam, and WBG "Daheim" eG each show a static
"no vacancies" line on their offers page even without JS — confirmed live, not
guessed. Building a full parser (matching listings against rooms/price/area
criteria) for a source with nothing to parse would be wasted work — instead
this just re-checks that exact line on a schedule and pings the admin the
moment it disappears. That disappearance is the actual signal that a real
per-listing scraper is worth building for that source.

People can subscribe via housing_monitor's "🏘 Кооперативи" screen
(CoopWatchdogFilter / coop_watchdog_store) and see it as a green/yellow/red
light in their own status screen, same as every other source — but this
job deliberately does NOT push a "check manually" ping to them the way it
does to the admin (2026-08-21: built, then explicitly turned back off before
shipping — a subscriber should just see "still green, nothing yet" until
there's a real filtered listing to send, not an unfiltered "go look
yourself" message on day one).
"""

import html
import logging
import os
import re
from datetime import datetime
from typing import Dict

import requests

from database import CoopWatchdogStatus, DBSession

logger = logging.getLogger(__name__)

CHECK_ENABLED = os.getenv("COOP_WATCHDOG_CHECK_ENABLED", "1") == "1"
TIMEOUT = int(os.getenv("COOP_WATCHDOG_TIMEOUT", "30") or 30)
CHECK_INTERVAL_SECONDS = 30 * 60
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
# WBG 1903 briefly answered a self-identifying bot User-Agent with a 403 in
# production (retried moments later and succeeded) — switched to a realistic
# browser UA + headers to lower the odds of that recurring, rather than just
# hoping the flake stays rare.
_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
}

COOPERATIVES = [
    {
        "key": "gewoba",
        "label": "Gewoba eG Babelsberg",
        "url": "https://www.gewoba-eg-babelsberg.de/seite/351561/wohnungsangebote.html",
        "empty_marker": re.compile(r"derzeit\s+keine\s+freien\s+wohnungen\s+anbieten", re.I),
    },
    {
        "key": "wbg1903",
        "label": "WBG 1903 Potsdam",
        "url": "https://www.1903.de/wohnungsangebote/",
        "empty_marker": re.compile(r"derzeit\s+haben\s+wir\s+leider\s+keine\s+freien\s+objekte", re.I),
    },
    {
        "key": "wbg_daheim",
        "label": "WBG „Daheim\" eG",
        "url": "https://www.wbgdaheim.de/angebote/",
        "empty_marker": re.compile(r"zur\s+zeit\s+k[oö]nnen\s+wir\s+ihnen\s+leider\s+keine\s+freien\s+wohnungen\s+anbieten", re.I),
    },
]


def _fetch(url: str) -> str:
    response = requests.get(url, timeout=TIMEOUT, headers=_HEADERS)
    response.raise_for_status()
    return response.text


def _notify_admin_vacancy_appeared(bot, coop: Dict[str, str]) -> None:
    text = (
        f"🏘 <b>{html.escape(coop['label'])}: можливо, з'явилось вільне житло!</b>\n\n"
        "Текст «немає вільного житла» більше не знайдено на сторінці — варто "
        "перевірити вручну і, якщо підтвердиться, додати повноцінний парсер.\n\n"
        f"{html.escape(coop['url'])}"
    )
    try:
        bot.send_message(chat_id=ADMIN_ID, text=text, parse_mode="HTML", disable_web_page_preview=False)
    except Exception:
        logger.exception("Could not notify admin about %s vacancy watchdog", coop["key"])


def check_job(context) -> Dict[str, int]:
    if not CHECK_ENABLED:
        return {"ok": 1, "enabled": 0, "alerts": 0}
    bot = context.bot
    alerts = 0
    session = DBSession()
    try:
        for coop in COOPERATIVES:
            row = session.query(CoopWatchdogStatus).filter(CoopWatchdogStatus.key == coop["key"]).first()
            if row is None:
                row = CoopWatchdogStatus(key=coop["key"])
                session.add(row)
            row.last_checked_at = datetime.utcnow()
            try:
                text = _fetch(coop["url"])
            except Exception as exc:
                row.last_status = "error"
                row.last_error = str(exc)[:500]
                logger.warning("Coop watchdog fetch failed for %s: %s", coop["key"], exc)
                continue
            still_empty = bool(coop["empty_marker"].search(text))
            row.last_status = "ok"
            row.last_error = ""
            # Only the empty->not-empty edge triggers an alert — `was_empty is
            # True` (not just truthy) so a brand-new row (was_empty=None,
            # unknown prior state) baselines silently on its first check.
            if row.was_empty is True and not still_empty and ADMIN_ID:
                _notify_admin_vacancy_appeared(bot, coop)
                alerts += 1
            row.was_empty = still_empty
    finally:
        session.commit()
        session.close()
    return {"ok": 1, "enabled": 1, "alerts": alerts}
