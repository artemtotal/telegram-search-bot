"""Persistent operations for inberlinwohnen.de filters, listings, deliveries and status.

Mirrors `vonovia_store.py`; the listing additionally keeps the company, the
postcode, whether a WBS is needed and the move-in date. The list is read
newest first and only the new pages most of the time, so a listing is taken
down only by a full pass (`deactivate_missing`).
"""

from datetime import datetime
from typing import Dict, Iterable, List, Optional, Set, Tuple

from database import (
    DBSession,
    InberlinDelivery,
    InberlinFilter,
    InberlinListing,
    InberlinStatus,
)
from user_jobs import housing_area
from user_jobs import inberlin_matching, inberlin_parser

STATUS_KEY = "global"


def utc_now() -> datetime:
    return datetime.utcnow()


def parse_optional_number(text) -> Optional[float]:
    return inberlin_parser.parse_decimal(text)


def filter_to_dict(row: InberlinFilter) -> Dict:
    return {
        "filter_id": row.filter_id,
        "user_id": row.user_id,
        "title": row.title,
        "min_rooms": row.min_rooms,
        "max_rooms": row.max_rooms,
        "min_area_m2": row.min_area_m2,
        "max_area_m2": row.max_area_m2,
        "min_price_eur": row.min_price_eur,
        "max_price_eur": row.max_price_eur,
        "min_price_warm_eur": row.min_price_warm_eur,
        "max_price_warm_eur": row.max_price_warm_eur,
        "area": housing_area.load_area(row.area),
        "wbs": row.wbs,
        "active": row.active,
    }


def listing_to_dict(row: InberlinListing) -> Dict:
    return {
        "listing_key": row.listing_key,
        "title": row.title,
        "address": row.address,
        "rooms": row.rooms,
        "area_m2": row.area_m2,
        "price_eur": row.price_eur,
        "price_warm_eur": row.price_warm_eur,
        "gallery_urls": [url for url in str(row.gallery_urls or "").splitlines() if url.strip()],
        "detail_url": row.detail_url,
        "cover_image_url": row.cover_image_url,
        "plz": row.plz or "",
        "company": row.company or "",
        "wbs": row.wbs or "",
        "available_from": row.available_from or "",
        "region": "berlin",
        "first_seen_at": row.first_seen_at,
    }


def create_filter(
    user_id: int,
    title: str,
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
        row = InberlinFilter(
            user_id=int(user_id),
            title=str(title)[:120],
            min_rooms=min_rooms,
            max_rooms=max_rooms,
            min_area_m2=min_area_m2,
            max_area_m2=max_area_m2,
            min_price_eur=min_price_eur,
            max_price_eur=max_price_eur,
            min_price_warm_eur=min_price_warm_eur,
            max_price_warm_eur=max_price_warm_eur,
            area=housing_area.dump_area(area),
            wbs=wbs or None,
            active=True,
            created_at=utc_now(),
        )
        session.add(row)
        session.flush()
        filter_id = int(row.filter_id)
        filter_data = filter_to_dict(row)
        now = utc_now()
        for listing_row in session.query(InberlinListing).filter(
            InberlinListing.is_active.is_(True)
        ).all():
            listing = listing_to_dict(listing_row)
            if inberlin_matching.matches_filter(listing, filter_data):
                session.add(InberlinDelivery(
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
    min_rooms: Optional[float] = None,
    max_rooms: Optional[float] = None,
    min_area_m2: Optional[float] = None,
    max_area_m2: Optional[float] = None,
    min_price_eur: Optional[float] = None,
    max_price_eur: Optional[float] = None,
    min_price_warm_eur: Optional[float] = None,
    max_price_warm_eur: Optional[float] = None,
    area=housing_area.KEEP,
    wbs=housing_area.KEEP,
) -> bool:
    session = DBSession()
    try:
        row = session.query(InberlinFilter).filter(
            InberlinFilter.filter_id == int(filter_id),
            InberlinFilter.user_id == int(user_id),
        ).first()
        if not row:
            return False
        row.title = str(title)[:120]
        row.min_rooms = min_rooms
        row.max_rooms = max_rooms
        row.min_area_m2 = min_area_m2
        row.max_area_m2 = max_area_m2
        row.min_price_eur = min_price_eur
        row.max_price_eur = max_price_eur
        row.min_price_warm_eur = min_price_warm_eur
        row.max_price_warm_eur = max_price_warm_eur
        if area is not housing_area.KEEP:
            row.area = housing_area.dump_area(area)
        if wbs is not housing_area.KEEP:
            row.wbs = wbs or None
        session.flush()
        filter_data = filter_to_dict(row)
        now = utc_now()
        session.query(InberlinDelivery).filter(InberlinDelivery.filter_id == int(filter_id)).delete()
        for listing_row in session.query(InberlinListing).filter(
            InberlinListing.is_active.is_(True)
        ).all():
            listing = listing_to_dict(listing_row)
            if inberlin_matching.matches_filter(listing, filter_data):
                session.add(InberlinDelivery(
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
        query = session.query(InberlinFilter)
        if user_id is not None:
            query = query.filter(InberlinFilter.user_id == int(user_id))
        if active_only:
            query = query.filter(InberlinFilter.active.is_(True))
        return [filter_to_dict(row) for row in query.order_by(InberlinFilter.filter_id.asc()).all()]
    finally:
        session.close()


def set_filter_active(filter_id: int, active: bool, user_id: Optional[int] = None) -> bool:
    session = DBSession()
    try:
        query = session.query(InberlinFilter).filter(InberlinFilter.filter_id == int(filter_id))
        if user_id is not None:
            query = query.filter(InberlinFilter.user_id == int(user_id))
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
        query = session.query(InberlinFilter).filter(InberlinFilter.filter_id == int(filter_id))
        if user_id is not None:
            query = query.filter(InberlinFilter.user_id == int(user_id))
        row = query.first()
        if not row:
            return False
        session.query(InberlinDelivery).filter(
            InberlinDelivery.filter_id == int(filter_id)
        ).delete(synchronize_session=False)
        session.delete(row)
        session.commit()
        return True
    finally:
        session.close()


def known_keys() -> Set[str]:
    session = DBSession()
    try:
        return {str(row.listing_key) for row in session.query(InberlinListing.listing_key).all()}
    finally:
        session.close()


def upsert_listings(listings: Iterable[Dict], deactivate_missing: bool = False) -> int:
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
            row = session.query(InberlinListing).filter(InberlinListing.listing_key == key).first()
            if row is None:
                row = InberlinListing(listing_key=key, first_seen_at=now)
                session.add(row)
            row.title = listing.get("title") or "Wohnung"
            row.plz = listing.get("plz")
            row.company = listing.get("company")
            row.wbs = listing.get("wbs")
            row.available_from = listing.get("available_from")
            row.address = listing.get("address")
            row.rooms = listing.get("rooms")
            row.area_m2 = listing.get("area_m2")
            row.price_eur = listing.get("price_eur")
            if listing.get("price_warm_eur") is not None:
                row.price_warm_eur = listing.get("price_warm_eur")
            gallery = [str(url).strip() for url in (listing.get("gallery_urls") or []) if str(url).strip()]
            if gallery:
                row.gallery_urls = "\n".join(gallery)
            row.detail_url = listing.get("detail_url")
            row.cover_image_url = listing.get("cover_image_url")
            row.last_seen_at = now
            row.is_active = True
            count += 1
        # Знятими робить лише повний обхід: звичайний читає тільки нові сторінки.
        if deactivate_missing and seen:
            session.query(InberlinListing).filter(
                InberlinListing.is_active.is_(True), ~InberlinListing.listing_key.in_(seen),
            ).update({"is_active": False}, synchronize_session=False)
        session.commit()
        return count
    finally:
        session.close()


def list_active_listings() -> List[Dict]:
    session = DBSession()
    try:
        rows = session.query(InberlinListing).filter(InberlinListing.is_active.is_(True)).all()
        return [listing_to_dict(row) for row in rows]
    finally:
        session.close()


def list_active_listings_since(cutoff: datetime) -> List[Dict]:
    """Active listings first observed at or after `cutoff` — powers the "show
    me what appeared in the last hour/day" offer right after a filter is
    created, which deliberately bypasses the create-time baseline."""
    session = DBSession()
    try:
        rows = session.query(InberlinListing).filter(
            InberlinListing.is_active.is_(True), InberlinListing.first_seen_at >= cutoff
        ).all()
        return [listing_to_dict(row) for row in rows]
    finally:
        session.close()


def delivered_pairs() -> Set[Tuple[int, str]]:
    session = DBSession()
    try:
        return {(int(row.filter_id), str(row.listing_key)) for row in session.query(InberlinDelivery).all()}
    finally:
        session.close()


def mark_delivered(filter_id: int, listing_key: str) -> None:
    session = DBSession()
    try:
        exists = session.query(InberlinDelivery).filter(
            InberlinDelivery.filter_id == int(filter_id),
            InberlinDelivery.listing_key == str(listing_key),
        ).first()
        if not exists:
            session.add(InberlinDelivery(filter_id=int(filter_id), listing_key=str(listing_key), sent_at=utc_now()))
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
            if inberlin_matching.matches_filter(listing, filt):
                matches.append((filt, listing))
    return matches


def record_status(status: str, listings_count: int = 0, error: str = "") -> None:
    record_status_for(STATUS_KEY, status, listings_count=listings_count, error=error)


def record_status_for(key: str, status: str, listings_count: int = 0, error: str = "") -> None:
    """Окремі рядки статусу: «global» — кожен обхід, «full» — останній повний."""
    session = DBSession()
    try:
        row = session.query(InberlinStatus).filter(InberlinStatus.key == key).first()
        if row is None:
            row = InberlinStatus(key=key)
            session.add(row)
        row.last_checked_at = utc_now()
        row.last_status = status
        row.last_error = str(error or "")[:500]
        row.listings_count = int(listings_count or 0)
        session.commit()
    finally:
        session.close()


def latest_status() -> Dict:
    return latest_status_for(STATUS_KEY)


def latest_status_for(key: str) -> Dict:
    session = DBSession()
    try:
        row = session.query(InberlinStatus).filter(InberlinStatus.key == key).first()
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
