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
            # «Open DP Document checks to all users» зняв білий список, тож чергу
            # бачить будь-який Telegram ID; закритим лишається тільки порожній.
            self.assertTrue(monitor.is_allowed(333))
            self.assertFalse(monitor.is_allowed(None))
            self.assertEqual(len(list(monitor.private_home_rows(111))), 1)

    def test_cloudflare_challenge_detection(self):
        from user_handlers.equeue_monitor import _looks_like_cloudflare_challenge

        self.assertTrue(_looks_like_cloudflare_challenge("Just a moment... cf_chl", 403))
        self.assertFalse(_looks_like_cloudflare_challenge("Just a moment... cf_chl", 200))

    def test_latest_status_uses_newest_browser_check_globally(self):
        """`_latest_status_text` prefers the live `EqueueStatus` singleton row
        (kept fresh by this same container's real scheduled browser check) and
        only falls back to `EqueueSubscription` rows when that singleton has
        no timestamp yet. The real row is neutralized for the duration of the
        test (not deleted) and always restored, since it reflects genuine
        production monitoring state that other code and users rely on.
        """
        import user_handlers.equeue_monitor as monitor

        session = monitor.DBSession()
        user_ids = [900000001, 900000002]
        status_row = session.query(monitor.EqueueStatus).filter(
            monitor.EqueueStatus.service == monitor.SERVICE_KEY
        ).first()
        original_status = None
        if status_row is not None:
            original_status = {
                "last_checked_at": status_row.last_checked_at,
                "last_status": status_row.last_status,
                "last_reason": status_row.last_reason,
            }
            status_row.last_checked_at = None
            session.commit()
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
            if status_row is not None:
                status_row.last_checked_at = original_status["last_checked_at"]
                status_row.last_status = original_status["last_status"]
                status_row.last_reason = original_status["last_reason"]
            session.commit()
            session.close()


class EqueueTranslationSmokeTests(unittest.TestCase):
    """Not a full duplicate of every uk-language assertion - just enough to
    prove lang actually reaches the rendered text for this module."""

    def test_menu_keyboard_in_russian_and_german(self):
        from user_handlers.equeue_monitor import _menu_keyboard

        ru_labels = [b.text for row in _menu_keyboard(False, lang='ru').inline_keyboard for b in row]
        de_labels = [b.text for row in _menu_keyboard(False, lang='de').inline_keyboard for b in row]

        self.assertIn('🔔 Подписаться на проверку', ru_labels)
        self.assertIn('🔔 Prüfung abonnieren', de_labels)

    def test_render_menu_in_russian_and_german(self):
        from user_handlers.equeue_monitor import _render_menu

        ru = _render_menu(True, lang='ru')
        de = _render_menu(True, lang='de')

        self.assertIn('включена', ru)
        self.assertIn('aktiviert', de)


if __name__ == "__main__":
    unittest.main()
