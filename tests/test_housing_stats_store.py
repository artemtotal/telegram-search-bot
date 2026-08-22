import unittest
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database import Base, ImmoweltListing, ProPotsdamListing, SemmelhaackListing
from user_jobs import housing_stats_store


class HousingStatsStoreTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.original_session = housing_stats_store.DBSession
        housing_stats_store.DBSession = sessionmaker(bind=self.engine)

    def tearDown(self):
        housing_stats_store.DBSession = self.original_session
        self.engine.dispose()

    def _seed(self, now):
        session = housing_stats_store.DBSession()
        session.add(ImmoweltListing(
            listing_key='imm-1', rooms=2.0, area_m2=55.0, price_eur=800.0, first_seen_at=now,
        ))
        session.add(ProPotsdamListing(
            listing_key='pp-1', title='x', rooms=3.0, area_m2=70.0, total_rent_eur=900.0,
            first_seen_at=now, last_seen_at=now, is_active=True,
        ))
        session.add(SemmelhaackListing(
            listing_key='sh-old', title='x', rooms=1.0, area_m2=30.0, price_eur=500.0,
            first_seen_at=now - timedelta(days=40), last_seen_at=now, is_active=True,
        ))
        session.commit()
        session.close()

    def test_aggregates_across_sources_and_aliases_propotsdam_total_rent(self):
        now = datetime.utcnow()
        self._seed(now)

        rows = housing_stats_store.fetch_listings_since(now - timedelta(days=7))

        self.assertEqual(sorted(tuple(r) for r in rows), [(2.0, 55.0, 800.0), (3.0, 70.0, 900.0)])

    def test_listings_before_the_cutoff_are_excluded(self):
        now = datetime.utcnow()
        self._seed(now)

        rows = housing_stats_store.fetch_listings_since(now - timedelta(days=7))

        self.assertNotIn((1.0, 30.0, 500.0), [tuple(r) for r in rows])


if __name__ == "__main__":
    unittest.main()
