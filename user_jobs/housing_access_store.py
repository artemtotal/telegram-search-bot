"""Persistent allow-list for housing monitoring self-service users."""

from datetime import datetime

from database import DBSession, HousingAccessUser


def utc_now() -> datetime:
    return datetime.utcnow()


def grant_access(user_id: int, display_name: str = "") -> None:
    session = DBSession()
    try:
        row = session.query(HousingAccessUser).get(int(user_id))
        now = utc_now()
        if row is None:
            row = HousingAccessUser(
                user_id=int(user_id),
                display_name=str(display_name or "")[:120],
                active=True,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
        else:
            row.display_name = str(display_name or row.display_name or "")[:120]
            row.active = True
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
            }
            for row in query.order_by(HousingAccessUser.user_id.asc()).all()
        ]
    finally:
        session.close()