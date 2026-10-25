"""Storage behind «✍️ Лист орендодавцю» (25.10.2026): the one-time profile a
letter is written from, and the short references the link under every flat
carries.

Flats are often sent as photo albums, and Telegram shows no buttons on an
album - so the letter is a deep link in the flat's text
(t.me/<bot>?start=lt<id>). A start parameter is at most 64 characters and
listing keys can be longer, hence a numeric reference per (source, key).
"""

import logging
import os
from datetime import datetime
from typing import Dict, Optional

import i18n
from database import DBSession, HousingLetterProfile, HousingLetterRef

logger = logging.getLogger(__name__)

PROFILE_FIELDS = ("full_name", "household", "income", "move_in", "contact", "about")
OPTIONAL_FIELDS = ("contact", "about")
START_PREFIX = "lt"

_bot_username: Optional[str] = os.getenv("BOT_USERNAME") or None


def utc_now() -> datetime:
    return datetime.utcnow()


def set_bot_username(username: Optional[str]) -> None:
    global _bot_username
    if username:
        _bot_username = str(username).lstrip("@")


def get_profile(user_id: int) -> Optional[Dict[str, str]]:
    session = DBSession()
    try:
        row = session.query(HousingLetterProfile).get(int(user_id))
        if row is None:
            return None
        return {field: getattr(row, field) or "" for field in PROFILE_FIELDS}
    finally:
        session.close()


def save_profile(user_id: int, answers: Dict[str, str]) -> None:
    session = DBSession()
    try:
        row = session.query(HousingLetterProfile).get(int(user_id))
        if row is None:
            row = HousingLetterProfile(user_id=int(user_id))
            session.add(row)
        for field in PROFILE_FIELDS:
            setattr(row, field, str(answers.get(field) or "").strip()[:500])
        row.updated_at = utc_now()
        session.commit()
    finally:
        session.close()


def ref_for(source: str, listing_key: str) -> Optional[int]:
    session = DBSession()
    try:
        row = session.query(HousingLetterRef).filter(
            HousingLetterRef.source == str(source), HousingLetterRef.listing_key == str(listing_key),
        ).first()
        if row is None:
            row = HousingLetterRef(source=str(source), listing_key=str(listing_key), created_at=utc_now())
            session.add(row)
            session.commit()
        return int(row.id)
    except Exception:
        session.rollback()
        logger.exception("Could not make a letter reference for %s %s", source, listing_key)
        return None
    finally:
        session.close()


def resolve_ref(ref_id: int) -> Optional[Dict[str, str]]:
    session = DBSession()
    try:
        row = session.query(HousingLetterRef).get(int(ref_id))
        return {"source": row.source, "listing_key": row.listing_key} if row is not None else None
    finally:
        session.close()


def letter_link(user_id: int, source: str, listing_key: str, premium: bool) -> str:
    """The line under a flat: a deep link that opens the letter for it.
    Empty when the bot's username isn't known yet (no broken link)."""
    if not _bot_username or not listing_key:
        return ""
    ref_id = ref_for(source, listing_key)
    if ref_id is None:
        return ""
    lang = i18n.get_lang(int(user_id))
    label = i18n.t("housing.letter.link_paid" if premium else "housing.letter.link_free", lang)
    return f'\n\n<a href="https://t.me/{_bot_username}?start={START_PREFIX}{ref_id}">{label}</a>'
