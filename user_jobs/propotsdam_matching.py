"""Matching helpers for ProPotsdam listings and user filters."""

from typing import Any, Dict, Optional

from user_jobs import propotsdam_parser, rent_bounds


def _norm(value: Any) -> str:
    return str(value or "").strip().casefold()


def _num(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return propotsdam_parser.parse_decimal(value)


# Портал пише свої зведені райони, а майстер фільтра пропонує звичні назви
# (PROPOT_DISTRICTS у housing_monitor). Без перекладу «Am Stern» не збігався
# з «Stern», а «Nördl. VS/IS» - з «Innenstadt», і квартири з цих районів не
# отримував ніхто (16.10.2026). Склад зведених районів - за індексами адрес:
# 14467 - центр і північні передмістя, 14473 - Teltower Vorstadt і Zentrum
# Ost, 14469 - Bornstedter Feld і північ.
_PORTAL_DISTRICTS = {
    "am stern": {"stern"},
    "nördl. vs/is": {"innenstadt", "nauener vorstadt", "jägervorstadt", "berliner vorstadt"},
    "südl. vs/is": {"innenstadt", "teltower vorstadt"},
    "potsdam nord": {"bornstedt", "bornim", "eiche", "golm"},
}
# Портал не ділить Babelsberg, а майстер пропонує ще й Nord/Süd.
_WANTED_ALIASES = {"babelsberg nord": "babelsberg", "babelsberg süd": "babelsberg"}


def _district_allowed(listing: Dict[str, Any], filt: Dict[str, Any]) -> bool:
    raw = str(filt.get("districts") or "").strip()
    if not raw:
        return True
    wanted = {_WANTED_ALIASES.get(_norm(part), _norm(part)) for part in raw.split(",") if _norm(part)}
    district = _norm(listing.get("district"))
    return district in wanted or bool(_PORTAL_DISTRICTS.get(district, set()) & wanted)


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
        # Холодної в списку немає, тож межа «лише холодна» інакше не діяла
        # б тут зовсім — див. rent_bounds.
        and rent_bounds.missing_price_within(
            price, rent, area,
            _num(filt.get("max_price_eur")), _num(filt.get("max_total_rent_eur")),
        )
    )


def format_notification(listing: Dict[str, Any], portal_url: str, lang: str = "uk") -> str:
    return propotsdam_parser.format_listing_message(listing, lang=lang, portal_url=portal_url)
