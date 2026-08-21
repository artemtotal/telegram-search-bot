import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database import Base
from user_jobs import coop_watchdog_store


class CoopWatchdogStoreTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            'sqlite://',
            connect_args={'check_same_thread': False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.session = sessionmaker(bind=self.engine)
        self.original_session = coop_watchdog_store.DBSession
        coop_watchdog_store.DBSession = self.session

    def tearDown(self):
        coop_watchdog_store.DBSession = self.original_session
        self.engine.dispose()

    def test_subscribing_creates_an_active_filter(self):
        filter_id = coop_watchdog_store.create_filter(777, 'gewoba', 'Gewoba eG Babelsberg')

        self.assertIsInstance(filter_id, int)
        rows = coop_watchdog_store.list_filters(user_id=777)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['coop_key'], 'gewoba')
        self.assertTrue(rows[0]['active'])

    def test_subscribing_twice_reactivates_instead_of_duplicating(self):
        first_id = coop_watchdog_store.create_filter(777, 'gewoba', 'Gewoba eG Babelsberg')
        coop_watchdog_store.set_filter_active(777, 'gewoba', False)

        second_id = coop_watchdog_store.create_filter(777, 'gewoba', 'Gewoba eG Babelsberg')

        self.assertEqual(first_id, second_id)
        self.assertEqual(len(coop_watchdog_store.list_filters(user_id=777)), 1)
        self.assertTrue(coop_watchdog_store.list_filters(user_id=777)[0]['active'])

    def test_active_only_hides_paused_subscriptions(self):
        coop_watchdog_store.create_filter(777, 'gewoba', 'Gewoba eG Babelsberg')
        coop_watchdog_store.set_filter_active(777, 'gewoba', False)

        self.assertEqual(coop_watchdog_store.list_filters(user_id=777, active_only=True), [])
        self.assertEqual(len(coop_watchdog_store.list_filters(user_id=777)), 1)

    def test_set_active_on_an_unknown_subscription_reports_no_row_found(self):
        self.assertFalse(coop_watchdog_store.set_filter_active(999, 'gewoba', True))

    def test_list_subscriber_ids_only_includes_active_subscribers_of_that_coop(self):
        coop_watchdog_store.create_filter(1, 'gewoba', 'Gewoba eG Babelsberg')
        coop_watchdog_store.create_filter(2, 'gewoba', 'Gewoba eG Babelsberg')
        coop_watchdog_store.create_filter(3, 'wbg1903', 'WBG 1903 Potsdam')
        coop_watchdog_store.set_filter_active(2, 'gewoba', False)

        self.assertEqual(coop_watchdog_store.list_subscriber_ids('gewoba'), [1])
        self.assertEqual(coop_watchdog_store.list_subscriber_ids('wbg1903'), [3])


if __name__ == '__main__':
    unittest.main()
