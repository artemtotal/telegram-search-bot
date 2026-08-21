"""Per-user bot preferences — currently just the chosen UI language."""

from datetime import datetime

from database import DBSession, UserSettings

DEFAULT_LANGUAGE = "uk"


def utc_now() -> datetime:
    return datetime.utcnow()


def get_language(user_id: int) -> str:
    session = DBSession()
    try:
        row = session.query(UserSettings).filter(UserSettings.user_id == int(user_id)).first()
        return row.language if row else DEFAULT_LANGUAGE
    finally:
        session.close()


def set_language(user_id: int, language: str) -> None:
    session = DBSession()
    try:
        row = session.query(UserSettings).filter(UserSettings.user_id == int(user_id)).first()
        if row is None:
            row = UserSettings(user_id=int(user_id), language=language, updated_at=utc_now())
            session.add(row)
        else:
            row.language = language
            row.updated_at = utc_now()
        session.commit()
    finally:
        session.close()
