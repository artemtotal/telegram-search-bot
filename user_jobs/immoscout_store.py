"""Persistent operations for ImmoScout24 filters, listings, deliveries, and status.

Mirrors `vonovia_store.py`, plus districts: ImmoScout24 names the Stadtteil
of almost every listing, so a filter keeps the districts the person chose.
The listing page is read once per listing (`detail_fetched_at`) for the full
rent, the exact district and the gallery.
"""

import json
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Set, Tuple

from database import (
    DBSession,
    ImmoscoutDelivery,
    ImmoscoutFilter,
    ImmoscoutListing,
    ImmoscoutStatus,
)
from user_jobs import housing_area
from user_jobs import immoscout_matching, immoscout_parser

STATUS_KEY = "global"
# update_filter без area лишає місце як є: старі шляхи (майстер адміна) його не знають.
KEEP = object()


def utc_now() -> datetime:
    return datetime.utcnow()


def parse_optional_number(text) -> Optional[float]:
    return immoscout_parser.parse_decimal(text)


def filter_to_dict(row: ImmoscoutFilter) -> Dict:
    return {
        "filter_id": row.filter_id,
        "user_id": row.user_id,
        "title": row.title,
        "districts": row.districts or "",
        "min_rooms": row.min_rooms,
        "max_rooms": row.max_rooms,
        "min_area_m2": row.min_area_m2,
        "max_area_m2": row.max_area_m2,
        "min_price_eur": row.min_price_eur,
        "max_price_eur": row.max_price_eur,
        "min_price_warm_eur": row.min_price_warm_eur,
        "max_price_warm_eur": row.max_price_warm_eur,
        "area": _load_area(row.area),
        "wbs": row.wbs,
        "active": row.active,
    }


def _load_area(raw) -> Optional[Dict]:
    try:
        area = json.loads(raw) if raw else None
    except (TypeError, ValueError):
        return None
    return area if isinstance(area, dict) else None


def _dump_area(area: Optional[Dict]) -> Optional[str]:
    return json.dumps(area, ensure_ascii=False, sort_keys=True) if area else None


def listing_to_dict(row: ImmoscoutListing) -> Dict:
    return {
        "listing_key": row.listing_key,
        "title": row.title,
        "address": row.address,
        "district": row.district,
        "rooms": row.rooms,
        "area_m2": row.area_m2,
        "price_eur": row.price_eur,
        "price_warm_eur": row.price_warm_eur,
        "gallery_urls": [url for url in str(row.gallery_urls or "").splitlines() if url.strip()],
        "detail_url": row.detail_url,
        "cover_image_url": row.cover_image_url,
        "plus_until": row.plus_until or "",
        "detail_fetched": row.detail_fetched_at is not None,
        "first_seen_at": row.first_seen_at,
        "plz": row.plz or "",
        "city": row.city or "",
        "region": row.region or "potsdam",
    }


def create_filter(
    user_id: int,
    title: str,
    districts: str = "",
    min_rooms: Optional[float] = None,
    max_rooms: Optional[float] = None,
    min_area_m2: Optional[float] = None,
    max_area_m2: Optional[float] = None,
    min_price_eur: Optional[float] = None,
    max_price_eur: Optional[float] = None,
    min_price_warm_eur: Optional[float] = None,
    max_price_warm_eur: Optional[float] = None,
    area: Optional[Dict] = None,
    wbs: Optional[str] = None,
) -> int:
    session = DBSession()
    try:
        row = ImmoscoutFilter(
            user_id=int(user_id),
            title=str(title)[:120],
            districts=normalize_districts(districts),
            min_rooms=min_rooms,
            max_rooms=max_rooms,
            min_area_m2=min_area_m2,
            max_area_m2=max_area_m2,
            min_price_eur=min_price_eur,
            max_price_eur=max_price_eur,
            min_price_warm_eur=min_price_warm_eur,
            max_price_warm_eur=max_price_warm_eur,
            area=_dump_area(area),
            wbs=wbs or None,
            active=True,
            created_at=utc_now(),
        )
        session.add(row)
        session.flush()
        filter_id = int(row.filter_id)
        filter_data = filter_to_dict(row)
        now = utc_now()
        for listing_row in session.query(ImmoscoutListing).filter(
            ImmoscoutListing.is_active.is_(True)
        ).all():
            listing = listing_to_dict(listing_row)
            if immoscout_matching.matches_filter(listing, filter_data):
                session.add(ImmoscoutDelivery(
                    filter_id=filter_id,
                    listing_key=str(listing["listing_key"]),
                    sent_at=now,
                ))
        session.commit()
        return filter_id
    finally:
        session.close()


def update_filter(
    filter_id: int,
    user_id: int,
    title: str,
    districts: str = "",
    min_rooms: Optional[float] = None,
    max_rooms: Optional[float] = None,
    min_area_m2: Optional[float] = None,
    max_area_m2: Optional[float] = None,
    min_price_eur: Optional[float] = None,
    max_price_eur: Optional[float] = None,
    min_price_warm_eur: Optional[float] = None,
    max_price_warm_eur: Optional[float] = None,
    area=KEEP,
    wbs=housing_area.KEEP,
) -> bool:
    session = DBSession()
    try:
        row = session.query(ImmoscoutFilter).filter(
            ImmoscoutFilter.filter_id == int(filter_id),
            ImmoscoutFilter.user_id == int(user_id),
        ).first()
        if not row:
            return False
        row.title = str(title)[:120]
        row.districts = normalize_districts(districts)
        row.min_rooms = min_rooms
        row.max_rooms = max_rooms
        row.min_area_m2 = min_area_m2
        row.max_area_m2 = max_area_m2
        row.min_price_eur = min_price_eur
        row.max_price_eur = max_price_eur
        row.min_price_warm_eur = min_price_warm_eur
        row.max_price_warm_eur = max_price_warm_eur
        if area is not KEEP:
            row.area = _dump_area(area)
        if wbs is not housing_area.KEEP:
            row.wbs = wbs or None
        session.flush()
        filter_data = filter_to_dict(row)
        now = utc_now()
        session.query(ImmoscoutDelivery).filter(ImmoscoutDelivery.filter_id == int(filter_id)).delete()
        for listing_row in session.query(ImmoscoutListing).filter(
            ImmoscoutListing.is_active.is_(True)
        ).all():
            listing = listing_to_dict(listing_row)
            if immoscout_matching.matches_filter(listing, filter_data):
                session.add(ImmoscoutDelivery(
                    filter_id=int(filter_id),
                    listing_key=str(listing["listing_key"]),
                    sent_at=now,
                ))
        session.commit()
        return True
    finally:
        session.close()


def list_filters(user_id: Optional[int] = None, active_only: bool = False) -> List[Dict]:
    session = DBSession()
    try:
        query = session.query(ImmoscoutFilter)
        if user_id is not None:
            query = query.filter(ImmoscoutFilter.user_id == int(user_id))
        if active_only:
            query = query.filter(ImmoscoutFilter.active.is_(True))
        return [filter_to_dict(row) for row in query.order_by(ImmoscoutFilter.filter_id.asc()).all()]
    finally:
        session.close()


def set_filter_active(filter_id: int, active: bool, user_id: Optional[int] = None) -> bool:
    session = DBSession()
    try:
        query = session.query(ImmoscoutFilter).filter(ImmoscoutFilter.filter_id == int(filter_id))
        if user_id is not None:
            query = query.filter(ImmoscoutFilter.user_id == int(user_id))
        row = query.first()
        if not row:
            return False
        row.active = bool(active)
        session.commit()
        return True
    finally:
        session.close()


def delete_filter(filter_id: int, user_id: Optional[int] = None) -> bool:
    session = DBSession()
    try:
        query = session.query(ImmoscoutFilter).filter(ImmoscoutFilter.filter_id == int(filter_id))
        if user_id is not None:
            query = query.filter(ImmoscoutFilter.user_id == int(user_id))
        row = query.first()
        if not row:
            return False
        session.query(ImmoscoutDelivery).filter(
            ImmoscoutDelivery.filter_id == int(filter_id)
        ).delete(synchronize_session=False)
        session.delete(row)
        session.commit()
        return True
    finally:
        session.close()


def normalize_districts(value) -> str:
    return ",".join(immoscout_matching.split_districts(value))


def keys_with_details() -> Set[str]:
    """Оголошення, картку яких уже прочитано (повна оренда, район, галерея)."""
    session = DBSession()
    try:
        rows = session.query(ImmoscoutListing.listing_key).filter(
            ImmoscoutListing.detail_fetched_at.isnot(None)
        ).all()
        return {str(row.listing_key) for row in rows}
    finally:
        session.close()


def upsert_listings(listings: Iterable[Dict], region: str = "potsdam", deactivate_missing: bool = True) -> int:
    """Зберігає видачу одного регіону. Знятими (`is_active=False`) стають лише
    квартири цього ж регіону, яких немає в повній видачі: пошук «Берлін», що
    дочитав лише нові сторінки, не повинен «знімати» решту Берліна, а
    потсдамський — берлінські квартири."""
    session = DBSession()
    try:
        now = utc_now()
        count = 0
        seen = set()
        for listing in listings:
            key = str(listing.get("listing_key") or "")
            if not key:
                continue
            seen.add(key)
            row = session.query(ImmoscoutListing).filter(ImmoscoutListing.listing_key == key).first()
            if row is None:
                row = ImmoscoutListing(listing_key=key, first_seen_at=now, region=region)
                session.add(row)
            elif not row.region:
                row.region = "potsdam"
            if listing.get("plz"):
                row.plz = listing.get("plz")
            if listing.get("city"):
                row.city = listing.get("city")
            row.title = listing.get("title") or "ImmoScout24 Wohnung"
            # Картка оголошення знає адресу й район точніше за видачу — після
            # неї видача їх уже не перезаписує.
            if row.detail_fetched_at is None or listing.get("detail_fetched"):
                row.address = listing.get("address")
                row.district = listing.get("district")
            row.rooms = listing.get("rooms")
            row.area_m2 = listing.get("area_m2")
            price = listing.get("price_eur")
            # Видача округлює холодну до євро (2.904 € проти 2.903,56 € у картці): точну ціну
            # з картки вона не перезаписує, лише справжню зміну ціни.
            if (listing.get("detail_fetched") or row.detail_fetched_at is None or row.price_eur is None
                    or price is None or abs(float(price) - float(row.price_eur)) >= 1):
                row.price_eur = price
            if listing.get("detail_fetched"):
                row.price_warm_eur = listing.get("price_warm_eur")  # картка без повної: невідома
            elif listing.get("price_warm_eur") is not None:
                row.price_warm_eur = listing.get("price_warm_eur")
            row.plus_until = listing.get("plus_until") or ""
            if listing.get("detail_fetched"):
                row.detail_fetched_at = now
            gallery = [str(url).strip() for url in (listing.get("gallery_urls") or []) if str(url).strip()]
            # Галерея картки повніша за мініатюри видачі.
            if gallery and (listing.get("detail_fetched") or not row.gallery_urls):
                row.gallery_urls = "\n".join(gallery)
            row.detail_url = listing.get("detail_url")
            row.cover_image_url = listing.get("cover_image_url")
            row.last_seen_at = now
            row.is_active = True
            count += 1
        if deactivate_missing:
            same_region = ImmoscoutListing.region == region
            if region == "potsdam":
                same_region = same_region | ImmoscoutListing.region.is_(None)
            query = session.query(ImmoscoutListing).filter(same_region, ImmoscoutListing.is_active.is_(True))
            if seen:
                query = query.filter(~ImmoscoutListing.listing_key.in_(seen))
            query.update({"is_active": False}, synchronize_session=False)
        session.commit()
        return count
    finally:
        session.close()


def known_keys() -> Set[str]:
    """Усі оголошення, які вже бачили (щоб пошук регіону знав, де зупинитися)."""
    session = DBSession()
    try:
        return {str(row.listing_key) for row in session.query(ImmoscoutListing.listing_key).all()}
    finally:
        session.close()


def recent_keys_without_details(since: datetime) -> Set[str]:
    """Нові (з `since`) оголошення без прочитаної картки."""
    session = DBSession()
    try:
        rows = session.query(ImmoscoutListing.listing_key).filter(
            ImmoscoutListing.detail_fetched_at.is_(None), ImmoscoutListing.first_seen_at >= since,
        ).all()
        return {str(row.listing_key) for row in rows}
    finally:
        session.close()


def list_active_listings() -> List[Dict]:
    session = DBSession()
    try:
        rows = session.query(ImmoscoutListing).filter(ImmoscoutListing.is_active.is_(True)).all()
        return [listing_to_dict(row) for row in rows]
    finally:
        session.close()


def list_active_listings_since(cutoff: datetime) -> List[Dict]:
    """Active listings first observed at or after `cutoff` — powers the "show
    me what appeared in the last hour/day" offer right after a filter is
    created, which deliberately bypasses the create-time baseline."""
    session = DBSession()
    try:
        rows = session.query(ImmoscoutListing).filter(
            ImmoscoutListing.is_active.is_(True), ImmoscoutListing.first_seen_at >= cutoff
        ).all()
        return [listing_to_dict(row) for row in rows]
    finally:
        session.close()


def delivered_pairs() -> Set[Tuple[int, str]]:
    session = DBSession()
    try:
        return {(int(row.filter_id), str(row.listing_key)) for row in session.query(ImmoscoutDelivery).all()}
    finally:
        session.close()


def mark_delivered(filter_id: int, listing_key: str) -> None:
    session = DBSession()
    try:
        exists = session.query(ImmoscoutDelivery).filter(
            ImmoscoutDelivery.filter_id == int(filter_id),
            ImmoscoutDelivery.listing_key == str(listing_key),
        ).first()
        if not exists:
            session.add(ImmoscoutDelivery(filter_id=int(filter_id), listing_key=str(listing_key), sent_at=utc_now()))
            session.commit()
    finally:
        session.close()


def select_unsent_matches(
    listings: Iterable[Dict],
    filters: Iterable[Dict],
    delivered: Set[Tuple[int, str]],
) -> List[Tuple[Dict, Dict]]:
    matches = []
    for filt in filters:
        if not filt.get("active", True):
            continue
        filter_id = int(filt["filter_id"])
        for listing in listings:
            key = str(listing.get("listing_key") or "")
            if not key or (filter_id, key) in delivered:
                continue
            if immoscout_matching.matches_filter(listing, filt):
                matches.append((filt, listing))
    return matches


def record_status(status: str, listings_count: int = 0, error: str = "") -> None:
    record_status_for(STATUS_KEY, status, listings_count=listings_count, error=error)


def latest_status() -> Dict:
    return latest_status_for(STATUS_KEY)


def record_status_for(key: str, status: str, listings_count: int = 0, error: str = "") -> None:
    session = DBSession()
    try:
        row = session.query(ImmoscoutStatus).filter(ImmoscoutStatus.key == key).first()
        if row is None:
            row = ImmoscoutStatus(key=key)
            session.add(row)
        row.last_checked_at = utc_now()
        row.last_status = status
        row.last_error = str(error or "")[:500]
        row.listings_count = int(listings_count or 0)
        session.commit()
    finally:
        session.close()


def latest_status_for(key: str) -> Dict:
    session = DBSession()
    try:
        row = session.query(ImmoscoutStatus).filter(ImmoscoutStatus.key == key).first()
        if not row:
            return {}
        return {
            "last_checked_at": row.last_checked_at,
            "last_status": row.last_status,
            "last_error": row.last_error,
            "listings_count": row.listings_count,
        }
    finally:
        session.close()
