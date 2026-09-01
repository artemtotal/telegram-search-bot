"""Matching helpers for ProPotsdam listings and user filters."""

from typing import Any, Dict, Optional

from user_jobs import propotsdam_parser


def _norm(value: Any) -> str:
    return str(value or "").strip().casefold()


def _num(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return propotsdam_parser.parse_decimal(value)


def _district_allowed(listing: Dict[str, Any], filt: Dict[str, Any]) -> bool:
    raw = str(filt.get("districts") or "").strip()
    if not raw:
        return True
    wanted = {_norm(part) for part in raw.split(",") if _norm(part)}
    return _norm(listing.get("district")) in wanted


def _within_min(value: Optional[float], bound: Optional[float]) -> bool:
    return bound is None or value is None or value >= bound


def _within_max(value: Optional[float], bound: Optional[float]) -> bool:
    return bound is None or value is None or value <= bound


def matches_filter(listing: Dict[str, Any], filt: Dict[str, Any]) -> bool:
    if not _district_allowed(listing, filt):
        return False
    rooms = _num(listing.get("rooms"))
    area = _num(listing.get("area_m2"))
    rent = _num(listing.get("total_rent_eur"))
    price = _num(listing.get("price_eur"))
    return (
        _within_min(rooms, _num(filt.get("min_rooms")))
        and _within_max(rooms, _num(filt.get("max_rooms")))
        and _within_min(area, _num(filt.get("min_area_m2")))
        and _within_max(area, _num(filt.get("max_area_m2")))
        and _within_min(rent, _num(filt.get("min_total_rent_eur")))
        and _within_max(rent, _num(filt.get("max_total_rent_eur")))
        # Холодна оренда відома лише для квартир, чию картку вже відкривали:
        # у списку її немає. Поки її немає, умова не застосовується — так само,
        # як для будь-якого іншого невідомого показника.
        and _within_min(price, _num(filt.get("min_price_eur")))
        and _within_max(price, _num(filt.get("max_price_eur")))
    )


def format_notification(listing: Dict[str, Any], portal_url: str) -> str:
    return propotsdam_parser.format_listing_message(listing, portal_url=portal_url)
