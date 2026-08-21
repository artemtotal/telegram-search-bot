"""Per-user subscriptions for the housing-cooperative watchdogs (Gewoba, WBG
1903, WBG "Daheim"). See CoopWatchdogFilter's docstring in database.py for
why this has no rooms/price/area criteria - there's nothing to match yet."""

from datetime import datetime
from typing import Dict, List, Optional

from database import CoopWatchdogFilter, CoopWatchdogStatus, DBSession


def utc_now() -> datetime:
    return datetime.utcnow()


def filter_to_dict(row: CoopWatchdogFilter) -> Dict:
    return {
        "filter_id": int(row.filter_id),
        "user_id": int(row.user_id),
        "coop_key": row.coop_key,
        "title": row.title,
        "active": bool(row.active),
    }


def create_filter(user_id: int, coop_key: str, title: str) -> int:
    """Subscribes (or re-activates an existing, previously-paused
    subscription to) this cooperative for this user."""
    session = DBSession()
    try:
        row = (
            session.query(CoopWatchdogFilter)
            .filter(CoopWatchdogFilter.user_id == int(user_id), CoopWatchdogFilter.coop_key == coop_key)
            .first()
        )
        if row is None:
            row = CoopWatchdogFilter(
                user_id=int(user_id), coop_key=coop_key, title=title,
                active=True, created_at=utc_now(),
            )
            session.add(row)
        else:
            row.active = True
            row.title = title
        session.commit()
        return int(row.filter_id)
    finally:
        session.close()


def list_filters(user_id: Optional[int] = None, active_only: bool = False) -> List[Dict]:
    session = DBSession()
    try:
        query = session.query(CoopWatchdogFilter)
        if user_id is not None:
            query = query.filter(CoopWatchdogFilter.user_id == int(user_id))
        if active_only:
            query = query.filter(CoopWatchdogFilter.active.is_(True))
        return [filter_to_dict(row) for row in query.order_by(CoopWatchdogFilter.filter_id.asc()).all()]
    finally:
        session.close()


def set_filter_active(user_id: int, coop_key: str, active: bool) -> bool:
    session = DBSession()
    try:
        row = (
            session.query(CoopWatchdogFilter)
            .filter(CoopWatchdogFilter.user_id == int(user_id), CoopWatchdogFilter.coop_key == coop_key)
            .first()
        )
        if row is None:
            return False
        row.active = bool(active)
        session.commit()
        return True
    finally:
        session.close()


def get_status(coop_key: str) -> Dict:
    """Read-side counterpart to coop_watchdog.check_job's direct writes to
    CoopWatchdogStatus - lets housing_monitor's status screen show the same
    freshness info without importing the raw model itself."""
    session = DBSession()
    try:
        row = session.query(CoopWatchdogStatus).filter(CoopWatchdogStatus.key == coop_key).first()
        if not row:
            return {}
        return {
            "last_checked_at": row.last_checked_at,
            "last_status": row.last_status,
            "last_error": row.last_error,
            "was_empty": row.was_empty,
        }
    finally:
        session.close()


def list_subscriber_ids(coop_key: str) -> List[int]:
    """Active subscribers to notify when this cooperative's page flips from
    empty to not-empty (see coop_watchdog.check_job)."""
    session = DBSession()
    try:
        rows = (
            session.query(CoopWatchdogFilter.user_id)
            .filter(CoopWatchdogFilter.coop_key == coop_key, CoopWatchdogFilter.active.is_(True))
            .all()
        )
        return [int(user_id) for (user_id,) in rows]
    finally:
        session.close()
