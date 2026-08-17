import unittest
from types import SimpleNamespace
from unittest import mock

from user_jobs import karlmarx_monitor


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


def _listing(key, title="Wohnung", rooms=2.0, area=97.0, price=2861.5):
    return {
        "listing_key": key, "title": title, "address": "Potsdam",
        "rooms": rooms, "area_m2": area, "price_eur": price,
        "detail_url": f"https://wgkarlmarx.de/fuer-wohnungssucher/expose/{key}",
    }


class KarlmarxMonitorCheckJobTests(unittest.TestCase):
    def test_disabled_flag_skips_the_scan_entirely(self):
        context = SimpleNamespace(bot=FakeBot())
        with mock.patch.object(karlmarx_monitor, 'CHECK_ENABLED', False), \
             mock.patch('user_jobs.karlmarx_monitor.karlmarx_store.record_status') as record_status, \
             mock.patch('requests.get') as get:
            result = karlmarx_monitor.check_job(context)

        get.assert_not_called()
        record_status.assert_called_once_with('disabled', listings_count=0)
        self.assertEqual(result, {'ok': 1, 'enabled': 0, 'sent': 0})

    def test_fetch_failure_records_an_error_status_without_crashing(self):
        context = SimpleNamespace(bot=FakeBot())
        with mock.patch.object(karlmarx_monitor, 'CHECK_ENABLED', True), \
             mock.patch('requests.get', side_effect=RuntimeError('timeout')), \
             mock.patch('user_jobs.karlmarx_monitor.karlmarx_store.record_status') as record_status:
            result = karlmarx_monitor.check_job(context)

        record_status.assert_called_once_with('error', listings_count=0, error='timeout')
        self.assertEqual(result['ok'], 0)

    def test_zero_residential_cards_is_not_treated_as_a_broken_parse(self):
        """0 residential matches is a normal day for Karl Marx — only 0 cards
        of ANY type (commercial included) signals a broken parser."""
        context = SimpleNamespace(bot=FakeBot())
        with mock.patch.object(karlmarx_monitor, 'CHECK_ENABLED', True), \
             mock.patch('requests.get', return_value=FakeResponse('irrelevant')), \
             mock.patch('user_jobs.karlmarx_monitor.karlmarx_parser.count_all_cards', return_value=8), \
             mock.patch('user_jobs.karlmarx_monitor.karlmarx_parser.parse_listings', return_value=[]), \
             mock.patch('user_jobs.karlmarx_monitor.karlmarx_store.record_status') as record_status, \
             mock.patch('user_jobs.karlmarx_monitor.karlmarx_store.upsert_listings', return_value=0), \
             mock.patch('user_jobs.karlmarx_monitor.karlmarx_store.list_active_listings', return_value=[]), \
             mock.patch('user_jobs.karlmarx_monitor.karlmarx_store.list_filters', return_value=[]), \
             mock.patch('user_jobs.karlmarx_monitor.karlmarx_store.delivered_pairs', return_value=set()):
            result = karlmarx_monitor.check_job(context)

        record_status.assert_called_once_with('ok', listings_count=0)
        self.assertEqual(len(context.bot.sent), 0)
        self.assertEqual(result['ok'], 1)

    def test_zero_cards_of_any_type_alerts_the_admin(self):
        context = SimpleNamespace(bot=FakeBot())
        with mock.patch.object(karlmarx_monitor, 'CHECK_ENABLED', True), \
             mock.patch.object(karlmarx_monitor, 'ADMIN_ID', 312029534), \
             mock.patch('requests.get', return_value=FakeResponse('irrelevant')), \
             mock.patch('user_jobs.karlmarx_monitor.karlmarx_parser.count_all_cards', return_value=0), \
             mock.patch('user_jobs.karlmarx_monitor.karlmarx_parser.parse_listings', return_value=[]), \
             mock.patch('user_jobs.karlmarx_monitor.karlmarx_store.record_status'), \
             mock.patch('user_jobs.karlmarx_monitor.karlmarx_store.upsert_listings', return_value=0), \
             mock.patch('user_jobs.karlmarx_monitor.karlmarx_store.list_active_listings', return_value=[]), \
             mock.patch('user_jobs.karlmarx_monitor.karlmarx_store.list_filters', return_value=[]), \
             mock.patch('user_jobs.karlmarx_monitor.karlmarx_store.delivered_pairs', return_value=set()):
            karlmarx_monitor.check_job(context)

        self.assertEqual(len(context.bot.sent), 1)
        self.assertEqual(context.bot.sent[0][0], 312029534)

    def test_matches_are_sent_and_marked_delivered(self):
        listing = _listing('potsdamer-mitte-gewerbe-bueroflaechen')
        filt = {'filter_id': 9, 'user_id': 544675510, 'max_price_eur': 3000.0}
        context = SimpleNamespace(bot=FakeBot())

        with mock.patch.object(karlmarx_monitor, 'CHECK_ENABLED', True), \
             mock.patch('requests.get', return_value=FakeResponse('irrelevant')), \
             mock.patch('user_jobs.karlmarx_monitor.karlmarx_parser.count_all_cards', return_value=2), \
             mock.patch('user_jobs.karlmarx_monitor.karlmarx_parser.parse_listings', return_value=[listing]), \
             mock.patch('user_jobs.karlmarx_monitor.karlmarx_store.record_status'), \
             mock.patch('user_jobs.karlmarx_monitor.karlmarx_store.upsert_listings', return_value=1), \
             mock.patch('user_jobs.karlmarx_monitor.karlmarx_store.list_active_listings', return_value=[listing]), \
             mock.patch('user_jobs.karlmarx_monitor.karlmarx_store.list_filters', return_value=[filt]), \
             mock.patch('user_jobs.karlmarx_monitor.karlmarx_store.delivered_pairs', return_value=set()), \
             mock.patch('user_jobs.karlmarx_monitor.karlmarx_store.mark_delivered') as mark_delivered:
            result = karlmarx_monitor.check_job(context)

        self.assertEqual(len(context.bot.sent), 1)
        mark_delivered.assert_called_once_with(9, 'potsdamer-mitte-gewerbe-bueroflaechen')
        self.assertEqual(result['sent'], 1)


if __name__ == '__main__':
    unittest.main()
