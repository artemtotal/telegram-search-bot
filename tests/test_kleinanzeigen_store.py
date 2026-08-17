import unittest
from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database import Base, KleinanzeigenDelivery, KleinanzeigenFilter, KleinanzeigenListing
from user_jobs import kleinanzeigen_store


class KleinanzeigenStoreTests(unittest.TestCase):
    def _fresh_session(self):
        engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
        Base.metadata.create_all(engine)
        return engine, sessionmaker(bind=engine)

    def test_create_filter_baselines_current_matching_listings(self):
        engine, test_session = self._fresh_session()
        now = datetime.utcnow()
        session = test_session()
        session.add_all([
            KleinanzeigenListing(
                listing_key='3368739590', title='Komfort Wohnung', address='Potsdam',
                rooms=4.0, area_m2=162.0, price_eur=2445.0,
                first_seen_at=now, last_seen_at=now, is_active=True,
            ),
            KleinanzeigenListing(
                listing_key='too-expensive', title='Villa', address='Potsdam',
                rooms=4.0, area_m2=160.0, price_eur=5000.0,
                first_seen_at=now, last_seen_at=now, is_active=True,
            ),
        ])
        session.commit()
        session.close()

        original_session = kleinanzeigen_store.DBSession
        kleinanzeigen_store.DBSession = test_session
        try:
            filter_id = kleinanzeigen_store.create_filter(
                user_id=544675510, title='Katya', min_rooms=3.0, max_price_eur=3000.0,
            )
        finally:
            kleinanzeigen_store.DBSession = original_session

        session = test_session()
        deliveries = session.query(KleinanzeigenDelivery).filter(KleinanzeigenDelivery.filter_id == filter_id).all()
        session.close()
        engine.dispose()

        self.assertEqual([row.listing_key for row in deliveries], ['3368739590'])

    def test_update_filter_rebaselines_delivery_for_the_new_criteria(self):
        engine, test_session = self._fresh_session()
        now = datetime.utcnow()
        session = test_session()
        session.add(KleinanzeigenListing(
            listing_key='bigger', title='5-Zi.', address='Potsdam',
            rooms=5.0, area_m2=124.0, price_eur=2050.0,
            first_seen_at=now, last_seen_at=now, is_active=True,
        ))
        session.commit()
        session.close()

        original_session = kleinanzeigen_store.DBSession
        kleinanzeigen_store.DBSession = test_session
        try:
            filter_id = kleinanzeigen_store.create_filter(user_id=544675510, title='Katya', max_rooms=4.0)
            session = test_session()
            self.assertEqual(
                session.query(KleinanzeigenDelivery).filter(KleinanzeigenDelivery.filter_id == filter_id).count(), 0
            )
            session.close()

            ok = kleinanzeigen_store.update_filter(
                filter_id=filter_id, user_id=544675510, title='Katya', max_rooms=None,
            )
        finally:
            kleinanzeigen_store.DBSession = original_session

        self.assertTrue(ok)
        session = test_session()
        deliveries = session.query(KleinanzeigenDelivery).filter(KleinanzeigenDelivery.filter_id == filter_id).all()
        row = session.query(KleinanzeigenFilter).filter(KleinanzeigenFilter.filter_id == filter_id).first()
        session.close()
        engine.dispose()

        self.assertEqual([d.listing_key for d in deliveries], ['bigger'])
        self.assertIsNone(row.max_rooms)

    def test_update_filter_rejects_someone_elses_filter(self):
        engine, test_session = self._fresh_session()
        original_session = kleinanzeigen_store.DBSession
        kleinanzeigen_store.DBSession = test_session
        try:
            filter_id = kleinanzeigen_store.create_filter(user_id=544675510, title='Katya')
            ok = kleinanzeigen_store.update_filter(filter_id=filter_id, user_id=312029534, title='Hijacked')
        finally:
            kleinanzeigen_store.DBSession = original_session
            engine.dispose()

        self.assertFalse(ok)

    def test_filter_owner_scope_prevents_other_user_from_toggling(self):
        engine, test_session = self._fresh_session()
        original_session = kleinanzeigen_store.DBSession
        kleinanzeigen_store.DBSession = test_session
        try:
            filter_id = kleinanzeigen_store.create_filter(user_id=544675510, title='Katya')
            self.assertFalse(kleinanzeigen_store.set_filter_active(filter_id, False, user_id=312029534))
            self.assertTrue(kleinanzeigen_store.list_filters(user_id=544675510)[0]['active'])
            self.assertTrue(kleinanzeigen_store.set_filter_active(filter_id, False, user_id=544675510))
            self.assertFalse(kleinanzeigen_store.list_filters(user_id=544675510)[0]['active'])
        finally:
            kleinanzeigen_store.DBSession = original_session
            engine.dispose()

    def test_delete_filter_also_removes_its_deliveries(self):
        engine, test_session = self._fresh_session()
        original_session = kleinanzeigen_store.DBSession
        kleinanzeigen_store.DBSession = test_session
        try:
            filter_id = kleinanzeigen_store.create_filter(user_id=544675510, title='Katya')
            self.assertFalse(kleinanzeigen_store.delete_filter(filter_id, user_id=312029534))
            self.assertTrue(kleinanzeigen_store.delete_filter(filter_id, user_id=544675510))
            self.assertEqual(kleinanzeigen_store.list_filters(user_id=544675510), [])
        finally:
            kleinanzeigen_store.DBSession = original_session
            engine.dispose()

    def test_empty_scan_deactivates_previously_active_listings(self):
        engine, test_session = self._fresh_session()
        now = datetime.utcnow()
        session = test_session()
        session.add(KleinanzeigenListing(
            listing_key='gone', title='Withdrawn', address='Potsdam',
            rooms=3.0, area_m2=76.0, price_eur=572.0,
            first_seen_at=now, last_seen_at=now, is_active=True,
        ))
        session.commit()
        session.close()

        original_session = kleinanzeigen_store.DBSession
        kleinanzeigen_store.DBSession = test_session
        try:
            self.assertEqual(kleinanzeigen_store.upsert_listings([]), 0)
            self.assertEqual(kleinanzeigen_store.list_active_listings(), [])
        finally:
            kleinanzeigen_store.DBSession = original_session
            engine.dispose()

    def test_select_unsent_matches_uses_delivery_keys(self):
        listing = {'listing_key': '3368739590', 'rooms': 4.0, 'area_m2': 162.0, 'price_eur': 2445.0}
        filt = {'filter_id': 7, 'user_id': 123, 'max_price_eur': 3000.0}

        matches = kleinanzeigen_store.select_unsent_matches([listing], [filt], delivered={(7, '3368739590')})
        self.assertEqual(matches, [])

        matches = kleinanzeigen_store.select_unsent_matches([listing], [filt], delivered=set())
        self.assertEqual(matches, [(filt, listing)])


if __name__ == '__main__':
    unittest.main()
