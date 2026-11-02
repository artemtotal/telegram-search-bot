"""Matching ImmoScout24 listings against user filters.

ImmoScout24 names the Stadtteil finer than the wizard does: "Babelsberg Nord"
and "Babelsberg Süd" where the wizard offers "Babelsberg", "Südliche
Innenstadt" for "Innenstadt". A chosen district therefore matches a listing
district that contains it as whole words — but "Waldstadt I" must not match
"Waldstadt II".
"""

import re
from typing import Any, Dict, Iterable, List, Optional

from user_jobs import housing_area, housing_wbs, immoscout_parser, rent_bounds

# ProPotsdam пише Waldstadt цифрою, Immowelt та ImmoScout24 — римською.
_DISTRICT_ALIASES = {"waldstadt 1": "waldstadt i", "waldstadt 2": "waldstadt ii"}


def _num(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return immoscout_parser.parse_decimal(value)


def _within_min(value: Optional[float], bound: Optional[float]) -> bool:
    return bound is None or value is None or value >= bound


def _within_max(value: Optional[float], bound: Optional[float]) -> bool:
    return bound is None or value is None or value <= bound


def _norm_district(name: Any) -> str:
    text = re.sub(r"\s+", " ", str(name or "")).strip().casefold()
    return _DISTRICT_ALIASES.get(text, text)


def split_districts(value: Any) -> List[str]:
    if isinstance(value, str):
        items: Iterable = value.split(",")
    else:
        items = value or []
    return [str(item).strip() for item in items if str(item).strip()]


def district_matches(listing_district: Any, wanted: Iterable[str]) -> bool:
    wanted = [_norm_district(item) for item in wanted if _norm_district(item)]
    if not wanted:
        return True
    district = _norm_district(listing_district)
    if not district:
        # Адресу без району не можна приписати жодному: людина, що обрала
        # райони, отримала б квартиру невідомо звідки.
        return False
    return any(re.search(rf"(?<!\w){re.escape(item)}(?!\w)", district) for item in wanted)


# Обмін квартир («Tauschwohnung: 2-Zimmer-Wohnung …»): зняти її може лише
# той, хто віддає свою. Перевірка тут, а не лише в моніторі: кнопка «надіслати
# за останні години» після нового фільтра йшла повз монітор і до 01.11.2026
# слала обміни (7 відправок за 22.10–01.11).
SWAP_KEYWORDS = ("tausch", "swap")


def is_swap(listing: Dict[str, Any]) -> bool:
    title = str(listing.get("title") or "").casefold()
    return any(keyword in title for keyword in SWAP_KEYWORDS)


def matches_filter(listing: Dict[str, Any], filt: Dict[str, Any]) -> bool:
    if is_swap(listing):
        return False
    # Місце фільтра (з 02.11.2026); без нього — лише Потсдам, як і раніше.
    if not housing_area.listing_in_area(listing, filt.get("area")):
        return False
    # WBS (02.11.2026): людині без WBS — без квартир, де він обов'язковий.
    if not housing_wbs.allowed(listing, filt.get("wbs")):
        return False
    rooms = _num(listing.get("rooms"))
    area = _num(listing.get("area_m2"))
    price = _num(listing.get("price_eur"))
    price_warm = _num(listing.get("price_warm_eur"))
    return (
        district_matches(listing.get("district"), split_districts(filt.get("districts")))
        and _within_min(rooms, _num(filt.get("min_rooms")))
        and _within_max(rooms, _num(filt.get("max_rooms")))
        and _within_min(area, _num(filt.get("min_area_m2")))
        and _within_max(area, _num(filt.get("max_area_m2")))
        and _within_min(price, _num(filt.get("min_price_eur")))
        and _within_max(price, _num(filt.get("max_price_eur")))
        # Повну оренду знаємо лише після картки оголошення; до того теплу
        # межу не застосовуємо (монітор не розсилає таких — див. check_job).
        and _within_min(price_warm, _num(filt.get("min_price_warm_eur")))
        and _within_max(price_warm, _num(filt.get("max_price_warm_eur")))
        and rent_bounds.missing_price_within(
            price, price_warm, area,
            _num(filt.get("max_price_eur")), _num(filt.get("max_price_warm_eur")),
        )
    )


def format_notification(listing: Dict[str, Any], lang: str = "uk") -> str:
    return immoscout_parser.format_listing_message(listing, lang=lang)
