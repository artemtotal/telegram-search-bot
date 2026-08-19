"""Persistent allow-list for housing monitoring self-service users."""

from datetime import datetime, timedelta
from typing import Optional

from database import DBSession, HousingAccessUser


def utc_now() -> datetime:
    return datetime.utcnow()


def grant_access(user_id: int, display_name: str = "", expires_at: Optional[datetime] = None) -> None:
    """(Re)opens access, optionally until `expires_at`.

    Also used for renewals: granting again resets `expiry_notice_sent`, so a
    user who renews after getting the 3-day warning doesn't fall straight
    back into the expired list on the new expiry date without a fresh one.
    """
    session = DBSession()
    try:
        row = session.query(HousingAccessUser).get(int(user_id))
        now = utc_now()
        if row is None:
            row = HousingAccessUser(
                user_id=int(user_id),
                display_name=str(display_name or "")[:120],
                active=True,
                expires_at=expires_at,
                expiry_notice_sent=False,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
        else:
            row.display_name = str(display_name or row.display_name or "")[:120]
            row.active = True
            row.expires_at = expires_at
            row.expiry_notice_sent = False
            row.updated_at = now
        session.commit()
    finally:
        session.close()


def is_allowed(user_id: int) -> bool:
    session = DBSession()
    try:
        row = session.query(HousingAccessUser).get(int(user_id))
        return bool(row and row.active)
    finally:
        session.close()


def set_active(user_id: int, active: bool) -> bool:
    session = DBSession()
    try:
        row = session.query(HousingAccessUser).get(int(user_id))
        if row is None:
            return False
        row.active = bool(active)
        row.updated_at = utc_now()
        session.commit()
        return True
    finally:
        session.close()


def revoke_access(user_id: int) -> bool:
    session = DBSession()
    try:
        row = session.query(HousingAccessUser).get(int(user_id))
        if row is None:
            return False
        session.delete(row)
        session.commit()
        return True
    finally:
        session.close()


def list_users(active_only: bool = False) -> list:
    session = DBSession()
    try:
        query = session.query(HousingAccessUser)
        if active_only:
            query = query.filter(HousingAccessUser.active.is_(True))
        return [
            {
                "user_id": int(row.user_id),
                "display_name": str(row.display_name or ""),
                "active": bool(row.active),
                "expires_at": row.expires_at,
            }
            for row in query.order_by(HousingAccessUser.user_id.asc()).all()
        ]
    finally:
        session.close()


def list_expiring_soon(within_days: int = 3) -> list:
    """Active users whose access expires within `within_days` and who
    haven't been warned about it yet (see `mark_notice_sent`)."""
    session = DBSession()
    try:
        cutoff = utc_now() + timedelta(days=within_days)
        rows = (
            session.query(HousingAccessUser)
            .filter(HousingAccessUser.active.is_(True))
            .filter(HousingAccessUser.expires_at.isnot(None))
            .filter(HousingAccessUser.expires_at <= cutoff)
            .filter(HousingAccessUser.expiry_notice_sent.isnot(True))
            .all()
        )
        return [
            {"user_id": int(row.user_id), "display_name": str(row.display_name or ""), "expires_at": row.expires_at}
            for row in rows
        ]
    finally:
        session.close()


def mark_notice_sent(user_id: int) -> None:
    session = DBSession()
    try:
        row = session.query(HousingAccessUser).get(int(user_id))
        if row is not None:
            row.expiry_notice_sent = True
            row.updated_at = utc_now()
            session.commit()
    finally:
        session.close()


def list_expired() -> list:
    """Active users whose expiry date has already passed."""
    session = DBSession()
    try:
        rows = (
            session.query(HousingAccessUser)
            .filter(HousingAccessUser.active.is_(True))
            .filter(HousingAccessUser.expires_at.isnot(None))
            .filter(HousingAccessUser.expires_at <= utc_now())
            .all()
        )
        return [
            {"user_id": int(row.user_id), "display_name": str(row.display_name or ""), "expires_at": row.expires_at}
            for row in rows
        ]
    finally:
        session.close()