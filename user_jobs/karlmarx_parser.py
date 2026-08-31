"""Parser for Wohnungsgenossenschaft "Karl Marx" Potsdam eG's offers page.

Static server-rendered HTML (TYPO3), no login and no JS needed. The page mixes
commercial (`data-type="Büro/Praxis"`, `"Gastronomie/Hotel"`) and residential
(`data-type="Wohnung Miete"`) cards in one client-side-filterable list — the
dropdown filter is pure JS/CSS over already-loaded cards, so a plain GET sees
every card regardless of which option the dropdown shows. Only the residential
type is kept; the rest is noise for this bot (Karl Marx is largely a commercial
landlord right now, but the same feed carries real rentals the moment one
appears).

The residential card's price is explicitly labelled "Warmmiete" (warm rent,
including utilities) — a different quantity than the Kaltmiete the other
broker sites report — so it is not folded into their shared price question.
"""

from __future__ import annotations

import html as html_lib
import re
from typing import Any

LISTINGS_URL = "https://wgkarlmarx.de/fuer-wohnungssucher"
BASE_URL = "https://wgkarlmarx.de"
RESIDENTIAL_TYPE = "Wohnung Miete"

_CARD_SPLIT_RE = re.compile(r'<div class="immo-object card"')
_DATA_TYPE_RE = re.compile(r'data-type="([^"]*)"')
# wgkarlmarx.de has mistagged at least one real listing this way: an office
# ("Potsdamer Mitte - Gewerbe, Bürofläche zu vermieten") carries
# data-type="Wohnung Miete" on the live page despite being unambiguously
# commercial. The dropdown/data-type alone can't be trusted, so anything
# whose own title admits it's commercial space is dropped too, regardless
# of what data-type claims.
_COMMERCIAL_TITLE_RE = re.compile(
    r"\b(gewerbe|b[uü]ro(?:fl[aä]che)?|praxis|ladenfl[aä]che|gastronomie|hotel|lagerfl[aä]che|lagerhalle|werkstatt)\b",
    re.I,
)
_HREF_RE = re.compile(r'<a class="card-link" href="([^"]+)"')
_TITLE_RE = re.compile(r'<h3 class="card-title">([^<]*)</h3>')
_FIELD_RE = re.compile(r'<div class="number">\s*([^<]*?)\s*</div>\s*<div class="title">\s*([^<]*?)\s*</div>')
_STREET_RE = re.compile(r'<div class="street">\s*([^<]*?)\s*</div>')
_CITY_RE = re.compile(r'<div class="city">\s*([^<]*?)\s*</div>')
_PLZ_CITY_RE = re.compile(r"(\d{4,5})\s+(.+)")
_TAG_RE = re.compile(r"<[^>]+>")
_COVER_IMAGE_RE = re.compile(r'<img[^>]*\ssrc="([^"]+)"')
# Галерея сторінки оголошення — Bootstrap-карусель, кожен кадр промальовано
# як background-image, а не звичайний <img>; поруч ще й Grundriss/Energieausweis
# у тому самому форматі — залишаємо, як і в SEMMELHAACK.
_GALLERY_IMAGE_RE = re.compile(r"background-image:\s*url\('([^']+)'\)")


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
        number = number.replace(".", "")
    try:
        return float(number)
    except ValueError:
        return None


def _listing_key_from_url(url: str) -> str:
    match = re.search(r"/expose/([^/?#]+)", url)
    return match.group(1) if match else url


def count_all_cards(html: str) -> int:
    """Total cards regardless of type (commercial + residential).

    Used only as a parser-health signal: the page is a mixed commercial/
    residential feed that's rarely if ever truly empty, so 0 cards of any
    type — not just 0 residential ones — means the markup likely changed,
    unlike a 0-residential result which is a normal, expected day.
    """
    return len(_CARD_SPLIT_RE.split(html)) - 1


def parse_listings(html: str) -> list[dict[str, Any]]:
    """Extracts every residential ("Wohnung Miete") card on the offers page.

    Commercial cards are dropped here, not by the caller — nothing downstream
    ever needs to see a Büro/Gastronomie listing for this source.
    """
    listings: list[dict[str, Any]] = []
    chunks = _CARD_SPLIT_RE.split(html)[1:]
    for chunk in chunks:
        data_type_match = _DATA_TYPE_RE.search(chunk)
        data_type = clean_text(data_type_match.group(1)) if data_type_match else ""
        if data_type != RESIDENTIAL_TYPE:
            continue

        href_match = _HREF_RE.search(chunk)
        detail_url = BASE_URL + href_match.group(1) if href_match else ""
        title_match = _TITLE_RE.search(chunk)
        title = clean_text(title_match.group(1)) if title_match else ""

        if _COMMERCIAL_TITLE_RE.search(title):
            continue

        fields = {clean_text(label).casefold(): value for value, label in _FIELD_RE.findall(chunk)}
        area = fields.get("wohnfläche") or fields.get("hauptfläche")
        price = fields.get("warmmiete") or fields.get("kaltmiete") or fields.get("miete pro monat")
        rooms = fields.get("zimmer")

        street_match = _STREET_RE.search(chunk)
        street = clean_text(street_match.group(1)) if street_match else ""
        city_match = _CITY_RE.search(chunk)
        city_raw = clean_text(city_match.group(1)) if city_match else ""
        plz_city_match = _PLZ_CITY_RE.match(city_raw)
        city = plz_city_match.group(2) if plz_city_match else city_raw

        cover_match = _COVER_IMAGE_RE.search(chunk)
        cover_image_url = BASE_URL + cover_match.group(1) if cover_match else ""

        if not detail_url and not title:
            continue
        listings.append({
            "listing_key": _listing_key_from_url(detail_url) if detail_url else title,
            "title": title or "Wohnung",
            "address": ", ".join(part for part in [street, city_raw] if part),
            "city": city,
            "rooms": parse_decimal(rooms),
            "area_m2": parse_decimal(area),
            "price_eur": parse_decimal(price),
            "detail_url": detail_url,
            "cover_image_url": cover_image_url,
        })
    return listings


def parse_gallery_urls(html: str) -> list[str]:
    """Усі фото й плани поверху оголошення — зі сторінки самого оголошення.

    Картка каталогу показує лише одну обкладинку; повна карусель є тільки на
    сторінці оголошення, і кожен її кадр промальовано як background-image
    (Bootstrap carousel), а не звичайний <img src>.
    """
    seen: list[str] = []
    for path in _GALLERY_IMAGE_RE.findall(html):
        url = BASE_URL + path
        if url not in seen:
            seen.append(url)
    return seen


def _line(label: str, value: Any) -> str | None:
    if value is None or value == "":
        return None
    return f"{label}: {html_lib.escape(str(value))}"


def format_listing_message(listing: dict[str, Any]) -> str:
    lines = ["🧱 Нова квартира Wohnungsgenossenschaft Karl Marx", ""]
    for label, key in [
        ("Назва", "title"),
        ("Адреса", "address"),
        ("Кімнати", "rooms"),
        ("Площа м²", "area_m2"),
        ("Warmmiete EUR", "price_eur"),
    ]:
        line = _line(label, listing.get(key))
        if line:
            lines.append(line)
    link = listing.get("detail_url") or LISTINGS_URL
    lines.extend(["", f"Відкрити: {html_lib.escape(str(link))}"])
    return "\n".join(lines)
