"""Parser for locals®'s static "Wohnung mieten in Potsdam" landing page.

locals.de's real search form (/immobilie-finden) is CAPTCHA-gated — submitting
it programmatically hits a "Sicherheitsabfrage" text-code challenge, so that
path was ruled out. This landing page (locals.de/wohnung-mieten-potsdam) is a
plain server-rendered page with no JS and no CAPTCHA: it lists every current
Potsdam rental with rooms/area/Kaltmiete already inline, which is everything
this bot needs — no headless browser required at all.
"""

from __future__ import annotations

import html as html_lib
import re
from typing import Any

LISTINGS_URL = "https://locals.de/wohnung-mieten-potsdam"
BASE_URL = "https://locals.de"

_CARD_SPLIT_RE = re.compile(r'<div class="item--wrapper"[^>]*>')
_HREF_RE = re.compile(r'href="(/immobilien/[^"]+)"')
_ARIA_LABEL_RE = re.compile(r'aria-label="([^"]*)"')
_TAGLINE_RE = re.compile(r'<p class="h6 fw500 tagline">([^<]*)</p>')
_FIELD_RE = re.compile(r'>{label}</p>\s*<h3[^>]*>([^<]*)</h3>')
_TAG_RE = re.compile(r"<[^>]+>")
# Кожне фото галереї — це посилання glightbox, підтверджено на двох різних
# оголошеннях, що воно завжди веде на власне фото цього самого оголошення, а
# не сусіднього — id "data-gallery" у розмітці спільний для всіх оголошень
# сайту (це просто ім'я групи для JS-плагіна), а не унікальний на оголошення.
_GALLERY_IMAGE_RE = re.compile(r'href="(https://live-files\.ynfinite\.de/[^"]+)"\s+class="glightbox"')
_COVER_IMAGE_RE = re.compile(r'class="yn-image[^"]*"[^>]*\ssrc="([^"?]+)')


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
        # Kaltmiete is shown as whole euros with a bare thousands dot and no
        # decimal comma ("2.180 €") — never a decimal point in German
        # formatting, so it's a thousands separator to strip, not keep.
        number = number.replace(".", "")
    try:
        return float(number)
    except ValueError:
        return None


def _field(chunk: str, label: str) -> str:
    match = re.search(_FIELD_RE.pattern.format(label=re.escape(label)), chunk)
    return match.group(1) if match else ""


def _listing_key_from_url(url: str) -> str:
    match = re.search(r"/immobilien/([^/?#]+)", url)
    return match.group(1) if match else url


def parse_listings(html: str) -> list[dict[str, Any]]:
    """Extracts every listing card on the Potsdam rentals landing page."""
    listings: list[dict[str, Any]] = []
    chunks = _CARD_SPLIT_RE.split(html)[1:]
    for chunk in chunks:
        href_match = _HREF_RE.search(chunk)
        detail_url = BASE_URL + href_match.group(1) if href_match else ""
        aria_match = _ARIA_LABEL_RE.search(chunk)
        title = clean_text(aria_match.group(1)) if aria_match else ""
        tagline_match = _TAGLINE_RE.search(chunk)
        tagline = clean_text(tagline_match.group(1)) if tagline_match else ""
        plz_match = re.match(r"(\d{4,5})\s+(.+?)\s*-", tagline)
        plz, city = plz_match.groups() if plz_match else ("", tagline)

        rooms = _field(chunk, "Zimmer")
        area = _field(chunk, "Wohnfläche")
        price = _field(chunk, "Kaltmiete")

        cover_match = _COVER_IMAGE_RE.search(chunk)
        cover_image_url = cover_match.group(1) if cover_match else ""

        if not detail_url and not title:
            continue
        listings.append({
            "listing_key": _listing_key_from_url(detail_url) if detail_url else title,
            "title": title or "locals® Wohnung",
            "address": tagline or (f"{plz} {city}".strip()),
            "city": city.strip(),
            "rooms": parse_decimal(rooms),
            "area_m2": parse_decimal(area),
            "price_eur": parse_decimal(price),
            "detail_url": detail_url,
            "cover_image_url": cover_image_url,
        })
    return listings


def parse_gallery_urls(html: str) -> list[str]:
    """Усі фото й плани поверху оголошення — зі сторінки самого оголошення.

    Картка каталогу показує лише одну обкладинку (``titelbild.jpg``); повна
    галерея є тільки на сторінці конкретного оголошення, в елементах
    glightbox. Реальні фото йдуть у розмітці раніше плану поверху, тому обрізка
    до ліміту альбому Telegram природно лишає саме фото, а не план.
    """
    seen: list[str] = []
    for url in _GALLERY_IMAGE_RE.findall(html):
        if url not in seen:
            seen.append(url)
    return seen


def _line(label: str, value: Any) -> str | None:
    if value is None or value == "":
        return None
    return f"{label}: {html_lib.escape(str(value))}"


def format_listing_message(listing: dict[str, Any]) -> str:
    lines = ["🔑 Нова квартира locals®", ""]
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
