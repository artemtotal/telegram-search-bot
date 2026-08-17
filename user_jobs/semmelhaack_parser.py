"""Parser for SEMMELHAACK's static rental listings page (semmelhaack.de/mietangebote/).

No login and no JavaScript rendering needed — every listing already sits in the
initial HTML as a `<div class="objekt-single">` card with labelled rows
(Adresse/Wohnfläche or Nutzfläche/Zimmer or Räume/Kaltmiete). The page also embeds a
`ddata = [...]` JSON blob for the map widget, but its `gesamtmiete` field does not
match the rendered `Kaltmiete` value shown to visitors — the rendered cards are the
only trustworthy source for price.
"""

from __future__ import annotations

import html as html_lib
import re
from typing import Any

LISTINGS_URL = "https://semmelhaack.de/mietangebote/"
BASE_URL = "https://semmelhaack.de"

_CARD_SPLIT_RE = re.compile(r'<div class="objekt-single">')
_TITLE_RE = re.compile(r"<h3>(.*?)</h3>", re.S)
_ROW_VALUE_RE = re.compile(r'"label">\s*([^<]+?):\s*</span>.*?"value">(.*?)</span>', re.S)
_DETAIL_LINK_RE = re.compile(r'<a href="([^"]+)"[^>]*class="[^"]*zur-objektbeschreibung')
_IMAGE_RE = re.compile(r'data-src="([^"]+)"')
_ADDRESS_RE = re.compile(r"^(.*?),\s*(\d{4,5})\s+(.+)$")
_TAG_RE = re.compile(r"<[^>]+>")


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


def _split_address(raw: str) -> tuple[str, str, str]:
    match = _ADDRESS_RE.match(raw)
    if not match:
        return raw, "", ""
    street, plz, city = match.groups()
    return street.strip(), plz.strip(), city.strip()


def _listing_id_from_url(url: str) -> str:
    match = re.search(r"/(\d+)/?$", url)
    return match.group(1) if match else ""


def parse_listings(html: str) -> list[dict[str, Any]]:
    """Extracts every `objekt-single` card from the page, residential and commercial alike.

    Filtering to Potsdam (or to residential-only, if ever needed) is the caller's
    job — the parser stays a straight HTML-to-dict mapping so it can be tested
    against a fixed fixture without depending on today's live inventory.
    """
    listings: list[dict[str, Any]] = []
    chunks = _CARD_SPLIT_RE.split(html)[1:]
    for chunk in chunks:
        title_match = _TITLE_RE.search(chunk)
        title = clean_text(title_match.group(1)) if title_match else ""
        rows = {clean_text(label).casefold(): value for label, value in _ROW_VALUE_RE.findall(chunk)}
        address_raw = clean_text(rows.get("adresse"))
        street, plz, city = _split_address(address_raw)
        area = rows.get("wohnfläche") or rows.get("nutzfläche")
        rooms = rows.get("zimmer") or rows.get("räume")
        price = rows.get("kaltmiete")
        link_match = _DETAIL_LINK_RE.search(chunk)
        detail_url = BASE_URL + link_match.group(1) if link_match else ""
        image_match = _IMAGE_RE.search(chunk)
        image_url = image_match.group(1) if image_match else ""
        listing_id = _listing_id_from_url(detail_url)
        if not title and not listing_id:
            continue
        listings.append({
            "listing_key": listing_id or detail_url or title,
            "title": title or "SEMMELHAACK Wohnung",
            "address": address_raw,
            "street": street,
            "plz": plz,
            "city": city,
            "rooms": parse_decimal(rooms),
            "area_m2": parse_decimal(area),
            "price_eur": parse_decimal(price),
            "detail_url": detail_url,
            "image_url": image_url,
        })
    return listings


def _line(label: str, value: Any) -> str | None:
    if value is None or value == "":
        return None
    return f"{label}: {html_lib.escape(str(value))}"


def format_listing_message(listing: dict[str, Any]) -> str:
    lines = ["🏘 Нова квартира SEMMELHAACK", ""]
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
    link = listing.get("detail_url") or LISTINGS_URL
    lines.extend(["", f"Відкрити: {html_lib.escape(str(link))}"])
    return "\n".join(lines)
