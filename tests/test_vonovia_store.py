import unittest
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database import Base, VonoviaDelivery, VonoviaFilter, VonoviaListing
from user_jobs import vonovia_store


class VonoviaStoreTests(unittest.TestCase):
    def _fresh_session(self):
        engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
        Base.metadata.create_all(engine)
        return engine, sessionmaker(bind=engine)

    def _listing(self, session, key, **kwargs):
        now = datetime.utcnow()
        fields = dict(
            title='2,5-Zi. Wohnung', address='Weitmarer Str. 145 a, 44795 Bochum',
            rooms=2.5, area_m2=63.3, price_eur=841.89,
            first_seen_at=now, last_seen_at=now, is_active=True,
        )
        fields.update(kwargs)
        session.add(VonoviaListing(listing_key=key, **fields))

    def test_create_filter_baselines_current_matching_listings(self):
        engine, test_session = self._fresh_session()
        session = test_session()
        self._listing(session, 'matching')
        self._listing(session, 'too-expensive', price_eur=2313.22, rooms=2.0, area_m2=177.94)
        session.commit()
        session.close()

        original_session = vonovia_store.DBSession
        vonovia_store.DBSession = test_session
        try:
            filter_id = vonovia_store.create_filter(
                user_id=544675510, title='Katya', min_rooms=1.0, max_price_eur=1500.0,
            )
        finally:
            vonovia_store.DBSession = original_session

        session = test_session()
        deliveries = session.query(VonoviaDelivery).filter(VonoviaDelivery.filter_id == filter_id).all()
        session.close()
        engine.dispose()

        self.assertEqual([row.listing_key for row in deliveries], ['matching'])

    def test_the_warm_bound_applies_once_the_listing_page_has_been_read(self):
        """Поки повної ціни немає, тепла межа не має відкидати квартиру."""
        engine, test_session = self._fresh_session()
        session = test_session()
        self._listing(session, 'priced', price_warm_eur=1111.89)
        self._listing(session, 'not-yet-priced')
        self._listing(session, 'too-warm', price_warm_eur=1900.0)
        session.commit()
        session.close()

        original_session = vonovia_store.DBSession
        vonovia_store.DBSession = test_session
        try:
            filter_id = vonovia_store.create_filter(
                user_id=544675510, title='Katya', max_price_warm_eur=1200.0,
            )
        finally:
            vonovia_store.DBSession = original_session

        session = test_session()
        keys = sorted(
            row.listing_key
            for row in session.query(VonoviaDelivery).filter(VonoviaDelivery.filter_id == filter_id).all()
        )
        session.close()
        engine.dispose()

        self.assertEqual(keys, ['not-yet-priced', 'priced'])

    def test_a_repeat_scan_does_not_wipe_the_gallery_or_the_full_rent(self):
        """Каталог не знає повної ціни, тож черговий обхід не має її стирати."""
        engine, test_session = self._fresh_session()
        original_session = vonovia_store.DBSession
        vonovia_store.DBSession = test_session
        try:
            vonovia_store.upsert_listings([{
                'listing_key': '1439890008', 'title': 'Wohnung', 'address': 'Bochum',
                'rooms': 2.5, 'area_m2': 63.3, 'price_eur': 841.89,
                'price_warm_eur': 1111.89,
                'gallery_urls': ['https://cdn.expose.vonovia.de/VNA-a.jpg?width=1200'],
            }])
            vonovia_store.upsert_listings([{
                'listing_key': '1439890008', 'title': 'Wohnung', 'address': 'Bochum',
                'rooms': 2.5, 'area_m2': 63.3, 'price_eur': 841.89,
            }])
            listings = vonovia_store.list_active_listings()
        finally:
            vonovia_store.DBSession = original_session
        engine.dispose()

        self.assertEqual(listings[0]['price_warm_eur'], 1111.89)
        self.assertEqual(listings[0]['gallery_urls'], ['https://cdn.expose.vonovia.de/VNA-a.jpg?width=1200'])

    def test_a_listing_that_left_the_portal_stops_being_active(self):
        engine, test_session = self._fresh_session()
        original_session = vonovia_store.DBSession
        vonovia_store.DBSession = test_session
        try:
            vonovia_store.upsert_listings([
                {'listing_key': 'gone', 'title': 'A', 'price_eur': 500.0},
                {'listing_key': 'stays', 'title': 'B', 'price_eur': 600.0},
            ])
            vonovia_store.upsert_listings([{'listing_key': 'stays', 'title': 'B', 'price_eur': 600.0}])
            keys = [item['listing_key'] for item in vonovia_store.list_active_listings()]
        finally:
            vonovia_store.DBSession = original_session
        engine.dispose()

        self.assertEqual(keys, ['stays'])

    def test_only_listings_still_missing_the_full_rent_are_visited_again(self):
        engine, test_session = self._fresh_session()
        session = test_session()
        self._listing(session, 'priced', price_warm_eur=1111.89)
        self._listing(session, 'unpriced')
        session.commit()
        session.close()

        original_session = vonovia_store.DBSession
        vonovia_store.DBSession = test_session
        try:
            priced = vonovia_store.keys_with_full_rent()
        finally:
            vonovia_store.DBSession = original_session
        engine.dispose()

        self.assertEqual(priced, {'priced'})

    def test_recent_listings_are_the_ones_first_seen_after_the_cutoff(self):
        engine, test_session = self._fresh_session()
        now = datetime.utcnow()
        session = test_session()
        self._listing(session, 'old', first_seen_at=now - timedelta(days=2), last_seen_at=now)
        self._listing(session, 'fresh', first_seen_at=now - timedelta(minutes=10), last_seen_at=now)
        session.commit()
        session.close()

        original_session = vonovia_store.DBSession
        vonovia_store.DBSession = test_session
        try:
            recent = vonovia_store.list_active_listings_since(now - timedelta(hours=1))
        finally:
            vonovia_store.DBSession = original_session
        engine.dispose()

        self.assertEqual([item['listing_key'] for item in recent], ['fresh'])

    def test_a_paused_filter_gets_nothing_selected_for_it(self):
        engine, test_session = self._fresh_session()
        original_session = vonovia_store.DBSession
        vonovia_store.DBSession = test_session
        try:
            filter_id = vonovia_store.create_filter(user_id=1, title='Katya', max_price_eur=1500.0)
            vonovia_store.set_filter_active(filter_id, False)
            filters = vonovia_store.list_filters(active_only=True)
        finally:
            vonovia_store.DBSession = original_session
        engine.dispose()

        self.assertEqual(filters, [])

    def test_deleting_a_filter_takes_its_delivery_history_with_it(self):
        engine, test_session = self._fresh_session()
        original_session = vonovia_store.DBSession
        vonovia_store.DBSession = test_session
        try:
            filter_id = vonovia_store.create_filter(user_id=1, title='Katya')
            vonovia_store.mark_delivered(filter_id, 'some-key')
            vonovia_store.delete_filter(filter_id, user_id=1)
            left = vonovia_store.delivered_pairs()
            filters = vonovia_store.list_filters()
        finally:
            vonovia_store.DBSession = original_session
        session = test_session()
        rows = session.query(VonoviaFilter).all()
        session.close()
        engine.dispose()

        self.assertEqual((left, filters, rows), (set(), [], []))
