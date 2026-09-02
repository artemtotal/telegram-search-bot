import unittest
from types import SimpleNamespace
from unittest import mock

from user_jobs import vonovia_monitor


class FakeBot:
    def __init__(self):
        self.sent = []
        self.photos = []
        self.albums = []
        self.calls = []

    def send_message(self, chat_id, text, **kwargs):
        self.calls.append('message')
        self.sent.append((chat_id, text, kwargs))

    def send_photo(self, **kwargs):
        self.calls.append('photo')
        self.photos.append(kwargs)

    def send_media_group(self, **kwargs):
        self.calls.append('album')
        self.albums.append(kwargs)


class FakeResponse:
    def __init__(self, payload=None, text=''):
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload

    def raise_for_status(self):
        pass


def _api_result(key, rooms=2, area=63.3, price=841.89, title='2-Zimmer-Wohnung'):
    return {
        'wrk_id': key, 'titel': title, 'strasse': 'Weitmarer Str. 145 a',
        'plz': '44795', 'ort': 'Bochum OT Weitmar', 'preis': price,
        'groesse': area, 'anzahl_zimmer': rooms,
        'preview_img_url': 'https://cdn.expose.vonovia.de/VNA-a.jpg?width=324&crop=4:3',
        'imageUrls': ['https://cdn.expose.vonovia.de/VNA-a.jpg?width=324&crop=4:3'],
        'slug': f'wohnung-84-{key}',
    }


class FakeSession:
    """Двійник сесії: запам'ятовує, у якому порядку куди ходили.

    Порядок тут не дрібниця — API Vonovia відповідає 406 усьому, що прийшло
    без куки сторінки видачі.
    """

    def __init__(self, pages):
        self.pages = pages
        self.visited = []
        self.headers = {}

    def get(self, url, params=None, headers=None, timeout=None):
        self.visited.append(url)
        if url == vonovia_monitor.vonovia_parser.SEARCH_PAGE_URL:
            return FakeResponse(text='<html>Suchergebnisse</html>')
        offset = int((params or {}).get('offset') or 0)
        index = offset // vonovia_monitor.vonovia_parser.PAGE_SIZE
        results = self.pages[index] if index < len(self.pages) else []
        total = sum(len(page) for page in self.pages)
        return FakeResponse(payload={'paging': {'info': {'count': total, 'limit': 15}}, 'results': results})


class VonoviaFetchTests(unittest.TestCase):
    def test_the_results_page_is_visited_before_the_api(self):
        """Без куки з тієї сторінки портал відповідає 406, а не даними."""
        session = FakeSession([[_api_result('1')]])
        with mock.patch('requests.Session', return_value=session):
            vonovia_monitor._fetch_listings()

        self.assertEqual(session.visited[0], vonovia_monitor.vonovia_parser.SEARCH_PAGE_URL)
        self.assertEqual(session.visited[1], vonovia_monitor.vonovia_parser.LIST_URL)

    def test_a_second_page_is_fetched_when_the_portal_reports_more(self):
        first = [_api_result(str(index)) for index in range(15)]
        session = FakeSession([first, [_api_result('extra')]])
        with mock.patch('requests.Session', return_value=session):
            listings = vonovia_monitor._fetch_listings()

        self.assertEqual(len(listings), 16)
        self.assertIn('extra', [item['listing_key'] for item in listings])

    def test_paging_stops_once_the_reported_total_is_covered(self):
        session = FakeSession([[_api_result('1'), _api_result('2')]])
        with mock.patch('requests.Session', return_value=session):
            vonovia_monitor._fetch_listings()

        self.assertEqual(session.visited.count(vonovia_monitor.vonovia_parser.LIST_URL), 1)


class VonoviaCheckJobTests(unittest.TestCase):
    def test_disabled_flag_skips_the_scan_entirely(self):
        context = SimpleNamespace(bot=FakeBot())
        with mock.patch.object(vonovia_monitor, 'CHECK_ENABLED', False), \
             mock.patch('user_jobs.vonovia_monitor.vonovia_store.record_status') as record_status, \
             mock.patch('requests.Session') as session:
            result = vonovia_monitor.check_job(context)

        session.assert_not_called()
        record_status.assert_called_once_with('disabled', listings_count=0)
        self.assertEqual(result, {'ok': 1, 'enabled': 0, 'sent': 0})

    def test_an_empty_potsdam_is_a_normal_day_not_a_broken_parser(self):
        """У Потсдамі Vonovia зараз не має жодної квартири — самі гаражі.

        Порожній результат тут не привід будити адміна: тривога лишається
        тільки на випадок, коли запит справді не пройшов.
        """
        context = SimpleNamespace(bot=FakeBot())
        with mock.patch.object(vonovia_monitor, 'CHECK_ENABLED', True), \
             mock.patch.object(vonovia_monitor, 'ADMIN_ID', 312029534), \
             mock.patch.object(vonovia_monitor, '_fetch_listings', return_value=[]), \
             mock.patch.object(vonovia_monitor, '_add_full_rent', return_value=0), \
             mock.patch('user_jobs.vonovia_monitor.vonovia_store.upsert_listings', return_value=0), \
             mock.patch('user_jobs.vonovia_monitor.vonovia_store.record_status') as record_status, \
             mock.patch('user_jobs.vonovia_monitor.vonovia_store.list_active_listings', return_value=[]), \
             mock.patch('user_jobs.vonovia_monitor.vonovia_store.list_filters', return_value=[]), \
             mock.patch('user_jobs.vonovia_monitor.vonovia_store.delivered_pairs', return_value=set()):
            result = vonovia_monitor.check_job(context)

        record_status.assert_called_once_with('ok', listings_count=0)
        self.assertEqual(context.bot.calls, [])
        self.assertEqual(result['ok'], 1)

    def test_fetch_failure_records_an_error_and_alerts_the_admin(self):
        context = SimpleNamespace(bot=FakeBot())
        with mock.patch.object(vonovia_monitor, 'CHECK_ENABLED', True), \
             mock.patch.object(vonovia_monitor, 'ADMIN_ID', 312029534), \
             mock.patch('requests.Session', side_effect=RuntimeError('connection refused')), \
             mock.patch('user_jobs.vonovia_monitor.vonovia_store.latest_status', return_value={}), \
             mock.patch('user_jobs.vonovia_monitor.vonovia_store.record_status') as record_status:
            result = vonovia_monitor.check_job(context)

        record_status.assert_called_once_with('error', listings_count=0, error='connection refused')
        self.assertIn('connection refused', context.bot.sent[0][1])
        self.assertEqual(result['admin_alerted'], 1)

    def test_a_second_failure_within_the_cooldown_does_not_alert_again(self):
        context = SimpleNamespace(bot=FakeBot())
        previous = {'last_status': 'error', 'last_checked_at': vonovia_monitor.vonovia_store.utc_now()}
        with mock.patch.object(vonovia_monitor, 'CHECK_ENABLED', True), \
             mock.patch.object(vonovia_monitor, 'ADMIN_ID', 312029534), \
             mock.patch('requests.Session', side_effect=RuntimeError('still down')), \
             mock.patch('user_jobs.vonovia_monitor.vonovia_store.latest_status', return_value=previous), \
             mock.patch('user_jobs.vonovia_monitor.vonovia_store.record_status'):
            result = vonovia_monitor.check_job(context)

        self.assertEqual(context.bot.sent, [])
        self.assertEqual(result['admin_alerted'], 0)

    def test_a_match_arrives_as_a_photo_post_with_the_text_as_caption(self):
        listing = {
            'listing_key': '1439890008', 'title': 'Wohnung', 'address': 'Bochum',
            'rooms': 2.5, 'area_m2': 63.3, 'price_eur': 841.89, 'price_warm_eur': 1111.89,
            'gallery_urls': [
                'https://cdn.expose.vonovia.de/VNA-a.jpg?width=1200',
                'https://cdn.expose.vonovia.de/VNA-b.jpg?width=1200',
            ],
            'detail_url': 'https://www.vonovia.de/zuhause-finden/immobilien/wohnung-84-1439890008',
        }
        filt = {'filter_id': 7, 'user_id': 544675510, 'active': True, 'max_price_eur': 1500.0}
        context = SimpleNamespace(bot=FakeBot())
        with mock.patch.object(vonovia_monitor, 'CHECK_ENABLED', True), \
             mock.patch.object(vonovia_monitor, '_fetch_listings', return_value=[listing]), \
             mock.patch.object(vonovia_monitor, '_add_full_rent', return_value=0), \
             mock.patch('user_jobs.vonovia_monitor.vonovia_store.upsert_listings', return_value=1), \
             mock.patch('user_jobs.vonovia_monitor.vonovia_store.record_status'), \
             mock.patch('user_jobs.vonovia_monitor.vonovia_store.list_active_listings', return_value=[listing]), \
             mock.patch('user_jobs.vonovia_monitor.vonovia_store.list_filters', return_value=[filt]), \
             mock.patch('user_jobs.vonovia_monitor.vonovia_store.delivered_pairs', return_value=set()), \
             mock.patch('user_jobs.vonovia_monitor.vonovia_store.mark_delivered') as mark:
            result = vonovia_monitor.check_job(context)

        self.assertEqual(context.bot.calls, ['album'])
        self.assertEqual(len(context.bot.albums[0]['media']), 2)
        mark.assert_called_once_with(7, '1439890008')
        self.assertEqual(result['sent'], 1)

    def test_a_listing_without_photos_still_arrives_as_text(self):
        listing = {
            'listing_key': 'nopics', 'title': 'Wohnung', 'address': 'Bochum',
            'rooms': 2.0, 'area_m2': 50.0, 'price_eur': 700.0,
            'gallery_urls': [], 'cover_image_url': '',
            'detail_url': 'https://www.vonovia.de/zuhause-finden/immobilien/nopics',
        }
        filt = {'filter_id': 7, 'user_id': 544675510, 'active': True}
        context = SimpleNamespace(bot=FakeBot())
        with mock.patch.object(vonovia_monitor, 'CHECK_ENABLED', True), \
             mock.patch.object(vonovia_monitor, '_fetch_listings', return_value=[listing]), \
             mock.patch.object(vonovia_monitor, '_add_full_rent', return_value=0), \
             mock.patch('user_jobs.vonovia_monitor.vonovia_store.upsert_listings', return_value=1), \
             mock.patch('user_jobs.vonovia_monitor.vonovia_store.record_status'), \
             mock.patch('user_jobs.vonovia_monitor.vonovia_store.list_active_listings', return_value=[listing]), \
             mock.patch('user_jobs.vonovia_monitor.vonovia_store.list_filters', return_value=[filt]), \
             mock.patch('user_jobs.vonovia_monitor.vonovia_store.delivered_pairs', return_value=set()), \
             mock.patch('user_jobs.vonovia_monitor.vonovia_store.mark_delivered'):
            vonovia_monitor.check_job(context)

        self.assertEqual(context.bot.calls, ['message'])


class VonoviaFullRentTests(unittest.TestCase):
    DETAIL = (
        '<div data-vonovia-data="&#x7B;&quot;rent&quot;&#x3A;841.89,'
        '&quot;warmRent&quot;&#x3A;1111.89,&quot;operatingCosts&quot;&#x3A;176,'
        '&quot;heatingCosts&quot;&#x3A;94&#x7D;"></div>'
    )

    def test_only_listings_without_the_full_rent_are_visited(self):
        listings = [
            {'listing_key': 'priced', 'detail_url': 'https://www.vonovia.de/x/priced'},
            {'listing_key': 'unpriced', 'detail_url': 'https://www.vonovia.de/x/unpriced'},
        ]
        with mock.patch('user_jobs.vonovia_monitor.vonovia_store.keys_with_full_rent', return_value={'priced'}), \
             mock.patch('requests.get', return_value=FakeResponse(text=self.DETAIL)) as get:
            filled = vonovia_monitor._add_full_rent(listings)

        self.assertEqual(filled, 1)
        self.assertEqual(get.call_count, 1)
        self.assertEqual(listings[1]['price_warm_eur'], 1111.89)
        self.assertNotIn('price_warm_eur', listings[0])

    def test_one_unreadable_listing_page_does_not_break_the_rest(self):
        listings = [
            {'listing_key': 'broken', 'detail_url': 'https://www.vonovia.de/x/broken'},
            {'listing_key': 'fine', 'detail_url': 'https://www.vonovia.de/x/fine'},
        ]
        responses = [RuntimeError('500'), FakeResponse(text=self.DETAIL)]
        with mock.patch('user_jobs.vonovia_monitor.vonovia_store.keys_with_full_rent', return_value=set()), \
             mock.patch('requests.get', side_effect=responses):
            filled = vonovia_monitor._add_full_rent(listings)

        self.assertEqual(filled, 1)
        self.assertEqual(listings[1]['price_warm_eur'], 1111.89)
