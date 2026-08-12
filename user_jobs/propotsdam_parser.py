"""Parsing and formatting helpers for ProPotsdam/easysquare listings."""

import hashlib
import html
import json
import re
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional

PORTAL_URL = "https://propotsdam-kundenportal.easysquare.com/propotsdam-kundenportal/index.html#/formlist/%252Fsection%252F0%252Fbox%252F0"
IMAGE_URL_TEMPLATE = "https://propotsdam-kundenportal.easysquare.com/propotsdam-kundenportal/api5/accndocs2/{resource_id}"


def clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def parse_decimal(value: Any) -> Optional[float]:
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


def _stable_key(payload: Dict[str, Any]) -> str:
    explicit = clean_text(payload.get("id") or payload.get("listing_key") or payload.get("detail_url"))
    if explicit:
        return explicit
    basis = "|".join(clean_text(payload.get(name)).lower() for name in (
        "title", "address", "district", "rooms", "area", "total_rent", "available_from"
    ))
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:24]


def normalize_listing(payload: Dict[str, Any]) -> Dict[str, Any]:
    extra = payload.get("extra") if isinstance(payload.get("extra"), dict) else {}
    listing = {
        "listing_key": _stable_key(payload),
        "title": clean_text(payload.get("title")),
        "address": clean_text(payload.get("address")),
        "district": clean_text(payload.get("district")),
        "rooms": parse_decimal(payload.get("rooms")),
        "area_m2": parse_decimal(payload.get("area") or payload.get("area_m2")),
        "total_rent_eur": parse_decimal(payload.get("total_rent") or payload.get("total_rent_eur")),
        "available_from": clean_text(payload.get("available_from")),
        "detail_url": clean_text(payload.get("detail_url")),
        "image_url": clean_text(payload.get("image_url")),
        "extra": {clean_text(k): clean_text(v) for k, v in extra.items() if clean_text(k) and clean_text(v)},
    }
    return listing


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def parse_boxlist_xml(xml_text: str) -> List[Dict[str, Any]]:
    """Extract apartment heads from easysquare/OpenPromos boxlist XML."""
    root = ET.fromstring(xml_text)
    listings = []
    for box in root.iter():
        if _local_name(box.tag) != "box" or box.attrib.get("boxid") != "ESQ_VM_REOBJ_ALL":
            continue
        for head in box:
            if _local_name(head.tag) != "head":
                continue
            data: Dict[str, Any] = {"extra": {}}
            images = []
            address = {}
            for child in head:
                name = _local_name(child.tag)
                text = clean_text(child.text)
                if name == "id":
                    data["id"] = text
                elif name == "originalId":
                    data["extra"]["originalId"] = text
                elif name == "title":
                    data["title"] = text
                elif name == "address":
                    address = child.attrib
                elif name == "details":
                    for row in child:
                        if _local_name(row.tag) != "row":
                            continue
                        title = clean_text(row.attrib.get("title"))
                        value = clean_text(row.text)
                        data["extra"][title] = value
                        if title == "Stadtteil":
                            data["district"] = value
                        elif title == "Zimmer":
                            data["rooms"] = value
                        elif title == "Wohnfläche":
                            data["area"] = value
                        elif title == "Gesamtmiete":
                            data["total_rent"] = value
                        elif title == "Verfügbarkeit":
                            data["available_from"] = value
                elif name == "image" and child.attrib.get("resourceId"):
                    images.append(child.attrib["resourceId"])
                elif name == "headBar" and child.attrib.get("barText"):
                    data["extra"]["headBar"] = child.attrib["barText"]
            street = clean_text(address.get("street"))
            postcode = clean_text(address.get("postcode"))
            city = clean_text(address.get("city"))
            data["address"] = clean_text(", ".join(part for part in [street, f"{postcode} {city}".strip()] if part))
            if images:
                data["image_url"] = IMAGE_URL_TEMPLATE.format(resource_id=images[0])
                data["extra"]["image_resource_ids"] = ",".join(images)
            if not data.get("title"):
                data["title"] = data.get("extra", {}).get("headBar") or "ProPotsdam Wohnung"
            listings.append(normalize_listing(data))
    return listings


def dumps_raw(listing: Dict[str, Any]) -> str:
    return json.dumps(listing, ensure_ascii=False, sort_keys=True)


def _line(label: str, value: Any) -> Optional[str]:
    if value is None or value == "":
        return None
    return f"{label}: {html.escape(str(value))}"


def format_listing_message(listing: Dict[str, Any], portal_url: str = PORTAL_URL) -> str:
    """Render all extracted apartment data, not just filter-matching fields."""
    lines = ["🏢 Нова квартира ProPotsdam", ""]
    for label, key in [
        ("Назва", "title"),
        ("Адреса", "address"),
        ("Район", "district"),
        ("Кімнати", "rooms"),
        ("Площа м²", "area_m2"),
        ("Оренда EUR", "total_rent_eur"),
        ("Доступна", "available_from"),
        ("ID/ключ", "listing_key"),
        ("Фото", "image_url"),
        ("Деталі", "detail_url"),
    ]:
        line = _line(label, listing.get(key))
        if line:
            lines.append(line)
    extra = listing.get("extra") or {}
    if extra:
        lines.append("")
        lines.append("Додаткові дані:")
        for key in sorted(extra):
            lines.append(_line(key, extra[key]))
    link = listing.get("detail_url") or portal_url
    lines.extend(["", f"Відкрити: {html.escape(str(link))}"])
    if not listing.get("detail_url"):
        lines.append("Після входу: Immobiliensuche → mehr anzeigen → Immobilien")
    return "\n".join(line for line in lines if line is not None)
