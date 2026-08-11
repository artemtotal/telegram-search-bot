import os
import unittest
from datetime import datetime
from unittest import mock


class EqueueMonitorTest(unittest.TestCase):
    def test_parse_negative_text(self):
        from user_handlers.equeue_monitor import parse_availability

        available, reason = parse_availability("Наразі вільних місць немає")
        self.assertFalse(available)
        self.assertIn("немає", reason)

    def test_parse_positive_text(self):
        from user_handlers.equeue_monitor import parse_availability

        available, _reason = parse_availability("Є вільні терміни на завтра")
        self.assertTrue(available)

    def test_allowed_admin_and_extra_ids(self):
        with mock.patch.dict(
            os.environ,
            {
                "ADMIN_ID": "312029534",
                "PASSPORT_EQUEUE_ALLOWED_USER_IDS": "111, 222",
            },
            clear=False,
        ):
            import importlib
            import user_handlers.equeue_monitor as monitor

            importlib.reload(monitor)
            self.assertTrue(monitor.is_allowed(312029534))
            self.assertTrue(monitor.is_allowed(111))
            self.assertFalse(monitor.is_allowed(333))
            self.assertEqual(len(list(monitor.private_home_rows(111))), 1)

    def test_cloudflare_challenge_detection(self):
        from user_handlers.equeue_monitor import _looks_like_cloudflare_challenge

        self.assertTrue(_looks_like_cloudflare_challenge("Just a moment... cf_chl", 403))
        self.assertFalse(_looks_like_cloudflare_challenge("Just a moment... cf_chl", 200))

    def test_latest_status_uses_newest_browser_check_globally(self):
        import user_handlers.equeue_monitor as monitor

        session = monitor.DBSession()
        user_ids = [900000001, 900000002]
        try:
            session.query(monitor.EqueueSubscription).filter(
                monitor.EqueueSubscription.user_id.in_(user_ids)
            ).delete(synchronize_session=False)
            old_row = monitor.EqueueSubscription(
                user_id=user_ids[0],
                username="old",
                display_name="Old",
                service=monitor.SERVICE_KEY,
                active=True,
                last_status="none",
                last_checked_at=datetime(2099, 1, 1, 8, 0, 0),
                created_at=datetime(2099, 1, 1, 8, 0, 0),
                updated_at=datetime(2099, 1, 1, 8, 0, 0),
            )
            newest_row = monitor.EqueueSubscription(
                user_id=user_ids[1],
                username="new",
                display_name="New",
                service=monitor.SERVICE_KEY,
                active=True,
                last_status="available",
                last_checked_at=datetime(2099, 1, 1, 10, 0, 0),
                created_at=datetime(2099, 1, 1, 10, 0, 0),
                updated_at=datetime(2099, 1, 1, 10, 0, 0),
            )
            session.add_all([old_row, newest_row])
            session.commit()

            text = monitor._latest_status_text(user_ids[0])
            self.assertIn("11:00", text)
            self.assertIn("є ознаки", text)
        finally:
            session.query(monitor.EqueueSubscription).filter(
                monitor.EqueueSubscription.user_id.in_(user_ids)
            ).delete(synchronize_session=False)
            session.commit()
            session.close()


if __name__ == "__main__":
    unittest.main()
