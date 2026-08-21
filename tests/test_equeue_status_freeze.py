"""Відмітка браузерної перевірки ДП більше не залежить від підписки.

Перевірку робить Chrome один раз на всіх, але час писався лише в активні
підписки. З вимкненою підпискою жоден рядок не оновлювався, і меню роками
показувало б момент останнього вмикання — мовчазний простій виглядав як
свіжа перевірка «вільні терміни не підтверджені».
"""

import unittest
from datetime import datetime, timedelta
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import database
from database import Base, EqueueStatus, EqueueSubscription


class EqueueStatusFreezeTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://')
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.patches = [
            mock.patch.object(database, 'DBSession', self.Session),
        ]
        import user_handlers.equeue_monitor as monitor

        self.monitor = monitor
        self.patches.append(mock.patch.object(monitor, 'DBSession', self.Session))
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in self.patches:
            patch.stop()

    def _disabled_subscription(self, checked_at):
        session = self.Session()
        now = datetime.utcnow()
        session.add(EqueueSubscription(
            user_id=312029534,
            service=self.monitor.SERVICE_KEY,
            active=False,
            last_status='none',
            last_checked_at=checked_at,
            created_at=now,
            updated_at=now,
        ))
        session.commit()
        session.close()

    def test_check_is_recorded_while_the_subscription_is_off(self):
        stale = datetime.utcnow() - timedelta(hours=2)
        self._disabled_subscription(stale)

        self.monitor.handle_browser_result(
            mock.Mock(),
            {"source": self.monitor.SERVICE_KEY, "status": "none", "reason": "місць немає"},
        )

        session = self.Session()
        row = session.query(EqueueStatus).filter(EqueueStatus.service == self.monitor.SERVICE_KEY).first()
        session.close()
        self.assertIsNotNone(row, "браузерний результат мусить зберігатись і без підписок")
        self.assertGreater(row.last_checked_at, stale)
        self.assertEqual(row.last_status, "none")

    def test_menu_shows_the_fresh_check_not_the_frozen_one(self):
        stale = datetime.utcnow() - timedelta(hours=2)
        self._disabled_subscription(stale)
        self.monitor.handle_browser_result(
            mock.Mock(),
            {"source": self.monitor.SERVICE_KEY, "status": "none", "reason": "місць немає"},
        )

        text = self.monitor._latest_status_text(312029534)

        frozen = self.monitor._format_berlin_time(stale)
        self.assertNotIn(frozen, text)
        self.assertIn("вільні терміни не підтверджені", text)

    def test_old_database_still_falls_back_to_subscription_rows(self):
        """База, яка ще не бачила жодного результату, не мусить втратити історію."""

        stale = datetime.utcnow() - timedelta(hours=2)
        self._disabled_subscription(stale)

        text = self.monitor._latest_status_text(312029534)

        self.assertIn(self.monitor._format_berlin_time(stale), text)

    def test_empty_database_says_no_check_arrived(self):
        text = self.monitor._latest_status_text(312029534)

        self.assertIn("ще не надходила", text)

    def test_admin_is_alerted_on_a_blocked_check_even_with_no_subscribers(self):
        """A failed/blocked browser check used to skip the admin alert
        entirely whenever nobody was actively subscribed (handle_browser_result
        checked `if not subscribers: return` before checking `result["ok"]`) -
        an operational problem (Cloudflare blocking the checker) has nothing
        to do with subscriber count and must always reach the admin."""
        bot = mock.Mock()
        with mock.patch.object(self.monitor, 'ADMIN_ID', 312029534):
            self.monitor.handle_browser_result(
                bot,
                {"source": self.monitor.SERVICE_KEY, "status": "blocked", "reason": "Cloudflare"},
            )

        bot.send_message.assert_called_once()
        args, kwargs = bot.send_message.call_args
        self.assertEqual(args[0], 312029534)


if __name__ == '__main__':
    unittest.main()
