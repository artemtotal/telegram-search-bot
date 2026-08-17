import unittest
from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database import Base, RegiomaklerDelivery, RegiomaklerFilter, RegiomaklerListing
from user_jobs import regiomakler_store


class RegiomaklerStoreTests(unittest.TestCase):
    def _fresh_session(self):
        engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
        Base.metadata.create_all(engine)
        return engine, sessionmaker(bind=engine)

    def test_create_filter_baselines_current_matching_listings(self):
        engine, test_session = self._fresh_session()
        now = datetime.utcnow()
        session = test_session()
        session.add_all([
            RegiomaklerListing(
                listing_key='12863_4', title='Maisonette', address='Potsdam-Babelsberg',
                rooms=3.0, area_m2=73.12, price_eur=1754.88, source='immoteam',
                first_seen_at=now, last_seen_at=now, is_active=True,
            ),
            RegiomaklerListing(
                listing_key='12863_2', title='Große Maisonette', address='Potsdam-Babelsberg',
                rooms=5.0, area_m2=129.99, price_eur=3119.76, source='alpha',
                first_seen_at=now, last_seen_at=now, is_active=True,
            ),
        ])
        session.commit()
        session.close()

        original_session = regiomakler_store.DBSession
        regiomakler_store.DBSession = test_session
        try:
            filter_id = regiomakler_store.create_filter(
                user_id=544675510, title='Katya', min_rooms=2.0, max_price_eur=2000.0,
            )
        finally:
            regiomakler_store.DBSession = original_session

        session = test_session()
        deliveries = session.query(RegiomaklerDelivery).filter(RegiomaklerDelivery.filter_id == filter_id).all()
        session.close()
        engine.dispose()

        self.assertEqual([row.listing_key for row in deliveries], ['12863_4'])

    def test_update_filter_rebaselines_delivery_for_the_new_criteria(self):
        engine, test_session = self._fresh_session()
        now = datetime.utcnow()
        session = test_session()
        session.add(RegiomaklerListing(
            listing_key='bigger', title='5-Zi.', address='Potsdam', source='immoteam',
            rooms=5.0, area_m2=120.0, price_eur=1200.0,
            first_seen_at=now, last_seen_at=now, is_active=True,
        ))
        session.commit()
        session.close()

        original_session = regiomakler_store.DBSession
        regiomakler_store.DBSession = test_session
        try:
            filter_id = regiomakler_store.create_filter(user_id=544675510, title='Katya', max_rooms=4.0)
            session = test_session()
            self.assertEqual(
                session.query(RegiomaklerDelivery).filter(RegiomaklerDelivery.filter_id == filter_id).count(), 0
            )
            session.close()

            ok = regiomakler_store.update_filter(
                filter_id=filter_id, user_id=544675510, title='Katya', max_rooms=None,
            )
        finally:
            regiomakler_store.DBSession = original_session

        self.assertTrue(ok)
        session = test_session()
        deliveries = session.query(RegiomaklerDelivery).filter(RegiomaklerDelivery.filter_id == filter_id).all()
        row = session.query(RegiomaklerFilter).filter(RegiomaklerFilter.filter_id == filter_id).first()
        session.close()
        engine.dispose()

        self.assertEqual([d.listing_key for d in deliveries], ['bigger'])
        self.assertIsNone(row.max_rooms)

    def test_update_filter_rejects_someone_elses_filter(self):
        engine, test_session = self._fresh_session()
        original_session = regiomakler_store.DBSession
        regiomakler_store.DBSession = test_session
        try:
            filter_id = regiomakler_store.create_filter(user_id=544675510, title='Katya')
            ok = regiomakler_store.update_filter(filter_id=filter_id, user_id=312029534, title='Hijacked')
        finally:
            regiomakler_store.DBSession = original_session
            engine.dispose()

        self.assertFalse(ok)

    def test_filter_owner_scope_prevents_other_user_from_toggling(self):
        engine, test_session = self._fresh_session()
        original_session = regiomakler_store.DBSession
        regiomakler_store.DBSession = test_session
        try:
            filter_id = regiomakler_store.create_filter(user_id=544675510, title='Katya')
            self.assertFalse(regiomakler_store.set_filter_active(filter_id, False, user_id=312029534))
            self.assertTrue(regiomakler_store.list_filters(user_id=544675510)[0]['active'])
            self.assertTrue(regiomakler_store.set_filter_active(filter_id, False, user_id=544675510))
            self.assertFalse(regiomakler_store.list_filters(user_id=544675510)[0]['active'])
        finally:
            regiomakler_store.DBSession = original_session
            engine.dispose()

    def test_delete_filter_also_removes_its_deliveries(self):
        engine, test_session = self._fresh_session()
        original_session = regiomakler_store.DBSession
        regiomakler_store.DBSession = test_session
        try:
            filter_id = regiomakler_store.create_filter(user_id=544675510, title='Katya')
            self.assertFalse(regiomakler_store.delete_filter(filter_id, user_id=312029534))
            self.assertTrue(regiomakler_store.delete_filter(filter_id, user_id=544675510))
            self.assertEqual(regiomakler_store.list_filters(user_id=544675510), [])
        finally:
            regiomakler_store.DBSession = original_session
            engine.dispose()

    def test_empty_scan_deactivates_previously_active_listings(self):
        engine, test_session = self._fresh_session()
        now = datetime.utcnow()
        session = test_session()
        session.add(RegiomaklerListing(
            listing_key='gone', title='Withdrawn', address='Potsdam', source='immoteam',
            rooms=3.0, area_m2=73.0, price_eur=1700.0,
            first_seen_at=now, last_seen_at=now, is_active=True,
        ))
        session.commit()
        session.close()

        original_session = regiomakler_store.DBSession
        regiomakler_store.DBSession = test_session
        try:
            self.assertEqual(regiomakler_store.upsert_listings([]), 0)
            self.assertEqual(regiomakler_store.list_active_listings(), [])
        finally:
            regiomakler_store.DBSession = original_session
            engine.dispose()

    def test_select_unsent_matches_uses_delivery_keys(self):
        listing = {'listing_key': '12863_4', 'rooms': 3.0, 'area_m2': 73.12, 'price_eur': 1754.88}
        filt = {'filter_id': 7, 'user_id': 123, 'max_price_eur': 2000.0}

        matches = regiomakler_store.select_unsent_matches([listing], [filt], delivered={(7, '12863_4')})
        self.assertEqual(matches, [])

        matches = regiomakler_store.select_unsent_matches([listing], [filt], delivered=set())
        self.assertEqual(matches, [(filt, listing)])


if __name__ == '__main__':
    unittest.main()
