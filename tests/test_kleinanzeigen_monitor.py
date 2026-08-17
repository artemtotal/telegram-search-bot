import unittest
from types import SimpleNamespace
from unittest import mock

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
    def test_polls_once_an_hour_not_every_30_minutes(self):
        """Explicit user decision: Kleinanzeigen is scraped less often than the other sources."""
        self.assertEqual(kleinanzeigen_monitor.CHECK_INTERVAL_SECONDS, 3600)

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
             mock.patch('requests.get', side_effect=RuntimeError('timeout')), \
             mock.patch('user_jobs.kleinanzeigen_monitor.kleinanzeigen_store.record_status') as record_status:
            result = kleinanzeigen_monitor.check_job(context)

        record_status.assert_called_once_with('error', listings_count=0, error='timeout')
        self.assertEqual(result['ok'], 0)

    def test_nearby_towns_and_swap_listings_are_filtered_out(self):
        context = SimpleNamespace(bot=FakeBot())
        potsdam = _listing('1')
        nearby = _listing('2', city='Rathenow')
        swap = _listing('3', title='TAUSCHWOHNUNG 2-Zimmer gegen Berlin')

        with mock.patch.object(kleinanzeigen_monitor, 'CHECK_ENABLED', True), \
             mock.patch('requests.get', return_value=FakeResponse('irrelevant')), \
             mock.patch(
                 'user_jobs.kleinanzeigen_monitor.kleinanzeigen_parser.parse_listings',
                 return_value=[potsdam, nearby, swap],
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
