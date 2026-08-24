"""Persistent allow-list for housing monitoring self-service users."""

from datetime import datetime, timedelta
from typing import Optional

from database import DBSession, HousingAccessUser, HousingTrialUsed


def utc_now() -> datetime:
    return datetime.utcnow()


def grant_access(user_id: int, display_name: str = "", expires_at: Optional[datetime] = None) -> None:
    """(Re)opens access, optionally until `expires_at`.

    Also used for renewals: granting again resets `expiry_notice_sent`, so a
    user who renews after getting the 3-day warning doesn't fall straight
    back into the expired list on the new expiry date without a fresh one.

    Always marks the row as a full (non-trial) grant - this is the
    admin-approved path, so any leftover trial state (is_trial, a pending
    grace-period deadline) from a prior self-service trial no longer
    applies.
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
                is_trial=False,
                trial_grace_ends_at=None,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
        else:
            row.display_name = str(display_name or row.display_name or "")[:120]
            row.active = True
            row.expires_at = expires_at
            row.expiry_notice_sent = False
            row.is_trial = False
            row.trial_grace_ends_at = None
            row.updated_at = now
        session.commit()
    finally:
        session.close()


def has_used_trial(user_id: int) -> bool:
    session = DBSession()
    try:
        return session.query(HousingTrialUsed).get(int(user_id)) is not None
    finally:
        session.close()


def grant_trial(user_id: int, display_name: str = "", expires_at: Optional[datetime] = None) -> None:
    """Self-service, no-approval-needed grant. Callers must check
    `has_used_trial` first - this always (re)marks the trial as used so it
    can never be triggered twice for the same Telegram ID, even across a
    later `revoke_access`/`_close_access` that deletes the access row."""
    session = DBSession()
    try:
        now = utc_now()
        row = session.query(HousingAccessUser).get(int(user_id))
        if row is None:
            row = HousingAccessUser(
                user_id=int(user_id),
                display_name=str(display_name or "")[:120],
                active=True,
                expires_at=expires_at,
                expiry_notice_sent=False,
                is_trial=True,
                trial_grace_ends_at=None,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
        else:
            row.display_name = str(display_name or row.display_name or "")[:120]
            row.active = True
            row.expires_at = expires_at
            row.expiry_notice_sent = False
            row.is_trial = True
            row.trial_grace_ends_at = None
            row.updated_at = now
        if session.query(HousingTrialUsed).get(int(user_id)) is None:
            session.add(HousingTrialUsed(user_id=int(user_id), used_at=now))
        session.commit()
    finally:
        session.close()


def is_trial(user_id: int) -> bool:
    session = DBSession()
    try:
        row = session.query(HousingAccessUser).get(int(user_id))
        return bool(row and row.active and row.is_trial)
    finally:
        session.close()


def set_trial_dormant(user_id: int, grace_ends_at: datetime) -> bool:
    """Trial's 7 days are up: stops monitoring (active=False) but keeps the
    row and its `is_trial` flag so the filters can be left in place until
    `grace_ends_at` instead of being deleted right away."""
    session = DBSession()
    try:
        row = session.query(HousingAccessUser).get(int(user_id))
        if row is None:
            return False
        row.active = False
        row.trial_grace_ends_at = grace_ends_at
        row.updated_at = utc_now()
        session.commit()
        return True
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
                "is_trial": bool(row.is_trial),
            }
            for row in query.order_by(HousingAccessUser.user_id.asc()).all()
        ]
    finally:
        session.close()


def list_expiring_soon(within_days: int = 3, trial: Optional[bool] = None) -> list:
    """Active users whose access expires within `within_days` and who
    haven't been warned about it yet (see `mark_notice_sent`).

    `trial` narrows to trial rows (True) or paid rows (False); left as None
    it doesn't filter by kind at all. The two kinds get warned on different
    schedules (see EXPIRY_WARNING_DAYS vs TRIAL_WARNING_DAYS in
    housing_monitor.py), so callers should always pass it explicitly.
    """
    session = DBSession()
    try:
        cutoff = utc_now() + timedelta(days=within_days)
        query = (
            session.query(HousingAccessUser)
            .filter(HousingAccessUser.active.is_(True))
            .filter(HousingAccessUser.expires_at.isnot(None))
            .filter(HousingAccessUser.expires_at <= cutoff)
            .filter(HousingAccessUser.expiry_notice_sent.isnot(True))
        )
        if trial is not None:
            query = query.filter(HousingAccessUser.is_trial.is_(bool(trial)))
        return [
            {"user_id": int(row.user_id), "display_name": str(row.display_name or ""), "expires_at": row.expires_at}
            for row in query.all()
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


def list_expired(trial: Optional[bool] = None) -> list:
    """Active users whose expiry date has already passed.

    `trial` narrows to trial rows (True) or paid rows (False), same as in
    `list_expiring_soon` - paid rows close immediately on expiry, trial rows
    instead go through `set_trial_dormant`'s grace period, so callers must
    pick one or the other rather than mixing both in a single pass.
    """
    session = DBSession()
    try:
        query = (
            session.query(HousingAccessUser)
            .filter(HousingAccessUser.active.is_(True))
            .filter(HousingAccessUser.expires_at.isnot(None))
            .filter(HousingAccessUser.expires_at <= utc_now())
        )
        if trial is not None:
            query = query.filter(HousingAccessUser.is_trial.is_(bool(trial)))
        return [
            {"user_id": int(row.user_id), "display_name": str(row.display_name or ""), "expires_at": row.expires_at}
            for row in query.all()
        ]
    finally:
        session.close()


def list_trial_grace_expired() -> list:
    """Dormant trials (monitoring already stopped by `set_trial_dormant`)
    whose grace period has now run out - their filters are due for deletion."""
    session = DBSession()
    try:
        rows = (
            session.query(HousingAccessUser)
            .filter(HousingAccessUser.active.is_(False))
            .filter(HousingAccessUser.is_trial.is_(True))
            .filter(HousingAccessUser.trial_grace_ends_at.isnot(None))
            .filter(HousingAccessUser.trial_grace_ends_at <= utc_now())
            .all()
        )
        return [
            {"user_id": int(row.user_id), "display_name": str(row.display_name or "")}
            for row in rows
        ]
    finally:
        session.close()