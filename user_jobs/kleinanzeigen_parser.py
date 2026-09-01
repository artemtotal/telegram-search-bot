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

# Прив'язуємось до того, що переживає перевёрстку: data-атрибути картки й самі
# текстові вузли. 2026-09-01 сайт перейшов на службові класи в стилі Tailwind
# («aditem», «aditem-main--middle--tags» тощо зникли за одну ніч), і розбір,
# який спирався на назви класів, віддав нуль оголошень при живій сторінці.
_CARD_RE = re.compile(r'<article[^>]*\sdata-adid="(\d+)"')
_DETAIL_HREF_RE = re.compile(r'\sdata-href="([^"]+)"')
_LOCATION_RE = re.compile(r">\s*(\d{5}\s+[^<>]{2,60}?)\s*<")
_TITLE_LINK_RE = re.compile(r'<a[^>]+href="(/s-anzeige/[^"]+)"[^>]*>\s*([^<>]+?)\s*</a>', re.S)
# Характеристики стоять одним вузлом: «80 m² · 2,5 Zi.». Просто «вузол, де є
# m²» брати не можна — опис оголошення теж повний метрів («Ruhige 120 m²
# Maisonette…»), і саме він трапляється першим. Тому вузол розбиваємо по «·» і
# вимагаємо, щоб хоч одна частина була рівно числом з одиницею.
_TEXT_NODE_RE = re.compile(r">([^<>]+)<")
_SPEC_PART_RE = re.compile(r"^\d+(?:[.,]\d+)?\s*(?:m²|Zi\.)$")
_PRICE_RE = re.compile(r">\s*(\d[\d.]*(?:,\d+)?)\s*(?:€|&euro;|&#8364;)(?:\s*VB)?\s*<")
_SCRIPT_RE = re.compile(r"<script.*?</script>", re.S | re.I)
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


def _specs_text(body: str) -> str:
    """Текст вузла з характеристиками картки, а не з опису оголошення."""
    for node in _TEXT_NODE_RE.finditer(body):
        text = clean_text(node.group(1))
        if not text:
            continue
        if any(_SPEC_PART_RE.match(part.strip()) for part in text.split("·")):
            return text
    return ""


def parse_listings(html: str) -> list[dict[str, Any]]:
    """Extracts every card on the page. Filtering, if any, is the caller's job."""
    listings: list[dict[str, Any]] = []
    matches = list(_CARD_RE.finditer(html))
    for index, match in enumerate(matches):
        listing_id = match.group(1)
        chunk_start = match.end()
        chunk_end = matches[index + 1].start() if index + 1 < len(matches) else len(html)
        chunk = html[chunk_start:chunk_end]

        image_match = _IMAGE_RE.search(chunk)
        cover_image_url = html_lib.unescape(image_match.group(1)) if image_match else ""

        # Обкладинку беремо до вирізання <script> — вона живе в JSON-LD, — а
        # решту шукаємо вже без нього: опис оголошення всередині JSON-LD теж
        # містить і «m²», і ціну, і перехопив би їх у характеристик картки.
        body = _SCRIPT_RE.sub(" ", chunk)

        location_match = _LOCATION_RE.search(body)
        address = clean_text(location_match.group(1)) if location_match else ""
        address_match = _ADDRESS_RE.match(address)
        plz, city = address_match.groups() if address_match else ("", address)

        title_match = _TITLE_LINK_RE.search(body)
        detail_path = title_match.group(1) if title_match else ""
        title = clean_text(title_match.group(2)) if title_match else ""
        if not detail_path:
            href_match = _DETAIL_HREF_RE.search(match.group(0) + body[:200])
            detail_path = href_match.group(1) if href_match else ""
        detail_url = BASE_URL + detail_path if detail_path.startswith("/") else detail_path

        specs_text = _specs_text(body)
        area_match = _AREA_TAG_RE.search(specs_text)
        rooms_match = _ROOMS_TAG_RE.search(specs_text)

        price_match = _PRICE_RE.search(body)
        price = parse_decimal(price_match.group(1)) if price_match else None

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


_DETAIL_ATTR_RE = re.compile(r'"(Warmmiete|Nebenkosten|ExactPreis)"\s*:\s*"([^"]*)"')


def parse_detail_prices(html: str) -> dict[str, float | None]:
    """Ціни зі сторінки оголошення.

    Сама площадка тримає їх готовими числами серед атрибутів оголошення:
    «ExactPreis» — це ціна з поля категорії (Kaltmiete), поруч лежать
    «Nebenkosten» і вже порахована «Warmmiete». Перевірено на живому
    оголошенні: 1390 + 150 = 1540.
    """
    values: dict[str, str] = {}
    for name, value in _DETAIL_ATTR_RE.findall(html):
        values.setdefault(name, value)
    cold = parse_decimal(values.get("ExactPreis"))
    extra = parse_decimal(values.get("Nebenkosten"))
    warm = parse_decimal(values.get("Warmmiete"))
    if warm is None and cold is not None and extra is not None:
        warm = round(cold + extra, 2)
    return {"price_eur": cold, "price_warm_eur": warm, "nebenkosten_eur": extra}


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
        ("Warmmiete EUR", "price_warm_eur"),
    ]:
        line = _line(label, listing.get(key))
        if line:
            lines.append(line)
    link = listing.get("detail_url") or LISTINGS_URL
    lines.extend(["", f"Відкрити: {html_lib.escape(str(link))}"])
    return "\n".join(lines)
