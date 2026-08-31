"""Parser for Kleinanzeigen's Potsdam rental-apartment search page.

Static server-rendered HTML, no CAPTCHA seen on a plain GET — but this is a
large platform (not a small local broker) whose Terms of Service prohibit
automated scraping, so `kleinanzeigen_monitor.py` polls at most once an hour,
far below normal browsing traffic, unlike the other sources' 30-minute cycle.

Kleinanzeigen listings are user-submitted (private renters and small agencies
alike) — the shown price has no reliable Kalt/Warm-miete label the way the
broker sites do, so it is treated as a plain headline price, not folded into
the shared Kaltmiete question the other sources use.
"""

from __future__ import annotations

import html as html_lib
import re
from typing import Any

LISTINGS_URL = "https://www.kleinanzeigen.de/s-wohnung-mieten/potsdam/k0c203"
BASE_URL = "https://www.kleinanzeigen.de"

_CARD_RE = re.compile(r'<article class="aditem" data-adid="(\d+)"')
_LOCATION_RE = re.compile(r'icon-pin-gray"[^>]*></i>\s*([^<\n]+)', re.S)
_TITLE_LINK_RE = re.compile(r'<a class="ellipsis"\s*href="([^"]+)">([^<]*)</a>', re.S)
_TAGS_BLOCK_RE = re.compile(r'aditem-main--middle--tags">(.*?)</p>', re.S)
_PRICE_RE = re.compile(r'aditem-main--middle--price-shipping--price">([^<]*)<')
# Обкладинка з JSON-LD у самій картці пошуку — тут лише одне фото на
# оголошення, повної галереї сторінка пошуку не показує. Детальну сторінку
# заради решти фото свідомо не запитуємо: Kleinanzeigen — велика платформа,
# чиї Умови використання забороняють автоматичний збір, і check_job.py вже й
# так тримає інтервал перевірки помітно рідшим через це; другий запит на
# кожне нове оголошення суперечив би цій обережності.
_IMAGE_RE = re.compile(r'"contentUrl":"([^"]+)"')
_AREA_TAG_RE = re.compile(r"(\d+(?:,\d+)?)\s*m²")
_ROOMS_TAG_RE = re.compile(r"(\d+(?:,\d+)?)\s*Zi\.")
_ADDRESS_RE = re.compile(r"^(\d{4,5})\s+(.+)$")
_TAG_RE = re.compile(r"<[^>]+>")


def clean_text(value: Any) -> str:
    text = _TAG_RE.sub("", str(value or ""))
    return re.sub(r"\s+", " ", html_lib.unescape(text)).strip()


def parse_decimal(value: Any) -> float | None:
    text = clean_text(value)
    if not text:
        return None
    match = re.search(r"\d+(?:[.,]\d{3})*(?:[,.]\d+)?|\d+", text)
    if not match:
        return None
    number = match.group(0)
    if "," in number and "." in number:
        number = number.replace(".", "").replace(",", ".")
    elif "," in number:
        number = number.replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(\.\d{3})+", number):
        # Kleinanzeigen prices show whole euros with a bare thousands dot and
        # no decimal comma at all ("2.445 €") — unlike the broker sites this
        # parser was copied from, which always pair the thousands dot with a
        # decimal comma. A lone dot here is never a decimal point in German
        # formatting, so it's a thousands separator to strip, not keep.
        number = number.replace(".", "")
    try:
        return float(number)
    except ValueError:
        return None


def parse_listings(html: str) -> list[dict[str, Any]]:
    """Extracts every card on the page. Filtering, if any, is the caller's job."""
    listings: list[dict[str, Any]] = []
    matches = list(_CARD_RE.finditer(html))
    for index, match in enumerate(matches):
        listing_id = match.group(1)
        chunk_start = match.end()
        chunk_end = matches[index + 1].start() if index + 1 < len(matches) else len(html)
        chunk = html[chunk_start:chunk_end]

        location_match = _LOCATION_RE.search(chunk)
        address = clean_text(location_match.group(1)) if location_match else ""
        address_match = _ADDRESS_RE.match(address)
        plz, city = address_match.groups() if address_match else ("", address)

        title_match = _TITLE_LINK_RE.search(chunk)
        detail_path = title_match.group(1) if title_match else ""
        title = clean_text(title_match.group(2)) if title_match else ""
        detail_url = BASE_URL + detail_path if detail_path.startswith("/") else detail_path

        tags_match = _TAGS_BLOCK_RE.search(chunk)
        tags_text = clean_text(tags_match.group(1)) if tags_match else ""
        area_match = _AREA_TAG_RE.search(tags_text)
        rooms_match = _ROOMS_TAG_RE.search(tags_text)

        price_match = _PRICE_RE.search(chunk)
        price = parse_decimal(price_match.group(1)) if price_match else None

        image_match = _IMAGE_RE.search(chunk)
        cover_image_url = html_lib.unescape(image_match.group(1)) if image_match else ""

        if not listing_id and not title:
            continue
        listings.append({
            "listing_key": listing_id,
            "title": title or "Wohnung",
            "address": address,
            "city": city.strip(),
            "rooms": parse_decimal(rooms_match.group(1)) if rooms_match else None,
            "area_m2": parse_decimal(area_match.group(1)) if area_match else None,
            "price_eur": price,
            "detail_url": detail_url,
            "cover_image_url": cover_image_url,
        })
    return listings


def _line(label: str, value: Any) -> str | None:
    if value is None or value == "":
        return None
    return f"{label}: {html_lib.escape(str(value))}"


def format_listing_message(listing: dict[str, Any]) -> str:
    lines = ["📋 Нове оголошення Kleinanzeigen", ""]
    for label, key in [
        ("Назва", "title"),
        ("Адреса", "address"),
        ("Кімнати", "rooms"),
        ("Площа м²", "area_m2"),
        ("Ціна EUR", "price_eur"),
    ]:
        line = _line(label, listing.get(key))
        if line:
            lines.append(line)
    link = listing.get("detail_url") or LISTINGS_URL
    lines.extend(["", f"Відкрити: {html_lib.escape(str(link))}"])
    return "\n".join(lines)
