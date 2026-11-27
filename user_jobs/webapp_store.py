"""Хто дозволив боту писати йому через Mini App каталогу (27.11.2026). Лише позначка, без текстів і імен."""

from datetime import datetime

from database import DBSession, WebappUser


def utc_now() -> datetime:
    return datetime.utcnow()


def mark_write_allowed(user_id: int, source: str = "write_access_allowed", app_name: str = "") -> bool:
    """True — запис новий (людина вперше дозволила), False — вже була."""
    session = DBSession()
    try:
        uid = int(user_id)
        now = utc_now()
        row = session.query(WebappUser).filter(WebappUser.user_id == uid).first()
        if row is None:
            session.add(WebappUser(user_id=uid, write_ok=True, source=source, app_name=app_name[:64] if app_name else None,
                                   first_at=now, updated_at=now))
            created = True
        else:
            row.write_ok = True
            row.source = source
            if app_name:
                row.app_name = app_name[:64]
            row.updated_at = now
            created = False
        session.commit()
        return created
    finally:
        session.close()


def is_write_allowed(user_id: int) -> bool:
    session = DBSession()
    try:
        row = session.query(WebappUser).filter(WebappUser.user_id == int(user_id)).first()
        return bool(row and row.write_ok)
    finally:
        session.close()


def count_allowed() -> int:
    session = DBSession()
    try:
        return session.query(WebappUser).filter(WebappUser.write_ok.is_(True)).count()
    finally:
        session.close()
