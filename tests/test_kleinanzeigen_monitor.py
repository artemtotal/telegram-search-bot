import unittest
from types import SimpleNamespace
from unittest import mock

import requests

from user_jobs import kleinanzeigen_monitor


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


def _listing(key, city="Potsdam", title="Wohnung", rooms=3.0, area=76.0, price=572.0):
    return {
        "listing_key": key, "title": title, "address": f"{city}", "city": city,
        "rooms": rooms, "area_m2": area, "price_eur": price,
        "detail_url": f"https://www.kleinanzeigen.de/s-anzeige/x/{key}",
    }


class KleinanzeigenMonitorCheckJobTests(unittest.TestCase):
    def test_polls_every_20_minutes(self):
        """Explicit user decision, 2026-08-21: an experiment. Suchauftrag email
        alerts run an hour or slower and would duplicate the bot's own delivery
        tracking, so instead of switching channels, tighten scraping and watch
        _notify_admin_fetch_failed() for signs of being blocked (60 -> 30 -> 20)."""
        self.assertEqual(kleinanzeigen_monitor.CHECK_INTERVAL_SECONDS, 1200)

    def test_disabled_flag_skips_the_scan_entirely(self):
        context = SimpleNamespace(bot=FakeBot())
        with mock.patch.object(kleinanzeigen_monitor, 'CHECK_ENABLED', False), \
             mock.patch('user_jobs.kleinanzeigen_monitor.kleinanzeigen_store.record_status') as record_status, \
             mock.patch('requests.get') as get:
            result = kleinanzeigen_monitor.check_job(context)

        get.assert_not_called()
        record_status.assert_called_once_with('disabled', listings_count=0)
        self.assertEqual(result, {'ok': 1, 'enabled': 0, 'sent': 0})

    def test_fetch_failure_records_an_error_status_without_crashing(self):
        context = SimpleNamespace(bot=FakeBot())
        with mock.patch.object(kleinanzeigen_monitor, 'CHECK_ENABLED', True), \
             mock.patch.object(kleinanzeigen_monitor, 'ADMIN_ID', 0), \
             mock.patch('requests.get', side_effect=RuntimeError('timeout')), \
             mock.patch('user_jobs.kleinanzeigen_monitor.kleinanzeigen_store.latest_status', return_value={}), \
             mock.patch('user_jobs.kleinanzeigen_monitor.kleinanzeigen_store.record_status') as record_status:
            result = kleinanzeigen_monitor.check_job(context)

        record_status.assert_called_once_with('error', listings_count=0, error='timeout')
        self.assertEqual(result['ok'], 0)

    def test_first_fetch_failure_alerts_the_admin(self):
        """The one thing to actually watch during the 20-minute experiment."""
        context = SimpleNamespace(bot=FakeBot())
        with mock.patch.object(kleinanzeigen_monitor, 'CHECK_ENABLED', True), \
             mock.patch.object(kleinanzeigen_monitor, 'ADMIN_ID', 312029534), \
             mock.patch('requests.get', side_effect=RuntimeError('connection refused')), \
             mock.patch('user_jobs.kleinanzeigen_monitor.kleinanzeigen_store.latest_status', return_value={}), \
             mock.patch('user_jobs.kleinanzeigen_monitor.kleinanzeigen_store.record_status'):
            result = kleinanzeigen_monitor.check_job(context)

        self.assertEqual(len(context.bot.sent), 1)
        chat_id, text, _ = context.bot.sent[0]
        self.assertEqual(chat_id, 312029534)
        self.assertIn('connection refused', text)
        self.assertEqual(result['admin_alerted'], 1)

    def test_a_403_response_flags_it_as_a_likely_block(self):
        context = SimpleNamespace(bot=FakeBot())
        error = requests.HTTPError('403 Client Error')
        error.response = SimpleNamespace(status_code=403)
        with mock.patch.object(kleinanzeigen_monitor, 'CHECK_ENABLED', True), \
             mock.patch.object(kleinanzeigen_monitor, 'ADMIN_ID', 312029534), \
             mock.patch('requests.get', side_effect=error), \
             mock.patch('user_jobs.kleinanzeigen_monitor.kleinanzeigen_store.latest_status', return_value={}), \
             mock.patch('user_jobs.kleinanzeigen_monitor.kleinanzeigen_store.record_status'):
            kleinanzeigen_monitor.check_job(context)

        text = context.bot.sent[0][1]
        self.assertIn('блокувати', text)

    def test_a_second_failure_within_the_cooldown_does_not_alert_again(self):
        context = SimpleNamespace(bot=FakeBot())
        previous_status = {'last_status': 'error', 'last_checked_at': kleinanzeigen_monitor.kleinanzeigen_store.utc_now()}
        with mock.patch.object(kleinanzeigen_monitor, 'CHECK_ENABLED', True), \
             mock.patch.object(kleinanzeigen_monitor, 'ADMIN_ID', 312029534), \
             mock.patch('requests.get', side_effect=RuntimeError('still down')), \
             mock.patch('user_jobs.kleinanzeigen_monitor.kleinanzeigen_store.latest_status', return_value=previous_status), \
             mock.patch('user_jobs.kleinanzeigen_monitor.kleinanzeigen_store.record_status'):
            result = kleinanzeigen_monitor.check_job(context)

        self.assertEqual(context.bot.sent, [])
        self.assertEqual(result['admin_alerted'], 0)

    def test_nearby_towns_and_swap_listings_are_filtered_out(self):
        # Regression: "Wohnungsswap - 3 Zimmer, ..." (a commercial swap
        # account's ads) slipped through notifications because only the
        # German "tausch" was excluded, not the English "swap" some swap
        # posters use in their titles instead.
        context = SimpleNamespace(bot=FakeBot())
        potsdam = _listing('1')
        nearby = _listing('2', city='Rathenow')
        swap_de = _listing('3', title='TAUSCHWOHNUNG 2-Zimmer gegen Berlin')
        swap_en = _listing('4', title='Wohnungsswap - 3 Zimmer, schöne Terrasse')

        with mock.patch.object(kleinanzeigen_monitor, 'CHECK_ENABLED', True), \
             mock.patch('requests.get', return_value=FakeResponse('irrelevant')), \
             mock.patch(
                 'user_jobs.kleinanzeigen_monitor.kleinanzeigen_parser.parse_listings',
                 return_value=[potsdam, nearby, swap_de, swap_en],
             ), \
             mock.patch('user_jobs.kleinanzeigen_monitor.kleinanzeigen_store.record_status'), \
             mock.patch('user_jobs.kleinanzeigen_monitor.kleinanzeigen_store.upsert_listings', return_value=1) as upsert, \
             mock.patch('user_jobs.kleinanzeigen_monitor.kleinanzeigen_store.list_active_listings', return_value=[]), \
             mock.patch('user_jobs.kleinanzeigen_monitor.kleinanzeigen_store.list_filters', return_value=[]), \
             mock.patch('user_jobs.kleinanzeigen_monitor.kleinanzeigen_store.delivered_pairs', return_value=set()):
            kleinanzeigen_monitor.check_job(context)

        upsert.assert_called_once_with([potsdam])

    def test_matches_are_sent_and_marked_delivered(self):
        potsdam = _listing('1')
        filt = {'filter_id': 9, 'user_id': 544675510, 'max_price_eur': 1000.0}
        context = SimpleNamespace(bot=FakeBot())

        with mock.patch.object(kleinanzeigen_monitor, 'CHECK_ENABLED', True), \
             mock.patch('requests.get', return_value=FakeResponse('irrelevant')), \
             mock.patch('user_jobs.kleinanzeigen_monitor.kleinanzeigen_parser.parse_listings', return_value=[potsdam]), \
             mock.patch('user_jobs.kleinanzeigen_monitor.kleinanzeigen_store.record_status'), \
             mock.patch('user_jobs.kleinanzeigen_monitor.kleinanzeigen_store.upsert_listings', return_value=1), \
             mock.patch('user_jobs.kleinanzeigen_monitor.kleinanzeigen_store.list_active_listings', return_value=[potsdam]), \
             mock.patch('user_jobs.kleinanzeigen_monitor.kleinanzeigen_store.list_filters', return_value=[filt]), \
             mock.patch('user_jobs.kleinanzeigen_monitor.kleinanzeigen_store.delivered_pairs', return_value=set()), \
             mock.patch('user_jobs.kleinanzeigen_monitor.kleinanzeigen_store.mark_delivered') as mark_delivered:
            result = kleinanzeigen_monitor.check_job(context)

        self.assertEqual(len(context.bot.sent), 1)
        mark_delivered.assert_called_once_with(9, '1')
        self.assertEqual(result['sent'], 1)


if __name__ == '__main__':
    unittest.main()
