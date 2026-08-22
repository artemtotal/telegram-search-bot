# coding: utf-8
"""Aggregates found-listing counts across every housing source for the stats dashboard."""

from datetime import datetime

from database import (
    DBSession,
    ImmoweltListing,
    KarlmarxListing,
    KleinanzeigenListing,
    LocalsListing,
    ProPotsdamListing,
    RegiomaklerListing,
    SchobaListing,
    SemmelhaackListing,
)

# (model, column holding the rent) — ProPotsdam is the one source metered as
# `total_rent_eur` (Gesamtmiete) rather than `price_eur` (Kaltmiete/Warmmiete).
_SOURCES = (
    (ImmoweltListing, ImmoweltListing.price_eur),
    (ProPotsdamListing, ProPotsdamListing.total_rent_eur),
    (SemmelhaackListing, SemmelhaackListing.price_eur),
    (SchobaListing, SchobaListing.price_eur),
    (RegiomaklerListing, RegiomaklerListing.price_eur),
    (KleinanzeigenListing, KleinanzeigenListing.price_eur),
    (LocalsListing, LocalsListing.price_eur),
    (KarlmarxListing, KarlmarxListing.price_eur),
)


def utc_now():
    return datetime.utcnow()


def fetch_listings_since(cutoff):
    """Every listing first seen at/after `cutoff`, across all 8 housing
    sources, as (rooms, area_m2, price_eur) tuples. Any of the three can be
    None — chart bucketing just skips what's missing per metric."""
    session = DBSession()
    try:
        rows = []
        for model, price_col in _SOURCES:
            found = session.query(model.rooms, model.area_m2, price_col).filter(
                model.first_seen_at >= cutoff,
            ).all()
            rows.extend(found)
        return rows
    finally:
        session.close()
