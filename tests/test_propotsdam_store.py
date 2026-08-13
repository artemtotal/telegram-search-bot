import unittest
from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database import Base, ProPotsdamDelivery, ProPotsdamListing
from user_jobs import propotsdam_store


class ProPotsdamStoreTests(unittest.TestCase):
    def test_create_filter_baselines_current_matching_listings(self):
        engine = create_engine(
            'sqlite://',
            connect_args={'check_same_thread': False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        test_session = sessionmaker(bind=engine)
        now = datetime.utcnow()
        session = test_session()
        session.add_all([
            ProPotsdamListing(
                listing_key='matching',
                title='Matching apartment',
                district='Drewitz',
                rooms=3.0,
                area_m2=70.0,
                total_rent_eur=1200.0,
                first_seen_at=now,
                last_seen_at=now,
                is_active=True,
            ),
            ProPotsdamListing(
                listing_key='not-matching',
                title='Too expensive apartment',
                district='Drewitz',
                rooms=3.0,
                area_m2=70.0,
                total_rent_eur=1800.0,
                first_seen_at=now,
                last_seen_at=now,
                is_active=True,
            ),
        ])
        session.commit()
        session.close()

        original_session = propotsdam_store.DBSession
        propotsdam_store.DBSession = test_session
        try:
            filter_id = propotsdam_store.create_filter(
                user_id=544675510,
                title='Katya',
                districts='Drewitz',
                min_rooms=3.0,
                min_area_m2=60.0,
                max_total_rent_eur=1500.0,
            )
        finally:
            propotsdam_store.DBSession = original_session

        session = test_session()
        deliveries = session.query(ProPotsdamDelivery).filter(
            ProPotsdamDelivery.filter_id == filter_id
        ).all()
        session.close()
        engine.dispose()

        self.assertEqual([row.listing_key for row in deliveries], ['matching'])

    def test_numeric_text_parsing_for_admin_flow(self):
        self.assertEqual(propotsdam_store.parse_optional_number('1,5'), 1.5)
        self.assertEqual(propotsdam_store.parse_optional_number('-'), None)
        self.assertEqual(propotsdam_store.parse_optional_number(''), None)

    def test_districts_are_normalized_and_deduplicated(self):
        self.assertEqual(
            propotsdam_store.normalize_districts('Babelsberg, babelsberg, Waldstadt 2'),
            'Babelsberg,Waldstadt 2',
        )
        self.assertEqual(propotsdam_store.normalize_districts('всі'), '')

    def test_select_unsent_matches_uses_delivery_keys(self):
        listing = {'listing_key': 'abc', 'district': 'Babelsberg', 'rooms': 2.0, 'area_m2': 64.0, 'total_rent_eur': 900.0}
        filt = {'filter_id': 7, 'user_id': 123, 'districts': 'Babelsberg', 'max_total_rent_eur': 1000.0}

        matches = propotsdam_store.select_unsent_matches([listing], [filt], delivered={(7, 'abc')})
        self.assertEqual(matches, [])

        matches = propotsdam_store.select_unsent_matches([listing], [filt], delivered=set())
        self.assertEqual(matches, [(filt, listing)])


if __name__ == '__main__':
    unittest.main()
