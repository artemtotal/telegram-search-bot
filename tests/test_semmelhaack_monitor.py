import unittest
from types import SimpleNamespace
from unittest import mock

from user_jobs import semmelhaack_monitor


class FakeBot:
    def __init__(self):
        self.sent = []

    def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text, kwargs))


class FakeResponse:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


class SemmelhaackMonitorCheckJobTests(unittest.TestCase):
    def test_disabled_flag_skips_the_scan_entirely(self):
        context = SimpleNamespace(bot=FakeBot())
        with mock.patch.object(semmelhaack_monitor, 'CHECK_ENABLED', False), \
             mock.patch('user_jobs.semmelhaack_monitor.semmelhaack_store.record_status') as record_status, \
             mock.patch('requests.get') as get:
            result = semmelhaack_monitor.check_job(context)

        get.assert_not_called()
        record_status.assert_called_once_with('disabled', listings_count=0)
        self.assertEqual(result, {'ok': 1, 'enabled': 0, 'sent': 0})

    def test_fetch_failure_records_an_error_status_without_crashing(self):
        context = SimpleNamespace(bot=FakeBot())
        with mock.patch.object(semmelhaack_monitor, 'CHECK_ENABLED', True), \
             mock.patch.object(semmelhaack_monitor, 'ADMIN_ID', 0), \
             mock.patch('requests.get', side_effect=RuntimeError('timeout')), \
             mock.patch('user_jobs.semmelhaack_monitor.semmelhaack_store.latest_status', return_value={}), \
             mock.patch('user_jobs.semmelhaack_monitor.semmelhaack_store.record_status') as record_status:
            result = semmelhaack_monitor.check_job(context)

        record_status.assert_called_once_with('error', listings_count=0, error='timeout')
        self.assertEqual(result['ok'], 0)

    def test_first_fetch_failure_alerts_the_admin(self):
        context = SimpleNamespace(bot=FakeBot())
        with mock.patch.object(semmelhaack_monitor, 'CHECK_ENABLED', True), \
             mock.patch.object(semmelhaack_monitor, 'ADMIN_ID', 312029534), \
             mock.patch('requests.get', side_effect=RuntimeError('connection refused')), \
             mock.patch('user_jobs.semmelhaack_monitor.semmelhaack_store.latest_status', return_value={}), \
             mock.patch('user_jobs.semmelhaack_monitor.semmelhaack_store.record_status'):
            result = semmelhaack_monitor.check_job(context)

        self.assertEqual(len(context.bot.sent), 1)
        chat_id, text, _ = context.bot.sent[0]
        self.assertEqual(chat_id, 312029534)
        self.assertIn('connection refused', text)
        self.assertEqual(result['admin_alerted'], 1)

    def test_a_second_failure_within_the_cooldown_does_not_alert_again(self):
        context = SimpleNamespace(bot=FakeBot())
        previous_status = {'last_status': 'error', 'last_checked_at': semmelhaack_monitor.semmelhaack_store.utc_now()}
        with mock.patch.object(semmelhaack_monitor, 'CHECK_ENABLED', True), \
             mock.patch.object(semmelhaack_monitor, 'ADMIN_ID', 312029534), \
             mock.patch('requests.get', side_effect=RuntimeError('still down')), \
             mock.patch('user_jobs.semmelhaack_monitor.semmelhaack_store.latest_status', return_value=previous_status), \
             mock.patch('user_jobs.semmelhaack_monitor.semmelhaack_store.record_status'):
            result = semmelhaack_monitor.check_job(context)

        self.assertEqual(context.bot.sent, [])
        self.assertEqual(result['admin_alerted'], 0)

    def test_zero_listings_across_all_of_germany_alerts_the_admin(self):
        context = SimpleNamespace(bot=FakeBot())
        with mock.patch.object(semmelhaack_monitor, 'CHECK_ENABLED', True), \
             mock.patch.object(semmelhaack_monitor, 'ADMIN_ID', 312029534), \
             mock.patch('requests.get', return_value=FakeResponse('<html>no cards here</html>')), \
             mock.patch('user_jobs.semmelhaack_monitor.semmelhaack_store.record_status'), \
             mock.patch('user_jobs.semmelhaack_monitor.semmelhaack_store.upsert_listings', return_value=0), \
             mock.patch('user_jobs.semmelhaack_monitor.semmelhaack_store.list_active_listings', return_value=[]), \
             mock.patch('user_jobs.semmelhaack_monitor.semmelhaack_store.list_filters', return_value=[]), \
             mock.patch('user_jobs.semmelhaack_monitor.semmelhaack_store.delivered_pairs', return_value=set()):
            semmelhaack_monitor.check_job(context)

        self.assertEqual(len(context.bot.sent), 1)
        self.assertEqual(context.bot.sent[0][0], 312029534)
        self.assertIn('0 оголошень по всій Німеччині', context.bot.sent[0][1])

    def test_matches_are_sent_and_marked_delivered_for_potsdam_listings_only(self):
        html_page = "irrelevant, parse_listings is mocked"
        potsdam_listing = {
            'listing_key': '1', 'title': 'Wohnung', 'address': 'X, 14476 Potsdam', 'city': 'Potsdam',
            'rooms': 4.0, 'area_m2': 90.0, 'price_eur': 1500.0,
            'detail_url': 'https://semmelhaack.de/x', 'image_url': '',
        }
        hamburg_listing = {**potsdam_listing, 'listing_key': '2', 'city': 'Hamburg'}
        filt = {'filter_id': 9, 'user_id': 544675510, 'max_price_eur': 2000.0}
        context = SimpleNamespace(bot=FakeBot())

        with mock.patch.object(semmelhaack_monitor, 'CHECK_ENABLED', True), \
             mock.patch('requests.get', return_value=FakeResponse(html_page)), \
             mock.patch(
                 'user_jobs.semmelhaack_monitor.semmelhaack_parser.parse_listings',
                 return_value=[potsdam_listing, hamburg_listing],
             ) as parse_listings, \
             mock.patch('user_jobs.semmelhaack_monitor.semmelhaack_store.record_status'), \
             mock.patch('user_jobs.semmelhaack_monitor.semmelhaack_store.upsert_listings', return_value=1) as upsert, \
             mock.patch('user_jobs.semmelhaack_monitor.semmelhaack_store.list_active_listings', return_value=[potsdam_listing]), \
             mock.patch('user_jobs.semmelhaack_monitor.semmelhaack_store.list_filters', return_value=[filt]), \
             mock.patch('user_jobs.semmelhaack_monitor.semmelhaack_store.delivered_pairs', return_value=set()), \
             mock.patch('user_jobs.semmelhaack_monitor.semmelhaack_store.mark_delivered') as mark_delivered:
            result = semmelhaack_monitor.check_job(context)

        parse_listings.assert_called_once_with(html_page)
        # Only the Potsdam listing is passed on to storage — Hamburg/etc. never enters the DB.
        upsert.assert_called_once_with([potsdam_listing])
        self.assertEqual(len(context.bot.sent), 1)
        self.assertEqual(context.bot.sent[0][0], 544675510)
        mark_delivered.assert_called_once_with(9, '1')
        self.assertEqual(result['sent'], 1)


if __name__ == '__main__':
    unittest.main()
