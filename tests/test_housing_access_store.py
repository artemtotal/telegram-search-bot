import unittest
from datetime import datetime, timedelta

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
            [{
                'user_id': 777, 'display_name': 'Новий користувач', 'active': True,
                'expires_at': None, 'is_trial': False,
            }],
        )

    def test_grant_access_reactivates_existing_user(self):
        housing_access_store.grant_access(777, 'Старе імʼя')
        housing_access_store.set_active(777, False)

        housing_access_store.grant_access(777, 'Нове імʼя')

        self.assertTrue(housing_access_store.is_allowed(777))
        self.assertEqual(housing_access_store.list_users()[0]['display_name'], 'Нове імʼя')

    def test_revoke_access_removes_the_user_entirely(self):
        housing_access_store.grant_access(777, 'Хтось')

        self.assertTrue(housing_access_store.revoke_access(777))

        self.assertFalse(housing_access_store.is_allowed(777))
        self.assertEqual(housing_access_store.list_users(), [])

    def test_revoke_access_on_an_unknown_user_reports_no_row_removed(self):
        self.assertFalse(housing_access_store.revoke_access(999))

    def test_grant_access_stores_the_expiry_date(self):
        expires = datetime.utcnow() + timedelta(days=30)

        housing_access_store.grant_access(777, 'Хтось', expires_at=expires)

        self.assertEqual(housing_access_store.list_users()[0]['expires_at'], expires)

    def test_expiring_soon_only_lists_users_within_the_window_who_are_not_yet_warned(self):
        soon = datetime.utcnow() + timedelta(days=2)
        far = datetime.utcnow() + timedelta(days=30)
        housing_access_store.grant_access(1, 'Скоро закінчується', expires_at=soon)
        housing_access_store.grant_access(2, 'Ще далеко', expires_at=far)
        housing_access_store.grant_access(3, 'Без терміну')

        expiring = [row['user_id'] for row in housing_access_store.list_expiring_soon(within_days=3)]

        self.assertEqual(expiring, [1])

    def test_marking_the_notice_sent_drops_the_user_from_the_expiring_list(self):
        soon = datetime.utcnow() + timedelta(days=1)
        housing_access_store.grant_access(1, 'Хтось', expires_at=soon)

        housing_access_store.mark_notice_sent(1)

        self.assertEqual(housing_access_store.list_expiring_soon(within_days=3), [])

    def test_renewing_access_resets_the_notice_flag(self):
        soon = datetime.utcnow() + timedelta(days=1)
        housing_access_store.grant_access(1, 'Хтось', expires_at=soon)
        housing_access_store.mark_notice_sent(1)

        new_expiry = datetime.utcnow() + timedelta(days=30)
        housing_access_store.grant_access(1, 'Хтось', expires_at=new_expiry)

        self.assertEqual(
            [row['user_id'] for row in housing_access_store.list_expiring_soon(within_days=3)], [],
        )

    def test_expired_lists_only_active_users_past_their_expiry(self):
        past = datetime.utcnow() - timedelta(days=1)
        future = datetime.utcnow() + timedelta(days=30)
        housing_access_store.grant_access(1, 'Прострочено', expires_at=past)
        housing_access_store.grant_access(2, 'Ще діє', expires_at=future)
        housing_access_store.grant_access(3, 'Прострочено, але вимкнено', expires_at=past)
        housing_access_store.set_active(3, False)

        expired = [row['user_id'] for row in housing_access_store.list_expired()]

        self.assertEqual(expired, [1])

    def test_expiring_soon_and_expired_can_be_narrowed_to_trial_or_paid(self):
        soon = datetime.utcnow() + timedelta(hours=12)
        housing_access_store.grant_trial(1, 'Тріал', expires_at=soon)
        housing_access_store.grant_access(2, 'Платно', expires_at=soon)

        trial_expiring = [r['user_id'] for r in housing_access_store.list_expiring_soon(within_days=3, trial=True)]
        paid_expiring = [r['user_id'] for r in housing_access_store.list_expiring_soon(within_days=3, trial=False)]
        self.assertEqual(trial_expiring, [1])
        self.assertEqual(paid_expiring, [2])

        housing_access_store.mark_notice_sent(1)
        housing_access_store.mark_notice_sent(2)
        # Push both into the past so list_expired sees them.
        housing_access_store.grant_trial(1, 'Тріал', expires_at=datetime.utcnow() - timedelta(hours=1))
        housing_access_store.grant_access(2, 'Платно', expires_at=datetime.utcnow() - timedelta(hours=1))

        self.assertEqual([r['user_id'] for r in housing_access_store.list_expired(trial=True)], [1])
        self.assertEqual([r['user_id'] for r in housing_access_store.list_expired(trial=False)], [2])

    def test_grant_trial_marks_the_trial_used_permanently(self):
        self.assertFalse(housing_access_store.has_used_trial(777))

        housing_access_store.grant_trial(777, 'Хтось', expires_at=datetime.utcnow() + timedelta(days=7))

        self.assertTrue(housing_access_store.has_used_trial(777))
        self.assertTrue(housing_access_store.is_trial(777))
        # Even after the access row is deleted entirely, the trial stays
        # marked as used - that's the whole point of the separate table.
        housing_access_store.revoke_access(777)
        self.assertTrue(housing_access_store.has_used_trial(777))

    def test_grant_access_clears_any_leftover_trial_state(self):
        housing_access_store.grant_trial(777, 'Хтось', expires_at=datetime.utcnow() + timedelta(days=7))
        housing_access_store.set_trial_dormant(777, datetime.utcnow() + timedelta(days=3))

        housing_access_store.grant_access(777, 'Хтось', expires_at=datetime.utcnow() + timedelta(days=30))

        self.assertFalse(housing_access_store.is_trial(777))
        self.assertTrue(housing_access_store.is_allowed(777))

    def test_set_trial_dormant_stops_access_without_forgetting_it_was_a_trial(self):
        housing_access_store.grant_trial(777, 'Хтось', expires_at=datetime.utcnow() - timedelta(hours=1))

        grace_ends = datetime.utcnow() + timedelta(days=3)
        self.assertTrue(housing_access_store.set_trial_dormant(777, grace_ends))

        self.assertFalse(housing_access_store.is_allowed(777))
        self.assertEqual(housing_access_store.list_trial_grace_expired(), [])

    def test_list_trial_grace_expired_only_lists_dormant_trials_past_their_grace_period(self):
        housing_access_store.grant_trial(1, 'Скоро прибрати', expires_at=datetime.utcnow() - timedelta(days=8))
        housing_access_store.set_trial_dormant(1, datetime.utcnow() - timedelta(hours=1))
        housing_access_store.grant_trial(2, 'Ще в грейсі', expires_at=datetime.utcnow() - timedelta(days=1))
        housing_access_store.set_trial_dormant(2, datetime.utcnow() + timedelta(days=2))

        expired = [r['user_id'] for r in housing_access_store.list_trial_grace_expired()]

        self.assertEqual(expired, [1])


if __name__ == '__main__':
    unittest.main()