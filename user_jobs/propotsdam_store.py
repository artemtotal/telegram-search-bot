"""Persistent operations for ProPotsdam filters, listings, deliveries, and status."""

from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Set, Tuple

from database import (
    DBSession,
    ProPotsdamDelivery,
    ProPotsdamFilter,
    ProPotsdamListing,
    ProPotsdamStatus,
)
from user_jobs import propotsdam_matching, propotsdam_parser

ALL_DISTRICTS_WORDS = {"", "-", "all", "alle", "всі", "все", "усі", "любой", "любые"}
STATUS_KEY = "global"


def utc_now() -> datetime:
    return datetime.utcnow()


def parse_optional_number(text) -> Optional[float]:
    return propotsdam_parser.parse_decimal(text)


def normalize_districts(text: str) -> str:
    raw = str(text or "").strip()
    if raw.casefold() in ALL_DISTRICTS_WORDS:
        return ""
    result = []
    seen = set()
    for part in raw.split(","):
        district = " ".join(part.strip().split())
        key = district.casefold()
        if district and key not in seen:
            seen.add(key)
            result.append(district)
    return ",".join(result)


def filter_to_dict(row: ProPotsdamFilter) -> Dict:
    return {
        "filter_id": row.filter_id,
        "user_id": row.user_id,
        "title": row.title,
        "districts": row.districts or "",
        "min_rooms": row.min_rooms,
        "max_rooms": row.max_rooms,
        "min_area_m2": row.min_area_m2,
        "max_area_m2": row.max_area_m2,
        "min_total_rent_eur": row.min_total_rent_eur,
        "max_total_rent_eur": row.max_total_rent_eur,
        "active": row.active,
    }


def listing_to_dict(row: ProPotsdamListing) -> Dict:
    base = propotsdam_parser.normalize_listing({
        "listing_key": row.listing_key,
        "title": row.title,
        "address": row.address,
        "district": row.district,
        "rooms": row.rooms,
        "area_m2": row.area_m2,
        "total_rent_eur": row.total_rent_eur,
        "available_from": row.available_from,
        "detail_url": row.detail_url,
        "image_url": row.image_url,
    })
    try:
        import json
        raw = json.loads(row.raw_json or "{}")
        if isinstance(raw, dict) and isinstance(raw.get("extra"), dict):
            base["extra"] = raw["extra"]
    except Exception:
        pass
    return base


def create_filter(
    user_id: int,
    title: str,
    districts: str = "",
    min_rooms: Optional[float] = None,
    max_rooms: Optional[float] = None,
    min_area_m2: Optional[float] = None,
    max_area_m2: Optional[float] = None,
    min_total_rent_eur: Optional[float] = None,
    max_total_rent_eur: Optional[float] = None,
) -> int:
    session = DBSession()
    try:
        row = ProPotsdamFilter(
            user_id=int(user_id),
            title=str(title)[:120],
            districts=normalize_districts(districts),
            min_rooms=min_rooms,
            max_rooms=max_rooms,
            min_area_m2=min_area_m2,
            max_area_m2=max_area_m2,
            min_total_rent_eur=min_total_rent_eur,
            max_total_rent_eur=max_total_rent_eur,
            active=True,
            created_at=utc_now(),
        )
        session.add(row)
        session.flush()
        filter_id = int(row.filter_id)
        filter_data = filter_to_dict(row)
        now = utc_now()
        for listing_row in session.query(ProPotsdamListing).filter(
            ProPotsdamListing.is_active.is_(True)
        ).all():
            listing = listing_to_dict(listing_row)
            if propotsdam_matching.matches_filter(listing, filter_data):
                session.add(ProPotsdamDelivery(
                    filter_id=filter_id,
                    listing_key=str(listing["listing_key"]),
                    sent_at=now,
                ))
        session.commit()
        return filter_id
    finally:
        session.close()


def list_filters(user_id: Optional[int] = None, active_only: bool = False) -> List[Dict]:
    session = DBSession()
    try:
        query = session.query(ProPotsdamFilter)
        if user_id is not None:
            query = query.filter(ProPotsdamFilter.user_id == int(user_id))
        if active_only:
            query = query.filter(ProPotsdamFilter.active.is_(True))
        return [filter_to_dict(row) for row in query.order_by(ProPotsdamFilter.filter_id.asc()).all()]
    finally:
        session.close()


def set_filter_active(filter_id: int, active: bool) -> bool:
    session = DBSession()
    try:
        row = session.query(ProPotsdamFilter).filter(ProPotsdamFilter.filter_id == int(filter_id)).first()
        if not row:
            return False
        row.active = bool(active)
        session.commit()
        return True
    finally:
        session.close()


def upsert_listings(listings: Iterable[Dict]) -> int:
    session = DBSession()
    try:
        now = utc_now()
        count = 0
        seen = set()
        for raw in listings:
            listing = propotsdam_parser.normalize_listing(raw)
            key = listing["listing_key"]
            seen.add(key)
            row = session.query(ProPotsdamListing).filter(ProPotsdamListing.listing_key == key).first()
            if row is None:
                row = ProPotsdamListing(listing_key=key, first_seen_at=now)
                session.add(row)
            row.title = listing.get("title") or "ProPotsdam Wohnung"
            row.address = listing.get("address")
            row.district = listing.get("district")
            row.rooms = listing.get("rooms")
            row.area_m2 = listing.get("area_m2")
            row.total_rent_eur = listing.get("total_rent_eur")
            row.available_from = listing.get("available_from")
            row.detail_url = listing.get("detail_url")
            row.image_url = listing.get("image_url")
            row.raw_json = propotsdam_parser.dumps_raw(listing)
            row.last_seen_at = now
            row.is_active = True
            count += 1
        if seen:
            session.query(ProPotsdamListing).filter(~ProPotsdamListing.listing_key.in_(seen)).update({"is_active": False}, synchronize_session=False)
        session.commit()
        return count
    finally:
        session.close()


def list_active_listings() -> List[Dict]:
    session = DBSession()
    try:
        rows = session.query(ProPotsdamListing).filter(ProPotsdamListing.is_active.is_(True)).all()
        return [listing_to_dict(row) for row in rows]
    finally:
        session.close()


def delivered_pairs() -> Set[Tuple[int, str]]:
    session = DBSession()
    try:
        return {(int(row.filter_id), str(row.listing_key)) for row in session.query(ProPotsdamDelivery).all()}
    finally:
        session.close()


def mark_delivered(filter_id: int, listing_key: str) -> None:
    session = DBSession()
    try:
        exists = session.query(ProPotsdamDelivery).filter(
            ProPotsdamDelivery.filter_id == int(filter_id),
            ProPotsdamDelivery.listing_key == str(listing_key),
        ).first()
        if not exists:
            session.add(ProPotsdamDelivery(filter_id=int(filter_id), listing_key=str(listing_key), sent_at=utc_now()))
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
            if propotsdam_matching.matches_filter(listing, filt):
                matches.append((filt, listing))
    return matches


def record_status(status: str, listings_count: int = 0, error: str = "") -> None:
    session = DBSession()
    try:
        row = session.query(ProPotsdamStatus).filter(ProPotsdamStatus.key == STATUS_KEY).first()
        if row is None:
            row = ProPotsdamStatus(key=STATUS_KEY)
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
        row = session.query(ProPotsdamStatus).filter(ProPotsdamStatus.key == STATUS_KEY).first()
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
