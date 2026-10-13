"""Matching helpers for Kleinanzeigen listings and user filters.

No reliable district vocabulary here either — a filter is rooms/area/price
bounds, same shape as the other sources.
"""

from typing import Any, Dict, Optional

from user_jobs import kleinanzeigen_parser, rent_bounds


def _num(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return kleinanzeigen_parser.parse_decimal(value)


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
        and rent_bounds.missing_price_within(
            price, price_warm, area,
            _num(filt.get("max_price_eur")), _num(filt.get("max_price_warm_eur")),
        )
    )


def near_miss_over(listing: Dict[str, Any], filt: Dict[str, Any]) -> Optional[float]:
    """На скільки євро квартира дорожча за холодну межу — якщо лише цим вона
    й не проходить, а показана ціна може бути повною орендою.

    Kleinanzeigen не підписує ціну в списку, і приватні оголошення часто
    ставлять туди повну оренду: у 49 із 95 оголошень, де портал назвав
    Warmmiete, вона дорівнює показаній ціні. Тоді «холодна» 750 € проти межі
    700 € — насправді 750 € повної, і квартира може підійти. Таку не
    відкидаємо мовчки: людина отримує її окремою підказкою (рішення Артема
    13.10.2026). None — не той випадок.
    """
    price = _num(listing.get("price_eur"))
    limit = _num(filt.get("max_price_eur"))
    if price is None or limit is None or price <= limit:
        return None
    warm = _num(listing.get("price_warm_eur"))
    if warm is not None and warm != price:
        # Портал назвав окрему повну оренду — показана ціна справді холодна.
        return None
    if matches_filter(listing, filt):
        return None
    if not matches_filter({**listing, "price_eur": None, "price_warm_eur": price}, filt):
        return None
    return price - limit


def format_notification(listing: Dict[str, Any], lang: str = "uk") -> str:
    return kleinanzeigen_parser.format_listing_message(listing, lang=lang)
