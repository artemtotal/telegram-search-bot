"""Дата й час перегляду простим вводом: «завтра 15:00», «28.11 10:30»,
«morgen 9:15», «послезавтра в 18:00». Повертає момент у UTC (naive) або None."""

import re
from datetime import datetime, timedelta
from typing import Optional

import pytz

BERLIN = pytz.timezone("Europe/Berlin")

_DAY_WORDS = {
    0: ("сьогодні", "сегодня", "heute", "today"),
    1: ("завтра", "morgen", "tomorrow"),
    2: ("післязавтра", "послезавтра", "übermorgen", "uebermorgen"),
}
_TIME_RE = re.compile(r"(?<!\d)([01]?\d|2[0-3]):([0-5]\d)(?!\d)")
_DATE_RE = re.compile(r"(?<![\d:])(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?\.?(?![\d:])")


def parse_viewing(text: str, now_utc: Optional[datetime] = None) -> Optional[datetime]:
    """None - не зрозуміло, або час без дати/дата без часу, або вже минуло."""
    raw = str(text or "").strip().lower()
    if not raw or len(raw) > 80:
        return None
    now_utc = now_utc or datetime.utcnow()
    now_local = pytz.utc.localize(now_utc).astimezone(BERLIN)

    time_match = _TIME_RE.search(raw)
    if not time_match:
        return None
    hour, minute = int(time_match.group(1)), int(time_match.group(2))
    rest = raw[:time_match.start()] + " " + raw[time_match.end():]

    day = None
    for offset in (2, 1, 0):  # «післязавтра» містить «завтра»: спершу довше
        if any(word in rest for word in _DAY_WORDS[offset]):
            day = (now_local + timedelta(days=offset)).date()
            break
    if day is None:
        date_match = _DATE_RE.search(rest)
        if date_match:
            d, m = int(date_match.group(1)), int(date_match.group(2))
            y_raw = date_match.group(3)
            try:
                if y_raw:
                    y = int(y_raw) + (2000 if len(y_raw) <= 2 else 0)
                    day = datetime(y, m, d).date()
                else:
                    day = datetime(now_local.year, m, d).date()
                    if day < now_local.date() - timedelta(days=1):
                        day = datetime(now_local.year + 1, m, d).date()
            except ValueError:
                return None
    if day is None:
        return None
    try:
        local = BERLIN.localize(datetime(day.year, day.month, day.day, hour, minute), is_dst=None)
    except Exception:
        # неіснуючий/неоднозначний час переходу на літній: беремо як є
        local = BERLIN.localize(datetime(day.year, day.month, day.day, hour, minute))
    moment = local.astimezone(pytz.utc).replace(tzinfo=None)
    return moment if moment > now_utc else None
