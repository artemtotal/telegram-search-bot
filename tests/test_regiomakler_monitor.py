import unittest
from types import SimpleNamespace
from unittest import mock

from user_jobs import regiomakler_monitor


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


def _listing(key, source, rooms=3.0, area=73.0, price=1700.0, is_rental=True, is_vacant=True, city="Potsdam"):
    return {
        "listing_key": key, "title": f"Wohnung {key}", "address": city, "city": city,
        "rooms": rooms, "area_m2": area, "price_eur": price if is_rental else None,
        "is_rental": is_rental, "is_vacant": is_vacant, "status": "" if is_vacant else "vermietet",
        "detail_url": f"https://example.de/{key}", "source": source,
    }


class RegiomaklerMonitorCheckJobTests(unittest.TestCase):
    def test_disabled_flag_skips_the_scan_entirely(self):
        context = SimpleNamespace(bot=FakeBot())
        with mock.patch.object(regiomakler_monitor, 'CHECK_ENABLED', False), \
             mock.patch('user_jobs.regiomakler_monitor.regiomakler_store.record_status') as record_status, \
             mock.patch('requests.get') as get:
            result = regiomakler_monitor.check_job(context)

        get.assert_not_called()
        record_status.assert_called_once_with('disabled', listings_count=0)
        self.assertEqual(result, {'ok': 1, 'enabled': 0, 'sent': 0})

    def test_fetch_failure_on_any_url_records_an_error_status(self):
        context = SimpleNamespace(bot=FakeBot())
        with mock.patch.object(regiomakler_monitor, 'CHECK_ENABLED', True), \
             mock.patch('requests.get', side_effect=RuntimeError('timeout')), \
             mock.patch('user_jobs.regiomakler_monitor.regiomakler_store.record_status') as record_status:
            result = regiomakler_monitor.check_job(context)

        record_status.assert_called_once_with('error', listings_count=0, error='timeout')
        self.assertEqual(result['ok'], 0)

    def test_duplicate_listing_from_both_sites_is_stored_and_sent_only_once(self):
        """The same Objekt-ID appears on both immoteam URLs and the alpha page —
        confirms real-world duplication is deduped before storage/notification."""
        dup_immoteam = _listing('12863_4', 'immoteam')
        dup_alpha = _listing('12863_4', 'alpha')
        immoteam_only = _listing('12766', 'immoteam', is_vacant=False)  # vermietet, excluded
        filt = {'filter_id': 9, 'user_id': 544675510, 'max_price_eur': 2000.0}
        context = SimpleNamespace(bot=FakeBot())

        def fake_parse(html_text, source):
            if source == 'immoteam':
                return [dup_immoteam, immoteam_only]
            return [dup_alpha]

        with mock.patch.object(regiomakler_monitor, 'CHECK_ENABLED', True), \
             mock.patch('requests.get', return_value=FakeResponse('irrelevant')), \
             mock.patch('user_jobs.regiomakler_monitor.regiomakler_parser.parse_listings', side_effect=fake_parse), \
             mock.patch('user_jobs.regiomakler_monitor.regiomakler_store.record_status'), \
             mock.patch('user_jobs.regiomakler_monitor.regiomakler_store.upsert_listings', return_value=1) as upsert, \
             mock.patch('user_jobs.regiomakler_monitor.regiomakler_store.list_active_listings', return_value=[dup_immoteam]), \
             mock.patch('user_jobs.regiomakler_monitor.regiomakler_store.list_filters', return_value=[filt]), \
             mock.patch('user_jobs.regiomakler_monitor.regiomakler_store.delivered_pairs', return_value=set()), \
             mock.patch('user_jobs.regiomakler_monitor.regiomakler_store.mark_delivered') as mark_delivered:
            result = regiomakler_monitor.check_job(context)

        # 3 requests total: two immoteam URLs + one alpha URL.
        stored_arg = upsert.call_args.args[0]
        self.assertEqual([item['listing_key'] for item in stored_arg], ['12863_4'])
        self.assertEqual(len(context.bot.sent), 1)
        mark_delivered.assert_called_once_with(9, '12863_4')
        self.assertEqual(result['sent'], 1)

    def test_non_potsdam_and_sale_listings_are_filtered_out(self):
        context = SimpleNamespace(bot=FakeBot())
        wide_region = _listing('99999', 'immoteam', city='Kleinmachnow')
        sale_listing = _listing('88888', 'immoteam', is_rental=False)

        with mock.patch.object(regiomakler_monitor, 'CHECK_ENABLED', True), \
             mock.patch('requests.get', return_value=FakeResponse('irrelevant')), \
             mock.patch(
                 'user_jobs.regiomakler_monitor.regiomakler_parser.parse_listings',
                 side_effect=lambda h, s: [wide_region, sale_listing] if s == 'immoteam' else [],
             ), \
             mock.patch('user_jobs.regiomakler_monitor.regiomakler_store.record_status'), \
             mock.patch('user_jobs.regiomakler_monitor.regiomakler_store.upsert_listings', return_value=0) as upsert, \
             mock.patch('user_jobs.regiomakler_monitor.regiomakler_store.list_active_listings', return_value=[]), \
             mock.patch('user_jobs.regiomakler_monitor.regiomakler_store.list_filters', return_value=[]), \
             mock.patch('user_jobs.regiomakler_monitor.regiomakler_store.delivered_pairs', return_value=set()):
            regiomakler_monitor.check_job(context)

        upsert.assert_called_once_with([])


if __name__ == '__main__':
    unittest.main()
