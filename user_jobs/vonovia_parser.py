"""Parser for Vonovia's own apartment search.

Vonovia was written off in the provider survey as "app only, no scraper can
take it". That was wrong when re-checked on 2026-09-02: vonovia.de/zuhause-finden
is an ordinary GET form, and the results come from an open JSON endpoint
(`/api/real-estate/list`) that plain `requests` can read — no browser, no login,
no CAPTCHA. The one condition is a session cookie from the results page first;
without it the endpoint answers 406 (see `vonovia_monitor`).

Two things about their data are worth knowing before reading the code:

* Their Potsdam inventory is dominated by parking spaces — on the day this was
  written 24 of 25 Potsdam entries were garages and one was a commercial floor,
  with zero apartments. So an empty scan here is a perfectly normal day, not a
  broken parser, and everything non-residential has to be dropped.
* `anzahl_zimmer` is rounded down: a flat titled "2,5-Zimmer-Wohnung" arrives
  as `2`. The exact figure is in the title, so that is where rooms are read
  from when it is stated there.
"""

from __future__ import annotations

import html as html_lib
import json
import re
from typing import Any

# Пошук віддає результати цим запитом — його ж робить і сама сторінка видачі.
LIST_URL = "https://www.vonovia.de/api/real-estate/list"
# Сторінка видачі: потрібна не заради розмітки, а заради куки для запиту вище.
SEARCH_PAGE_URL = "https://www.vonovia.de/zuhause-finden/immobilien"
DETAIL_BASE_URL = "https://www.vonovia.de/zuhause-finden/immobilien"
CITY = "Potsdam"
# `immoType=wohnung` відсіює гаражі й комерцію ще на боці порталу; решта
# параметрів — те, що ставить сама форма пошуку.
LIST_PARAMS = {"rentType": "miete", "immoType": "wohnung"}
PAGE_SIZE = 15

_TAG_RE = re.compile(r"<[^>]+>")
# Сторінка оголошення несе всі свої дані одним JSON в атрибуті — це надійніше,
# ніж вишукувати числа у верстці, яку портал перемальовує коли завгодно.
_DETAIL_DATA_RE = re.compile(r'data-vonovia-data="([^"]*)"')
# «2,5-Zimmer-Wohnung» у заголовку: API округлює таке до 2, заголовок — ні.
_ROOMS_IN_TITLE_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*-?\s*Zimmer", re.I)
# Рекламні картинки порталу (зелений струм, реклама застосунку) лежать у тій
# самій галереї, що й фото квартири, і відрізняються лише префіксом імені.
_MARKETING_IMAGE_RE = re.compile(r"/CAMP-", re.I)
# Портал віддає прев'ю 324 px завширшки — для альбому в Telegram замало.
_IMAGE_WIDTH_RE = re.compile(r"[?&]width=\d+(?:&crop=[^&]*)?$")
IMAGE_WIDTH = 1200


def clean_text(value: Any) -> str:
    text = _TAG_RE.sub("", str(value or ""))
    return re.sub(r"\s+", " ", html_lib.unescape(text)).strip()


def parse_decimal(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
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
        # Німецький формат: крапка тут — розділювач тисяч («1.111 €»),
        # десятковий знак завжди кома.
        number = number.replace(".", "")
    try:
        return float(number)
    except ValueError:
        return None


def full_size_image(url: str) -> str:
    """Те саме фото, але не прев'ю: 324 px у стрічці Telegram виглядають кашею."""
    text = str(url or "").strip()
    if not text:
        return ""
    return _IMAGE_WIDTH_RE.sub(f"?width={IMAGE_WIDTH}", text)


def _photos(urls: Any) -> list[str]:
    photos: list[str] = []
    for url in urls or []:
        text = str(url or "").strip()
        if not text or _MARKETING_IMAGE_RE.search(text):
            continue
        full = full_size_image(text)
        if full not in photos:
            photos.append(full)
    return photos


def _rooms(item: dict) -> float | None:
    """Кімнати: із заголовка, якщо він називає їх точно, інакше з API.

    `anzahl_zimmer` округлює вниз — «2,5-Zimmer-Wohnung» приходить як 2. Для
    межі фільтра це різниця в пів кімнати не на нашу користь, тому точніше
    число з заголовка має перевагу.
    """
    match = _ROOMS_IN_TITLE_RE.search(str(item.get("titel") or ""))
    if match:
        rooms = parse_decimal(match.group(1))
        if rooms:
            return rooms
    return parse_decimal(item.get("anzahl_zimmer"))


def _address(item: dict) -> str:
    street = clean_text(item.get("strasse"))
    place = " ".join(part for part in (clean_text(item.get("plz")), clean_text(item.get("ort"))) if part)
    return ", ".join(part for part in (street, place) if part)


def is_apartment(item: dict) -> bool:
    """Чи це житло взагалі.

    Гаражі й паркомісця Vonovia лежать у тій самій видачі й приходять з нулями
    в площі та кімнатах. Портал уміє відсіяти їх сам (`immoType=wohnung`), але
    перевірка тут коштує нічого, а мовчазна зміна на їхньому боці інакше
    надіслала б людині «квартиру» за 44 € без жодної кімнати.
    """
    area = parse_decimal(item.get("groesse")) or 0
    rooms = _rooms(item) or 0
    return area > 0 and rooms > 0


def parse_listings(payload: Any) -> list[dict[str, Any]]:
    """Оголошення з відповіді пошукового API."""
    if isinstance(payload, str):
        payload = json.loads(payload)
    results = (payload or {}).get("results") if isinstance(payload, dict) else None
    listings: list[dict[str, Any]] = []
    for item in results or []:
        if not isinstance(item, dict) or not is_apartment(item):
            continue
        key = clean_text(item.get("wrk_id"))
        if not key:
            continue
        slug = clean_text(item.get("slug"))
        gallery = _photos(item.get("imageUrls"))
        cover = full_size_image(item.get("preview_img_url")) or (gallery[0] if gallery else "")
        listings.append({
            "listing_key": key,
            "title": clean_text(item.get("titel")) or "Vonovia Wohnung",
            "address": _address(item),
            "rooms": _rooms(item),
            "area_m2": parse_decimal(item.get("groesse")),
            # Число під карткою портал підписує «Kaltmiete» — це холодна оренда.
            "price_eur": parse_decimal(item.get("preis")),
            "detail_url": f"{DETAIL_BASE_URL}/{slug}" if slug else SEARCH_PAGE_URL,
            "cover_image_url": cover,
            "gallery_urls": gallery,
        })
    return listings


def total_count(payload: Any) -> int:
    """Скільки всього оголошень знайшов портал — по ньому й гортаємо сторінки."""
    if isinstance(payload, str):
        payload = json.loads(payload)
    paging = (payload or {}).get("paging") if isinstance(payload, dict) else None
    info = (paging or {}).get("info") if isinstance(paging, dict) else None
    count = (info or {}).get("count") if isinstance(info, dict) else None
    try:
        return int(count)
    except (TypeError, ValueError):
        return 0


def detail_payload(html: str) -> dict[str, Any]:
    """JSON зі сторінки оголошення — усе, що вона знає про квартиру."""
    match = _DETAIL_DATA_RE.search(html or "")
    if not match:
        return {}
    try:
        data = json.loads(html_lib.unescape(match.group(1)))
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def parse_detail_prices(html: str) -> dict[str, float | None]:
    """Повна оренда зі сторінки оголошення.

    Каталог друкує лише холодну; повну (`warmRent`) портал рахує сам і кладе
    поруч із комуналкою та опаленням. Своєї арифметики не робимо — беремо
    назване число, а суму лишаємо запасним варіантом на випадок, коли поле
    порожнє, але доданки є.
    """
    data = detail_payload(html)
    cold = parse_decimal(data.get("rent"))
    warm = parse_decimal(data.get("warmRent"))
    extra = parse_decimal(data.get("operatingCosts"))
    heating = parse_decimal(data.get("heatingCosts"))
    if warm is None and cold is not None:
        parts = [value for value in (extra, heating) if value is not None]
        if parts:
            warm = round(cold + sum(parts), 2)
    return {
        "price_eur": cold,
        "price_warm_eur": round(warm, 2) if warm is not None else None,
        "nebenkosten_eur": extra,
        "heizkosten_eur": heating,
    }


def parse_gallery_urls(html: str) -> list[str]:
    """Фото зі сторінки оголошення — запасний шлях.

    Зазвичай галерея приходить уже в каталозі, тож окремий похід сюди не
    потрібен; ця функція знадобиться, якщо в каталозі фото не виявиться.
    """
    images = detail_payload(html).get("images") or []
    return _photos(
        image.get("url") if isinstance(image, dict) else image for image in images
    )


def _line(label: str, value: Any) -> str | None:
    if value is None or value == "":
        return None
    return f"{label}: {html_lib.escape(str(value))}"


def format_listing_message(listing: dict[str, Any]) -> str:
    lines = ["🏢 Нова квартира Vonovia", ""]
    for label, key in [
        ("Назва", "title"),
        ("Адреса", "address"),
        ("Кімнати", "rooms"),
        ("Площа м²", "area_m2"),
        ("Kaltmiete EUR", "price_eur"),
        ("Warmmiete EUR", "price_warm_eur"),
    ]:
        line = _line(label, listing.get(key))
        if line:
            lines.append(line)
    link = listing.get("detail_url") or SEARCH_PAGE_URL
    lines.extend(["", f"Відкрити: {html_lib.escape(str(link))}"])
    return "\n".join(lines)
