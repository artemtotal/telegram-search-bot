"""Підказки про платні функції під квартирою безкоштовного (25.11.2026)."""

import os
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import i18n
from database import Base, HousingAccessUser, KleinanzeigenListing
from user_handlers import chat_start
from user_jobs import (
    housing_free_hints, housing_letter_store, housing_links, housing_pay_store, housing_tier,
    user_settings_store,
)
from user_jobs.housing_free_hints import HousingFreeHintState, ORDER


class _Db(unittest.TestCase):
    MODULES = (housing_free_hints, housing_tier, housing_pay_store, housing_links, user_settings_store)

    def setUp(self):
        self.engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self._saved = [(m, m.DBSession) for m in self.MODULES]
        for module in self.MODULES:
            module.DBSession = sessionmaker(bind=self.engine)
        self._patches = [
            mock.patch.object(housing_letter_store, '_bot_username', 'PotsdamTestBot'),
            mock.patch.object(housing_tier, 'QUIET_START_HOUR', 23),
            mock.patch.object(housing_tier, 'QUIET_END_HOUR', 8),
            mock.patch.dict(os.environ, {'HOUSING_FREE_HINTS': '1', 'HOUSING_APPLY_TRACKER': '1'}),
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

    def _missed(self, user_id, count, days_ago=1):
        session = self._session()
        for index in range(count):
            session.add(housing_pay_store.HousingMissedLog(
                user_id=user_id, source='kleinanzeigen', listing_key=f'm{days_ago}-{index}',
                created_at=datetime.utcnow() - timedelta(days=days_ago),
            ))
        session.commit()
        session.close()

    def _state(self, user_id):
        session = self._session()
        try:
            row = session.query(HousingFreeHintState).get(user_id)
            return None if row is None else row.next_index
        finally:
            session.close()

    def _set_state(self, user_id, index):
        session = self._session()
        session.add(HousingFreeHintState(user_id=user_id, next_index=index))
        session.commit()
        session.close()

    def _gate(self):
        return housing_tier.DeliveryGate('kleinanzeigen', KleinanzeigenListing)


class CircleTests(_Db):
    def test_the_first_hint_is_about_speed_and_ends_with_the_payment_link(self):
        text = housing_free_hints.build(777)
        self.assertTrue(text.startswith('\n\n⏱ '))
        self.assertIn('~2 год', text)
        self.assertIn('<a href="https://t.me/PotsdamTestBot?start=sub">⚡ Підписка від 5 €</a>', text)

    def test_every_next_flat_gets_the_next_hint_and_the_circle_closes(self):
        seen = [housing_free_hints.build(777) for _ in range(len(ORDER) - 1)]   # без «упущених»: 9 підказок
        self.assertEqual(len(set(seen)), len(ORDER) - 1)
        self.assertEqual(housing_free_hints.build(777), seen[0])   # коло замкнулось
        for text in seen:
            self.assertEqual(text.count('\n'), 3)   # \n\n + підказка + \n + посилання
            self.assertNotIn('!', text)

    def test_the_position_is_stored_so_it_survives_a_restart(self):
        housing_free_hints.build(777)
        self.assertEqual(self._state(777), 1)
        housing_free_hints.build(777)
        self.assertEqual(self._state(777), 2)
        # «перезапуск»: пам'ять процесу порожня, а лічильник у базі
        again = housing_free_hints.build(777)
        self.assertIn('📁', again)   # третя - папка
        self.assertEqual(self._state(777), 3)

    def test_people_have_their_own_positions(self):
        a1 = housing_free_hints.build(1)
        housing_free_hints.build(1)
        b1 = housing_free_hints.build(2)
        self.assertEqual(a1, b1)
        self.assertEqual((self._state(1), self._state(2)), (2, 1))

    def test_the_order_is_the_one_asked_for(self):
        names = []
        for _ in range(len(ORDER)):
            self._missed(5, 1, days_ago=len(names) + 1)   # щоб «упущені» теж були
            text = housing_free_hints.build(5)
            names.append(text)
        keys = ['⏱', '📝', '📁', '📋', '🌙', '🔍', '🔎', '⏳', '📦', '🎁']
        self.assertEqual([t.lstrip('\n')[0] for t in names], keys)

    def test_a_scan_with_several_flats_gives_different_hints(self):
        gate = self._gate()
        footers = [gate.footer(777, key) for key in ('k1', 'k2', 'k3')]
        self.assertEqual(len(set(self._hint_line(f) for f in footers)), 3)

    @staticmethod
    def _hint_line(footer):
        return [line for line in footer.split('\n') if line.strip()][0]


class MissedTests(_Db):
    def test_the_real_number_is_shown_when_it_is_above_zero(self):
        self._missed(777, 3)
        self._set_state(777, ORDER.index('missed'))
        text = housing_free_hints.build(777)
        self.assertIn('⏳ За 7 днів безкоштовна версія не встигла надіслати вам 3 квартири', text)
        self.assertEqual(self._state(777), ORDER.index('missed') + 1)

    def test_one_and_five_use_their_own_word_forms(self):
        for count, word in ((1, '1 квартиру'), (2, '2 квартири'), (5, '5 квартир'), (21, '21 квартиру'), (12, '12 квартир')):
            self._missed(700 + count, count)
            self.assertIn(word, housing_free_hints.render('missed', 700 + count, 'uk'))

    def test_zero_skips_the_hint_and_the_next_one_takes_its_place(self):
        self._set_state(777, ORDER.index('missed'))
        text = housing_free_hints.build(777)
        self.assertIn('📦', text)   # пакети
        self.assertNotIn('⏳', text)
        self.assertEqual(self._state(777), ORDER.index('packages') + 1)

    def test_only_the_last_seven_days_and_only_this_person_count(self):
        self._missed(777, 4, days_ago=8)
        self._missed(999, 2)
        self.assertIsNone(housing_free_hints.render('missed', 777, 'uk'))

    def test_the_same_flat_logged_twice_is_counted_once(self):
        session = self._session()
        for _ in range(2):
            session.add(housing_pay_store.HousingMissedLog(
                user_id=777, source='kleinanzeigen', listing_key='dup', created_at=datetime.utcnow(),
            ))
        session.commit()
        session.close()
        self.assertIn('1 квартиру', housing_free_hints.render('missed', 777, 'uk'))


class PackagesAndTextTests(_Db):
    def test_prices_come_from_the_packages_module(self):
        with mock.patch.dict(housing_free_hints.housing_packages.PRICES_EUR, {'m3': 13, 'until_found': 17}):
            text = housing_free_hints.render('packages', 1, 'uk')
        self.assertIn('13 €', text)
        self.assertIn('17 €', text)

    def test_every_hint_has_all_three_languages_and_no_leftover_placeholders(self):
        self._missed(1, 2)
        for lang in ('uk', 'ru', 'de'):
            for name in ORDER:
                text = housing_free_hints.render(name, 1, lang)
                self.assertIsNotNone(text, (name, lang))
                self.assertNotIn('[[', text)
                self.assertNotIn('{', text)
                self.assertEqual(text.count('\n'), 1)
                self.assertNotIn('!', text)

    def test_the_quiet_night_hint_shows_the_real_window(self):
        self.assertIn(housing_tier.quiet_hours_label(), housing_free_hints.render('quiet', 1, 'uk'))

    def test_without_a_bot_name_it_is_plain_text_that_still_names_the_price(self):
        with mock.patch.object(housing_letter_store, '_bot_username', None):
            text = housing_free_hints.build(1)
        self.assertNotIn('t.me', text)
        self.assertIn('від 5 €', text)

    def test_referral_hint_is_dropped_when_the_feature_is_off(self):
        with mock.patch.dict(os.environ, {'HOUSING_REFERRAL_ENABLED': '0'}):
            self.assertIsNone(housing_free_hints.render('referral', 1, 'uk'))


class FooterIntegrationTests(_Db):
    def test_free_footer_carries_the_hint_and_keeps_the_letter_and_social_lines(self):
        footer = self._gate().footer(777, 'k1')
        self.assertIn('~2 год', footer)
        self.assertIn('start=sub', footer)
        self.assertIn('✍️ Лист · 1 безкоштовно</a>', footer)
        self.assertNotIn('Безкоштовна версія', footer)

    def test_a_paid_person_gets_nothing(self):
        self._subscribe(888)
        footer = self._gate().footer(888, 'k1')
        self.assertNotIn('start=sub', footer)
        self.assertNotIn('Підписка від', footer)
        self.assertNotIn('Безкоштовна', footer)
        self.assertIsNone(self._state(888))   # і лічильник не рухається

    def test_any_error_in_the_hint_gives_the_old_line_and_the_flat_still_goes(self):
        with mock.patch.object(housing_free_hints, 'build', side_effect=RuntimeError('boom')):
            footer = self._gate().footer(777, 'k1')
        self.assertIn('Безкоштовна версія: квартира прийшла із затримкою ~2 год', footer)
        self.assertIn('✍️ Лист · 1 безкоштовно</a>', footer)

    def test_a_broken_counter_table_gives_the_old_line(self):
        with mock.patch.object(housing_free_hints, 'DBSession', mock.Mock(side_effect=RuntimeError('db'))):
            footer = self._gate().footer(777, 'k1')
        self.assertIn('Безкоштовна версія', footer)

    def test_the_switch_returns_the_old_line_and_does_not_touch_the_counter(self):
        with mock.patch.dict(os.environ, {'HOUSING_FREE_HINTS': '0'}):
            footer = self._gate().footer(777, 'k1')
        self.assertIn('Безкоштовна версія', footer)
        self.assertNotIn('start=sub', footer)
        self.assertIsNone(self._state(777))

    def test_immowelt_path_uses_the_same_footer(self):
        text = housing_tier.free_footer(777)
        self.assertIn('start=sub', text)
        self.assertEqual(self._state(777), 1)


class StartParamTests(_Db):
    def _update(self):
        user = SimpleNamespace(id=777)
        message = SimpleNamespace(from_user=user, reply_text=mock.Mock())
        return SimpleNamespace(
            message=message, effective_message=message, effective_user=user, callback_query=None,
            effective_chat=SimpleNamespace(type='private', id=777),
        )

    def test_start_sub_opens_the_package_choice(self):
        context = SimpleNamespace(args=['sub'], bot=mock.Mock())
        with mock.patch('user_handlers.housing_payment.enabled', return_value=True), \
                mock.patch('user_handlers.housing_payment.show_months') as show, \
                mock.patch('user_handlers.anonymous_posts.show_home') as home:
            chat_start.start(self._update(), context)
        show.assert_called_once()
        home.assert_not_called()

    def test_start_sub_without_payments_shows_home(self):
        context = SimpleNamespace(args=['sub'], bot=mock.Mock())
        with mock.patch('user_handlers.housing_payment.enabled', return_value=False), \
                mock.patch('user_handlers.housing_payment.show_months') as show, \
                mock.patch('user_handlers.anonymous_posts.show_home') as home:
            chat_start.start(self._update(), context)
        show.assert_not_called()
        home.assert_called_once()

    def test_a_failure_in_start_sub_still_shows_home(self):
        context = SimpleNamespace(args=['sub'], bot=mock.Mock())
        with mock.patch('user_handlers.housing_payment.enabled', return_value=True), \
                mock.patch('user_handlers.housing_payment.show_months', side_effect=RuntimeError('x')), \
                mock.patch('user_handlers.anonymous_posts.show_home') as home:
            chat_start.start(self._update(), context)
        home.assert_called_once()


if __name__ == '__main__':
    unittest.main()
