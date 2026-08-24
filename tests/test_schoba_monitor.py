import unittest
from types import SimpleNamespace
from unittest import mock

import requests

from user_jobs import schoba_monitor


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


class SchobaMonitorCheckJobTests(unittest.TestCase):
    def test_disabled_flag_skips_the_scan_entirely(self):
        context = SimpleNamespace(bot=FakeBot())
        with mock.patch.object(schoba_monitor, 'CHECK_ENABLED', False), \
             mock.patch('user_jobs.schoba_monitor.schoba_store.record_status') as record_status, \
             mock.patch('requests.get') as get:
            result = schoba_monitor.check_job(context)

        get.assert_not_called()
        record_status.assert_called_once_with('disabled', listings_count=0)
        self.assertEqual(result, {'ok': 1, 'enabled': 0, 'sent': 0})

    def test_fetch_failure_records_an_error_status_without_crashing(self):
        context = SimpleNamespace(bot=FakeBot())
        with mock.patch.object(schoba_monitor, 'CHECK_ENABLED', True), \
             mock.patch.object(schoba_monitor, 'ADMIN_ID', 0), \
             mock.patch('requests.get', side_effect=RuntimeError('timeout')), \
             mock.patch('user_jobs.schoba_monitor.schoba_store.latest_status', return_value={}), \
             mock.patch('user_jobs.schoba_monitor.schoba_store.record_status') as record_status:
            result = schoba_monitor.check_job(context)

        record_status.assert_called_once_with('error', listings_count=0, error='timeout')
        self.assertEqual(result['ok'], 0)

    def test_first_fetch_failure_alerts_the_admin(self):
        context = SimpleNamespace(bot=FakeBot())
        with mock.patch.object(schoba_monitor, 'CHECK_ENABLED', True), \
             mock.patch.object(schoba_monitor, 'ADMIN_ID', 312029534), \
             mock.patch('requests.get', side_effect=RuntimeError('connection refused')), \
             mock.patch('user_jobs.schoba_monitor.schoba_store.latest_status', return_value={}), \
             mock.patch('user_jobs.schoba_monitor.schoba_store.record_status'):
            result = schoba_monitor.check_job(context)

        self.assertEqual(len(context.bot.sent), 1)
        chat_id, text, _ = context.bot.sent[0]
        self.assertEqual(chat_id, 312029534)
        self.assertIn('connection refused', text)
        self.assertEqual(result['admin_alerted'], 1)

    def test_a_second_failure_within_the_cooldown_does_not_alert_again(self):
        context = SimpleNamespace(bot=FakeBot())
        previous_status = {'last_status': 'error', 'last_checked_at': schoba_monitor.schoba_store.utc_now()}
        with mock.patch.object(schoba_monitor, 'CHECK_ENABLED', True), \
             mock.patch.object(schoba_monitor, 'ADMIN_ID', 312029534), \
             mock.patch('requests.get', side_effect=RuntimeError('still down')), \
             mock.patch('user_jobs.schoba_monitor.schoba_store.latest_status', return_value=previous_status), \
             mock.patch('user_jobs.schoba_monitor.schoba_store.record_status'):
            result = schoba_monitor.check_job(context)

        self.assertEqual(context.bot.sent, [])
        self.assertEqual(result['admin_alerted'], 0)

    def test_zero_cards_at_all_alerts_the_admin(self):
        context = SimpleNamespace(bot=FakeBot())
        with mock.patch.object(schoba_monitor, 'CHECK_ENABLED', True), \
             mock.patch.object(schoba_monitor, 'ADMIN_ID', 312029534), \
             mock.patch('requests.get', return_value=FakeResponse('<html>no cards</html>')), \
             mock.patch('user_jobs.schoba_monitor.schoba_store.record_status'), \
             mock.patch('user_jobs.schoba_monitor.schoba_store.upsert_listings', return_value=0), \
             mock.patch('user_jobs.schoba_monitor.schoba_store.list_active_listings', return_value=[]), \
             mock.patch('user_jobs.schoba_monitor.schoba_store.list_filters', return_value=[]), \
             mock.patch('user_jobs.schoba_monitor.schoba_store.delivered_pairs', return_value=set()):
            schoba_monitor.check_job(context)

        self.assertEqual(len(context.bot.sent), 1)
        self.assertEqual(context.bot.sent[0][0], 312029534)
        self.assertIn('0 оголошень', context.bot.sent[0][1])

    def test_only_vacant_listings_are_stored_and_matched(self):
        vacant = {
            'listing_key': '1', 'title': 'Wohnung', 'address': 'Potsdam, Babelsberg', 'is_vacant': True,
            'rooms': 3.0, 'area_m2': 61.0, 'price_eur': 700.37, 'detail_url': 'https://www.schoba.de/x.htm',
        }
        rented = {**vacant, 'listing_key': '2', 'is_vacant': False, 'price_eur': 0.0}
        filt = {'filter_id': 9, 'user_id': 544675510, 'max_price_eur': 1000.0}
        context = SimpleNamespace(bot=FakeBot())

        with mock.patch.object(schoba_monitor, 'CHECK_ENABLED', True), \
             mock.patch('requests.get', return_value=FakeResponse('irrelevant')), \
             mock.patch(
                 'user_jobs.schoba_monitor.schoba_parser.parse_listings', return_value=[vacant, rented],
             ) as parse_listings, \
             mock.patch('user_jobs.schoba_monitor.schoba_store.record_status'), \
             mock.patch('user_jobs.schoba_monitor.schoba_store.upsert_listings', return_value=1) as upsert, \
             mock.patch('user_jobs.schoba_monitor.schoba_store.list_active_listings', return_value=[vacant]), \
             mock.patch('user_jobs.schoba_monitor.schoba_store.list_filters', return_value=[filt]), \
             mock.patch('user_jobs.schoba_monitor.schoba_store.delivered_pairs', return_value=set()), \
             mock.patch('user_jobs.schoba_monitor.schoba_store.mark_delivered') as mark_delivered:
            result = schoba_monitor.check_job(context)

        parse_listings.assert_called_once_with('irrelevant')
        upsert.assert_called_once_with([vacant])
        self.assertEqual(len(context.bot.sent), 1)
        self.assertEqual(context.bot.sent[0][0], 544675510)
        mark_delivered.assert_called_once_with(9, '1')
        self.assertEqual(result['sent'], 1)


class SchobaFetchRetryTests(unittest.TestCase):
    """schoba.de intermittently drops the TLS connection mid-handshake
    (SSLZeroReturnError); an immediate retry succeeds. Without retries every
    one of those blips raised an admin alert about a broken check even though
    the site was perfectly healthy."""

    def test_a_transient_tls_drop_is_retried_and_succeeds(self):
        error = requests.exceptions.SSLError('TLS/SSL connection has been closed (EOF)')

        with mock.patch('requests.get', side_effect=[error, FakeResponse('page html')]) as get, \
             mock.patch('user_jobs.schoba_monitor.time.sleep') as sleep, \
             mock.patch(
                 'user_jobs.schoba_monitor.schoba_parser.parse_listings', return_value=['listing'],
             ):
            listings = schoba_monitor._fetch_listings()

        self.assertEqual(listings, ['listing'])
        self.assertEqual(get.call_count, 2)
        sleep.assert_called_once_with(schoba_monitor._RETRY_BACKOFF_SECONDS[0])

    def test_a_genuinely_down_site_still_raises_after_every_attempt(self):
        error = requests.exceptions.SSLError('still down')

        with mock.patch('requests.get', side_effect=error) as get, \
             mock.patch('user_jobs.schoba_monitor.time.sleep'):
            with self.assertRaises(requests.exceptions.SSLError):
                schoba_monitor._fetch_listings()

        self.assertEqual(get.call_count, schoba_monitor._FETCH_ATTEMPTS)

    def test_a_working_site_is_fetched_once_without_any_sleeping(self):
        with mock.patch('requests.get', return_value=FakeResponse('page html')) as get, \
             mock.patch('user_jobs.schoba_monitor.time.sleep') as sleep, \
             mock.patch(
                 'user_jobs.schoba_monitor.schoba_parser.parse_listings', return_value=[],
             ):
            schoba_monitor._fetch_listings()

        self.assertEqual(get.call_count, 1)
        sleep.assert_not_called()

    def test_repeated_transient_drops_still_recover_on_the_last_attempt(self):
        error = requests.exceptions.SSLError('flaky')

        with mock.patch(
            'requests.get', side_effect=[error, error, FakeResponse('page html')],
        ) as get, \
             mock.patch('user_jobs.schoba_monitor.time.sleep'), \
             mock.patch(
                 'user_jobs.schoba_monitor.schoba_parser.parse_listings', return_value=['listing'],
             ):
            listings = schoba_monitor._fetch_listings()

        self.assertEqual(listings, ['listing'])
        self.assertEqual(get.call_count, 3)


if __name__ == '__main__':
    unittest.main()
