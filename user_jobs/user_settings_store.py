"""Per-user bot preferences — UI language and the news-broadcast opt-out."""

from datetime import datetime

from database import DBSession, UserSettings

DEFAULT_LANGUAGE = "uk"


def utc_now() -> datetime:
    return datetime.utcnow()


def get_language(user_id: int) -> str:
    """Also doubles as the "have we seen this user before" registration
    point: a missing row gets created here (news_subscribed defaults to
    True), since this is called on nearly every private screen already —
    see UserSettings' docstring for why that matters for the broadcast."""
    session = DBSession()
    try:
        uid = int(user_id)
        row = session.query(UserSettings).filter(UserSettings.user_id == uid).first()
        if row is not None:
            return row.language
        session.add(UserSettings(user_id=uid, language=DEFAULT_LANGUAGE, news_subscribed=True, updated_at=utc_now()))
        session.commit()
        return DEFAULT_LANGUAGE
    finally:
        session.close()


def set_language(user_id: int, language: str) -> None:
    session = DBSession()
    try:
        row = session.query(UserSettings).filter(UserSettings.user_id == int(user_id)).first()
        if row is None:
            row = UserSettings(user_id=int(user_id), language=language, news_subscribed=True, updated_at=utc_now())
            session.add(row)
        else:
            row.language = language
            row.updated_at = utc_now()
        session.commit()
    finally:
        session.close()


def get_news_subscribed(user_id: int) -> bool:
    session = DBSession()
    try:
        row = session.query(UserSettings).filter(UserSettings.user_id == int(user_id)).first()
        return bool(row.news_subscribed) if row is not None else True
    finally:
        session.close()


def set_news_subscribed(user_id: int, subscribed: bool) -> None:
    session = DBSession()
    try:
        uid = int(user_id)
        row = session.query(UserSettings).filter(UserSettings.user_id == uid).first()
        if row is None:
            row = UserSettings(
                user_id=uid, language=DEFAULT_LANGUAGE, news_subscribed=bool(subscribed), updated_at=utc_now(),
            )
            session.add(row)
        else:
            row.news_subscribed = bool(subscribed)
            row.updated_at = utc_now()
        session.commit()
    finally:
        session.close()


def list_subscribed_user_ids() -> list:
    """Recipients for the admin broadcast: every known private-chat user
    (see get_language) who hasn't opted out."""
    session = DBSession()
    try:
        rows = session.query(UserSettings.user_id).filter(UserSettings.news_subscribed.is_(True)).all()
        return [int(row[0]) for row in rows]
    finally:
        session.close()
