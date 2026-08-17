"""Parser for the shared "immomakler" WordPress plugin feed used by both
ImmoTeam Potsdam (immoteam-potsdam.de) and alpha Immobilien (potsdam-immobilien.de).

Both sites run the same plugin on different themes, so the wrapping markup
around each card differs (`property-thumbnail col-sm-12 vertical` vs
`property-details col-sm-7`), but the plugin's own output — the
`<h3 class="property-title">` heading, the `property-subtitle`, the
`property-data` rows, and the `property-status-*` badge — is identical on
both, so the parser anchors on those instead of the theme wrapper.

Confirmed by inspecting real pages: the exact same listing (Objekt-ID
"12863_4") appears on both domains with identical title/rooms/area/price —
the two sites genuinely republish one shared feed, which is why a caller
merging results from both MUST dedupe by Objekt-ID, not just concatenate.
"""

from __future__ import annotations

import html as html_lib
import re
from typing import Any

_TITLE_SPLIT_RE = re.compile(r'<h3 class="property-title">')
_LINK_TITLE_RE = re.compile(r'<a href="([^"]+)">([^<]*)</a>', re.S)
_SUBTITLE_RE = re.compile(r'property-subtitle">\s*([^<]*?)\s*</div>', re.S)
_ROW_RE = re.compile(r'<div class="row[^"]*\b(data-[a-z_]+)"[^>]*role="listitem">.*?<div class="dd[^"]*">([^<]*)</div>', re.S)
_STATUS_RE = re.compile(r'property-status property-status-([a-z-]+)"[^>]*>([^<]*)<')
_ADDRESS_RE = re.compile(r"^(\d{4,5})\s+([^,]+),\s*(.*)$")
_TAG_RE = re.compile(r"<[^>]+>")
_STATUS_WINDOW = 1500
_NOT_VACANT_STATUSES = {"vermietet", "reserviert", "verkauft"}


def clean_text(value: Any) -> str:
    text = _TAG_RE.sub("", str(value or ""))
    return re.sub(r"\s+", " ", html_lib.unescape(text)).strip()


def parse_decimal(value: Any) -> float | None:
    text = clean_text(value)
    if not text or text in {"-", "—", "–"}:
        return None
    match = re.search(r"\d+(?:[.,]\d{3})*(?:[,.]\d+)?|\d+", text)
    if not match:
        return None
    number = match.group(0)
    if "," in number and "." in number:
        number = number.replace(".", "").replace(",", ".")
    elif "," in number:
        number = number.replace(",", ".")
    try:
        return float(number)
    except ValueError:
        return None


def parse_listings(html: str, source: str) -> list[dict[str, Any]]:
    """Extracts every card: rentals and sales, Potsdam and the wider region alike.

    Filtering to rental + Potsdam + vacant is the caller's job, same as the
    other parsers — this stays a straight HTML-to-dict mapping so it can be
    tested against a fixed fixture regardless of what's live today.
    `source` tags each listing with which site it came from ("immoteam" or
    "alpha"), purely for diagnostics — matching/storage key off Objekt-ID.
    """
    listings: list[dict[str, Any]] = []
    title_matches = list(_TITLE_SPLIT_RE.finditer(html))
    for index, match in enumerate(title_matches):
        chunk_start = match.end()
        chunk_end = title_matches[index + 1].start() if index + 1 < len(title_matches) else len(html)
        chunk = html[chunk_start:chunk_end]

        link_match = _LINK_TITLE_RE.search(chunk)
        detail_url = link_match.group(1) if link_match else ""
        title = clean_text(link_match.group(2)) if link_match else ""

        subtitle_match = _SUBTITLE_RE.search(chunk)
        subtitle = clean_text(subtitle_match.group(1)) if subtitle_match else ""
        address_match = _ADDRESS_RE.match(subtitle)
        plz, city, kind = address_match.groups() if address_match else ("", "", "")
        city = city.strip()

        window_start = max(0, match.start() - _STATUS_WINDOW)
        status_matches = list(_STATUS_RE.finditer(html[window_start:match.start()]))
        status = status_matches[-1].group(1) if status_matches else ""

        rows = dict(_ROW_RE.findall(chunk))
        listing_key = clean_text(rows.get("data-objektnr_extern", ""))
        if not listing_key and not title:
            continue

        listings.append({
            "listing_key": listing_key or detail_url or title,
            "title": title or "Wohnung",
            "address": subtitle,
            "city": city,
            "kind": kind.strip(),
            "rooms": parse_decimal(rows.get("data-anzahl_zimmer")),
            "area_m2": parse_decimal(rows.get("data-wohnflaeche") or rows.get("data-nutzflaeche")),
            "price_eur": parse_decimal(rows.get("data-kaltmiete")),
            "is_rental": "data-kaltmiete" in rows,
            "status": status,
            "is_vacant": status.casefold() not in _NOT_VACANT_STATUSES,
            "detail_url": detail_url,
            "source": source,
        })
    return listings


def _line(label: str, value: Any) -> str | None:
    if value is None or value == "":
        return None
    return f"{label}: {html_lib.escape(str(value))}"


def format_listing_message(listing: dict[str, Any]) -> str:
    lines = ["🤝 Нова квартира ImmoTeam/alpha", ""]
    for label, key in [
        ("Назва", "title"),
        ("Адреса", "address"),
        ("Кімнати", "rooms"),
        ("Площа м²", "area_m2"),
        ("Kaltmiete EUR", "price_eur"),
    ]:
        line = _line(label, listing.get(key))
        if line:
            lines.append(line)
    link = listing.get("detail_url")
    if link:
        lines.extend(["", f"Відкрити: {html_lib.escape(str(link))}"])
    return "\n".join(lines)
