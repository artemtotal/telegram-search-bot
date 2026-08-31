import unittest
from types import SimpleNamespace
from unittest import mock

import requests

from user_jobs import kleinanzeigen_monitor


class FakeBot:
    def __init__(self):
        self.sent = []
        self.photos = []
        self.calls = []

    def send_message(self, chat_id, text, **kwargs):
        self.calls.append('message')
        self.sent.append((chat_id, text, kwargs))

    def send_photo(self, **kwargs):
        self.calls.append('photo')
        self.photos.append(kwargs)


class FakeResponse:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


def _listing(key, city="Potsdam", title="Wohnung", rooms=3.0, area=76.0, price=572.0, cover_image_url=""):
    return {
        "listing_key": key, "title": title, "address": f"{city}", "city": city,
        "rooms": rooms, "area_m2": area, "price_eur": price,
        "detail_url": f"https://www.kleinanzeigen.de/s-anzeige/x/{key}",
        "cover_image_url": cover_image_url,
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


class KleinanzeigenEmptyPageTests(unittest.TestCase):
    """The Potsdam search page never legitimately returns nothing - it steadily
    serves 25-26 cards. A lone zero (observed 2026-08-25 12:45 between
    neighbouring 25-26 scans) is a momentary hiccup on their side, not a markup
    change, and a retry immediately gets a normal page."""

    def test_a_one_off_empty_page_is_retried_and_recovers(self):
        with mock.patch('requests.get', return_value=FakeResponse('irrelevant')) as get, \
             mock.patch('user_jobs.kleinanzeigen_monitor.time.sleep') as sleep, \
             mock.patch(
                 'user_jobs.kleinanzeigen_monitor.kleinanzeigen_parser.parse_listings',
                 side_effect=[[], [_listing('1')]],
             ):
            listings = kleinanzeigen_monitor._fetch_listings()

        self.assertEqual(len(listings), 1)
        self.assertEqual(get.call_count, 2)
        sleep.assert_called_once()

    def test_a_genuinely_empty_page_still_reports_empty_after_every_attempt(self):
        with mock.patch('requests.get', return_value=FakeResponse('irrelevant')) as get, \
             mock.patch('user_jobs.kleinanzeigen_monitor.time.sleep'), \
             mock.patch(
                 'user_jobs.kleinanzeigen_monitor.kleinanzeigen_parser.parse_listings',
                 return_value=[],
             ):
            listings = kleinanzeigen_monitor._fetch_listings()

        self.assertEqual(listings, [])
        self.assertEqual(get.call_count, kleinanzeigen_monitor._FETCH_ATTEMPTS)

    def test_a_normal_page_is_fetched_once_without_retrying(self):
        with mock.patch('requests.get', return_value=FakeResponse('irrelevant')) as get, \
             mock.patch('user_jobs.kleinanzeigen_monitor.time.sleep') as sleep, \
             mock.patch(
                 'user_jobs.kleinanzeigen_monitor.kleinanzeigen_parser.parse_listings',
                 return_value=[_listing('1')],
             ):
            kleinanzeigen_monitor._fetch_listings()

        self.assertEqual(get.call_count, 1)
        sleep.assert_not_called()

    def test_a_transient_network_error_is_retried_too(self):
        error = requests.exceptions.SSLError('flaky')

        with mock.patch(
            'requests.get', side_effect=[error, FakeResponse('irrelevant')],
        ) as get, \
             mock.patch('user_jobs.kleinanzeigen_monitor.time.sleep'), \
             mock.patch(
                 'user_jobs.kleinanzeigen_monitor.kleinanzeigen_parser.parse_listings',
                 return_value=[_listing('1')],
             ):
            listings = kleinanzeigen_monitor._fetch_listings()

        self.assertEqual(len(listings), 1)
        self.assertEqual(get.call_count, 2)


class KleinanzeigenParseAlertCooldownTests(unittest.TestCase):
    """The empty-parse alert had no cooldown at all, unlike the fetch-error
    one - while the page stayed empty it fired on every 20-minute scan."""

    def test_the_first_empty_parse_alerts_immediately(self):
        bot = FakeBot()

        with mock.patch.object(kleinanzeigen_monitor, 'ADMIN_ID', 312029534):
            alerted = kleinanzeigen_monitor._notify_admin_parse_broke(bot, {})

        self.assertTrue(alerted)
        self.assertEqual(len(bot.sent), 1)

    def test_a_repeated_empty_parse_within_the_cooldown_stays_quiet(self):
        bot = FakeBot()
        previous = {
            'last_status': 'ok',
            'listings_count': 0,
            'last_checked_at': kleinanzeigen_monitor.kleinanzeigen_store.utc_now(),
        }

        with mock.patch.object(kleinanzeigen_monitor, 'ADMIN_ID', 312029534):
            alerted = kleinanzeigen_monitor._notify_admin_parse_broke(bot, previous)

        self.assertFalse(alerted)
        self.assertEqual(bot.sent, [])

    def test_an_empty_parse_after_a_healthy_scan_alerts_again(self):
        # A scan that saw listings resets the situation: the next zero is
        # genuinely new information, not a repeat of an ongoing outage.
        bot = FakeBot()
        previous = {
            'last_status': 'ok',
            'listings_count': 25,
            'last_checked_at': kleinanzeigen_monitor.kleinanzeigen_store.utc_now(),
        }

        with mock.patch.object(kleinanzeigen_monitor, 'ADMIN_ID', 312029534):
            alerted = kleinanzeigen_monitor._notify_admin_parse_broke(bot, previous)

        self.assertTrue(alerted)


class KleinanzeigenListingPostTests(unittest.TestCase):
    """_send_listing: единый пост — уже собранная обложка с текстом объявления подписью.

    Без похода на страницу объявления: Kleinanzeigen явно запрещает
    автосбор, поэтому второго запроса на новое объявление тут нет вовсе.
    """

    TEXT = 'Нове оголошення Kleinanzeigen\n\nАдреса: Beispielstr. 1'

    def test_a_cover_carries_the_text_as_its_caption(self):
        bot = FakeBot()
        listing = _listing('1', cover_image_url='https://img.kleinanzeigen.de/x.jpg')
        with mock.patch('requests.get') as get:
            posted_as_caption = kleinanzeigen_monitor._send_listing(bot, 312029534, listing, self.TEXT)

        get.assert_not_called()
        self.assertTrue(posted_as_caption)
        self.assertEqual(bot.calls, ['photo'])
        self.assertEqual(bot.photos[0]['caption'], self.TEXT)
        self.assertEqual(bot.photos[0]['photo'], 'https://img.kleinanzeigen.de/x.jpg')

    def test_a_listing_without_a_cover_sends_nothing(self):
        bot = FakeBot()
        listing = _listing('1', cover_image_url='')
        posted_as_caption = kleinanzeigen_monitor._send_listing(bot, 312029534, listing, self.TEXT)

        self.assertFalse(posted_as_caption)
        self.assertEqual(bot.calls, [])

    def test_text_over_the_caption_limit_still_goes_out_as_a_bare_photo(self):
        bot = FakeBot()
        listing = _listing('1', cover_image_url='https://img.kleinanzeigen.de/x.jpg')
        long_text = 'x' * (kleinanzeigen_monitor.CAPTION_LIMIT + 1)
        posted_as_caption = kleinanzeigen_monitor._send_listing(bot, 312029534, listing, long_text)

        self.assertFalse(posted_as_caption)
        self.assertEqual(bot.calls, ['photo'])
        self.assertIsNone(bot.photos[0]['caption'])

    def test_a_failed_send_is_swallowed(self):
        bot = FakeBot()
        bot.send_photo = mock.Mock(side_effect=RuntimeError('Telegram said no'))
        listing = _listing('1', cover_image_url='https://img.kleinanzeigen.de/x.jpg')
        posted_as_caption = kleinanzeigen_monitor._send_listing(bot, 312029534, listing, self.TEXT)

        self.assertFalse(posted_as_caption)


class KleinanzeigenDeliveryTests(unittest.TestCase):
    def test_a_listing_with_a_cover_goes_out_as_a_single_post(self):
        context = SimpleNamespace(bot=FakeBot())
        potsdam = _listing('1', cover_image_url='https://img.kleinanzeigen.de/x.jpg')
        filt = {'filter_id': 9, 'user_id': 544675510, 'max_price_eur': 1000.0}

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

        self.assertEqual(context.bot.calls, ['photo'])
        self.assertIn('Kleinanzeigen', context.bot.photos[0]['caption'])
        self.assertEqual(result['sent'], 1)
        mark_delivered.assert_called_once_with(9, '1')

    def test_a_listing_without_a_cover_is_still_delivered_as_text(self):
        context = SimpleNamespace(bot=FakeBot())
        potsdam = _listing('1', cover_image_url='')
        filt = {'filter_id': 9, 'user_id': 544675510, 'max_price_eur': 1000.0}

        with mock.patch.object(kleinanzeigen_monitor, 'CHECK_ENABLED', True), \
             mock.patch('requests.get', return_value=FakeResponse('irrelevant')), \
             mock.patch('user_jobs.kleinanzeigen_monitor.kleinanzeigen_parser.parse_listings', return_value=[potsdam]), \
             mock.patch('user_jobs.kleinanzeigen_monitor.kleinanzeigen_store.record_status'), \
             mock.patch('user_jobs.kleinanzeigen_monitor.kleinanzeigen_store.upsert_listings', return_value=1), \
             mock.patch('user_jobs.kleinanzeigen_monitor.kleinanzeigen_store.list_active_listings', return_value=[potsdam]), \
             mock.patch('user_jobs.kleinanzeigen_monitor.kleinanzeigen_store.list_filters', return_value=[filt]), \
             mock.patch('user_jobs.kleinanzeigen_monitor.kleinanzeigen_store.delivered_pairs', return_value=set()), \
             mock.patch('user_jobs.kleinanzeigen_monitor.kleinanzeigen_store.mark_delivered'):
            result = kleinanzeigen_monitor.check_job(context)

        self.assertEqual(context.bot.calls, ['message'])
        self.assertEqual(result['sent'], 1)


if __name__ == '__main__':
    unittest.main()
