import unittest
from datetime import timedelta
from unittest import mock

from user_jobs import propotsdam_monitor, propotsdam_store


class FakeBot:
    def __init__(self):
        self.messages = []

    def send_message(self, **kwargs):
        self.messages.append(kwargs)


class FakeContext:
    def __init__(self):
        self.bot = FakeBot()


class ProPotsdamMonitorTests(unittest.TestCase):
    def test_schedule_runs_every_thirty_minutes(self):
        self.assertEqual(propotsdam_monitor.CHECK_INTERVAL_SECONDS, 1800)

    def test_check_job_does_not_overlap_an_existing_scan(self):
        context = FakeContext()
        propotsdam_monitor._scan_lock.acquire()
        try:
            with mock.patch.object(propotsdam_monitor, 'PROPOTSDAM_CHECK_ENABLED', True), \
                 mock.patch.object(propotsdam_monitor, '_request_scan') as request_scan:
                result = propotsdam_monitor.check_job(context)
        finally:
            propotsdam_monitor._scan_lock.release()

        self.assertEqual(result, {'ok': 1, 'enabled': 1, 'skipped': 1, 'sent': 0})
        request_scan.assert_not_called()

    def test_failed_scan_notifies_admin_with_relogin_hint(self):
        context = FakeContext()
        with mock.patch.object(propotsdam_monitor, 'PROPOTSDAM_CHECK_ENABLED', True), \
             mock.patch.object(propotsdam_monitor, 'ADMIN_ID', 312029534), \
             mock.patch.object(propotsdam_monitor, '_request_scan', side_effect=RuntimeError('login required')), \
             mock.patch.object(propotsdam_store, 'latest_status', return_value={'last_status': 'ok'}), \
             mock.patch.object(propotsdam_store, 'record_status') as record_status:
            result = propotsdam_monitor.check_job(context)

        self.assertEqual(result['ok'], 0)
        self.assertEqual(result['admin_alerted'], 1)
        self.assertEqual(context.bot.messages[0]['chat_id'], 312029534)
        self.assertIn('ProPotsdam не смог собрать квартиры', context.bot.messages[0]['text'])
        self.assertIn('Как чинить', context.bot.messages[0]['text'])
        record_status.assert_called_once_with('error', listings_count=0, error='login required')

    def test_repeated_error_is_rate_limited(self):
        context = FakeContext()
        with mock.patch.object(propotsdam_monitor, 'PROPOTSDAM_CHECK_ENABLED', True), \
             mock.patch.object(propotsdam_monitor, 'ADMIN_ID', 312029534), \
             mock.patch.object(propotsdam_monitor, '_request_scan', side_effect=RuntimeError('login required')), \
             mock.patch.object(propotsdam_store, 'latest_status', return_value={
                 'last_status': 'error',
                 'last_checked_at': propotsdam_store.utc_now() - timedelta(minutes=5),
             }), \
             mock.patch.object(propotsdam_store, 'record_status'):
            result = propotsdam_monitor.check_job(context)

        self.assertEqual(result['admin_alerted'], 0)
        self.assertEqual(context.bot.messages, [])


if __name__ == '__main__':
    unittest.main()
