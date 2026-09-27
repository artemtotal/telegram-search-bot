"""Who gets new flats instantly and who a couple of hours later.

Housing monitoring is open to everyone. The free tier gets every flat its
filters catch, but only once the listing is FREE_DELAY old; a paid
subscription (an active `housing_access_user` row, which the admin grants)
gets it on the very scan that finds it. In a market where a good flat can
be gone within hours, that head start is what people pay for.

Also the one place every real send is written down (`log_sent`), whatever
the source: the per-source `*_delivery` tables mix real sends with the
silent baseline taken at filter creation and die with the filter, so they
could never say how many flats a person actually got.
"""

import logging
import os
from datetime import datetime, timedelta
from typing import Dict, Optional, Set

import i18n
from database import DBSession, HousingAccessUser, HousingSendLog

logger = logging.getLogger(__name__)

ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
FREE_DELAY = timedelta(hours=float(os.getenv("HOUSING_FREE_DELAY_HOURS", "2") or 2))
PREMIUM_PRICE_EUR = 5


def utc_now() -> datetime:
    return datetime.utcnow()


def premium_user_ids() -> Set[int]:
    session = DBSession()
    try:
        ids = {
            int(row.user_id)
            for row in session.query(HousingAccessUser.user_id).filter(HousingAccessUser.active.is_(True))
        }
    finally:
        session.close()
    if ADMIN_ID:
        ids.add(ADMIN_ID)
    return ids


def is_premium(user_id: int) -> bool:
    return int(user_id) in premium_user_ids()


def free_delay_hours() -> str:
    return f"{FREE_DELAY.total_seconds() / 3600:g}"


def free_footer(user_id: int) -> str:
    """The line under every flat a free-tier person gets."""
    lang = i18n.get_lang(int(user_id))
    return "\n\n" + i18n.t(
        "housing.free.footer", lang, hours=free_delay_hours(), price=PREMIUM_PRICE_EUR,
    )


def log_sent(
    user_id: int, source: str, listing_key: str, filter_id: Optional[int] = None, delayed: bool = False,
) -> None:
    """Never lets a logging problem get in the way of the send itself."""
    session = DBSession()
    try:
        session.add(HousingSendLog(
            user_id=int(user_id),
            source=str(source),
            listing_key=str(listing_key),
            filter_id=int(filter_id) if filter_id is not None else None,
            delayed=bool(delayed),
            sent_at=utc_now(),
        ))
        session.commit()
    except Exception:
        session.rollback()
        logger.exception("Could not log a %s send to %s", source, user_id)
    finally:
        session.close()


class DeliveryGate:
    """One per scan of a source: decides whether a match may go out now.

    A free-tier match that isn't old enough yet is simply skipped - not
    marked delivered - so the same match comes up again on a later scan and
    goes out once it is. A listing taken down before then is never sent to
    the free tier at all.
    """

    def __init__(self, source: str, listing_model=None, now: Optional[datetime] = None) -> None:
        self.source = source
        self.now = now or utc_now()
        self.premium = premium_user_ids()
        self._listing_model = listing_model
        self._first_seen: Optional[Dict[str, datetime]] = None

    def is_premium(self, user_id: int) -> bool:
        return int(user_id) in self.premium

    def _first_seen_at(self, listing: Dict) -> Optional[datetime]:
        value = listing.get("first_seen_at")
        if isinstance(value, datetime):
            return value
        if self._listing_model is None:
            return None
        if self._first_seen is None:
            session = DBSession()
            try:
                model = self._listing_model
                self._first_seen = {
                    str(key): seen for key, seen in session.query(model.listing_key, model.first_seen_at)
                }
            finally:
                session.close()
        return self._first_seen.get(str(listing.get("listing_key") or ""))

    def due(self, user_id: int, listing: Dict) -> bool:
        if self.is_premium(user_id):
            return True
        first_seen = self._first_seen_at(listing)
        if first_seen is None:
            return True
        return self.now - first_seen >= FREE_DELAY

    def footer(self, user_id: int) -> str:
        return "" if self.is_premium(user_id) else free_footer(user_id)

    def sent(self, user_id: int, listing_key: str, filter_id: Optional[int] = None) -> None:
        log_sent(user_id, self.source, listing_key, filter_id, delayed=not self.is_premium(user_id))
