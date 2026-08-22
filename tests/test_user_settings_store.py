import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database import Base
from user_jobs import user_settings_store


class UserSettingsStoreTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            'sqlite://',
            connect_args={'check_same_thread': False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.session = sessionmaker(bind=self.engine)
        self.original_session = user_settings_store.DBSession
        user_settings_store.DBSession = self.session

    def tearDown(self):
        user_settings_store.DBSession = self.original_session
        self.engine.dispose()

    def test_unknown_user_defaults_to_ukrainian(self):
        self.assertEqual(user_settings_store.get_language(999), 'uk')

    def test_set_then_get_roundtrips(self):
        user_settings_store.set_language(777, 'ru')

        self.assertEqual(user_settings_store.get_language(777), 'ru')

    def test_setting_again_updates_instead_of_duplicating(self):
        user_settings_store.set_language(777, 'ru')
        user_settings_store.set_language(777, 'de')

        self.assertEqual(user_settings_store.get_language(777), 'de')

    def test_a_new_user_is_subscribed_to_news_by_default(self):
        self.assertTrue(user_settings_store.get_news_subscribed(888))

    def test_looking_up_language_registers_the_user_as_a_subscriber(self):
        # get_language() is the "have we seen this user" hook the broadcast
        # relies on - it must create a row, not just return a default.
        user_settings_store.get_language(555)

        self.assertIn(555, user_settings_store.list_subscribed_user_ids())

    def test_opting_out_removes_the_user_from_the_broadcast_list(self):
        user_settings_store.get_language(555)
        user_settings_store.set_news_subscribed(555, False)

        self.assertFalse(user_settings_store.get_news_subscribed(555))
        self.assertNotIn(555, user_settings_store.list_subscribed_user_ids())

    def test_opting_back_in_restores_the_subscription(self):
        user_settings_store.set_news_subscribed(555, False)
        user_settings_store.set_news_subscribed(555, True)

        self.assertIn(555, user_settings_store.list_subscribed_user_ids())


if __name__ == '__main__':
    unittest.main()
