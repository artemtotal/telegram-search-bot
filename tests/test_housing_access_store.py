import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database import Base
from user_jobs import housing_access_store


class HousingAccessStoreTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            'sqlite://',
            connect_args={'check_same_thread': False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.session = sessionmaker(bind=self.engine)
        self.original_session = housing_access_store.DBSession
        housing_access_store.DBSession = self.session

    def tearDown(self):
        housing_access_store.DBSession = self.original_session
        self.engine.dispose()

    def test_granted_user_remains_allowed_without_filters(self):
        housing_access_store.grant_access(777, 'Новий користувач')

        self.assertTrue(housing_access_store.is_allowed(777))
        self.assertEqual(
            housing_access_store.list_users(),
            [{'user_id': 777, 'display_name': 'Новий користувач', 'active': True}],
        )

    def test_grant_access_reactivates_existing_user(self):
        housing_access_store.grant_access(777, 'Старе імʼя')
        housing_access_store.set_active(777, False)

        housing_access_store.grant_access(777, 'Нове імʼя')

        self.assertTrue(housing_access_store.is_allowed(777))
        self.assertEqual(housing_access_store.list_users()[0]['display_name'], 'Нове імʼя')


if __name__ == '__main__':
    unittest.main()