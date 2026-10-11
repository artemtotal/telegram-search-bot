"""Пауза між тривогами адміну рахується від самої тривоги, а не від перевірки.

Монітори рахували її від `last_checked_at`, який оновлюється на кожній
перевірці, тож довга поломка давала одну тривогу і далі тишу.
"""

import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest import mock

from user_jobs import alert_cooldown, vonovia_monitor


class FakeBot:
    def __init__(self):
        self.sent = []

    def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text))


class AlertCooldownTests(unittest.TestCase):
    def setUp(self):
        alert_cooldown.reset()
        self.addCleanup(alert_cooldown.reset)

    def test_first_alert_is_always_due(self):
        self.assertTrue(alert_cooldown.due("x", timedelta(hours=2)))

    def test_alert_is_quiet_within_the_pause_and_due_after_it(self):
        start = alert_cooldown._now()
        alert_cooldown.mark("x", now=start)

        self.assertFalse(alert_cooldown.due("x", timedelta(hours=2), now=start + timedelta(minutes=90)))
        self.assertTrue(alert_cooldown.due("x", timedelta(hours=2), now=start + timedelta(hours=2)))

    def test_sources_do_not_share_a_pause(self):
        alert_cooldown.mark("a")

        self.assertTrue(alert_cooldown.due("b", timedelta(hours=2)))

    def test_a_long_outage_alerts_again_although_the_last_check_is_fresh(self):
        # Перевірка щойно була (last_checked_at = зараз), але тривога пішла
        # понад дві години тому — адмін має почути про поломку ще раз.
        alert_cooldown.mark(
            "vonovia:fetch",
            now=alert_cooldown._now() - vonovia_monitor.ERROR_ALERT_COOLDOWN - timedelta(minutes=1),
        )
        previous = {'last_status': 'error', 'last_checked_at': vonovia_monitor.vonovia_store.utc_now()}
        context = SimpleNamespace(bot=FakeBot())
        with mock.patch.object(vonovia_monitor, 'CHECK_ENABLED', True), \
             mock.patch.object(vonovia_monitor, 'ADMIN_ID', 312029534), \
             mock.patch('requests.Session', side_effect=RuntimeError('still down')), \
             mock.patch('user_jobs.vonovia_monitor.vonovia_store.latest_status', return_value=previous), \
             mock.patch('user_jobs.vonovia_monitor.vonovia_store.record_status'):
            result = vonovia_monitor.check_job(context)

        self.assertEqual(result['admin_alerted'], 1)
        self.assertEqual(len(context.bot.sent), 1)
        self.assertFalse(alert_cooldown.due("vonovia:fetch", vonovia_monitor.ERROR_ALERT_COOLDOWN))


if __name__ == "__main__":
    unittest.main()
