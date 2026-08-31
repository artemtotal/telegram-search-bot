import unittest
from types import SimpleNamespace
from unittest import mock

from user_jobs import regiomakler_monitor


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
             mock.patch.object(regiomakler_monitor, 'ADMIN_ID', 0), \
             mock.patch('requests.get', side_effect=RuntimeError('timeout')), \
             mock.patch('user_jobs.regiomakler_monitor.regiomakler_store.latest_status', return_value={}), \
             mock.patch('user_jobs.regiomakler_monitor.regiomakler_store.record_status') as record_status:
            result = regiomakler_monitor.check_job(context)

        record_status.assert_called_once_with('error', listings_count=0, error='timeout')
        self.assertEqual(result['ok'], 0)

    def test_first_fetch_failure_alerts_the_admin(self):
        context = SimpleNamespace(bot=FakeBot())
        with mock.patch.object(regiomakler_monitor, 'CHECK_ENABLED', True), \
             mock.patch.object(regiomakler_monitor, 'ADMIN_ID', 312029534), \
             mock.patch('requests.get', side_effect=RuntimeError('connection refused')), \
             mock.patch('user_jobs.regiomakler_monitor.regiomakler_store.latest_status', return_value={}), \
             mock.patch('user_jobs.regiomakler_monitor.regiomakler_store.record_status'):
            result = regiomakler_monitor.check_job(context)

        self.assertEqual(len(context.bot.sent), 1)
        chat_id, text, _ = context.bot.sent[0]
        self.assertEqual(chat_id, 312029534)
        self.assertIn('connection refused', text)
        self.assertEqual(result['admin_alerted'], 1)

    def test_a_second_failure_within_the_cooldown_does_not_alert_again(self):
        context = SimpleNamespace(bot=FakeBot())
        previous_status = {'last_status': 'error', 'last_checked_at': regiomakler_monitor.regiomakler_store.utc_now()}
        with mock.patch.object(regiomakler_monitor, 'CHECK_ENABLED', True), \
             mock.patch.object(regiomakler_monitor, 'ADMIN_ID', 312029534), \
             mock.patch('requests.get', side_effect=RuntimeError('still down')), \
             mock.patch('user_jobs.regiomakler_monitor.regiomakler_store.latest_status', return_value=previous_status), \
             mock.patch('user_jobs.regiomakler_monitor.regiomakler_store.record_status'):
            result = regiomakler_monitor.check_job(context)

        self.assertEqual(context.bot.sent, [])
        self.assertEqual(result['admin_alerted'], 0)

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


class RegiomaklerGalleryTests(unittest.TestCase):
    LISTING = _listing('12863_4', 'immoteam')
    DETAIL_HTML = (
        '<img src="https://immoteam-potsdam.de/wp-content/uploads/immomakler/attachments/abc/p1.jpg">'
        '<img src="https://immoteam-potsdam.de/wp-content/uploads/immomakler/attachments/abc/p2.jpg">'
    )

    def test_the_full_gallery_is_fetched_from_the_detail_page(self):
        with mock.patch('requests.get', return_value=FakeResponse(self.DETAIL_HTML)) as get:
            urls = regiomakler_monitor._fetch_gallery(self.LISTING)

        self.assertEqual(urls, [
            'https://immoteam-potsdam.de/wp-content/uploads/immomakler/attachments/abc/p1.jpg',
            'https://immoteam-potsdam.de/wp-content/uploads/immomakler/attachments/abc/p2.jpg',
        ])
        get.assert_called_once()
        self.assertEqual(get.call_args.args[0], self.LISTING['detail_url'])

    def test_more_photos_than_an_album_holds_are_trimmed(self):
        many = ''.join(
            f'<img src="https://immoteam-potsdam.de/wp-content/uploads/immomakler/attachments/abc/p{i}.jpg">'
            for i in range(14)
        )
        with mock.patch('requests.get', return_value=FakeResponse(many)):
            urls = regiomakler_monitor._fetch_gallery(self.LISTING)

        self.assertEqual(len(urls), regiomakler_monitor.GALLERY_ALBUM_MAX)

    def test_a_page_without_a_gallery_yields_no_photos(self):
        with mock.patch('requests.get', return_value=FakeResponse('<html>no gallery</html>')):
            self.assertEqual(regiomakler_monitor._fetch_gallery(self.LISTING), [])

    def test_an_unreachable_detail_page_yields_no_photos_without_raising(self):
        with mock.patch('requests.get', side_effect=RuntimeError('timeout')):
            self.assertEqual(regiomakler_monitor._fetch_gallery(self.LISTING), [])

    def test_no_detail_url_yields_nothing_without_a_request(self):
        with mock.patch('requests.get') as get:
            urls = regiomakler_monitor._fetch_gallery({'listing_key': 'x'})

        get.assert_not_called()
        self.assertEqual(urls, [])


class RegiomaklerListingPostTests(unittest.TestCase):
    """_send_listing: единый пост — галерея фото с текстом объявления подписью."""

    LISTING = _listing('12863_4', 'immoteam')
    TEXT = 'Нова квартира ImmoTeam/alpha\n\nАдреса: Beispielstr. 1'

    def test_several_photos_go_out_as_one_album_with_the_text_as_caption(self):
        bot = FakeBot()
        with mock.patch.object(regiomakler_monitor, '_fetch_gallery', return_value=['u1', 'u2']):
            posted_as_caption = regiomakler_monitor._send_listing(bot, 312029534, self.LISTING, self.TEXT)

        self.assertTrue(posted_as_caption)
        self.assertEqual(bot.calls, ['album'])
        media = bot.albums[0]['media']
        self.assertEqual(len(media), 2)
        self.assertEqual(media[0].caption, self.TEXT)
        self.assertEqual(media[0].parse_mode, 'HTML')
        self.assertIsNone(getattr(media[1], 'caption', None))

    def test_a_single_photo_carries_the_text_as_its_caption(self):
        bot = FakeBot()
        with mock.patch.object(regiomakler_monitor, '_fetch_gallery', return_value=['u1']):
            posted_as_caption = regiomakler_monitor._send_listing(bot, 312029534, self.LISTING, self.TEXT)

        self.assertTrue(posted_as_caption)
        self.assertEqual(bot.calls, ['photo'])
        self.assertEqual(bot.photos[0]['caption'], self.TEXT)
        self.assertEqual(bot.photos[0]['photo'], 'u1')

    def test_a_listing_without_photos_sends_nothing(self):
        bot = FakeBot()
        with mock.patch.object(regiomakler_monitor, '_fetch_gallery', return_value=[]):
            posted_as_caption = regiomakler_monitor._send_listing(bot, 312029534, self.LISTING, self.TEXT)

        self.assertFalse(posted_as_caption)
        self.assertEqual(bot.calls, [])

    def test_text_over_the_caption_limit_still_goes_out_as_a_bare_album(self):
        bot = FakeBot()
        long_text = 'x' * (regiomakler_monitor.CAPTION_LIMIT + 1)
        with mock.patch.object(regiomakler_monitor, '_fetch_gallery', return_value=['u1', 'u2']):
            posted_as_caption = regiomakler_monitor._send_listing(bot, 312029534, self.LISTING, long_text)

        self.assertFalse(posted_as_caption)
        self.assertEqual(bot.calls, ['album'])
        self.assertIsNone(getattr(bot.albums[0]['media'][0], 'caption', None))

    def test_a_failed_album_is_swallowed(self):
        bot = FakeBot()
        bot.send_media_group = mock.Mock(side_effect=RuntimeError('Telegram said no'))
        with mock.patch.object(regiomakler_monitor, '_fetch_gallery', return_value=['u1', 'u2']):
            posted_as_caption = regiomakler_monitor._send_listing(bot, 312029534, self.LISTING, self.TEXT)

        self.assertFalse(posted_as_caption)


class RegiomaklerDeliveryTests(unittest.TestCase):
    VACANT = _listing('12863_4', 'immoteam')

    def test_a_listing_with_a_gallery_goes_out_as_a_single_post(self):
        context = SimpleNamespace(bot=FakeBot())
        filt = {'filter_id': 9, 'user_id': 544675510, 'max_price_eur': 2000.0}
        detail_html = '<img src="https://immoteam-potsdam.de/wp-content/uploads/immomakler/attachments/abc/p1.jpg">'
        with mock.patch.object(regiomakler_monitor, 'CHECK_ENABLED', True), \
             mock.patch('requests.get', return_value=FakeResponse(detail_html)), \
             mock.patch(
                 'user_jobs.regiomakler_monitor.regiomakler_parser.parse_listings',
                 side_effect=lambda h, s: [self.VACANT] if s == 'immoteam' else [],
             ), \
             mock.patch('user_jobs.regiomakler_monitor.regiomakler_store.record_status'), \
             mock.patch('user_jobs.regiomakler_monitor.regiomakler_store.upsert_listings', return_value=1), \
             mock.patch('user_jobs.regiomakler_monitor.regiomakler_store.list_active_listings', return_value=[self.VACANT]), \
             mock.patch('user_jobs.regiomakler_monitor.regiomakler_store.list_filters', return_value=[filt]), \
             mock.patch('user_jobs.regiomakler_monitor.regiomakler_store.delivered_pairs', return_value=set()), \
             mock.patch('user_jobs.regiomakler_monitor.regiomakler_store.mark_delivered') as mark_delivered:
            result = regiomakler_monitor.check_job(context)

        self.assertEqual(context.bot.calls, ['photo'])
        self.assertIn('ImmoTeam/alpha', context.bot.photos[0]['caption'])
        self.assertEqual(result['sent'], 1)
        mark_delivered.assert_called_once_with(9, '12863_4')

    def test_a_listing_is_still_delivered_as_text_when_it_has_no_gallery(self):
        context = SimpleNamespace(bot=FakeBot())
        filt = {'filter_id': 9, 'user_id': 544675510, 'max_price_eur': 2000.0}
        with mock.patch.object(regiomakler_monitor, 'CHECK_ENABLED', True), \
             mock.patch('requests.get', return_value=FakeResponse('<html>no gallery</html>')), \
             mock.patch(
                 'user_jobs.regiomakler_monitor.regiomakler_parser.parse_listings',
                 side_effect=lambda h, s: [self.VACANT] if s == 'immoteam' else [],
             ), \
             mock.patch('user_jobs.regiomakler_monitor.regiomakler_store.record_status'), \
             mock.patch('user_jobs.regiomakler_monitor.regiomakler_store.upsert_listings', return_value=1), \
             mock.patch('user_jobs.regiomakler_monitor.regiomakler_store.list_active_listings', return_value=[self.VACANT]), \
             mock.patch('user_jobs.regiomakler_monitor.regiomakler_store.list_filters', return_value=[filt]), \
             mock.patch('user_jobs.regiomakler_monitor.regiomakler_store.delivered_pairs', return_value=set()), \
             mock.patch('user_jobs.regiomakler_monitor.regiomakler_store.mark_delivered'):
            result = regiomakler_monitor.check_job(context)

        self.assertEqual(context.bot.calls, ['message'])
        self.assertEqual(result['sent'], 1)


if __name__ == '__main__':
    unittest.main()
