import unittest
from types import SimpleNamespace
from unittest import mock

import requests

from user_jobs import schoba_monitor


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


class SchobaGalleryTests(unittest.TestCase):
    LISTING = {'listing_key': '1', 'detail_url': 'https://www.schoba.de/immobilien/angebote/vm-ro-20.htm'}
    DETAIL_HTML = (
        '<img src="bilder/objekt-id-foto-galerie-1gr.jpg">'
        '<img src="bilder/objekt-id-foto-galerie-2gr.jpg">'
    )

    def test_the_full_gallery_is_fetched_from_the_detail_page(self):
        with mock.patch('requests.get', return_value=FakeResponse(self.DETAIL_HTML)) as get:
            urls = schoba_monitor._fetch_gallery(self.LISTING)

        self.assertEqual(urls, [
            'https://www.schoba.de/immobilien/angebote/bilder/objekt-id-foto-galerie-1gr.jpg',
            'https://www.schoba.de/immobilien/angebote/bilder/objekt-id-foto-galerie-2gr.jpg',
        ])
        get.assert_called_once()
        self.assertEqual(get.call_args.args[0], self.LISTING['detail_url'])

    def test_more_photos_than_an_album_holds_are_trimmed(self):
        many = ''.join(
            f'<img src="bilder/objekt-id-foto-galerie-{i}gr.jpg">' for i in range(1, 15)
        )
        with mock.patch('requests.get', return_value=FakeResponse(many)):
            urls = schoba_monitor._fetch_gallery(self.LISTING)

        self.assertEqual(len(urls), schoba_monitor.GALLERY_ALBUM_MAX)

    def test_a_page_without_a_gallery_yields_no_photos(self):
        """В отличие от SEMMELHAACK, у SCHOBA нет обложки про запас — карточка
        каталога вообще не содержит фото, поэтому падать назад некуда."""
        with mock.patch('requests.get', return_value=FakeResponse('<html>no gallery</html>')):
            self.assertEqual(schoba_monitor._fetch_gallery(self.LISTING), [])

    def test_an_unreachable_detail_page_yields_no_photos_without_raising(self):
        with mock.patch('requests.get', side_effect=RuntimeError('timeout')):
            self.assertEqual(schoba_monitor._fetch_gallery(self.LISTING), [])

    def test_no_detail_url_yields_nothing_without_a_request(self):
        with mock.patch('requests.get') as get:
            urls = schoba_monitor._fetch_gallery({'listing_key': 'x'})

        get.assert_not_called()
        self.assertEqual(urls, [])

    def test_a_transient_tls_drop_on_the_detail_page_is_retried(self):
        error = requests.exceptions.SSLError('TLS/SSL connection has been closed (EOF)')
        with mock.patch('requests.get', side_effect=[error, FakeResponse(self.DETAIL_HTML)]) as get, \
             mock.patch('user_jobs.schoba_monitor.time.sleep'):
            urls = schoba_monitor._fetch_gallery(self.LISTING)

        self.assertEqual(len(urls), 2)
        self.assertEqual(get.call_count, 2)


class SchobaListingPostTests(unittest.TestCase):
    """_send_listing: единый пост — галерея фото с текстом объявления подписью."""

    LISTING = {'listing_key': '1', 'title': 'Wohnung'}
    TEXT = 'Нова квартира SCHOBA\n\nАдреса: Beispielstr. 1'

    def test_several_photos_go_out_as_one_album_with_the_text_as_caption(self):
        bot = FakeBot()
        with mock.patch.object(schoba_monitor, '_fetch_gallery', return_value=['u1', 'u2']):
            posted_as_caption = schoba_monitor._send_listing(bot, 312029534, self.LISTING, self.TEXT)

        self.assertTrue(posted_as_caption)
        self.assertEqual(bot.calls, ['album'])
        media = bot.albums[0]['media']
        self.assertEqual(len(media), 2)
        self.assertEqual(media[0].caption, self.TEXT)
        self.assertEqual(media[0].parse_mode, 'HTML')
        self.assertIsNone(getattr(media[1], 'caption', None))

    def test_a_single_photo_carries_the_text_as_its_caption(self):
        bot = FakeBot()
        with mock.patch.object(schoba_monitor, '_fetch_gallery', return_value=['u1']):
            posted_as_caption = schoba_monitor._send_listing(bot, 312029534, self.LISTING, self.TEXT)

        self.assertTrue(posted_as_caption)
        self.assertEqual(bot.calls, ['photo'])
        self.assertEqual(bot.photos[0]['caption'], self.TEXT)
        self.assertEqual(bot.photos[0]['photo'], 'u1')

    def test_a_listing_without_photos_sends_nothing(self):
        bot = FakeBot()
        with mock.patch.object(schoba_monitor, '_fetch_gallery', return_value=[]):
            posted_as_caption = schoba_monitor._send_listing(bot, 312029534, self.LISTING, self.TEXT)

        self.assertFalse(posted_as_caption)
        self.assertEqual(bot.calls, [])

    def test_text_over_the_caption_limit_still_goes_out_as_a_bare_album(self):
        bot = FakeBot()
        long_text = 'x' * (schoba_monitor.CAPTION_LIMIT + 1)
        with mock.patch.object(schoba_monitor, '_fetch_gallery', return_value=['u1', 'u2']):
            posted_as_caption = schoba_monitor._send_listing(bot, 312029534, self.LISTING, long_text)

        self.assertFalse(posted_as_caption)
        self.assertEqual(bot.calls, ['album'])
        self.assertIsNone(getattr(bot.albums[0]['media'][0], 'caption', None))

    def test_a_failed_album_is_swallowed(self):
        bot = FakeBot()
        bot.send_media_group = mock.Mock(side_effect=RuntimeError('Telegram said no'))
        with mock.patch.object(schoba_monitor, '_fetch_gallery', return_value=['u1', 'u2']):
            posted_as_caption = schoba_monitor._send_listing(bot, 312029534, self.LISTING, self.TEXT)

        self.assertFalse(posted_as_caption)


class SchobaDeliveryTests(unittest.TestCase):
    VACANT = {
        'listing_key': '1', 'title': 'Wohnung', 'address': 'Potsdam, Babelsberg', 'is_vacant': True,
        'rooms': 3.0, 'area_m2': 61.0, 'price_eur': 700.37,
        'detail_url': 'https://www.schoba.de/immobilien/angebote/vm-gl-52.htm',
    }

    def test_a_listing_with_a_gallery_goes_out_as_a_single_post(self):
        context = SimpleNamespace(bot=FakeBot())
        filt = {'filter_id': 9, 'user_id': 544675510, 'max_price_eur': 1000.0}
        detail_html = '<img src="bilder/objekt-id-foto-galerie-1gr.jpg">'
        with mock.patch.object(schoba_monitor, 'CHECK_ENABLED', True), \
             mock.patch('requests.get', return_value=FakeResponse(detail_html)), \
             mock.patch(
                 'user_jobs.schoba_monitor.schoba_parser.parse_listings', return_value=[self.VACANT],
             ), \
             mock.patch('user_jobs.schoba_monitor.schoba_store.record_status'), \
             mock.patch('user_jobs.schoba_monitor.schoba_store.upsert_listings', return_value=1), \
             mock.patch('user_jobs.schoba_monitor.schoba_store.list_active_listings', return_value=[self.VACANT]), \
             mock.patch('user_jobs.schoba_monitor.schoba_store.list_filters', return_value=[filt]), \
             mock.patch('user_jobs.schoba_monitor.schoba_store.delivered_pairs', return_value=set()), \
             mock.patch('user_jobs.schoba_monitor.schoba_store.mark_delivered') as mark_delivered:
            result = schoba_monitor.check_job(context)

        self.assertEqual(context.bot.calls, ['photo'])
        self.assertIn('SCHOBA', context.bot.photos[0]['caption'])
        self.assertEqual(result['sent'], 1)
        mark_delivered.assert_called_once_with(9, '1')

    def test_a_listing_is_still_delivered_as_text_when_it_has_no_gallery(self):
        context = SimpleNamespace(bot=FakeBot())
        filt = {'filter_id': 9, 'user_id': 544675510, 'max_price_eur': 1000.0}
        with mock.patch.object(schoba_monitor, 'CHECK_ENABLED', True), \
             mock.patch('requests.get', return_value=FakeResponse('<html>no gallery</html>')), \
             mock.patch(
                 'user_jobs.schoba_monitor.schoba_parser.parse_listings', return_value=[self.VACANT],
             ), \
             mock.patch('user_jobs.schoba_monitor.schoba_store.record_status'), \
             mock.patch('user_jobs.schoba_monitor.schoba_store.upsert_listings', return_value=1), \
             mock.patch('user_jobs.schoba_monitor.schoba_store.list_active_listings', return_value=[self.VACANT]), \
             mock.patch('user_jobs.schoba_monitor.schoba_store.list_filters', return_value=[filt]), \
             mock.patch('user_jobs.schoba_monitor.schoba_store.delivered_pairs', return_value=set()), \
             mock.patch('user_jobs.schoba_monitor.schoba_store.mark_delivered'):
            result = schoba_monitor.check_job(context)

        self.assertEqual(context.bot.calls, ['message'])
        self.assertEqual(result['sent'], 1)


if __name__ == '__main__':
    unittest.main()
