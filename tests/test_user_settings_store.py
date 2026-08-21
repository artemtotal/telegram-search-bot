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


if __name__ == '__main__':
    unittest.main()
