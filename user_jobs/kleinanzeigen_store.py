"""Persistent operations for Kleinanzeigen filters, listings, deliveries, and status.

Mirrors `propotsdam_store.py` closely, minus everything district-related — the
source has no reliable Stadtteil vocabulary to filter on, so a filter here is
just rooms/area/price bounds.
"""

from datetime import datetime
from typing import Dict, Iterable, List, Optional, Set, Tuple

from database import (
    DBSession,
    KleinanzeigenDelivery,
    KleinanzeigenFilter,
    KleinanzeigenListing,
    KleinanzeigenStatus,
)
from user_jobs import kleinanzeigen_matching, kleinanzeigen_parser

STATUS_KEY = "global"


def utc_now() -> datetime:
    return datetime.utcnow()


def parse_optional_number(text) -> Optional[float]:
    return kleinanzeigen_parser.parse_decimal(text)


def filter_to_dict(row: KleinanzeigenFilter) -> Dict:
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
        "active": row.active,
    }


def listing_to_dict(row: KleinanzeigenListing) -> Dict:
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
) -> int:
    session = DBSession()
    try:
        row = KleinanzeigenFilter(
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
            active=True,
            created_at=utc_now(),
        )
        session.add(row)
        session.flush()
        filter_id = int(row.filter_id)
        filter_data = filter_to_dict(row)
        now = utc_now()
        for listing_row in session.query(KleinanzeigenListing).filter(
            KleinanzeigenListing.is_active.is_(True)
        ).all():
            listing = listing_to_dict(listing_row)
            if kleinanzeigen_matching.matches_filter(listing, filter_data):
                session.add(KleinanzeigenDelivery(
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
) -> bool:
    session = DBSession()
    try:
        row = session.query(KleinanzeigenFilter).filter(
            KleinanzeigenFilter.filter_id == int(filter_id),
            KleinanzeigenFilter.user_id == int(user_id),
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
        session.flush()
        filter_data = filter_to_dict(row)
        now = utc_now()
        session.query(KleinanzeigenDelivery).filter(KleinanzeigenDelivery.filter_id == int(filter_id)).delete()
        for listing_row in session.query(KleinanzeigenListing).filter(
            KleinanzeigenListing.is_active.is_(True)
        ).all():
            listing = listing_to_dict(listing_row)
            if kleinanzeigen_matching.matches_filter(listing, filter_data):
                session.add(KleinanzeigenDelivery(
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
        query = session.query(KleinanzeigenFilter)
        if user_id is not None:
            query = query.filter(KleinanzeigenFilter.user_id == int(user_id))
        if active_only:
            query = query.filter(KleinanzeigenFilter.active.is_(True))
        return [filter_to_dict(row) for row in query.order_by(KleinanzeigenFilter.filter_id.asc()).all()]
    finally:
        session.close()


def set_filter_active(filter_id: int, active: bool, user_id: Optional[int] = None) -> bool:
    session = DBSession()
    try:
        query = session.query(KleinanzeigenFilter).filter(KleinanzeigenFilter.filter_id == int(filter_id))
        if user_id is not None:
            query = query.filter(KleinanzeigenFilter.user_id == int(user_id))
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
        query = session.query(KleinanzeigenFilter).filter(KleinanzeigenFilter.filter_id == int(filter_id))
        if user_id is not None:
            query = query.filter(KleinanzeigenFilter.user_id == int(user_id))
        row = query.first()
        if not row:
            return False
        session.query(KleinanzeigenDelivery).filter(
            KleinanzeigenDelivery.filter_id == int(filter_id)
        ).delete(synchronize_session=False)
        session.delete(row)
        session.commit()
        return True
    finally:
        session.close()


def keys_already_enriched() -> Set[str]:
    """Оголошення, для яких зі сторінки вже забрано все потрібне.

    Саме «все», а не «повну ціну»: спершу тут стояла перевірка лише на ціну,
    і оголошення, збережені до появи галереї, лишились би з однією обкладинкою
    назавжди — ціна в них уже є, тож на сторінку по фото ніхто б не пішов.
    """
    session = DBSession()
    try:
        rows = session.query(
            KleinanzeigenListing.listing_key,
            KleinanzeigenListing.price_warm_eur,
            KleinanzeigenListing.gallery_urls,
        ).all()
        return {
            str(row.listing_key)
            for row in rows
            if row.price_warm_eur is not None and str(row.gallery_urls or "").strip()
        }
    finally:
        session.close()


def upsert_listings(listings: Iterable[Dict]) -> int:
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
            row = session.query(KleinanzeigenListing).filter(KleinanzeigenListing.listing_key == key).first()
            if row is None:
                row = KleinanzeigenListing(listing_key=key, first_seen_at=now)
                session.add(row)
            row.title = listing.get("title") or "Wohnung"
            row.address = listing.get("address")
            row.rooms = listing.get("rooms")
            row.area_m2 = listing.get("area_m2")
            row.price_eur = listing.get("price_eur")
            if listing.get("price_warm_eur") is not None:
                row.price_warm_eur = listing.get("price_warm_eur")
            gallery = [str(url).strip() for url in (listing.get("gallery_urls") or []) if str(url).strip()]
            # Галерея приходить лише зі сторінки оголошення, тож черговий
            # обхід списку не має її стирати.
            if gallery:
                row.gallery_urls = "\n".join(gallery)
            row.detail_url = listing.get("detail_url")
            row.cover_image_url = listing.get("cover_image_url")
            row.last_seen_at = now
            row.is_active = True
            count += 1
        if seen:
            session.query(KleinanzeigenListing).filter(~KleinanzeigenListing.listing_key.in_(seen)).update(
                {"is_active": False}, synchronize_session=False
            )
        else:
            session.query(KleinanzeigenListing).filter(KleinanzeigenListing.is_active.is_(True)).update(
                {"is_active": False}, synchronize_session=False
            )
        session.commit()
        return count
    finally:
        session.close()


def list_active_listings() -> List[Dict]:
    session = DBSession()
    try:
        rows = session.query(KleinanzeigenListing).filter(KleinanzeigenListing.is_active.is_(True)).all()
        return [listing_to_dict(row) for row in rows]
    finally:
        session.close()


def list_active_listings_since(cutoff: datetime) -> List[Dict]:
    """Active listings first observed at or after `cutoff` — powers the "show
    me what appeared in the last hour/day" offer right after a filter is
    created, which deliberately bypasses the create-time baseline that
    otherwise hides everything already in the catalog."""
    session = DBSession()
    try:
        rows = session.query(KleinanzeigenListing).filter(
            KleinanzeigenListing.is_active.is_(True), KleinanzeigenListing.first_seen_at >= cutoff
        ).all()
        return [listing_to_dict(row) for row in rows]
    finally:
        session.close()


def delivered_pairs() -> Set[Tuple[int, str]]:
    session = DBSession()
    try:
        return {(int(row.filter_id), str(row.listing_key)) for row in session.query(KleinanzeigenDelivery).all()}
    finally:
        session.close()


def mark_delivered(filter_id: int, listing_key: str) -> None:
    session = DBSession()
    try:
        exists = session.query(KleinanzeigenDelivery).filter(
            KleinanzeigenDelivery.filter_id == int(filter_id),
            KleinanzeigenDelivery.listing_key == str(listing_key),
        ).first()
        if not exists:
            session.add(KleinanzeigenDelivery(filter_id=int(filter_id), listing_key=str(listing_key), sent_at=utc_now()))
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
            if kleinanzeigen_matching.matches_filter(listing, filt):
                matches.append((filt, listing))
    return matches


def record_status(status: str, listings_count: int = 0, error: str = "") -> None:
    session = DBSession()
    try:
        row = session.query(KleinanzeigenStatus).filter(KleinanzeigenStatus.key == STATUS_KEY).first()
        if row is None:
            row = KleinanzeigenStatus(key=STATUS_KEY)
            session.add(row)
        row.last_checked_at = utc_now()
        row.last_status = status
        row.last_error = str(error or "")[:500]
        row.listings_count = int(listings_count or 0)
        session.commit()
    finally:
        session.close()


def latest_status() -> Dict:
    session = DBSession()
    try:
        row = session.query(KleinanzeigenStatus).filter(KleinanzeigenStatus.key == STATUS_KEY).first()
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
