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
from database import DBSession, HousingLetterProfile, HousingLetterRef, HousingLetterLog

logger = logging.getLogger(__name__)

PROFILE_FIELDS = ("full_name", "household", "income", "move_in", "contact", "about")
OPTIONAL_FIELDS = ("contact", "about")
START_PREFIX = "lt"

_bot_username: Optional[str] = os.getenv("BOT_USERNAME") or None


def log_letter(user_id: int, source: str, listing_key: str, title: str, url: str, letter: str) -> None:
    """Історія «куди я писав» для сайту підписника (02.11.2026)."""
    session = DBSession()
    try:
        session.add(HousingLetterLog(
            user_id=int(user_id), source=str(source), listing_key=str(listing_key), title=str(title or "")[:300],
            url=str(url or "")[:500], letter=str(letter or "")[:6000], created_at=utc_now(),
        ))
        session.commit()
    finally:
        session.close()


def list_letters(user_id: int, limit: int = 50) -> list:
    session = DBSession()
    try:
        rows = (session.query(HousingLetterLog).filter(HousingLetterLog.user_id == int(user_id))
                .order_by(HousingLetterLog.created_at.desc()).limit(int(limit)).all())
        return [{"source": row.source, "listing_key": row.listing_key, "title": row.title, "url": row.url,
                 "letter": row.letter, "created_at": row.created_at} for row in rows]
    finally:
        session.close()


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


def profile_complete(profile: Optional[Dict[str, str]]) -> bool:
    """Анкета листів готова до листа: усі обов'язкові поля заповнені (частково заповнена з папки заявника не рахується)."""
    return bool(profile) and all(profile.get(f) for f in PROFILE_FIELDS if f not in OPTIONAL_FIELDS)


def save_profile(user_id: int, answers: Dict[str, str], sync: bool = True) -> None:
    old = get_profile(user_id) if sync else None
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
    if sync:        # спільні поля -> анкета папки заявника (одне правило для чату, сайту й мосту)
        try:
            from user_jobs import housing_profile_sync

            housing_profile_sync.after_letter_save(int(user_id), old, get_profile(user_id) or {})
        except Exception:
            logger.exception("Could not sync the letter profile of %s to the mappe", user_id)


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


def letter_href(username: str, ref_id: int) -> str:
    """Адрес ссылки «✍️ Лист»: раздел письма сайта внутри Telegram (28.11.2026, H5a; t.me/Potsdambot/potsdam?startapp=go_letter_<ref>),
    а без ссылки Mini App в настройках, как раньше, старт бота ?start=lt<ref>."""
    try:
        from user_handlers import masters_hint

        if masters_hint.SITE_BUTTONS:
            link = masters_hint.app_url(f"go_letter_{int(ref_id)}")
            if link:
                return link
    except Exception:
        logger.exception("Could not make the site letter link")
    return f"https://t.me/{username}?start={START_PREFIX}{ref_id}"


def letter_link(user_id: int, source: str, listing_key: str, premium: bool) -> str:
    """The line under a flat: a deep link that opens the letter for it.
    Empty when the bot's username isn't known yet (no broken link)."""
    if not _bot_username or not listing_key:
        return ""
    try:
        # «✍️ Лист · ✅ Подався» одним рядком (HOUSING_APPLY_TRACKER=1).
        from user_handlers import housing_tracker

        if housing_tracker.enabled():
            line = housing_tracker.footer_line(user_id, source, listing_key, premium)
            if line:
                return line
    except Exception:
        logger.exception("Tracker footer failed, using the letter link only")
    ref_id = ref_for(source, listing_key)
    if ref_id is None:
        return ""
    lang = i18n.get_lang(int(user_id))
    label = i18n.t("housing.letter.link_paid" if premium else "housing.letter.link_free", lang)
    return f'\n\n<a href="{letter_href(_bot_username, ref_id)}">{label}</a>'
