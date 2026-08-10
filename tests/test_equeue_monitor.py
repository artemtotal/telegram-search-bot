import os
import unittest
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


if __name__ == "__main__":
    unittest.main()
