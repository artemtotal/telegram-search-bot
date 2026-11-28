"""Links through our redirect, the application tracker and the honest
«who looked» line (24.11.2026)."""

import html
import http.client
import os
import threading
import unittest
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from unittest import mock

import pytz
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import i18n
from database import (
    Base, HousingAccessUser, HousingApplication, HousingLinkOpen, HousingLinkRef, HousingSendLog,
    KleinanzeigenListing,
)
from user_handlers import chat_start, housing_leave, housing_letter, housing_monitor, housing_tracker, housing_web
from user_jobs import (
    housing_dateparse, housing_letter_store, housing_links, housing_pay_store, housing_tier, user_settings_store,
)

PORTAL = 'https://www.kleinanzeigen.de/s-anzeige/k1?a=1&b=2'
WEB = 'https://web.example'


class _Db(unittest.TestCase):
    MODULES = (
        housing_links, housing_tracker, housing_letter_store, housing_letter, housing_tier,
        user_settings_store, housing_monitor, housing_pay_store, housing_leave,
    )

    def setUp(self):
        self.engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self._saved = [(m, m.DBSession) for m in self.MODULES]
        for module in self.MODULES:
            module.DBSession = sessionmaker(bind=self.engine)
        self._patches = [
            mock.patch.object(housing_letter_store, '_bot_username', 'PotsdamTestBot'),
            mock.patch.dict(os.environ, {'HOUSING_WEB_URL': WEB, 'HOUSING_LINK_REDIRECT': '1',
                                         'HOUSING_APPLY_TRACKER': '1', 'HOUSING_SOCIAL_LINE': '1'}),
            mock.patch.object(housing_links, '_web_up', True),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        for module, session in self._saved:
            module.DBSession = session
        self.engine.dispose()

    def _session(self):
        return sessionmaker(bind=self.engine)()

    def _subscribe(self, user_id=888):
        session = self._session()
        session.add(HousingAccessUser(
            user_id=user_id, display_name='x', active=True, is_trial=False,
            created_at=datetime.utcnow(), updated_at=datetime.utcnow(),
        ))
        session.commit()
        session.close()

    def _listing(self, key='k1', url=PORTAL):
        session = self._session()
        session.add(KleinanzeigenListing(
            listing_key=key, title='Helle 3-Zimmer-Wohnung', address='Babelsberg', rooms=3, area_m2=75,
            price_eur=850, detail_url=url,
            first_seen_at=datetime.utcnow() - timedelta(hours=3), last_seen_at=datetime.utcnow(), is_active=True,
        ))
        session.commit()
        session.close()

    def _gate(self):
        return housing_tier.DeliveryGate('kleinanzeigen', KleinanzeigenListing)

    def _message(self, url=PORTAL):
        return f'<b>Wohnung</b>\nOpen: {html.escape(url)}'

    def _rows(self, model):
        session = self._session()
        try:
            return session.query(model).all()
        finally:
            session.close()


class RewriteTests(_Db):
    def test_only_the_whole_address_is_replaced_not_a_prefix_of_another(self):
        url = 'https://x.example/12'
        text = f'<a href="{url}">A</a> {url}\nhttps://x.example/123'
        out = housing_links.rewrite_text(text, 777, 'kleinanzeigen', 'k1', url)
        self.assertIn('https://x.example/123', out)
        self.assertEqual(out.count('/o/'), 2)


    def test_the_portal_address_in_the_text_becomes_our_link_and_resolves_back(self):
        text = housing_links.rewrite_text(self._message(), 777, 'kleinanzeigen', 'k1', PORTAL)
        self.assertNotIn('kleinanzeigen.de', text)
        token = text.split(f'{WEB}/o/')[1].split()[0]
        ref = housing_links.resolve(token)
        self.assertEqual(ref['url'], PORTAL)  # the saved address, with its & intact
        self.assertEqual((ref['user_id'], ref['listing_key']), (777, 'k1'))

    def test_the_same_person_and_flat_keep_one_token_and_other_people_get_their_own(self):
        a = housing_links.link_for(777, 'kleinanzeigen', 'k1', PORTAL)
        self.assertEqual(a, housing_links.link_for(777, 'kleinanzeigen', 'k1', PORTAL))
        self.assertNotEqual(a, housing_links.link_for(778, 'kleinanzeigen', 'k1', PORTAL))
        self.assertEqual(len(self._rows(HousingLinkRef)), 2)

    def test_switch_off_means_direct_links_as_before(self):
        with mock.patch.dict(os.environ, {'HOUSING_LINK_REDIRECT': '0'}):
            self.assertEqual(housing_links.rewrite_text(self._message(), 777, 'kleinanzeigen', 'k1', PORTAL), self._message())
            self.assertEqual(housing_links.link_for(777, 'kleinanzeigen', 'k1', PORTAL), PORTAL)
        self.assertEqual(self._rows(HousingLinkRef), [])

    def test_no_public_address_or_web_not_running_means_direct_links(self):
        with mock.patch.dict(os.environ, {'HOUSING_WEB_URL': ''}):
            self.assertEqual(housing_links.link_for(777, 'kleinanzeigen', 'k1', PORTAL), PORTAL)
        with mock.patch.object(housing_links, '_web_up', False):
            self.assertEqual(housing_links.link_for(777, 'kleinanzeigen', 'k1', PORTAL), PORTAL)

    def test_a_database_error_leaves_the_direct_link_in_the_message(self):
        broken = mock.Mock(side_effect=RuntimeError('db is down'))
        with mock.patch.object(housing_links, 'DBSession', broken):
            self.assertEqual(housing_links.link_for(777, 'kleinanzeigen', 'k1', PORTAL), PORTAL)
            self.assertEqual(housing_links.rewrite_text(self._message(), 777, 'kleinanzeigen', 'k1', PORTAL), self._message())

    def test_an_error_while_building_our_link_never_breaks_the_gate(self):
        self._listing()
        with mock.patch.object(housing_links, 'token_for', side_effect=RuntimeError('boom')):
            out = self._gate().finish(self._message(), 777, 'k1')
        self.assertIn(html.escape(PORTAL), out)  # direct link
        self.assertIn('start=lt', out)  # and the footer is still there

    def test_an_error_in_the_rewrite_itself_still_leaves_the_flat_with_a_footer(self):
        self._listing()
        with mock.patch.object(housing_links, 'rewrite_for_gate', side_effect=RuntimeError('boom')):
            out = self._gate().finish(self._message(), 777, 'k1')
        self.assertIn(html.escape(PORTAL), out)
        self.assertIn('start=lt', out)

    def test_text_without_the_address_and_unsafe_addresses_are_left_alone(self):
        self.assertEqual(housing_links.rewrite_text('no link here', 777, 'kleinanzeigen', 'k1', PORTAL), 'no link here')
        for bad in ('javascript:alert(1)', 'ftp://x/y', 'https://a b', '//evil.example/x', ''):
            self.assertEqual(housing_links.link_for(777, 'kleinanzeigen', 'k1', bad), bad)
        self.assertEqual(self._rows(HousingLinkRef), [])

    def test_the_gate_takes_the_address_from_the_stored_flat(self):
        self._listing()
        out = self._gate().finish(self._message(), 777, 'k1')
        self.assertIn(f'{WEB}/o/', out)
        self.assertNotIn('kleinanzeigen.de', out)

    def test_a_flat_that_is_not_in_the_database_keeps_the_direct_link(self):
        out = self._gate().finish(self._message(), 777, 'nope')
        self.assertIn(html.escape(PORTAL), out)

    def test_match_line_of_the_current_matches_list_uses_our_link_for_the_person(self):
        line = housing_monitor._match_line('kleinanzeigen', {'listing_key': 'k1', 'title': 'W', 'detail_url': PORTAL}, 'uk', 777)
        self.assertIn(f'{WEB}/o/', line)
        direct = housing_monitor._match_line('kleinanzeigen', {'listing_key': 'k1', 'title': 'W', 'detail_url': PORTAL}, 'uk')
        self.assertIn(html.escape(PORTAL), direct)


class OpenHandlerTests(_Db):
    def _token(self):
        return housing_links.link_for(777, 'kleinanzeigen', 'k1', PORTAL).rsplit('/', 1)[1]

    def test_a_person_opening_the_link_gets_a_302_and_is_counted(self):
        status, target = housing_links.handle_open(self._token(), 'GET', 'Mozilla/5.0 (iPhone) Safari')
        self.assertEqual((status, target), (302, PORTAL))
        [row] = self._rows(HousingLinkOpen)
        self.assertTrue(row.is_human)
        self.assertEqual(row.user_id, 777)

    def test_robots_and_previews_are_redirected_but_not_counted(self):
        token = self._token()
        for method, agent in (('GET', 'TelegramBot (like TwitterBot)'), ('GET', 'Googlebot/2.1'), ('GET', ''),
                              ('HEAD', 'Mozilla/5.0 Safari'), ('GET', 'Mozilla/5.0 (compatible; bingbot)')):
            self.assertEqual(housing_links.handle_open(token, method, agent), (302, PORTAL))
        self.assertTrue(all(not row.is_human for row in self._rows(HousingLinkOpen)))
        self.assertEqual(housing_links.social_numbers('k1')['opened'], 0)

    def test_unknown_or_odd_tokens_are_404_and_never_redirect(self):
        for token in ('nope1234', '', 'a', '../etc', 'x' * 80, 'http://evil.example'):
            self.assertEqual(housing_links.handle_open(token, 'GET', 'Mozilla/5.0'), (404, None))

    def test_a_failed_write_of_the_open_does_not_stop_the_redirect(self):
        token = self._token()
        with mock.patch.object(housing_links, 'record_open', side_effect=RuntimeError('disk full')):
            self.assertEqual(housing_links.handle_open(token, 'GET', 'Mozilla/5.0 Safari'), (302, PORTAL))

    def test_a_database_error_inside_record_open_is_swallowed(self):
        ref = housing_links.resolve(self._token())
        with mock.patch.object(housing_links, 'DBSession', mock.Mock(side_effect=RuntimeError('x'))):
            with self.assertRaises(RuntimeError):  # raised by DBSession() itself ...
                housing_links.record_open(ref, True)
        # ... which handle_open guards:
        with mock.patch.object(housing_links, 'record_open', side_effect=RuntimeError('x')):
            self.assertEqual(housing_links.handle_open(ref['token'], 'GET', 'Mozilla/5.0 Safari')[0], 302)

    def test_the_not_found_page_is_plain_and_in_three_languages_without_request_data(self):
        page = housing_links.not_found_page()
        for lang in i18n.SUPPORTED_LANGS:
            self.assertIn(html.escape(i18n.t('housing.link.not_found', lang)), page)
        self.assertIn('t.me/PotsdamTestBot', page)


class WebServerTests(_Db):
    def setUp(self):
        super().setUp()
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), housing_web.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def _request(self, method, path, agent='Mozilla/5.0 (iPhone) Safari'):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        conn.request(method, path, headers={'User-Agent': agent})
        response = conn.getresponse()
        body = response.read().decode('utf-8')
        conn.close()
        return response, body

    def test_a_known_link_answers_302_to_the_saved_address(self):
        token = housing_links.link_for(777, 'kleinanzeigen', 'k1', PORTAL).rsplit('/', 1)[1]
        response, _ = self._request('GET', f'/o/{token}')
        self.assertEqual(response.status, 302)
        self.assertEqual(response.getheader('Location'), PORTAL)
        self.assertEqual(response.getheader('Referrer-Policy'), 'no-referrer')

    def test_an_address_with_umlauts_redirects_as_ascii_not_500(self):
        url = 'https://www.immowelt.de/expose/Wohnung-Königs–Wusterhausen?a=1&b=ß'
        token = housing_links.link_for(777, 'kleinanzeigen', 'k2', url).rsplit('/', 1)[1]
        response, _ = self._request('GET', f'/o/{token}')
        self.assertEqual(response.status, 302)
        location = response.getheader('Location')
        self.assertTrue(location.isascii())
        from urllib.parse import unquote
        self.assertEqual(unquote(location), url)

    def test_a_broken_lookup_answers_404_not_500(self):
        with mock.patch.object(housing_links, 'handle_open', side_effect=RuntimeError('db')):
            response, _ = self._request('GET', '/o/abcdefgh')
        self.assertEqual(response.status, 404)

    def test_the_address_in_the_request_is_ignored_no_open_redirect(self):
        token = housing_links.link_for(777, 'kleinanzeigen', 'k1', PORTAL).rsplit('/', 1)[1]
        for path in (f'/o/{token}?url=https://evil.example', f'/o/{token}?next=//evil.example'):
            response, _ = self._request('GET', path)
            self.assertEqual(response.getheader('Location'), PORTAL)
        response, body = self._request('GET', '/o/unknowntoken?url=https://evil.example')
        self.assertEqual(response.status, 404)
        self.assertIsNone(response.getheader('Location'))
        self.assertNotIn('evil.example', body)

    def test_head_and_telegram_preview_follow_the_link_without_being_counted(self):
        token = housing_links.link_for(777, 'kleinanzeigen', 'k1', PORTAL).rsplit('/', 1)[1]
        response, _ = self._request('HEAD', f'/o/{token}')
        self.assertEqual(response.status, 302)
        response, _ = self._request('GET', f'/o/{token}', agent='TelegramBot (like TwitterBot)')
        self.assertEqual(response.status, 302)
        self.assertEqual(housing_links.social_numbers('k1')['opened'], 0)
        self._request('GET', f'/o/{token}')
        self.assertEqual(housing_links.social_numbers('k1')['opened'], 1)

    def test_the_other_pages_still_need_a_login(self):
        with mock.patch.dict('os.environ', {'HOUSING_WEB_CABINET': 'on'}):      # 28.11.2026: кабинет по умолчанию выключен
            response, _ = self._request('GET', '/')
            self.assertEqual(response.status, 401)
        response, body = self._request('GET', '/health')
        self.assertEqual((response.status, body), (200, 'ok'))

    def test_the_cabinet_is_off_by_default_but_links_still_redirect(self):
        with mock.patch.dict('os.environ', {'HOUSING_WEB_CABINET': ''}), \
                mock.patch('user_handlers.masters_hint._file_cfg', return_value={}):
            response, body = self._request('GET', '/')
            self.assertEqual(response.status, 200)
            self.assertIn('Кабінет переїхав у Telegram', body)
            response, _ = self._request('GET', '/login?t=x')
            self.assertEqual(response.status, 200)
            response, _ = self._request('GET', '/o/nope')
            self.assertEqual(response.status, 404)      # переход по ссылке квартиры работает (токена нет)


class SocialLineTests(_Db):
    def _send(self, user_id, delayed, minutes_ago=30, key='k1'):
        session = self._session()
        session.add(HousingSendLog(
            user_id=user_id, source='kleinanzeigen', listing_key=key, delayed=delayed,
            sent_at=datetime.utcnow() - timedelta(minutes=minutes_ago),
        ))
        session.commit()
        session.close()

    def _open(self, user_id, human=True, key='k1'):
        session = self._session()
        session.add(HousingLinkOpen(
            token='t%s' % user_id, user_id=user_id, source='kleinanzeigen', listing_key=key,
            opened_at=datetime.utcnow(), is_human=human,
        ))
        session.commit()
        session.close()

    def test_no_data_means_no_line(self):
        self.assertEqual(housing_links.listing_social_line('k1', 'uk'), '')

    def test_instant_time_only_from_a_real_subscriber_send_and_in_berlin_time(self):
        self._send(5, delayed=True)  # a free person's delayed send is not «subscribers got it»
        self.assertEqual(housing_links.listing_social_line('k1', 'uk'), '')
        moment = datetime(2026, 11, 24, 13, 5)  # 14:05 in Berlin
        session = self._session()
        session.add(HousingSendLog(user_id=6, source='kleinanzeigen', listing_key='k1', delayed=False, sent_at=moment))
        session.commit()
        session.close()
        self.assertEqual(housing_links.listing_social_line('k1', 'uk'), '⚡ Підписники отримали о 14:05')

    def test_opened_counts_unique_humans_and_applied_counts_unique_people(self):
        self._send(6, delayed=False)
        self._open(1)
        self._open(1)
        self._open(2)
        self._open(3, human=False)
        self._listing()
        for uid in (1, 2, 9):
            housing_tracker.mark_applied(uid, 'kleinanzeigen', 'k1')
        housing_tracker.mark_applied(9, 'kleinanzeigen', 'k1')
        line = housing_links.listing_social_line('k1', 'uk')
        self.assertTrue(line.startswith('⚡ Підписники отримали о '))
        self.assertTrue(line.endswith(' · відкрили: 2 · подались: 3'))

    def test_zero_parts_are_not_shown(self):
        self._open(1)
        self.assertEqual(housing_links.listing_social_line('k1', 'uk'), 'відкрили: 1')
        self.assertEqual(housing_links.listing_social_line('k1', 'de'), 'angesehen: 1')

    def test_other_flats_do_not_leak_in(self):
        self._open(1, key='other')
        self.assertEqual(housing_links.listing_social_line('k1', 'uk'), '')

    def test_the_line_is_shown_to_free_people_only(self):
        self._listing()
        self._send(6, delayed=False)
        self._open(1)
        self._subscribe(888)
        free = self._gate().footer(777, 'k1')
        paid = self._gate().footer(888, 'k1')
        self.assertIn('відкрили: 1', free)
        self.assertNotIn('відкрили', paid)
        self.assertNotIn('Підписники отримали', paid)

    def test_its_own_switch_turns_only_this_line_off(self):
        self._listing()
        self._open(1)
        with mock.patch.dict(os.environ, {'HOUSING_SOCIAL_LINE': '0'}):
            self.assertEqual(housing_links.listing_social_line('k1', 'uk'), '')
            out = self._gate().finish(self._message(), 777, 'k1')
        self.assertNotIn('відкрили', out)
        self.assertIn(f'{WEB}/o/', out)  # redirect is independent of it

    def test_a_broken_database_gives_an_empty_line_not_an_error(self):
        with mock.patch.object(housing_links, 'DBSession', mock.Mock(side_effect=RuntimeError('x'))):
            self.assertEqual(housing_links.listing_social_line('k1', 'uk'), '')

    def test_the_source_argument_narrows_the_count(self):
        self._open(1)
        self.assertEqual(housing_links.listing_social_line('k1', 'uk', source='immowelt'), '')
        self.assertEqual(housing_links.listing_social_line('k1', 'uk', source='kleinanzeigen'), 'відкрили: 1')


class FooterTests(_Db):
    def test_one_compact_line_with_letter_and_apply_links(self):
        self._listing()
        self._subscribe(888)
        footer = self._gate().footer(888, 'k1')
        line =[part for part in footer.split('\n') if 'start=' in part]
        self.assertEqual(len(line), 1)
        self.assertIn('start=lt1">✍️ Лист</a> · <a href="https://t.me/PotsdamTestBot?start=ap1">✅ Подався</a>', line[0])

    def test_free_people_see_the_letter_marked_with_a_lightning(self):
        self.assertIn('✍️ Лист · 1 безкоштовно</a>', self._gate().footer(777, 'k1'))

    def test_tracker_switch_off_brings_back_the_old_letter_line(self):
        with mock.patch.dict(os.environ, {'HOUSING_APPLY_TRACKER': '0'}):
            footer = self._gate().footer(777, 'k1')
        self.assertIn('(1 безкоштовно)', footer)
        self.assertNotIn('start=ap', footer)

    def test_no_bot_name_no_links(self):
        with mock.patch.object(housing_letter_store, '_bot_username', None):
            self.assertEqual(self._gate().footer(888, 'k1').count('t.me'), 0)

    def test_immowelt_card_uses_our_link_on_the_button_and_the_footer_line(self):
        from user_handlers import housing_receiver

        sessions = [(housing_receiver, housing_receiver.DBSession)]
        housing_receiver.DBSession = sessionmaker(bind=self.engine)
        self._subscribe(888)
        bot = mock.Mock()
        payload = {'source': 'immowelt', 'user_id': 888, 'filter_title': 'f', 'listing': {
            'listing_id': 'iw-1', 'url': 'https://www.immowelt.de/expose/iw-1', 'title': 'Wohnung',
            'address': 'Potsdam', 'price': '600 €'}}
        try:
            with mock.patch.object(housing_receiver, 'immowelt_alive', return_value=None):
                housing_receiver.handle_immowelt_result(bot, payload)
        finally:
            for module, session in sessions:
                module.DBSession = session
        kwargs = bot.send_message.call_args.kwargs
        button = kwargs['reply_markup'].inline_keyboard[0][0]
        self.assertTrue(button.url.startswith(f'{WEB}/o/'))
        self.assertIn('start=ap', kwargs['text'])
        token = button.url.rsplit('/', 1)[1]
        self.assertEqual(housing_links.resolve(token)['url'], 'https://www.immowelt.de/expose/iw-1')

    def test_immowelt_card_keeps_the_direct_button_when_the_redirect_is_off(self):
        from user_handlers import housing_receiver

        original = housing_receiver.DBSession
        housing_receiver.DBSession = sessionmaker(bind=self.engine)
        self._subscribe(888)
        bot = mock.Mock()
        payload = {'source': 'immowelt', 'user_id': 888, 'filter_title': 'f', 'listing': {
            'listing_id': 'iw-2', 'url': 'https://www.immowelt.de/expose/iw-2', 'title': 'Wohnung', 'address': 'P'}}
        try:
            with mock.patch.dict(os.environ, {'HOUSING_LINK_REDIRECT': '0'}), \
                    mock.patch.object(housing_receiver, 'immowelt_alive', return_value=None):
                housing_receiver.handle_immowelt_result(bot, payload)
        finally:
            housing_receiver.DBSession = original
        button = bot.send_message.call_args.kwargs['reply_markup'].inline_keyboard[0][0]
        self.assertEqual(button.url, 'https://www.immowelt.de/expose/iw-2')


class DateParseTests(unittest.TestCase):
    NOW = datetime(2026, 11, 24, 10, 0)  # 11:00 in Berlin

    def _parse(self, text):
        return housing_dateparse.parse_viewing(text, self.NOW)

    def _berlin(self, moment):
        return pytz.utc.localize(moment).astimezone(pytz.timezone('Europe/Berlin')).strftime('%Y-%m-%d %H:%M')

    def test_words_in_three_languages(self):
        self.assertEqual(self._berlin(self._parse('завтра 15:00')), '2026-11-25 15:00')
        self.assertEqual(self._berlin(self._parse('Завтра о 9:30')), '2026-11-25 09:30')
        self.assertEqual(self._berlin(self._parse('післязавтра 18:00')), '2026-11-26 18:00')
        self.assertEqual(self._berlin(self._parse('послезавтра в 18:00')), '2026-11-26 18:00')
        self.assertEqual(self._berlin(self._parse('morgen 9:15')), '2026-11-25 09:15')
        self.assertEqual(self._berlin(self._parse('übermorgen 8:00')), '2026-11-26 08:00')
        self.assertEqual(self._berlin(self._parse('сьогодні 18:30')), '2026-11-24 18:30')
        self.assertEqual(self._berlin(self._parse('heute 12:00')), '2026-11-24 12:00')

    def test_numeric_dates(self):
        self.assertEqual(self._berlin(self._parse('28.11 10:30')), '2026-11-28 10:30')
        self.assertEqual(self._berlin(self._parse('28.11. 10:30')), '2026-11-28 10:30')
        self.assertEqual(self._berlin(self._parse('28.11.2026 10:30')), '2026-11-28 10:30')
        self.assertEqual(self._berlin(self._parse('10:30 28.11')), '2026-11-28 10:30')
        self.assertEqual(self._berlin(self._parse('5.12 9:00')), '2026-12-05 09:00')

    def test_a_date_that_has_passed_this_year_means_next_year(self):
        self.assertEqual(self._berlin(self._parse('10.01 9:00')), '2027-01-10 09:00')

    def test_summer_time_is_taken_from_berlin(self):
        moment = housing_dateparse.parse_viewing('15.07 12:00', self.NOW)
        self.assertEqual(moment, datetime(2027, 7, 15, 10, 0))  # CEST = UTC+2

    def test_unclear_or_past_input_is_none(self):
        for text in ('', 'коли-небудь', '15:00', 'завтра', '28.11', '32.11 10:00', '30.02 10:00', 'сьогодні 08:00',
                     'x' * 200, '24.11 10:30'):
            self.assertIsNone(self._parse(text), text)


class TrackerTests(_Db):
    def setUp(self):
        super().setUp()
        self._listing()
        self.allowed = mock.patch.object(housing_monitor, 'is_allowed', return_value=True)
        self.allowed.start()

    def tearDown(self):
        self.allowed.stop()
        super().tearDown()

    def _ctx(self):
        bot = mock.Mock()
        return SimpleNamespace(bot=bot, user_data={}, args=[]), bot

    def _update(self, user_id=777, text=None, data=None):
        query = SimpleNamespace(data=data, answer=mock.Mock(), edit_message_text=mock.Mock())
        message = SimpleNamespace(text=text) if text is not None else None
        return SimpleNamespace(effective_user=SimpleNamespace(id=user_id), callback_query=query if data else None,
                               message=message, effective_chat=SimpleNamespace(type='private')), query

    def _ref(self):
        return housing_letter_store.ref_for('kleinanzeigen', 'k1')

    def _apply(self, user_id=777):
        context, bot = self._ctx()
        update, _ = self._update(user_id)
        housing_tracker.handle_start(update, context, f'ap{self._ref()}')
        return context, bot

    def test_tapping_apply_records_a_copy_of_the_flat_and_answers_with_status_buttons(self):
        context, bot = self._apply()
        [app] = self._rows(HousingApplication)
        self.assertEqual((app.user_id, app.title, app.address, app.price_text, app.status),
                         (777, 'Helle 3-Zimmer-Wohnung', 'Babelsberg', '850 €', 'applied'))
        self.assertEqual(app.url, PORTAL)
        kwargs = bot.send_message.call_args.kwargs
        self.assertIn('✅ Записав: Helle 3-Zimmer-Wohnung', kwargs['text'])
        callbacks = [b.callback_data for row in kwargs['reply_markup'].inline_keyboard for b in row]
        self.assertIn(f'hap:st:{app.id}:re', callbacks)
        self.assertIn(f'hap:vw:{app.id}', callbacks)

    def test_a_second_tap_does_not_make_a_second_record(self):
        self._apply()
        _, bot = self._apply()
        self.assertEqual(len(self._rows(HousingApplication)), 1)
        self.assertIn('вже є в списку', bot.send_message.call_args.kwargs['text'])

    def test_start_parameter_is_routed_by_chat_start_and_other_payloads_are_not(self):
        context, bot = self._ctx()
        context.args = [f'ap{self._ref()}']
        update, _ = self._update(777)
        update.message = SimpleNamespace(from_user=SimpleNamespace(id=777))
        chat_start.start(update, context)
        self.assertEqual(len(self._rows(HousingApplication)), 1)
        with mock.patch('user_handlers.anonymous_posts.show_home') as home:
            context.args = ['apple']
            chat_start.start(update, context)
            home.assert_called_once()

    def test_a_stranger_does_not_pollute_the_numbers(self):
        with mock.patch.object(housing_monitor, 'is_allowed', return_value=False):
            context, bot = self._ctx()
            update, _ = self._update(555)
            housing_tracker.handle_start(update, context, f'ap{self._ref()}')
        self.assertEqual(self._rows(HousingApplication), [])
        self.assertIn('Не знайшов', bot.send_message.call_args.kwargs['text'])

    def test_unknown_reference_or_switch_off_records_nothing(self):
        context, bot = self._ctx()
        update, _ = self._update(777)
        housing_tracker.handle_start(update, context, 'ap999')
        with mock.patch.dict(os.environ, {'HOUSING_APPLY_TRACKER': '0'}):
            housing_tracker.handle_start(update, context, f'ap{self._ref()}')
        self.assertEqual(self._rows(HousingApplication), [])

    def test_statuses_and_removal(self):
        self._apply()
        [app] = self._rows(HousingApplication)
        context, bot = self._ctx()
        update, query = self._update(data=f'hap:st:{app.id}:re')
        housing_tracker.handle_callback(update, context)
        self.assertEqual(self._rows(HousingApplication)[0].status, 'replied')
        update, _ = self._update(data=f'hap:st:{app.id}:rj')
        housing_tracker.handle_callback(update, context)
        self.assertEqual(self._rows(HousingApplication)[0].status, 'rejected')
        update, _ = self._update(data=f'hap:rm:{app.id}')
        housing_tracker.handle_callback(update, context)
        self.assertEqual(housing_tracker.count_apps(777), 0)
        # the record stays: the person still counts in «подались»
        self.assertEqual(housing_links.social_numbers('k1')['applied'], 1)

    def test_nobody_can_touch_somebody_elses_application(self):
        self._apply(777)
        [app] = self._rows(HousingApplication)
        context, _ = self._ctx()
        update, _ = self._update(user_id=778, data=f'hap:st:{app.id}:re')
        housing_tracker.handle_callback(update, context)
        update, _ = self._update(user_id=778, data=f'hap:rm:{app.id}')
        housing_tracker.handle_callback(update, context)
        row = self._rows(HousingApplication)[0]
        self.assertEqual((row.status, row.hidden), ('applied', False))

    def test_the_viewing_date_is_asked_parsed_and_saved(self):
        self._apply()
        [app] = self._rows(HousingApplication)
        context, bot = self._ctx()
        update, _ = self._update(data=f'hap:vw:{app.id}')
        housing_tracker.handle_callback(update, context)
        self.assertEqual(context.user_data[housing_tracker.STATE_KEY], app.id)
        update, _ = self._update(text='нісенітниця')
        self.assertTrue(housing_tracker.handle_text(update, context))
        self.assertIn('Не зрозумів', bot.send_message.call_args.kwargs['text'])
        self.assertIn(housing_tracker.STATE_KEY, context.user_data)  # still waiting
        future = datetime.utcnow() + timedelta(days=3)
        local = pytz.utc.localize(future).astimezone(housing_tracker.BERLIN)
        update, _ = self._update(text=local.strftime('%d.%m 15:00'))
        self.assertTrue(housing_tracker.handle_text(update, context))
        row = self._rows(HousingApplication)[0]
        self.assertEqual(row.status, 'viewing')
        self.assertIsNotNone(row.viewing_at)
        self.assertNotIn(housing_tracker.STATE_KEY, context.user_data)
        self.assertIn('з підпискою', bot.send_message.call_args.kwargs['text'])  # free: no reminders

    def test_text_is_not_taken_when_no_date_is_expected(self):
        context, _ = self._ctx()
        update, _ = self._update(text='привіт')
        self.assertFalse(housing_tracker.handle_text(update, context))

    def test_free_people_see_five_applications_and_a_subscription_line_subscribers_all(self):
        session = self._session()
        for i in range(7):
            session.add(HousingApplication(
                user_id=777, source='kleinanzeigen', listing_key=f'x{i}', title=f'Flat {i}', status='applied',
                hidden=False, remind_eve_sent=False, remind_2h_sent=False,
                created_at=datetime.utcnow() - timedelta(hours=i), updated_at=datetime.utcnow(),
            ))
        session.commit()
        session.close()
        context, bot = self._ctx()
        update, query = self._update(data='hap:list:0')
        housing_tracker.handle_callback(update, context)
        text = query.edit_message_text.call_args.args[0]
        self.assertIn('Flat 0', text)
        self.assertIn('Flat 4', text)
        self.assertNotIn('Flat 5', text)
        self.assertLess(text.index('Flat 0'), text.index('Flat 4'))  # fresh first
        self.assertIn('Є ще 2', text)
        callbacks = [b.callback_data for row in query.edit_message_text.call_args.kwargs['reply_markup'].inline_keyboard for b in row]
        self.assertIn('housing:access_request', callbacks)
        self._subscribe(777)
        update, query = self._update(data='hap:list:0')
        housing_tracker.handle_callback(update, context)
        text = query.edit_message_text.call_args.args[0]
        self.assertIn('Flat 6', text)
        self.assertNotIn('Є ще', text)

    def test_empty_list_says_so_without_numbers(self):
        context, _ = self._ctx()
        update, query = self._update(data='hap:list:0')
        housing_tracker.handle_callback(update, context)
        self.assertIn('Заявок поки немає', query.edit_message_text.call_args.args[0])

    def test_found_calls_flow_a_function_when_it_exists_else_congratulates_itself(self):
        self._apply()
        [app] = self._rows(HousingApplication)
        context, bot = self._ctx()
        update, _ = self._update(data=f'hap:fd:{app.id}')
        housing_tracker.handle_callback(update, context)
        self.assertEqual(self._rows(HousingApplication)[0].status, 'found')
        texts = [c.kwargs.get('text', '') for c in bot.send_message.call_args_list]
        # Після злиття (25.11.2026) модуль потоку A є: пауза + його вітання з подякою, одне.
        self.assertEqual(sum('Вітаємо з новим житлом' in t for t in texts), 1)
        self.assertFalse(any('Вітаю з квартирою' in t for t in texts))
        # Повторне «Отримав квартиру» - друге вітання не йде.
        context, bot = self._ctx()
        housing_tracker.handle_callback(update, context)
        texts = [c.kwargs.get('text', '') for c in bot.send_message.call_args_list]
        self.assertFalse(any('Вітаємо з новим житлом' in t or 'Вітаю з квартирою' in t for t in texts))
        # Функцію вимкнено - вітаємо самі, пошуки не чіпаємо.
        with mock.patch.dict(os.environ, {'HOUSING_FOUND_ENABLED': '0'}):
            context, bot = self._ctx()
            housing_tracker.handle_callback(update, context)
        texts = [c.kwargs.get('text', '') for c in bot.send_message.call_args_list]
        self.assertTrue(any('Вітаю з квартирою' in t for t in texts))
        fake = SimpleNamespace(mark_found=mock.Mock())
        with mock.patch('importlib.import_module', side_effect=lambda name: fake if name == 'user_jobs.housing_found' else (_ for _ in ()).throw(ImportError(name))):
            context, bot = self._ctx()
            housing_tracker.handle_callback(update, context)
        fake.mark_found.assert_called_once_with(777)
        self.assertFalse(any('Вітаю з квартирою' in c.kwargs.get('text', '') for c in bot.send_message.call_args_list))

    def test_mark_found_failure_does_not_break_the_screen(self):
        self._apply()
        [app] = self._rows(HousingApplication)
        fake = SimpleNamespace(mark_found=mock.Mock(side_effect=RuntimeError('x')))
        context, bot = self._ctx()
        update, query = self._update(data=f'hap:fd:{app.id}')
        with mock.patch('importlib.import_module', return_value=fake):
            housing_tracker.handle_callback(update, context)
        self.assertEqual(self._rows(HousingApplication)[0].status, 'found')

    def test_letter_screen_offers_to_mark_the_flat_as_applied(self):
        self._subscribe(777)
        bot = mock.Mock()
        bot.send_message.return_value = SimpleNamespace(message_id=5)
        with mock.patch.object(housing_letter, '_call_ai', return_value='Sehr geehrte Damen und Herren'), \
                mock.patch.object(housing_letter.threading, 'Thread', lambda target, **_: SimpleNamespace(start=target)):
            housing_letter_store.save_profile(777, {'full_name': 'A', 'household': 'B', 'income': 'C', 'move_in': 'D'})
            housing_letter._generate_and_send(bot, 777, self._ref(), variant=False)
        markup = bot.send_message.call_args_list[-1].kwargs['reply_markup']
        callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertIn(f'hap:mark:{self._ref()}', callbacks)

    def test_mark_button_after_the_letter_records_the_application(self):
        context, bot = self._ctx()
        update, query = self._update(data=f'hap:mark:{self._ref()}')
        housing_tracker.handle_callback(update, context)
        self.assertEqual(len(self._rows(HousingApplication)), 1)

    def test_menu_has_the_applications_button_unless_switched_off(self):
        def callbacks():
            keyboard = housing_monitor._menu_keyboard(888, 'uk')
            return [b.callback_data for row in keyboard.inline_keyboard for b in row]

        self._subscribe(888)
        self.assertIn('hap:list:0', callbacks())
        with mock.patch.dict(os.environ, {'HOUSING_APPLY_TRACKER': '0'}):
            self.assertNotIn('hap:list:0', callbacks())

    def test_web_history_shows_the_applications_read_only(self):
        self._apply()
        with mock.patch.object(housing_web, 'full_access', return_value=True):
            block = housing_web._applications_block(777, 'uk', True)
        self.assertIn('Helle 3-Zimmer-Wohnung', block)
        self.assertIn('подано', block)
        self.assertEqual(housing_web._applications_block(999, 'uk', True), '')


class ReminderTests(_Db):
    def setUp(self):
        super().setUp()
        self._subscribe(888)
        self.bot = mock.Mock()

    def _app(self, viewing, user_id=888, status='viewing', set_at=None):
        session = self._session()
        session.add(HousingApplication(
            user_id=user_id, source='kleinanzeigen', listing_key=f'k{user_id}{viewing}', title='Flat', address='Addr 1',
            status='applied', hidden=False, remind_eve_sent=False, remind_2h_sent=False,
            created_at=datetime.utcnow(), updated_at=datetime.utcnow(),
        ))
        session.commit()
        app_id = session.query(HousingApplication).order_by(HousingApplication.id.desc()).first().id
        session.close()
        housing_tracker.set_viewing(user_id, app_id, viewing, now=set_at or datetime.utcnow())
        if status != 'viewing':
            housing_tracker.set_status(user_id, app_id, status)
        return app_id

    def test_eve_moment_is_19_berlin_the_day_before(self):
        viewing = datetime(2026, 11, 28, 9, 30)  # 10:30 Berlin
        self.assertEqual(housing_tracker.eve_moment(viewing), datetime(2026, 11, 27, 18, 0))  # 19:00 CET
        summer = datetime(2027, 7, 15, 8, 0)
        self.assertEqual(housing_tracker.eve_moment(summer), datetime(2027, 7, 14, 17, 0))  # 19:00 CEST

    def test_evening_before_then_two_hours_before_each_once(self):
        viewing = datetime(2026, 11, 28, 12, 0)  # 13:00 Berlin
        self._app(viewing, set_at=datetime(2026, 11, 24, 10, 0))
        self.assertEqual(housing_tracker.send_due_reminders(self.bot, now=datetime(2026, 11, 27, 17, 59)), 0)
        self.assertEqual(housing_tracker.send_due_reminders(self.bot, now=datetime(2026, 11, 27, 18, 5)), 1)
        self.assertIn('Завтра перегляд о 13:00', self.bot.send_message.call_args.kwargs['text'])
        self.assertIn('Addr 1', self.bot.send_message.call_args.kwargs['text'])
        self.assertEqual(housing_tracker.send_due_reminders(self.bot, now=datetime(2026, 11, 27, 18, 10)), 0)
        self.assertEqual(housing_tracker.send_due_reminders(self.bot, now=datetime(2026, 11, 28, 10, 5)), 1)
        self.assertIn('За 2 години перегляд, о 13:00', self.bot.send_message.call_args.kwargs['text'])
        self.assertEqual(housing_tracker.send_due_reminders(self.bot, now=datetime(2026, 11, 28, 10, 30)), 0)

    def test_if_the_eve_was_missed_only_the_two_hour_reminder_goes(self):
        viewing = datetime(2026, 11, 28, 12, 0)
        self._app(viewing, set_at=datetime(2026, 11, 24, 10, 0))
        self.assertEqual(housing_tracker.send_due_reminders(self.bot, now=datetime(2026, 11, 28, 10, 5)), 1)
        self.assertEqual(housing_tracker.send_due_reminders(self.bot, now=datetime(2026, 11, 28, 10, 10)), 0)

    def test_a_date_set_after_the_moment_does_not_get_a_late_evening_reminder(self):
        viewing = datetime(2026, 11, 28, 12, 0)
        self._app(viewing, set_at=datetime(2026, 11, 27, 20, 0))  # evening has passed already
        self.assertEqual(housing_tracker.send_due_reminders(self.bot, now=datetime(2026, 11, 27, 20, 5)), 0)
        self.assertEqual(housing_tracker.send_due_reminders(self.bot, now=datetime(2026, 11, 28, 10, 5)), 1)

    def test_free_people_get_no_reminders(self):
        viewing = datetime(2026, 11, 28, 12, 0)
        self._app(viewing, user_id=777, set_at=datetime(2026, 11, 24, 10, 0))
        self.assertEqual(housing_tracker.send_due_reminders(self.bot, now=datetime(2026, 11, 28, 10, 5)), 0)
        self.bot.send_message.assert_not_called()

    def test_removed_rejected_and_past_viewings_are_not_reminded(self):
        viewing = datetime(2026, 11, 28, 12, 0)
        app_id = self._app(viewing, set_at=datetime(2026, 11, 24, 10, 0))
        housing_tracker.set_status(888, app_id, 'rejected')
        self.assertEqual(housing_tracker.send_due_reminders(self.bot, now=datetime(2026, 11, 28, 10, 5)), 0)
        housing_tracker.set_status(888, app_id, 'viewing')
        housing_tracker.hide_app(888, app_id)
        self.assertEqual(housing_tracker.send_due_reminders(self.bot, now=datetime(2026, 11, 28, 10, 5)), 0)
        self.assertEqual(housing_tracker.send_due_reminders(self.bot, now=datetime(2026, 11, 29, 10, 5)), 0)

    def test_a_failed_send_is_retried_and_a_blocked_bot_is_not(self):
        viewing = datetime(2026, 11, 28, 12, 0)
        self._app(viewing, set_at=datetime(2026, 11, 24, 10, 0))
        self.bot.send_message.side_effect = RuntimeError('network')
        self.assertEqual(housing_tracker.send_due_reminders(self.bot, now=datetime(2026, 11, 28, 10, 5)), 0)
        self.bot.send_message.side_effect = None
        self.assertEqual(housing_tracker.send_due_reminders(self.bot, now=datetime(2026, 11, 28, 10, 10)), 1)

    def test_the_job_respects_the_switch(self):
        # Время зафиксировано (13:00 по Берлину): ночью 22-09 напоминания не шлются,
        # тест не должен зависеть от часа, в который его запускают.
        noon = datetime(2026, 11, 26, 12, 0)
        viewing = noon + timedelta(hours=1)
        self._app(viewing, set_at=noon - timedelta(days=2))
        with mock.patch.object(housing_tracker, 'utc_now', return_value=noon):
            with mock.patch.dict(os.environ, {'HOUSING_APPLY_TRACKER': '0'}):
                housing_tracker.reminders_job(SimpleNamespace(bot=self.bot))
            self.bot.send_message.assert_not_called()
            housing_tracker.reminders_job(SimpleNamespace(bot=self.bot))
        self.assertEqual(self.bot.send_message.call_count, 1)


class TranslationTests(unittest.TestCase):
    KEYS_PREFIXES = ('housing.link.', 'housing.social.', 'housing.track.', 'housing.menu.btn.applications')

    def test_every_new_string_exists_in_uk_ru_de(self):
        keys = {k for k in i18n._TRANSLATIONS['uk'] if k.startswith(self.KEYS_PREFIXES)}
        self.assertGreater(len(keys), 30)
        for lang in ('ru', 'de'):
            self.assertEqual(keys - set(i18n._TRANSLATIONS[lang]), set(), lang)

    def test_placeholders_match_between_languages(self):
        import re

        for key in (k for k in i18n._TRANSLATIONS['uk'] if k.startswith(self.KEYS_PREFIXES)):
            names = set(re.findall(r'{(\w+)}', i18n._TRANSLATIONS['uk'][key]))
            for lang in ('ru', 'de'):
                self.assertEqual(names, set(re.findall(r'{(\w+)}', i18n._TRANSLATIONS[lang][key])), (key, lang))


if __name__ == '__main__':
    unittest.main()
