import unittest
from datetime import timedelta
from unittest import mock

from user_jobs import propotsdam_monitor, propotsdam_store


class FakeBot:
    def __init__(self):
        self.messages = []
        self.photos = []
        self.albums = []
        self.calls = []

    def send_message(self, **kwargs):
        self.calls.append('message')
        self.messages.append(kwargs)

    def send_photo(self, **kwargs):
        self.calls.append('photo')
        self.photos.append(kwargs)

    def send_media_group(self, **kwargs):
        self.calls.append('album')
        self.albums.append(kwargs)


class FakeContext:
    def __init__(self):
        self.bot = FakeBot()


class ProPotsdamMonitorTests(unittest.TestCase):
    def test_schedule_runs_every_fifteen_minutes(self):
        self.assertEqual(propotsdam_monitor.CHECK_INTERVAL_SECONDS, 900)

    def test_check_job_does_not_overlap_an_existing_scan(self):
        context = FakeContext()
        propotsdam_monitor._scan_lock.acquire()
        try:
            with mock.patch.object(propotsdam_monitor, 'PROPOTSDAM_CHECK_ENABLED', True), \
                 mock.patch.object(propotsdam_monitor, '_request_scan') as request_scan:
                result = propotsdam_monitor.check_job(context)
        finally:
            propotsdam_monitor._scan_lock.release()

        self.assertEqual(result, {'ok': 1, 'enabled': 1, 'skipped': 1, 'sent': 0})
        request_scan.assert_not_called()

    def test_failed_scan_notifies_admin_with_relogin_hint(self):
        context = FakeContext()
        with mock.patch.object(propotsdam_monitor, 'PROPOTSDAM_CHECK_ENABLED', True), \
             mock.patch.object(propotsdam_monitor, 'ADMIN_ID', 312029534), \
             mock.patch.object(propotsdam_monitor, '_request_scan', side_effect=RuntimeError('login required')), \
             mock.patch.object(propotsdam_store, 'latest_status', return_value={'last_status': 'ok'}), \
             mock.patch.object(propotsdam_store, 'record_status') as record_status:
            result = propotsdam_monitor.check_job(context)

        self.assertEqual(result['ok'], 0)
        self.assertEqual(result['admin_alerted'], 1)
        self.assertEqual(context.bot.messages[0]['chat_id'], 312029534)
        self.assertIn('ProPotsdam не смог собрать квартиры', context.bot.messages[0]['text'])
        self.assertIn('Как чинить', context.bot.messages[0]['text'])
        record_status.assert_called_once_with('error', listings_count=0, error='login required')

    def test_repeated_error_is_rate_limited(self):
        context = FakeContext()
        with mock.patch.object(propotsdam_monitor, 'PROPOTSDAM_CHECK_ENABLED', True), \
             mock.patch.object(propotsdam_monitor, 'ADMIN_ID', 312029534), \
             mock.patch.object(propotsdam_monitor, '_request_scan', side_effect=RuntimeError('login required')), \
             mock.patch.object(propotsdam_store, 'latest_status', return_value={
                 'last_status': 'error',
                 'last_checked_at': propotsdam_store.utc_now() - timedelta(minutes=5),
             }), \
             mock.patch.object(propotsdam_store, 'record_status'):
            result = propotsdam_monitor.check_job(context)

        self.assertEqual(result['admin_alerted'], 0)
        self.assertEqual(context.bot.messages, [])

    def test_a_still_running_scan_is_not_reported_as_a_failure(self):
        """A slow browser must not look like a dead collector session.

        The old blocking call turned any scan longer than the HTTP timeout into
        an "error" plus a re-login alert, even though the scan then finished fine.
        """
        context = FakeContext()
        with mock.patch.object(propotsdam_monitor, 'PROPOTSDAM_CHECK_ENABLED', True), \
             mock.patch.object(propotsdam_monitor, 'ADMIN_ID', 312029534), \
             mock.patch.object(propotsdam_monitor, '_request_scan',
                               side_effect=propotsdam_monitor.ScanPending()), \
             mock.patch.object(propotsdam_store, 'record_status') as record_status:
            result = propotsdam_monitor.check_job(context)

        self.assertEqual(result, {'ok': 1, 'enabled': 1, 'pending': 1, 'sent': 0})
        self.assertEqual(context.bot.messages, [])
        record_status.assert_not_called()

    def test_scan_consumes_a_result_that_finished_while_polling(self):
        propotsdam_monitor._last_consumed_finished_at = None
        posted = mock.Mock(status_code=202)
        posted.json.return_value = {'ok': True, 'started': True, 'running': True}
        with mock.patch.object(propotsdam_monitor.requests, 'post', return_value=posted), \
             mock.patch.object(propotsdam_monitor, '_fetch_last_result', side_effect=[
                 {'ok': True, 'running': True, 'finished_at': None, 'listings': []},
                 {'ok': True, 'running': False, 'finished_at': '2026-08-27T12:00:00+00:00',
                  'listings': [{'listing_key': 'a'}]},
             ]), \
             mock.patch.object(propotsdam_monitor.time, 'sleep'):
            listings = propotsdam_monitor._request_scan()

        self.assertEqual(listings, [{'listing_key': 'a'}])
        self.assertEqual(propotsdam_monitor._last_consumed_finished_at, '2026-08-27T12:00:00+00:00')

    def test_scan_waits_instead_of_reconsuming_the_previous_result(self):
        """The same `finished_at` must not be counted as a fresh scan."""
        propotsdam_monitor._last_consumed_finished_at = '2026-08-27T12:00:00+00:00'
        posted = mock.Mock(status_code=202)
        posted.json.return_value = {'ok': True, 'started': True, 'running': True}
        with mock.patch.object(propotsdam_monitor.requests, 'post', return_value=posted), \
             mock.patch.object(propotsdam_monitor, 'PROPOTSDAM_SCAN_WAIT_SECONDS', 0), \
             mock.patch.object(propotsdam_monitor, '_fetch_last_result', return_value={
                 'ok': True, 'running': True, 'finished_at': '2026-08-27T12:00:00+00:00',
                 'listings': [{'listing_key': 'a'}]}), \
             mock.patch.object(propotsdam_monitor.time, 'sleep'):
            with self.assertRaises(propotsdam_monitor.ScanPending):
                propotsdam_monitor._request_scan()

    def test_a_collector_reported_failure_still_alerts(self):
        propotsdam_monitor._last_consumed_finished_at = None
        posted = mock.Mock(status_code=202)
        posted.json.return_value = {'ok': True, 'started': True, 'running': True}
        with mock.patch.object(propotsdam_monitor.requests, 'post', return_value=posted), \
             mock.patch.object(propotsdam_monitor, '_fetch_last_result', return_value={
                 'ok': False, 'running': False, 'finished_at': '2026-08-27T12:05:00+00:00',
                 'error': 'login required', 'listings': []}), \
             mock.patch.object(propotsdam_monitor.time, 'sleep'):
            with self.assertRaises(RuntimeError) as caught:
                propotsdam_monitor._request_scan()

        self.assertIn('login required', str(caught.exception))

    def _run_empty_scan(self, context, last_seen, last_alert=None):
        with mock.patch.object(propotsdam_monitor, 'PROPOTSDAM_CHECK_ENABLED', True), \
             mock.patch.object(propotsdam_monitor, 'ADMIN_ID', 312029534), \
             mock.patch.object(propotsdam_monitor, '_request_scan', return_value=[]), \
             mock.patch.object(propotsdam_store, 'upsert_listings', return_value=0), \
             mock.patch.object(propotsdam_store, 'record_status'), \
             mock.patch.object(propotsdam_store, 'latest_listing_seen_at', return_value=last_seen), \
             mock.patch.object(propotsdam_store, 'last_empty_alert_at', return_value=last_alert), \
             mock.patch.object(propotsdam_store, 'record_empty_alert') as record_empty_alert, \
             mock.patch.object(propotsdam_store, 'list_active_listings', return_value=[]), \
             mock.patch.object(propotsdam_store, 'list_filters', return_value=[]), \
             mock.patch.object(propotsdam_store, 'select_unsent_matches', return_value=[]), \
             mock.patch.object(propotsdam_store, 'delivered_pairs', return_value=set()):
            result = propotsdam_monitor.check_job(context)
        return result, record_empty_alert

    def test_prolonged_emptiness_alerts_admin(self):
        context = FakeContext()
        stale = propotsdam_store.utc_now() - timedelta(hours=30)
        result, record_empty_alert = self._run_empty_scan(context, stale)

        self.assertEqual(result['empty_alerted'], 1)
        self.assertEqual(context.bot.messages[0]['chat_id'], 312029534)
        self.assertIn('ни одной квартиры', context.bot.messages[0]['text'])
        record_empty_alert.assert_called_once_with()

    def test_short_emptiness_is_not_alerted(self):
        context = FakeContext()
        recent = propotsdam_store.utc_now() - timedelta(hours=2)
        result, record_empty_alert = self._run_empty_scan(context, recent)

        self.assertEqual(result['empty_alerted'], 0)
        self.assertEqual(context.bot.messages, [])
        record_empty_alert.assert_not_called()

    def test_empty_alert_is_rate_limited(self):
        context = FakeContext()
        stale = propotsdam_store.utc_now() - timedelta(hours=30)
        just_alerted = propotsdam_store.utc_now() - timedelta(hours=1)
        result, record_empty_alert = self._run_empty_scan(context, stale, last_alert=just_alerted)

        self.assertEqual(result['empty_alerted'], 0)
        self.assertEqual(context.bot.messages, [])
        record_empty_alert.assert_not_called()


class ProPotsdamPhotoTests(unittest.TestCase):
    LISTING = {
        'listing_key': 'ORIG',
        'title': 'Helle 3-Raum-Wohnung',
        'image_url': 'https://portal.example/api5/accndocs2/GOOD-RESOURCE-ID-0001',
        'extra': {'image_resource_ids': 'GOOD-RESOURCE-ID-0001,GOOD-RESOURCE-ID-0002'},
    }

    def _photo_response(self, content=b'\xff\xd8jpeg'):
        response = mock.Mock(status_code=200, content=content)
        response.raise_for_status.return_value = None
        return response

    def test_all_photos_are_fetched_from_the_collector_cache(self):
        """Фото лежат за логином портала, поэтому их отдаёт коллектор, а не ссылка."""
        with mock.patch.object(propotsdam_monitor.requests, 'get',
                               return_value=self._photo_response()) as get:
            photos = propotsdam_monitor._fetch_photos(self.LISTING)

        self.assertEqual(photos, [b'\xff\xd8jpeg', b'\xff\xd8jpeg'])
        requested = [call.args[0] for call in get.call_args_list]
        self.assertTrue(all('/api/propotsdam/photo/' in url for url in requested))
        self.assertIn('GOOD-RESOURCE-ID-0001', requested[0])
        self.assertIn('GOOD-RESOURCE-ID-0002', requested[1])

    def test_more_photos_than_an_album_holds_are_trimmed(self):
        ids = ','.join('GOOD-RESOURCE-ID-{:04d}'.format(index) for index in range(14))
        listing = {'listing_key': 'ORIG', 'extra': {'image_resource_ids': ids}}
        with mock.patch.object(propotsdam_monitor.requests, 'get',
                               return_value=self._photo_response()) as get:
            photos = propotsdam_monitor._fetch_photos(listing)

        self.assertEqual(len(photos), propotsdam_monitor.PROPOTSDAM_ALBUM_MAX)
        self.assertEqual(get.call_count, propotsdam_monitor.PROPOTSDAM_ALBUM_MAX)

    def test_photos_are_named_after_their_own_bytes(self):
        """python-telegram-bot без имени зовёт файл application.octet-stream,
        и такую «фотку» Telegram может не принять."""
        png = b'\x89PNG\r\n\x1a\n' + b'\x00' * 16
        webp = b'RIFF\x00\x00\x00\x00WEBP' + b'\x00' * 16
        jpeg = b'\xff\xd8\xff\xe0' + b'\x00' * 16

        self.assertEqual(propotsdam_monitor._photo_filename(png, 1), 'propotsdam-1.png')
        self.assertEqual(propotsdam_monitor._photo_filename(webp, 2), 'propotsdam-2.webp')
        self.assertEqual(propotsdam_monitor._photo_filename(jpeg, 3), 'propotsdam-3.jpg')

    def test_an_unreachable_collector_does_not_raise(self):
        with mock.patch.object(propotsdam_monitor.requests, 'get',
                               side_effect=RuntimeError('connection refused')):
            self.assertEqual(propotsdam_monitor._fetch_photos(self.LISTING), [])


class ProPotsdamListingPostTests(unittest.TestCase):
    """_send_listing: единый пост — альбом фото с текстом объявления подписью."""

    LISTING = {
        'listing_key': 'ORIG',
        'title': 'Helle 3-Raum-Wohnung',
        'image_url': 'https://portal.example/api5/accndocs2/GOOD-RESOURCE-ID-0001',
        'extra': {'image_resource_ids': 'GOOD-RESOURCE-ID-0001,GOOD-RESOURCE-ID-0002'},
    }
    TEXT = 'Нова квартира ProPotsdam\n\nАдреса: Beispielstr. 1'

    def test_several_photos_go_out_as_one_album_with_the_text_as_caption(self):
        bot = FakeBot()
        with mock.patch.object(propotsdam_monitor, '_fetch_photos', return_value=[b'a', b'b']):
            count, posted_as_caption = propotsdam_monitor._send_listing(
                bot, 312029534, self.LISTING, self.TEXT)

        self.assertEqual(count, 2)
        self.assertTrue(posted_as_caption)
        self.assertEqual(bot.calls, ['album'])
        self.assertEqual(bot.albums[0]['chat_id'], 312029534)
        media = bot.albums[0]['media']
        self.assertEqual(len(media), 2)
        # Telegram показывает подписью всей группы только подпись первого элемента.
        self.assertEqual(media[0].caption, self.TEXT)
        self.assertEqual(media[0].parse_mode, 'HTML')
        # PTB 13 не выставляет атрибут вовсе, если caption не передан (falsy) —
        # getattr(..., None), а не .caption, иначе AttributeError на реальной библиотеке.
        self.assertIsNone(getattr(media[1], 'caption', None))

    def test_a_single_photo_carries_the_text_as_its_caption(self):
        bot = FakeBot()
        with mock.patch.object(propotsdam_monitor, '_fetch_photos', return_value=[b'a']):
            count, posted_as_caption = propotsdam_monitor._send_listing(
                bot, 312029534, self.LISTING, self.TEXT)

        self.assertEqual(count, 1)
        self.assertTrue(posted_as_caption)
        self.assertEqual(bot.calls, ['photo'])
        self.assertEqual(bot.photos[0]['caption'], self.TEXT)
        self.assertEqual(bot.photos[0]['parse_mode'], 'HTML')
        self.assertEqual(bot.photos[0]['filename'], 'propotsdam-1.jpg')

    def test_a_listing_without_photos_sends_nothing_and_leaves_the_text_to_the_caller(self):
        bot = FakeBot()
        with mock.patch.object(propotsdam_monitor, '_fetch_photos', return_value=[]):
            count, posted_as_caption = propotsdam_monitor._send_listing(
                bot, 312029534, self.LISTING, self.TEXT)

        self.assertEqual(count, 0)
        self.assertFalse(posted_as_caption)
        self.assertEqual(bot.calls, [])

    def test_text_over_the_caption_limit_still_goes_out_as_a_bare_album(self):
        """Подпись Telegram обрезает жёстче обычного текста: фото уходят,
        а сам текст — отдельным сообщением, которое досылает вызывающий."""
        bot = FakeBot()
        long_text = 'x' * (propotsdam_monitor.PROPOTSDAM_CAPTION_LIMIT + 1)
        with mock.patch.object(propotsdam_monitor, '_fetch_photos', return_value=[b'a', b'b']):
            count, posted_as_caption = propotsdam_monitor._send_listing(
                bot, 312029534, self.LISTING, long_text)

        self.assertEqual(count, 2)
        self.assertFalse(posted_as_caption)
        self.assertEqual(bot.calls, ['album'])
        self.assertIsNone(getattr(bot.albums[0]['media'][0], 'caption', None))

    def test_a_failed_album_is_swallowed(self):
        """Сорвавшийся пост не должен стоить человеку самого объявления."""
        bot = FakeBot()
        bot.send_media_group = mock.Mock(side_effect=RuntimeError('Telegram said no'))
        with mock.patch.object(propotsdam_monitor, '_fetch_photos', return_value=[b'a', b'b']):
            count, posted_as_caption = propotsdam_monitor._send_listing(
                bot, 312029534, self.LISTING, self.TEXT)

        self.assertEqual(count, 0)
        self.assertFalse(posted_as_caption)


class ProPotsdamDeliveryTests(unittest.TestCase):
    LISTING = {
        'listing_key': 'ORIG',
        'title': 'Helle 3-Raum-Wohnung',
        'image_url': 'https://portal.example/api5/accndocs2/GOOD-RESOURCE-ID-0001',
        'extra': {'image_resource_ids': 'GOOD-RESOURCE-ID-0001,GOOD-RESOURCE-ID-0002'},
    }

    def test_a_listing_with_photos_goes_out_as_a_single_post(self):
        """Ничего похожего на голую ссылку в тексте: фото и подпись — один пост."""
        context = FakeContext()
        filt = {'filter_id': 7, 'user_id': 312029534}
        with mock.patch.object(propotsdam_monitor, 'PROPOTSDAM_CHECK_ENABLED', True), \
             mock.patch.object(propotsdam_monitor, '_request_scan', return_value=[self.LISTING]), \
             mock.patch.object(propotsdam_monitor, '_fetch_photos', return_value=[b'a', b'b']), \
             mock.patch.object(propotsdam_store, 'upsert_listings', return_value=1), \
             mock.patch.object(propotsdam_store, 'record_status'), \
             mock.patch.object(propotsdam_store, 'list_active_listings', return_value=[self.LISTING]), \
             mock.patch.object(propotsdam_store, 'list_filters', return_value=[filt]), \
             mock.patch.object(propotsdam_store, 'delivered_pairs', return_value=set()), \
             mock.patch.object(propotsdam_store, 'select_unsent_matches',
                               return_value=[(filt, self.LISTING)]), \
             mock.patch.object(propotsdam_store, 'mark_delivered') as mark_delivered:
            result = propotsdam_monitor.check_job(context)

        self.assertEqual(context.bot.calls, ['album'])
        self.assertEqual(result['sent'], 1)
        self.assertEqual(result['photos'], 2)
        caption = context.bot.albums[0]['media'][0].caption
        self.assertIn('ProPotsdam', caption)
        mark_delivered.assert_called_once_with(7, 'ORIG')

    def test_a_listing_is_still_delivered_as_text_when_its_photos_fail(self):
        context = FakeContext()
        filt = {'filter_id': 7, 'user_id': 312029534}
        with mock.patch.object(propotsdam_monitor, 'PROPOTSDAM_CHECK_ENABLED', True), \
             mock.patch.object(propotsdam_monitor, '_request_scan', return_value=[self.LISTING]), \
             mock.patch.object(propotsdam_monitor.requests, 'get',
                               side_effect=RuntimeError('collector is down')), \
             mock.patch.object(propotsdam_store, 'upsert_listings', return_value=1), \
             mock.patch.object(propotsdam_store, 'record_status'), \
             mock.patch.object(propotsdam_store, 'list_active_listings', return_value=[self.LISTING]), \
             mock.patch.object(propotsdam_store, 'list_filters', return_value=[filt]), \
             mock.patch.object(propotsdam_store, 'delivered_pairs', return_value=set()), \
             mock.patch.object(propotsdam_store, 'select_unsent_matches',
                               return_value=[(filt, self.LISTING)]), \
             mock.patch.object(propotsdam_store, 'mark_delivered'):
            result = propotsdam_monitor.check_job(context)

        self.assertEqual(context.bot.calls, ['message'])
        self.assertEqual(result['sent'], 1)
        self.assertEqual(result['photos'], 0)


if __name__ == '__main__':
    unittest.main()
