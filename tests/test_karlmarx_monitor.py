import unittest
from types import SimpleNamespace
from unittest import mock

from user_jobs import karlmarx_monitor


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


def _listing(key, title="Wohnung", rooms=2.0, area=97.0, price=2861.5, cover_image_url=""):
    return {
        "listing_key": key, "title": title, "address": "Potsdam",
        "rooms": rooms, "area_m2": area, "price_eur": price,
        "detail_url": f"https://wgkarlmarx.de/fuer-wohnungssucher/expose/{key}",
        "cover_image_url": cover_image_url,
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
             mock.patch.object(karlmarx_monitor, 'ADMIN_ID', 0), \
             mock.patch('requests.get', side_effect=RuntimeError('timeout')), \
             mock.patch('user_jobs.karlmarx_monitor.karlmarx_store.latest_status', return_value={}), \
             mock.patch('user_jobs.karlmarx_monitor.karlmarx_store.record_status') as record_status:
            result = karlmarx_monitor.check_job(context)

        record_status.assert_called_once_with('error', listings_count=0, error='timeout')
        self.assertEqual(result['ok'], 0)

    def test_first_fetch_failure_alerts_the_admin(self):
        context = SimpleNamespace(bot=FakeBot())
        with mock.patch.object(karlmarx_monitor, 'CHECK_ENABLED', True), \
             mock.patch.object(karlmarx_monitor, 'ADMIN_ID', 312029534), \
             mock.patch('requests.get', side_effect=RuntimeError('connection refused')), \
             mock.patch('user_jobs.karlmarx_monitor.karlmarx_store.latest_status', return_value={}), \
             mock.patch('user_jobs.karlmarx_monitor.karlmarx_store.record_status'):
            result = karlmarx_monitor.check_job(context)

        self.assertEqual(len(context.bot.sent), 1)
        chat_id, text, _ = context.bot.sent[0]
        self.assertEqual(chat_id, 312029534)
        self.assertIn('connection refused', text)
        self.assertEqual(result['admin_alerted'], 1)

    def test_a_second_failure_within_the_cooldown_does_not_alert_again(self):
        context = SimpleNamespace(bot=FakeBot())
        previous_status = {'last_status': 'error', 'last_checked_at': karlmarx_monitor.karlmarx_store.utc_now()}
        with mock.patch.object(karlmarx_monitor, 'CHECK_ENABLED', True), \
             mock.patch.object(karlmarx_monitor, 'ADMIN_ID', 312029534), \
             mock.patch('requests.get', side_effect=RuntimeError('still down')), \
             mock.patch('user_jobs.karlmarx_monitor.karlmarx_store.latest_status', return_value=previous_status), \
             mock.patch('user_jobs.karlmarx_monitor.karlmarx_store.record_status'):
            result = karlmarx_monitor.check_job(context)

        self.assertEqual(context.bot.sent, [])
        self.assertEqual(result['admin_alerted'], 0)

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


class KarlmarxGalleryTests(unittest.TestCase):
    LISTING = _listing('x', cover_image_url='https://wgkarlmarx.de/fileadmin/cover.jpg')
    DETAIL_HTML = (
        "<div style=\"background-image: url('/fileadmin/user_upload/wgkarlmarx/user_upload/1.jpg')\"></div>"
        "<div style=\"background-image: url('/fileadmin/user_upload/wgkarlmarx/user_upload/2.jpg')\"></div>"
    )

    def test_the_full_gallery_is_fetched_from_the_detail_page(self):
        with mock.patch('requests.get', return_value=FakeResponse(self.DETAIL_HTML)) as get:
            urls = karlmarx_monitor._fetch_gallery(self.LISTING)

        self.assertEqual(urls, [
            'https://wgkarlmarx.de/fileadmin/user_upload/wgkarlmarx/user_upload/1.jpg',
            'https://wgkarlmarx.de/fileadmin/user_upload/wgkarlmarx/user_upload/2.jpg',
        ])
        get.assert_called_once()
        self.assertEqual(get.call_args.args[0], self.LISTING['detail_url'])

    def test_more_photos_than_an_album_holds_are_trimmed(self):
        many = ''.join(
            f"<div style=\"background-image: url('/fileadmin/{i}.jpg')\"></div>" for i in range(14)
        )
        with mock.patch('requests.get', return_value=FakeResponse(many)):
            urls = karlmarx_monitor._fetch_gallery(self.LISTING)

        self.assertEqual(len(urls), karlmarx_monitor.GALLERY_ALBUM_MAX)

    def test_a_page_without_a_gallery_falls_back_to_the_cover(self):
        with mock.patch('requests.get', return_value=FakeResponse('<html>no gallery</html>')):
            urls = karlmarx_monitor._fetch_gallery(self.LISTING)

        self.assertEqual(urls, [self.LISTING['cover_image_url']])

    def test_an_unreachable_detail_page_falls_back_to_the_cover(self):
        with mock.patch('requests.get', side_effect=RuntimeError('timeout')):
            urls = karlmarx_monitor._fetch_gallery(self.LISTING)

        self.assertEqual(urls, [self.LISTING['cover_image_url']])

    def test_no_detail_url_and_no_cover_yields_nothing(self):
        self.assertEqual(karlmarx_monitor._fetch_gallery({'listing_key': 'x'}), [])

    def test_no_detail_url_falls_back_to_the_cover_without_a_request(self):
        with mock.patch('requests.get') as get:
            urls = karlmarx_monitor._fetch_gallery({'listing_key': 'x', 'cover_image_url': 'https://x/1.jpg'})

        get.assert_not_called()
        self.assertEqual(urls, ['https://x/1.jpg'])


class KarlmarxListingPostTests(unittest.TestCase):
    """_send_listing: единый пост — галерея фото с текстом объявления подписью."""

    LISTING = _listing('x')
    TEXT = 'Нова квартира Wohnungsgenossenschaft Karl Marx\n\nАдреса: Beispielstr. 1'

    def test_several_photos_go_out_as_one_album_with_the_text_as_caption(self):
        bot = FakeBot()
        with mock.patch.object(karlmarx_monitor, '_fetch_gallery', return_value=['u1', 'u2']):
            posted_as_caption = karlmarx_monitor._send_listing(bot, 312029534, self.LISTING, self.TEXT)

        self.assertTrue(posted_as_caption)
        self.assertEqual(bot.calls, ['album'])
        media = bot.albums[0]['media']
        self.assertEqual(len(media), 2)
        self.assertEqual(media[0].caption, self.TEXT)
        self.assertEqual(media[0].parse_mode, 'HTML')
        self.assertIsNone(getattr(media[1], 'caption', None))

    def test_a_single_photo_carries_the_text_as_its_caption(self):
        bot = FakeBot()
        with mock.patch.object(karlmarx_monitor, '_fetch_gallery', return_value=['u1']):
            posted_as_caption = karlmarx_monitor._send_listing(bot, 312029534, self.LISTING, self.TEXT)

        self.assertTrue(posted_as_caption)
        self.assertEqual(bot.calls, ['photo'])
        self.assertEqual(bot.photos[0]['caption'], self.TEXT)
        self.assertEqual(bot.photos[0]['photo'], 'u1')

    def test_a_listing_without_photos_sends_nothing(self):
        bot = FakeBot()
        with mock.patch.object(karlmarx_monitor, '_fetch_gallery', return_value=[]):
            posted_as_caption = karlmarx_monitor._send_listing(bot, 312029534, self.LISTING, self.TEXT)

        self.assertFalse(posted_as_caption)
        self.assertEqual(bot.calls, [])

    def test_text_over_the_caption_limit_still_goes_out_as_a_bare_album(self):
        bot = FakeBot()
        long_text = 'x' * (karlmarx_monitor.CAPTION_LIMIT + 1)
        with mock.patch.object(karlmarx_monitor, '_fetch_gallery', return_value=['u1', 'u2']):
            posted_as_caption = karlmarx_monitor._send_listing(bot, 312029534, self.LISTING, long_text)

        self.assertFalse(posted_as_caption)
        self.assertEqual(bot.calls, ['album'])
        self.assertIsNone(getattr(bot.albums[0]['media'][0], 'caption', None))

    def test_a_failed_album_is_swallowed(self):
        bot = FakeBot()
        bot.send_media_group = mock.Mock(side_effect=RuntimeError('Telegram said no'))
        with mock.patch.object(karlmarx_monitor, '_fetch_gallery', return_value=['u1', 'u2']):
            posted_as_caption = karlmarx_monitor._send_listing(bot, 312029534, self.LISTING, self.TEXT)

        self.assertFalse(posted_as_caption)


class KarlmarxDeliveryTests(unittest.TestCase):
    def test_a_listing_with_a_gallery_goes_out_as_a_single_post(self):
        context = SimpleNamespace(bot=FakeBot())
        listing = _listing('x')
        filt = {'filter_id': 9, 'user_id': 544675510, 'max_price_eur': 3000.0}
        detail_html = "<div style=\"background-image: url('/fileadmin/1.jpg')\"></div>"

        with mock.patch.object(karlmarx_monitor, 'CHECK_ENABLED', True), \
             mock.patch('requests.get', return_value=FakeResponse(detail_html)), \
             mock.patch('user_jobs.karlmarx_monitor.karlmarx_parser.count_all_cards', return_value=2), \
             mock.patch('user_jobs.karlmarx_monitor.karlmarx_parser.parse_listings', return_value=[listing]), \
             mock.patch('user_jobs.karlmarx_monitor.karlmarx_store.record_status'), \
             mock.patch('user_jobs.karlmarx_monitor.karlmarx_store.upsert_listings', return_value=1), \
             mock.patch('user_jobs.karlmarx_monitor.karlmarx_store.list_active_listings', return_value=[listing]), \
             mock.patch('user_jobs.karlmarx_monitor.karlmarx_store.list_filters', return_value=[filt]), \
             mock.patch('user_jobs.karlmarx_monitor.karlmarx_store.delivered_pairs', return_value=set()), \
             mock.patch('user_jobs.karlmarx_monitor.karlmarx_store.mark_delivered') as mark_delivered:
            result = karlmarx_monitor.check_job(context)

        self.assertEqual(context.bot.calls, ['photo'])
        self.assertIn('Karl Marx', context.bot.photos[0]['caption'])
        self.assertEqual(result['sent'], 1)
        mark_delivered.assert_called_once_with(9, 'x')

    def test_a_listing_is_still_delivered_as_text_when_it_has_no_gallery_or_cover(self):
        context = SimpleNamespace(bot=FakeBot())
        listing = _listing('x')
        filt = {'filter_id': 9, 'user_id': 544675510, 'max_price_eur': 3000.0}

        with mock.patch.object(karlmarx_monitor, 'CHECK_ENABLED', True), \
             mock.patch('requests.get', return_value=FakeResponse('<html>no gallery</html>')), \
             mock.patch('user_jobs.karlmarx_monitor.karlmarx_parser.count_all_cards', return_value=2), \
             mock.patch('user_jobs.karlmarx_monitor.karlmarx_parser.parse_listings', return_value=[listing]), \
             mock.patch('user_jobs.karlmarx_monitor.karlmarx_store.record_status'), \
             mock.patch('user_jobs.karlmarx_monitor.karlmarx_store.upsert_listings', return_value=1), \
             mock.patch('user_jobs.karlmarx_monitor.karlmarx_store.list_active_listings', return_value=[listing]), \
             mock.patch('user_jobs.karlmarx_monitor.karlmarx_store.list_filters', return_value=[filt]), \
             mock.patch('user_jobs.karlmarx_monitor.karlmarx_store.delivered_pairs', return_value=set()), \
             mock.patch('user_jobs.karlmarx_monitor.karlmarx_store.mark_delivered'):
            result = karlmarx_monitor.check_job(context)

        self.assertEqual(context.bot.calls, ['message'])
        self.assertEqual(result['sent'], 1)


if __name__ == '__main__':
    unittest.main()
