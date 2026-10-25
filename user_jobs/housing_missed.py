"""Tells a free-tier person when a flat that matched their filter was taken
down before the delay let it go out (25.10.2026).

Before, such a flat just never arrived, and people only saw silence - or, on
Immowelt, got a dead link two hours later. Now every polled source writes
down what it holds back for the free tier (housing_tier.DeliveryGate ->
HousingHeldMatch); this job looks at those flats every few minutes and,
for the ones gone from the site, sends a short "it was taken down while you
waited" note with the subscription button. Immowelt is checked separately
when its own queue releases a flat (housing_receiver.send_due_immowelt).

"Gone" has to be real, not a scan hiccup:
- a source whose last scan left no active listing at all is skipped (a
  broken page deactivates everything at once);
- a flat that dropped off a one-page feed because newer ones pushed it out
  is not "gone" (more than PUSHED_OFF_AFTER newer listings since);
- more than SUSPICIOUS_GONE distinct flats of one source vanishing in one
  run looks like a glitch too - nothing is sent, only a warning is logged.
"""

import logging
from collections import defaultdict
from datetime import timedelta
from typing import Dict

from database import (
    DBSession,
    HousingHeldMatch,
    HousingSearch,
    ImmoscoutListing,
    KarlmarxListing,
    KleinanzeigenListing,
    LocalsListing,
    ProPotsdamListing,
    RegiomaklerListing,
    SchobaListing,
    SemmelhaackListing,
    VonoviaListing,
)
from user_jobs import housing_tier

logger = logging.getLogger(__name__)

LISTING_MODELS = {
    "propotsdam": ProPotsdamListing,
    "semmelhaack": SemmelhaackListing,
    "schoba": SchobaListing,
    "regiomakler": RegiomaklerListing,
    "kleinanzeigen": KleinanzeigenListing,
    "locals": LocalsListing,
    "vonovia": VonoviaListing,
    "immoscout": ImmoscoutListing,
    "karlmarx": KarlmarxListing,
}
SOURCE_LABEL = {
    "immowelt": "Immowelt", "propotsdam": "ProPotsdam", "semmelhaack": "SEMMELHAACK",
    "schoba": "SCHOBA", "regiomakler": "ImmoTeam/alpha", "kleinanzeigen": "Kleinanzeigen",
    "locals": "locals®", "karlmarx": "Karl Marx", "vonovia": "Vonovia", "immoscout": "ImmoScout24",
}
# Kleinanzeigen reads one page of ~25 newest: after 20 newer listings ours may
# simply be on page two.
PUSHED_OFF_AFTER = 20
SUSPICIOUS_GONE = 5
# A held flat still online after this long will go out on its own or not at
# all; the row only keeps the table from growing.
FORGET_AFTER = timedelta(days=3)


def _send(bot, user_id: int, source: str, summary: str) -> None:
    text, keyboard = housing_tier.missed_notice(user_id, SOURCE_LABEL.get(source, source), summary)
    bot.send_message(chat_id=int(user_id), text=text, parse_mode="HTML", reply_markup=keyboard)


def missed_flats_job(context) -> Dict[str, int]:
    bot = context.bot
    now = housing_tier.utc_now()
    premium = housing_tier.premium_user_ids()
    session = DBSession()
    try:
        held = session.query(HousingHeldMatch).all()
        with_filters = {int(uid) for (uid,) in session.query(HousingSearch.user_id).distinct()}
        by_source = defaultdict(list)
        for row in held:
            by_source[row.source].append(row)
        to_notify, to_forget = [], []
        for source, rows in by_source.items():
            model = LISTING_MODELS.get(source)
            if model is None:
                to_forget.extend(rows)
                continue
            if not session.query(model.listing_key).filter(model.is_active.is_(True)).first():
                logger.warning("Missed flats: %s has no active listing at all, skipping this run", source)
                continue
            gone = []
            for row in rows:
                listing = session.query(model).filter(model.listing_key == row.listing_key).first()
                if listing is None or int(row.user_id) in premium or int(row.user_id) not in with_filters:
                    to_forget.append(row)
                    continue
                if listing.is_active:
                    if now - row.held_at > FORGET_AFTER:
                        to_forget.append(row)
                    continue
                newer = session.query(model.listing_key).filter(model.first_seen_at > listing.first_seen_at).count()
                if newer > PUSHED_OFF_AFTER:
                    to_forget.append(row)
                    continue
                gone.append(row)
            distinct = {row.listing_key for row in gone}
            if len(distinct) > SUSPICIOUS_GONE:
                logger.warning(
                    "Missed flats: %s distinct %s flats gone in one run - looks like a scan glitch, not sending",
                    len(distinct), source,
                )
                to_forget.extend(gone)
                continue
            to_notify.extend(gone)
        notify = [(int(r.user_id), r.source, r.summary or "", r.listing_key) for r in to_notify]
        for row in to_notify + to_forget:
            session.delete(row)
        session.commit()
    finally:
        session.close()

    sent = 0
    for user_id, source, summary, listing_key in notify:
        try:
            _send(bot, user_id, source, summary)
            sent += 1
            logger.info("Missed flat: told %s that %s %s was taken down while held", user_id, source, listing_key)
        except Exception as exc:
            if not housing_tier.recipient_gone(exc):
                logger.exception("Could not tell %s about a missed %s flat", user_id, source)
    return {"sent": sent, "held": len(held)}
