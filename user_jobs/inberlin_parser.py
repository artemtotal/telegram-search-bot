"""inberlinwohnen.de — спільний портал шести міських компаній Берліна (degewo,
Gewobag, HOWOGE, Stadt und Land, WBM, berlinovo). Етап 8 «Берлін і
Бранденбург» (02.11.2026, docs/housing-region-plan.md).

Сторінка «Wohnungsfinder» — Livewire: звичайний GET `?page=N` віддає по 10
квартир, найновіші першими (≈950 на 02.11.2026). Кожна картка несе всі свої
дані JSON-ом в атрибуті `wire:snapshot` (`data.item`): Kaltmiete (`rentNet`),
Nebenkosten, Gesamtmiete (`rentGross`), кімнати, площа, адреса з індексом і
районом, компанія, посилання на оголошення компанії (`deeplink`), фото, і в
деталях — чи потрібен WBS.
"""

from __future__ import annotations

import html as html_lib
import json
import re
from typing import Any, Optional

import i18n

BASE_URL = "https://www.inberlinwohnen.de"
LIST_URL = BASE_URL + "/wohnungsfinder/"
IMAGE_URL = BASE_URL + "/img/{path}?q=85&fit=crop&fm=jpg&w=1200"

_SNAPSHOT_RE = re.compile(r'wire:snapshot="([^"]+)"')
_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?")


def page_url(page: int) -> str:
    return LIST_URL if page <= 1 else f"{LIST_URL}?page={int(page)}"


def parse_decimal(value: Any) -> Optional[float]:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    text = str(value or "").strip()
    if not text:
        return None
    # Німецький формат: «1.234,56» або «340,11».
    if "," in text:
        text = text.replace(".", "").replace(",", ".")
    match = _NUMBER_RE.search(text)
    try:
        return float(match.group(0)) if match else None
    except ValueError:
        return None


def _first(value: Any) -> dict:
    """Livewire пакує об'єкт як [{…}, {"s": "arr"}]."""
    if isinstance(value, list) and value and isinstance(value[0], dict):
        return value[0]
    return value if isinstance(value, dict) else {}


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", html_lib.unescape(str(value or ""))).strip()


def _item(snapshot: str) -> Optional[dict]:
    try:
        data = json.loads(html_lib.unescape(snapshot))
    except ValueError:
        return None
    item = _first(((data or {}).get("data") or {}).get("item"))
    return item if item.get("id") is not None and "rentNet" in item else None


def _details(item: dict) -> dict:
    """Рядки «Alle Details»: {"Gesamtmiete": "645,91", "WBS": "nicht erforderlich", …}."""
    found: dict = {}

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if "label" in node and "value" in node:
                found.setdefault(_clean(node["label"]), _clean(re.sub(r"<[^>]+>", " ", str(node["value"] or ""))))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(item.get("details"))
    return found


def _wbs(item: dict, details: dict) -> str:
    value = details.get("WBS", "")
    title = _clean(item.get("title"))
    # Заголовок буває точнішим за поле («WBS ab 160 erforderlich» проти «erforderlich»).
    if "WBS" in title and "erforderlich" in title.casefold() and "nicht" not in value.casefold():
        return title
    return value


def parse_listings(page_html: str) -> list[dict[str, Any]]:
    listings: list[dict[str, Any]] = []
    seen: set[str] = set()
    for match in _SNAPSHOT_RE.finditer(page_html or ""):
        item = _item(match.group(1))
        if item is None:
            continue
        key = str(item["id"])
        if key in seen:
            continue
        seen.add(key)
        address = _first(item.get("address"))
        street = " ".join(part for part in (_clean(address.get("street")), _clean(address.get("number"))) if part)
        plz = _clean(address.get("zipCode"))
        district = _clean(address.get("district"))
        company = _clean(_first(item.get("company")).get("name"))
        image_path = _clean(item.get("imagePath"))
        details = _details(item)
        listings.append({
            "listing_key": key,
            "title": _clean(item.get("title")) or "Wohnung",
            "company": company,
            "address": ", ".join(part for part in (street, f"{plz} Berlin-{district}" if district else f"{plz} Berlin") if part),
            "plz": plz,
            "district": district,
            # Не з пошуку «Потсдам»: у місце фільтра потрапляє лише за індексом.
            "region": "berlin",
            "rooms": parse_decimal(item.get("rooms")),
            "area_m2": parse_decimal(item.get("area")),
            "price_eur": parse_decimal(item.get("rentNet")),
            # Gesamtmiete з деталей — з опаленням; `rentGross` буває без нього
            # (452,78 + 111,63 = 564,41, а на сайті 645,91).
            "price_warm_eur": parse_decimal(details.get("Gesamtmiete")) or parse_decimal(item.get("rentGross")),
            "wbs": _wbs(item, details),
            "available_from": _clean(item.get("occupationDate")),
            "detail_url": _clean(item.get("deeplink")) or LIST_URL,
            "cover_image_url": IMAGE_URL.format(path=image_path) if image_path else "",
        })
    return listings


def wbs_required(listing: dict) -> bool:
    """«WBS ab 160 erforderlich», «erforderlich» — так; «nicht erforderlich» — ні."""
    text = str(listing.get("wbs") or "").casefold()
    return "erforderlich" in text and "nicht" not in text


def _line(label: str, value: Any) -> Optional[str]:
    if value is None or value == "":
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return f"{label}: {html_lib.escape(str(value))}"


def format_listing_message(listing: dict[str, Any], lang: str = "uk") -> str:
    source = "inberlinwohnen" + (f" · {listing['company']}" if listing.get("company") else "")
    lines = [i18n.t("housing.notify.header_new_flat", lang, emoji="🏛", source=html_lib.escape(source)), ""]
    for label_key, key in [
        ("housing.notify.field.title", "title"),
        ("housing.notify.field.address", "address"),
        ("housing.notify.field.rooms", "rooms"),
        ("housing.notify.field.area", "area_m2"),
    ]:
        line = _line(i18n.t(label_key, lang), listing.get(key))
        if line:
            lines.append(line)
    # Терміни порталу лишаються німецькою в будь-якій мові інтерфейсу.
    for label, key in [("Kaltmiete EUR", "price_eur"), ("Gesamtmiete EUR", "price_warm_eur"),
                       ("WBS", "wbs"), ("Bezugsfertig ab", "available_from")]:
        line = _line(label, listing.get(key))
        if line:
            lines.append(line)
    link = listing.get("detail_url") or LIST_URL
    lines.extend(["", f"{i18n.t('housing.matches.open_link', lang)}: {html_lib.escape(str(link))}"])
    return "\n".join(lines)
