"""Matching helpers for SCHOBA listings and user filters.

Like SEMMELHAACK, SCHOBA carries no reliable district vocabulary shared with
Immowelt/ProPotsdam — a filter here is just rooms/area/Nettokaltmiete bounds.
"""

from typing import Any, Dict, Optional

from user_jobs import schoba_parser


def _num(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return schoba_parser.parse_decimal(value)


def _within_min(value: Optional[float], bound: Optional[float]) -> bool:
    return bound is None or value is None or value >= bound


def _within_max(value: Optional[float], bound: Optional[float]) -> bool:
    return bound is None or value is None or value <= bound


def matches_filter(listing: Dict[str, Any], filt: Dict[str, Any]) -> bool:
    rooms = _num(listing.get("rooms"))
    area = _num(listing.get("area_m2"))
    price = _num(listing.get("price_eur"))
    return (
        _within_min(rooms, _num(filt.get("min_rooms")))
        and _within_max(rooms, _num(filt.get("max_rooms")))
        and _within_min(area, _num(filt.get("min_area_m2")))
        and _within_max(area, _num(filt.get("max_area_m2")))
        and _within_min(price, _num(filt.get("min_price_eur")))
        and _within_max(price, _num(filt.get("max_price_eur")))
    )


def format_notification(listing: Dict[str, Any]) -> str:
    return schoba_parser.format_listing_message(listing)
