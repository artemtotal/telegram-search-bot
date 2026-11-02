"""ohne-makler.net — провізійно вільні оголошення приватних власників. Етап 6
«Берлін і Бранденбург» (02.11.2026, docs/housing-region-plan.md).

Звичайний GET сторінки пошуку, `?page=N`, по 24 оголошення; Берлін (≈170) і
Бранденбург (≈100, разом із Потсдамом). Сортування посиланням не змінюється, тож
обхід щоразу читає всі сторінки — їх небагато. Картка: ціна (це Kaltmiete — на
сторінці оголошення «Kaltmiete (zzgl. NK)»), заголовок, «13591 Berlin (Staaken)»,
кімнати, площа; фото — `/immobilie/<id>/picture/0/medium.jpg`.
"""

from __future__ import annotations

import html as html_lib
import re
from typing import Any, Optional

import i18n
from user_jobs import housing_area

BASE_URL = "https://www.ohne-makler.net"
REGION_PATHS = {
    "berlin": "/immobilien/wohnung-mieten/berlin/berlin/",
    "brandenburg": "/immobilien/wohnung-mieten/brandenburg/",
}
DETAIL_URL = BASE_URL + "/immobilie/{key}/"
IMAGE_URL = BASE_URL + "/immobilie/{key}/picture/{index}/medium.jpg"

_CARD_RE = re.compile(r'data-om-id="(\d+)"\s+data-om-type="RENT"')
_TAG_RE = re.compile(r"<[^>]+>")
_PRICE_RE = re.compile(r"^(\d{1,3}(?:\.\d{3})*|\d+)(?:,\d+)?\s*€$")
_ADDRESS_RE = re.compile(r"^(\d{5})\s+([^()]+?)(?:\s*\(([^)]+)\))?$")
_AREA_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*m²")
_ROOMS_RE = re.compile(r"^\d+(?:[.,]5)?$")


PAGE_SIZE = 24
_TOTAL_RE = re.compile(r"(\d[\d.]*)\s*Immobilien\b")


def total_count(page_html: str) -> int:
    """«172 Immobilien» — за останньою сторінкою сайт не дає порожньої, тож
    кількість сторінок рахуємо з цього числа."""
    match = _TOTAL_RE.search(page_html or "")
    return int(match.group(1).replace(".", "")) if match else 0


def page_url(region: str, page: int) -> str:
    url = BASE_URL + REGION_PATHS[region]
    return url if page <= 1 else f"{url}?page={int(page)}"


def _number(text: str) -> Optional[float]:
    text = text.strip().replace(".", "").replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


def _parts(chunk: str) -> list:
    text = _TAG_RE.sub("|", re.sub(r"<script.*?</script>", "", chunk, flags=re.S))
    return [part for part in (re.sub(r"\s+", " ", html_lib.unescape(piece)).strip() for piece in text.split("|")) if part]


def parse_listings(page_html: str) -> list[dict[str, Any]]:
    listings: list[dict[str, Any]] = []
    seen: set[str] = set()
    matches = list(_CARD_RE.finditer(page_html or ""))
    for index, match in enumerate(matches):
        key = match.group(1)
        if key in seen:
            continue
        seen.add(key)
        # Картка — один <a>…</a>, кінець — перше «</a>». Раніше остання на сторінці
        # бралась як 4000 символів, а SVG-іконки займають більше: кімнати й площа
        # губились, і квартира без них проходила будь-який фільтр (02.11.2026:
        # 2 кімнати в Гольмі пішли 17 людям «від 3–4 кімнат»).
        following = matches[index + 1].start() if index + 1 < len(matches) else len(page_html)
        close = page_html.find("</a>", match.end(), following)
        end = close if close >= 0 else min(following, match.end() + 12000)
        parts = _parts(page_html[match.end():end])
        price = title = plz = city = district = None
        rooms = area = None
        for position, part in enumerate(parts):
            if price is None and _PRICE_RE.match(part):
                price = _number(part.replace("€", ""))
                title = next((p for p in parts[position + 1:position + 3] if not _ADDRESS_RE.match(p)), None)
                continue
            address = _ADDRESS_RE.match(part)
            if plz is None and address:
                plz, city, district = address.group(1), address.group(2).strip(), (address.group(3) or "").strip()
                continue
            if plz is not None and rooms is None and _ROOMS_RE.match(part):
                rooms = _number(part)
                continue
            area_match = _AREA_RE.search(part)
            if area is None and area_match and plz is not None:
                area = _number(area_match.group(1))
        if not plz:
            continue
        place = f"{plz} {city}" + (f" ({district})" if district else "")
        listings.append({
            "listing_key": key,
            "title": title or "Wohnung",
            "address": place,
            "plz": plz,
            "city": city,
            # Не з пошуку «Потсдам»: у місце фільтра — лише за індексом.
            # За індексом: люди пишуть «Berlin-Köpenick», «BERLIN», просто «Köpenick».
            "region": "berlin" if plz in housing_area.bezirk_of_plz() else "brandenburg",
            "rooms": rooms,
            "area_m2": area,
            "price_eur": price,
            "price_warm_eur": None,
            "detail_url": DETAIL_URL.format(key=key),
            "cover_image_url": IMAGE_URL.format(key=key, index=0),
        })
    return listings


def _line(label: str, value: Any) -> Optional[str]:
    if value is None or value == "":
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return f"{label}: {html_lib.escape(str(value))}"


def format_listing_message(listing: dict[str, Any], lang: str = "uk") -> str:
    lines = [i18n.t("housing.notify.header_new_flat", lang, emoji="🔑", source="ohne-makler.net"), ""]
    for label_key, key in [
        ("housing.notify.field.title", "title"),
        ("housing.notify.field.address", "address"),
        ("housing.notify.field.rooms", "rooms"),
        ("housing.notify.field.area", "area_m2"),
    ]:
        line = _line(i18n.t(label_key, lang), listing.get(key))
        if line:
            lines.append(line)
    line = _line("Kaltmiete EUR", listing.get("price_eur"))
    if line:
        lines.append(line)
    lines.extend(["", f"{i18n.t('housing.matches.open_link', lang)}: {html_lib.escape(str(listing.get('detail_url') or BASE_URL))}"])
    return "\n".join(lines)
