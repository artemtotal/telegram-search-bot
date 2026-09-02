import unittest
from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database import Base, ProPotsdamDelivery, ProPotsdamFilter, ProPotsdamListing
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

    def test_update_filter_rebaselines_delivery_for_the_new_criteria(self):
        """Розширений фільтр тихо базується на нових умовах, а не шле все одним потоком."""
        engine = create_engine(
            'sqlite://',
            connect_args={'check_same_thread': False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        test_session = sessionmaker(bind=engine)
        now = datetime.utcnow()
        session = test_session()
        session.add(
            ProPotsdamListing(
                listing_key='bigger-flat',
                title='Bigger apartment',
                district='Drewitz',
                rooms=4.0,
                area_m2=90.0,
                total_rent_eur=1400.0,
                first_seen_at=now,
                last_seen_at=now,
                is_active=True,
            )
        )
        session.commit()
        session.close()

        original_session = propotsdam_store.DBSession
        propotsdam_store.DBSession = test_session
        try:
            filter_id = propotsdam_store.create_filter(
                user_id=544675510, title='Katya', districts='Drewitz',
                min_rooms=3.0, max_rooms=3.0,
            )
            # До оновлення 4-кімнатна квартира не підходила під ліміт "3 кімнати" —
            # доставки для неї ще немає.
            session = test_session()
            self.assertEqual(
                session.query(ProPotsdamDelivery).filter(ProPotsdamDelivery.filter_id == filter_id).count(), 0
            )
            session.close()

            ok = propotsdam_store.update_filter(
                filter_id=filter_id, user_id=544675510, title='Katya', districts='Drewitz',
                min_rooms=3.0, max_rooms=None,
            )
        finally:
            propotsdam_store.DBSession = original_session

        self.assertTrue(ok)
        session = test_session()
        deliveries = session.query(ProPotsdamDelivery).filter(ProPotsdamDelivery.filter_id == filter_id).all()
        row = session.query(ProPotsdamFilter).filter(ProPotsdamFilter.filter_id == filter_id).first()
        session.close()
        engine.dispose()

        self.assertEqual([d.listing_key for d in deliveries], ['bigger-flat'])
        self.assertIsNone(row.max_rooms)

    def test_update_filter_rejects_someone_elses_filter(self):
        engine = create_engine(
            'sqlite://',
            connect_args={'check_same_thread': False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        test_session = sessionmaker(bind=engine)

        original_session = propotsdam_store.DBSession
        propotsdam_store.DBSession = test_session
        try:
            filter_id = propotsdam_store.create_filter(user_id=544675510, title='Katya', districts='Drewitz')
            ok = propotsdam_store.update_filter(
                filter_id=filter_id, user_id=312029534, title='Hijacked', districts='Golm',
            )
        finally:
            propotsdam_store.DBSession = original_session
            engine.dispose()

        self.assertFalse(ok)

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

    def test_filter_owner_scope_prevents_other_user_from_toggling(self):
        engine = create_engine(
            'sqlite://',
            connect_args={'check_same_thread': False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        test_session = sessionmaker(bind=engine)
        original_session = propotsdam_store.DBSession
        propotsdam_store.DBSession = test_session
        try:
            filter_id = propotsdam_store.create_filter(
                user_id=544675510,
                title='Пошук Каті',
            )
            self.assertFalse(
                propotsdam_store.set_filter_active(
                    filter_id, False, user_id=312029534
                )
            )
            self.assertTrue(
                propotsdam_store.list_filters(user_id=544675510)[0]['active']
            )
            self.assertTrue(
                propotsdam_store.set_filter_active(
                    filter_id, False, user_id=544675510
                )
            )
            self.assertFalse(
                propotsdam_store.list_filters(user_id=544675510)[0]['active']
            )
        finally:
            propotsdam_store.DBSession = original_session
            engine.dispose()

    def test_empty_scan_deactivates_previously_active_listings(self):
        engine = create_engine(
            'sqlite://',
            connect_args={'check_same_thread': False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        test_session = sessionmaker(bind=engine)
        now = datetime.utcnow()
        session = test_session()
        session.add(
            ProPotsdamListing(
                listing_key='gone-from-portal',
                title='Withdrawn apartment',
                district='Drewitz',
                rooms=3.0,
                area_m2=70.0,
                total_rent_eur=1200.0,
                first_seen_at=now,
                last_seen_at=now,
                is_active=True,
            )
        )
        session.commit()
        session.close()

        original_session = propotsdam_store.DBSession
        propotsdam_store.DBSession = test_session
        try:
            self.assertEqual(propotsdam_store.upsert_listings([]), 0)
            self.assertEqual(propotsdam_store.list_active_listings(), [])
        finally:
            propotsdam_store.DBSession = original_session
            engine.dispose()

    def test_latest_listing_seen_at_returns_most_recent_observation(self):
        engine = create_engine(
            'sqlite://',
            connect_args={'check_same_thread': False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        test_session = sessionmaker(bind=engine)
        older = datetime(2026, 8, 12, 10, 0, 0)
        newer = datetime(2026, 8, 14, 21, 42, 0)
        session = test_session()
        session.add_all([
            ProPotsdamListing(listing_key='old', title='Older', first_seen_at=older, last_seen_at=older, is_active=False),
            ProPotsdamListing(listing_key='new', title='Newer', first_seen_at=older, last_seen_at=newer, is_active=False),
        ])
        session.commit()
        session.close()

        original_session = propotsdam_store.DBSession
        propotsdam_store.DBSession = test_session
        try:
            self.assertEqual(propotsdam_store.latest_listing_seen_at(), newer)
        finally:
            propotsdam_store.DBSession = original_session
            engine.dispose()

    def test_select_unsent_matches_uses_delivery_keys(self):
        listing = {'listing_key': 'abc', 'district': 'Babelsberg', 'rooms': 2.0, 'area_m2': 64.0, 'total_rent_eur': 900.0}
        filt = {'filter_id': 7, 'user_id': 123, 'districts': 'Babelsberg', 'max_total_rent_eur': 1000.0}

        matches = propotsdam_store.select_unsent_matches([listing], [filt], delivered={(7, 'abc')})
        self.assertEqual(matches, [])

        matches = propotsdam_store.select_unsent_matches([listing], [filt], delivered=set())
        self.assertEqual(matches, [(filt, listing)])


if __name__ == '__main__':
    unittest.main()


class CardPricesSurviveStorageTests(unittest.TestCase):
    """Ціни з картки мають доїхати до бази, а не загубитись дорогою.

    Знімки картки лежали на диску, ціни в них були — а в базі стояли самі
    Gesamtmiete: `normalize_listing` повертає лише перелічені поля, і все,
    чого немає в списку порталу, мовчки зникало на цьому кроці.
    """

    def setUp(self):
        self.engine = create_engine(
            'sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self._original = propotsdam_store.DBSession
        propotsdam_store.DBSession = sessionmaker(bind=self.engine)

    def tearDown(self):
        propotsdam_store.DBSession = self._original
        self.engine.dispose()

    def _listing(self, **kwargs):
        base = {
            'listing_key': 'AAAA-1111', 'title': 'Wohnung',
            'address': 'Alt Nowawes 84, 14482 Potsdam', 'district': 'Babelsberg',
            'rooms': '1', 'area': '37 m²', 'total_rent': '485,52 EUR',
        }
        base.update(kwargs)
        return base

    def test_the_cold_rent_from_the_card_reaches_the_database(self):
        propotsdam_store.upsert_listings([self._listing(
            price_eur=326.48, nebenkosten_eur=81.24, heizkosten_eur=77.8)])

        stored = propotsdam_store.list_active_listings()[0]

        self.assertEqual(stored['price_eur'], 326.48)
        self.assertEqual(stored['nebenkosten_eur'], 81.24)
        self.assertEqual(stored['heizkosten_eur'], 77.8)
        self.assertEqual(stored['total_rent_eur'], 485.52)

    def test_a_later_list_only_scan_does_not_wipe_it(self):
        """Обхід списку картку не відкриває — і не має стирати те, що вона дала."""
        propotsdam_store.upsert_listings([self._listing(price_eur=326.48)])
        propotsdam_store.upsert_listings([self._listing()])

        stored = propotsdam_store.list_active_listings()[0]

        self.assertEqual(stored['price_eur'], 326.48)
