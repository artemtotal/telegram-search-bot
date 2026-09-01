"""Parser for SCHOBA's static rental listings page (schoba.de/immobilien/angebote/mieten.htm).

No login and no JavaScript rendering needed. The page is a portfolio showcase that
mixes currently vacant listings with already-rented ones shown for reference (marked
"# vermietet" with a placeholder "0,00 EUR" price) — those must be filtered out, or
every notification would be about an apartment nobody can actually rent anymore.
"""

from __future__ import annotations

import html as html_lib
import re
from typing import Any

LISTINGS_URL = "https://www.schoba.de/immobilien/angebote/mieten.htm"
BASE_URL = "https://www.schoba.de/immobilien/angebote/"

_CARD_SPLIT_RE = re.compile(r'<div class="objektetabelle">')
_HEADER_RE = re.compile(r"<th[^>]*>(.*?)</th>", re.S)
_ROW_RE = re.compile(r"<td[^>]*>([^<]+):</td>\s*<td[^>]*>([^<]*)</td>")
_LINK_RE = re.compile(r'<a href="([^"]+)"[^>]*title="Obj')
_PLZ_CITY_DISTRICT_RE = re.compile(r"(\d{4,5})\s+([^(]+?)(?:\s*\(([^)]+)\))?$")
_TAG_RE = re.compile(r"<[^>]+>")
# "-Ngr.jpg" — полноразмерная фотография галереи; у неё есть маленькая пара
# "-Nkl.jpg" (миниатюра) и отдельные "bild-klein"/"bild-objekt"/"bild-liste" —
# те же кадры или обрезки для карточки каталога, не дополнительные фото.
_GALLERY_IMAGE_RE = re.compile(r'src="(bilder/[^"]*?-\d+gr\.jpg)"')


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


def _header_lines(chunk: str) -> list[str]:
    match = _HEADER_RE.search(chunk)
    if not match:
        return []
    raw = re.sub(r"<br\s*/?>", "\n", match.group(1))
    raw = _TAG_RE.sub("", raw)
    return [clean_text(line) for line in raw.split("\n") if clean_text(line)]


def _listing_id_from_url(url: str) -> str:
    match = re.search(r"/([^/]+)\.htm$", url)
    return match.group(1) if match else url


def parse_listings(html: str) -> list[dict[str, Any]]:
    """Extracts every listing card, vacant and already-rented alike.

    Filtering to currently-vacant offers (status != vermietet) is the caller's
    job — the parser is a plain HTML-to-dict mapping, testable against a fixed
    fixture without depending on which units happen to be rented out today.
    """
    listings: list[dict[str, Any]] = []
    chunks = _CARD_SPLIT_RE.split(html)[1:]
    for chunk in chunks:
        lines = _header_lines(chunk)
        status = lines[0] if lines else ""
        title = lines[3] if len(lines) > 3 else (lines[-1] if lines else "")
        plz = city = district = ""
        for line in lines[1:]:
            addr_match = _PLZ_CITY_DISTRICT_RE.match(line)
            if addr_match:
                plz, city, district = addr_match.groups()
                city = city.strip()
                break
        rows = {clean_text(label).casefold(): value for label, value in _ROW_RE.findall(chunk)}
        rooms = rows.get("zimmer")
        area = rows.get("wohnfläche") or rows.get("wohn-/ nutzfläche") or rows.get("wohn-/nutzfläche")
        price = rows.get("nettokaltmiete")
        link_match = _LINK_RE.search(chunk)
        detail_url = BASE_URL + link_match.group(1) if link_match else ""
        listing_id = _listing_id_from_url(detail_url) if detail_url else title
        if not title and not listing_id:
            continue
        listings.append({
            "listing_key": listing_id or title,
            "title": title or "SCHOBA Wohnung",
            "status": status,
            "is_vacant": bool(status) and "vermietet" not in status.casefold() and "reserviert" not in status.casefold(),
            "address": ", ".join(part for part in [f"{plz} {city}".strip(), district] if part).strip(", "),
            "city": city,
            "district": district or "",
            "rooms": parse_decimal(rooms),
            "area_m2": parse_decimal(area),
            "price_eur": parse_decimal(price),
            "detail_url": detail_url,
        })
    return listings


def parse_gallery_urls(html: str) -> list[str]:
    """Усі фотографії оголошення в повному розмірі — зі сторінки самого оголошення.

    Каталожна картка не містить жодного фото взагалі: там лише таблиця з
    характеристиками. Галерея є тільки на сторінці кожного оголошення — там
    кожен кадр повторюється двічі (велике фото "-Ngr.jpg" у верхній частині
    сторінки й ще раз ближче до низу), тому дублікати відкидаються, а порядок
    номерів зберігається таким, у якому кадри вперше зустрілись у розмітці.
    Усі детальні сторінки лежать прямо під ``BASE_URL`` без підкаталогів,
    тому відносний шлях "bilder/..." завжди дописується до нього.
    """
    seen: list[str] = []
    for relative_path in _GALLERY_IMAGE_RE.findall(html):
        url = BASE_URL + relative_path
        if url not in seen:
            seen.append(url)
    return seen


_DETAIL_PRICE_RE = re.compile(
    r"(Nettokaltmiete|Nebenkosten|Gesamtmietpreis|Bruttowarmmiete)\s*:?\s*</td>\s*<td[^>]*>\s*([^<]+?)\s*<",
    re.I | re.S,
)


def parse_detail_prices(html: str) -> dict[str, float | None]:
    """Розбивка ціни зі сторінки оголошення.

    Каталог друкує лише Nettokaltmiete; повну ціну сторінка оголошення дає
    готовою — «Gesamtmietpreis: 942,37 EUR», поруч із «Nebenkosten». Рахувати
    самим нічого не треба.
    """
    values = {clean_text(label).casefold(): value for label, value in _DETAIL_PRICE_RE.findall(html)}
    warm = values.get("gesamtmietpreis") or values.get("bruttowarmmiete")
    return {
        "price_eur": parse_decimal(values.get("nettokaltmiete")),
        "price_warm_eur": parse_decimal(warm),
        "nebenkosten_eur": parse_decimal(values.get("nebenkosten")),
    }


def _line(label: str, value: Any) -> str | None:
    if value is None or value == "":
        return None
    return f"{label}: {html_lib.escape(str(value))}"


def format_listing_message(listing: dict[str, Any]) -> str:
    lines = ["🏡 Нова квартира SCHOBA", ""]
    for label, key in [
        ("Назва", "title"),
        ("Адреса", "address"),
        ("Кімнати", "rooms"),
        ("Площа м²", "area_m2"),
        ("Nettokaltmiete EUR", "price_eur"),
        ("Gesamtmietpreis EUR", "price_warm_eur"),
    ]:
        line = _line(label, listing.get(key))
        if line:
            lines.append(line)
    link = listing.get("detail_url") or LISTINGS_URL
    lines.extend(["", f"Відкрити: {html_lib.escape(str(link))}"])
    return "\n".join(lines)
