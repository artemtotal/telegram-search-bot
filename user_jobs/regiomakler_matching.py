"""Matching helpers for the shared ImmoTeam/alpha (immomakler) feed and user filters.

Like SEMMELHAACK/SCHOBA, this feed carries no reliable district vocabulary — a
filter here is just rooms/area/Kaltmiete bounds.
"""

from typing import Any, Dict, Optional

from user_jobs import regiomakler_parser


def _num(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return regiomakler_parser.parse_decimal(value)


def _within_min(value: Optional[float], bound: Optional[float]) -> bool:
    return bound is None or value is None or value >= bound


def _within_max(value: Optional[float], bound: Optional[float]) -> bool:
    return bound is None or value is None or value <= bound


def matches_filter(listing: Dict[str, Any], filt: Dict[str, Any]) -> bool:
    rooms = _num(listing.get("rooms"))
    area = _num(listing.get("area_m2"))
    price = _num(listing.get("price_eur"))
    price_warm = _num(listing.get("price_warm_eur"))
    return (
        _within_min(rooms, _num(filt.get("min_rooms")))
        and _within_max(rooms, _num(filt.get("max_rooms")))
        and _within_min(area, _num(filt.get("min_area_m2")))
        and _within_max(area, _num(filt.get("max_area_m2")))
        and _within_min(price, _num(filt.get("min_price_eur")))
        and _within_max(price, _num(filt.get("max_price_eur")))
        # Тепла межа звіряється з теплою ціною, холодна — з холодною. Немає в
        # оголошення потрібної величини — умова просто не застосовується
        # (`_within_*` пропускає None), як і для будь-якого іншого невідомого
        # показника: інакше квартиру відкинуло б за те, чого портал не публікує.
        and _within_min(price_warm, _num(filt.get("min_price_warm_eur")))
        and _within_max(price_warm, _num(filt.get("max_price_warm_eur")))
    )


def format_notification(listing: Dict[str, Any]) -> str:
    return regiomakler_parser.format_listing_message(listing)
