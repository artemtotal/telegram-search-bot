"""Milestones of one person's way through housing monitoring.

Only what the one-off follow-ups need: when they first opened the section
(for the "you haven't made a filter yet" nudge), when they made their first
filter (the survey goes two weeks after that), whether each follow-up was
already sent, and the Jobcenter household size they gave the wizard.
"""

from datetime import datetime, timedelta
from typing import List, Optional, Set

from database import DBSession, HousingUserJourney


def utc_now() -> datetime:
    return datetime.utcnow()


def _row(session, user_id: int) -> HousingUserJourney:
    row = session.query(HousingUserJourney).get(int(user_id))
    if row is None:
        row = HousingUserJourney(user_id=int(user_id))
        session.add(row)
    return row


def touch_menu(user_id: int) -> None:
    session = DBSession()
    try:
        row = _row(session, user_id)
        if row.first_menu_at is None:
            row.first_menu_at = utc_now()
            session.commit()
    finally:
        session.close()


def mark_first_filter(user_id: int) -> bool:
    """True only the first time - i.e. this was the person's first filter."""
    session = DBSession()
    try:
        row = _row(session, user_id)
        if row.first_filter_at is not None:
            return False
        row.first_filter_at = utc_now()
        if row.first_menu_at is None:
            row.first_menu_at = row.first_filter_at
        session.commit()
        return True
    finally:
        session.close()


def list_due_no_filter_nudges(after: timedelta, not_before: datetime) -> List[int]:
    """Opened the section at least `after` ago (but not before `not_before`,
    so people from long ago aren't nudged out of the blue) and still no
    filter."""
    session = DBSession()
    try:
        now = utc_now()
        rows = session.query(HousingUserJourney).filter(
            HousingUserJourney.first_filter_at.is_(None),
            HousingUserJourney.no_filter_nudge_sent_at.is_(None),
            HousingUserJourney.first_menu_at.isnot(None),
            HousingUserJourney.first_menu_at <= now - after,
            HousingUserJourney.first_menu_at >= not_before,
        ).all()
        return [int(row.user_id) for row in rows]
    finally:
        session.close()


def mark_no_filter_nudge_sent(user_id: int) -> None:
    session = DBSession()
    try:
        _row(session, user_id).no_filter_nudge_sent_at = utc_now()
        session.commit()
    finally:
        session.close()


def list_due_surveys(after: timedelta) -> List[int]:
    session = DBSession()
    try:
        rows = session.query(HousingUserJourney).filter(
            HousingUserJourney.first_filter_at.isnot(None),
            HousingUserJourney.first_filter_at <= utc_now() - after,
            HousingUserJourney.survey_sent_at.is_(None),
        ).all()
        return [int(row.user_id) for row in rows]
    finally:
        session.close()


def mark_survey_sent(user_id: int) -> None:
    session = DBSession()
    try:
        _row(session, user_id).survey_sent_at = utc_now()
        session.commit()
    finally:
        session.close()


def save_survey_answer(user_id: int, answer: str) -> bool:
    """False if they already answered - one answer per person."""
    session = DBSession()
    try:
        row = _row(session, user_id)
        if row.survey_answer:
            return False
        row.survey_answer = str(answer)[:64]
        if row.survey_sent_at is None:
            row.survey_sent_at = utc_now()
        session.commit()
        return True
    finally:
        session.close()


def set_digest_off(user_id: int) -> None:
    session = DBSession()
    try:
        _row(session, user_id).digest_off = True
        session.commit()
    finally:
        session.close()


def digest_off_ids() -> Set[int]:
    session = DBSession()
    try:
        return {
            int(row.user_id)
            for row in session.query(HousingUserJourney.user_id).filter(HousingUserJourney.digest_off.is_(True))
        }
    finally:
        session.close()


def set_household(user_id: int, persons: Optional[int]) -> None:
    session = DBSession()
    try:
        _row(session, user_id).jobcenter_household = int(persons) if persons else None
        session.commit()
    finally:
        session.close()


def get_household(user_id: int) -> Optional[int]:
    session = DBSession()
    try:
        row = session.query(HousingUserJourney).get(int(user_id))
        return int(row.jobcenter_household) if row and row.jobcenter_household else None
    finally:
        session.close()
