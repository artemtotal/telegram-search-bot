"""Parser for ImmoScout24 — through the API of their mobile app.

The website answers any script with an AWS WAF image CAPTCHA ("pick the
curtains"), which no browser automation solves. Their mobile app asks
`api.mobile.immobilienscout24.de` for the very same results as plain JSON,
without a CAPTCHA (checked 12.10.2026: 212 Potsdam rentals, 50 per page).

What the data looks like:

* The result list names three bare values per flat — price, area, rooms —
  with empty labels. The price is the Kaltmiete (matches "Kaltmiete" on the
  listing page to the euro).
* The listing page (`/expose/<id>`) is a list of typed sections: Warmmiete,
  the full address with the Stadtteil and the photo gallery live there.
* "Noch exklusiv für Suchen+": a fresh listing is for paying Plus members
  only until `earlyAccessExpiresAt`; everyone else can see it but not write.
"""

from __future__ import annotations

import html as html_lib
import re
from typing import Any, Optional

import i18n

API_BASE = "https://api.mobile.immobilienscout24.de"
LIST_URL = f"{API_BASE}/search/list"
EXPOSE_API_URL = API_BASE + "/expose/{listing_id}"
WEB_EXPOSE_URL = "https://www.immobilienscout24.de/expose/{listing_id}"
SEARCH_PAGE_URL = "https://www.immobilienscout24.de/Suche/de/brandenburg/potsdam/wohnung-mieten?sorting=2"
# API пускає лише «свій» застосунок — за назвою в User-Agent.
USER_AGENT = "ImmoScout_27.12_26.2_._"
# Найновіші спершу — так нове оголошення завжди на першій сторінці.
LIST_PARAMS = {
    "searchType": "region",
    "geocodes": "/de/brandenburg/potsdam",
    "realestatetype": "apartmentrent",
    "sorting": "-firstactivation",
}
LIST_BODY = {"supportedResultListType": [], "userData": {}}
IMAGE_SIZE = "1200x900"

_SIZE_PLACEHOLDER = "%WIDTH%x%HEIGHT%"
_NUMBER_RE = re.compile(r"\d+(?:[.,]\d{3})*(?:[,.]\d+)?|\d+")


def clean_text(value: Any) -> str:
    text = str(value or "").replace("\xa0", " ")
    return re.sub(r"\s+", " ", html_lib.unescape(text)).strip()


def parse_decimal(value: Any) -> Optional[float]:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    match = _NUMBER_RE.search(clean_text(value))
    if not match:
        return None
    number = match.group(0)
    if "," in number and "." in number:
        number = number.replace(".", "").replace(",", ".")
    elif "," in number:
        number = number.replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(\.\d{3})+", number):
        # Німецький формат: крапка — розділювач тисяч («1.600 €»).
        number = number.replace(".", "")
    try:
        return float(number)
    except ValueError:
        return None


def jpeg_image(url: Any) -> str:
    """Фото у JPEG потрібного розміру: Telegram не бере webp як фото."""
    text = clean_text(url)
    if not text:
        return ""
    text = text.replace(_SIZE_PLACEHOLDER, IMAGE_SIZE)
    return re.sub(r"/format/webp(/quality/\d+)?", "/format/jpg", text)


def district_from_address(address: Any) -> Optional[str]:
    """Stadtteil — частина адреси одразу після «… Potsdam».

    «Zum Kahleberg 41, 14478 Potsdam, Waldstadt II» → «Waldstadt II»;
    «14482 Potsdam / Babelsberg, Babelsberg Süd (unvollständige Adresse)» → «Babelsberg Süd».
    """
    text = re.sub(r"\(.*?\)", "", clean_text(address)).strip()
    parts = [part.strip() for part in text.split(",") if part.strip()]
    for index, part in enumerate(parts):
        if "Potsdam" in part and index + 1 < len(parts):
            district = parts[index + 1]
            # Буває й «…, 14469 Potsdam, 14469 Potsdam» — індекс районом не є.
            if re.match(r"\d{5}\b", district) or "Potsdam" in district:
                return None
            return district
    return None


def _attribute_values(item: dict) -> list[str]:
    return [clean_text(attr.get("value")) for attr in (item.get("attributes") or []) if isinstance(attr, dict)]


def _pick(values: list[str], marker: str) -> Optional[float]:
    for value in values:
        if marker in value:
            return parse_decimal(value)
    return None


def parse_listings(payload: Any) -> list[dict[str, Any]]:
    """Оголошення зі сторінки видачі."""
    listings: list[dict[str, Any]] = []
    for entry in (payload or {}).get("resultListItems") or []:
        if not isinstance(entry, dict) or entry.get("type") != "EXPOSE_RESULT":
            continue
        item = entry.get("item") or {}
        key = clean_text(item.get("id"))
        if not key or item.get("realEstateType", "apartmentrent") != "apartmentrent":
            continue
        values = _attribute_values(item)
        address = re.sub(r"\s*\(unvollständige Adresse\)", "", clean_text((item.get("address") or {}).get("line")))
        paywall = item.get("paywallListing") or {}
        pictures = [jpeg_image(pic.get("urlScaleAndCrop")) for pic in item.get("pictures") or [] if isinstance(pic, dict)]
        cover = jpeg_image((item.get("titlePicture") or {}).get("full")) or (pictures[0] if pictures else "")
        listings.append({
            "listing_key": key,
            "title": clean_text(item.get("title")) or "ImmoScout24 Wohnung",
            "address": address,
            "district": district_from_address(address),
            "rooms": _pick(values, "Zi"),
            "area_m2": _pick(values, "m²"),
            "price_eur": _pick(values, "€"),
            "plus_until": clean_text(paywall.get("earlyAccessExpiresAt")) if paywall.get("active") else "",
            "detail_url": WEB_EXPOSE_URL.format(listing_id=key),
            "cover_image_url": cover,
            "gallery_urls": [url for url in pictures if url],
        })
    return listings


def page_count(payload: Any) -> int:
    try:
        return int((payload or {}).get("numberOfPages") or 0)
    except (TypeError, ValueError):
        return 0


def _sections(payload: Any) -> list[dict]:
    return [s for s in (payload or {}).get("sections") or [] if isinstance(s, dict)]


def parse_expose(payload: Any) -> dict[str, Any]:
    """Що дає картка оголошення: обидві ціни, повна адреса з районом, галерея."""
    cold = warm = side = heating = total_rent = None
    # «Gesamtmiete: 2.903,56 € zzgl. Heiz- und Nebenkosten» — це холодна, не повна; тоді й
    # totalRent у COST_CHECK — та сама холодна.
    total_is_cold = False
    address = ""
    gallery: list[str] = []
    for section in _sections(payload):
        kind = section.get("type")
        if kind == "MEDIA":
            for media in section.get("media") or []:
                if isinstance(media, dict) and media.get("type") == "PICTURE":
                    url = jpeg_image(media.get("imageUrlForWeb") or media.get("fullImageUrl"))
                    if url and url not in gallery:
                        gallery.append(url)
        elif kind in ("TOP_ATTRIBUTES", "ATTRIBUTE_LIST"):
            for attr in section.get("attributes") or []:
                label = clean_text((attr or {}).get("label"))
                text = clean_text((attr or {}).get("text"))
                value = parse_decimal(text)
                if value is None:
                    continue
                if label.startswith("Gesamtmiete") and "zzgl" in text:
                    total_is_cold = True
                elif cold is None and label.startswith("Kaltmiete"):
                    cold = value
                elif warm is None and (label.startswith("Warmmiete") or label.startswith("Gesamtmiete")):
                    warm = value
                elif side is None and label.startswith("Nebenkosten"):
                    side = value
                elif heating is None and label.startswith("Heizkosten:"):
                    heating = value
        elif kind == "COST_CHECK" and total_rent is None:
            total_rent = parse_decimal(section.get("totalRent"))
        elif kind == "MAP" and not address:
            address = ", ".join(
                part for part in (clean_text(section.get("addressLine1")), clean_text(section.get("addressLine2"))) if part
            )
    if warm is None and not total_is_cold:
        warm = total_rent
    if warm is not None and cold is not None and warm < cold:
        # Орендодавець суперечить сам собі («Kaltmiete 1.440 €, Nebenkosten 329 €, Warmmiete
        # 1.410 €»): повна = холодна + додаткові, а без них повна невідома.
        warm = cold + side + (heating or 0) if side is not None else None
    district = None
    if address:
        # «14473 Südliche Innenstadt, Potsdam» — тут район іде перед містом.
        match = re.search(r"\b\d{5}\s+([^,]+),\s*Potsdam\b", address)
        district = clean_text(match.group(1)) if match else None
    return {
        "price_eur": cold,
        "price_warm_eur": round(warm, 2) if warm is not None else None,
        "address": address,
        "district": district,
        "gallery_urls": gallery,
    }


def _line(label: str, value: Any) -> Optional[str]:
    if value is None or value == "":
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return f"{label}: {html_lib.escape(str(value))}"


def format_listing_message(listing: dict[str, Any], lang: str = "uk") -> str:
    lines = [i18n.t("housing.notify.header_new_flat", lang, emoji="🔵", source="ImmoScout24"), ""]
    for label_key, key in [
        ("housing.notify.field.title", "title"),
        ("housing.notify.field.address", "address"),
        ("housing.notify.field.rooms", "rooms"),
        ("housing.notify.field.area", "area_m2"),
    ]:
        line = _line(i18n.t(label_key, lang), listing.get(key))
        if line:
            lines.append(line)
    # Kaltmiete/Warmmiete — терміни порталу, лишаються німецькою.
    for label, key in [("Kaltmiete EUR", "price_eur"), ("Warmmiete EUR", "price_warm_eur")]:
        line = _line(label, listing.get(key))
        if line:
            lines.append(line)
    plus_until = plus_until_label(listing.get("plus_until"))
    if plus_until:
        lines.extend(["", i18n.t("housing.immoscout.plus_only", lang, until=plus_until)])
    link = listing.get("detail_url") or SEARCH_PAGE_URL
    lines.extend(["", f"{i18n.t('housing.matches.open_link', lang)}: {html_lib.escape(str(link))}"])
    return "\n".join(lines)


def plus_until_label(value: Any) -> str:
    """«12.10. 19:45», якщо ексклюзив для Plus ще не скінчився, інакше порожньо."""
    from datetime import datetime, timezone

    text = clean_text(value)
    if not text:
        return ""
    try:
        until = datetime.fromisoformat(text)
    except ValueError:
        return ""
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    if until <= datetime.now(timezone.utc):
        return ""
    return until.strftime("%d.%m. %H:%M")
