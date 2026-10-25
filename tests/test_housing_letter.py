"""«✍️ Лист орендодавцю» and the free-tier «Квартири, що підходять зараз»
(25.10.2026)."""

import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database import Base, HousingAccessUser, KleinanzeigenListing
from user_handlers import housing_letter, housing_monitor
from user_jobs import housing_letter_store, housing_tier, user_settings_store


class _Db(unittest.TestCase):
    MODULES = (housing_letter_store, housing_letter, housing_tier, user_settings_store, housing_monitor)

    def setUp(self):
        self.engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self._saved = [(m, m.DBSession) for m in self.MODULES]
        for module in self.MODULES:
            module.DBSession = sessionmaker(bind=self.engine)
        self._username = mock.patch.object(housing_letter_store, '_bot_username', 'PotsdamTestBot')
        self._username.start()
        housing_letter._daily.clear()

    def tearDown(self):
        self._username.stop()
        for module, session in self._saved:
            module.DBSession = session
        self.engine.dispose()

    def _session(self):
        return sessionmaker(bind=self.engine)()

    def _subscribe(self, user_id=777):
        session = self._session()
        session.add(HousingAccessUser(
            user_id=user_id, display_name='x', active=True, is_trial=False,
            created_at=datetime.utcnow(), updated_at=datetime.utcnow(),
        ))
        session.commit()
        session.close()

    def _listing(self, key='k1'):
        session = self._session()
        session.add(KleinanzeigenListing(
            listing_key=key, title='Helle 3-Zimmer-Wohnung', address='Babelsberg', rooms=3, area_m2=75,
            price_eur=850, detail_url='https://www.kleinanzeigen.de/s-anzeige/k1',
            first_seen_at=datetime.utcnow() - timedelta(hours=3), last_seen_at=datetime.utcnow(), is_active=True,
        ))
        session.commit()
        session.close()


class _SyncThread:
    def __init__(self, target, **_):
        self.target = target

    def start(self):
        self.target()


class LetterLinkTests(_Db):
    def test_every_flat_carries_the_link_free_people_see_it_marked_as_subscription(self):
        gate = housing_tier.DeliveryGate('kleinanzeigen', KleinanzeigenListing)
        free = gate.footer(777, 'k1')
        self.assertIn('https://t.me/PotsdamTestBot?start=lt1', free)
        self.assertIn('⚡ підписка', free)
        self._subscribe(888)
        paid = housing_tier.DeliveryGate('kleinanzeigen', KleinanzeigenListing).footer(888, 'k1')
        self.assertIn('?start=lt1', paid)  # the same flat keeps the same reference
        self.assertNotIn('підписка', paid)

    def test_no_link_while_the_bot_username_is_unknown(self):
        with mock.patch.object(housing_letter_store, '_bot_username', None):
            self.assertEqual(housing_tier.DeliveryGate('kleinanzeigen', KleinanzeigenListing).footer(888, 'k1')
                             .count('t.me'), 0)


class LetterFlowTests(_Db):
    def _start(self, payload, user_id=777):
        bot = mock.Mock()
        bot.send_message.return_value = SimpleNamespace(message_id=5)
        context = SimpleNamespace(bot=bot, user_data={}, args=[payload])
        update = SimpleNamespace(effective_user=SimpleNamespace(id=user_id), effective_chat=SimpleNamespace(type='private'))
        housing_letter.handle_start(update, context, payload)
        return bot, context

    def _text(self, context, text, user_id=777):
        update = SimpleNamespace(message=SimpleNamespace(text=text), effective_user=SimpleNamespace(id=user_id))
        return housing_letter.handle_text(update, context)

    def _tap(self, context, data, user_id=777):
        query = SimpleNamespace(data=data, answer=mock.Mock())
        update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=user_id))
        housing_letter.handle_callback(update, context)
        return query

    def test_a_free_person_gets_what_it_is_and_the_subscription_button(self):
        self._listing()
        ref = housing_letter_store.ref_for('kleinanzeigen', 'k1')
        bot, _ = self._start(f'lt{ref}')
        [call] = bot.send_message.call_args_list
        self.assertIn('функція підписки', call.kwargs['text'])
        callbacks = [b.callback_data for row in call.kwargs['reply_markup'].inline_keyboard for b in row]
        self.assertIn('housing:access_request', callbacks)

    def test_first_letter_asks_the_profile_once_then_writes_the_letter(self):
        self._subscribe()
        self._listing()
        ref = housing_letter_store.ref_for('kleinanzeigen', 'k1')
        prompts = []
        with mock.patch.object(housing_letter.threading, 'Thread', _SyncThread), \
                mock.patch.object(housing_letter, '_call_ai', side_effect=lambda p: prompts.append(p) or 'Sehr geehrte Damen und Herren,\nich <bewerbe> mich.'):
            bot, context = self._start(f'lt{ref}')
            self.assertIn('1/6', bot.send_message.call_args.kwargs['text'])
            for answer in ('Ihor Test', '2 дорослих і 2 дітей', 'оренду оплачує Jobcenter', 'з 01.12'):
                self.assertTrue(self._text(context, answer))
            self._tap(context, 'housing:letter:skip')
            self._tap(context, 'housing:letter:skip')

            self.assertEqual(housing_letter_store.get_profile(777)['income'], 'оренду оплачує Jobcenter')
            self.assertNotIn(housing_letter.STATE_KEY, context.user_data)
            [prompt] = prompts
            self.assertIn('Babelsberg', prompt)
            self.assertIn('Ihor Test', prompt)
            self.assertIn('Jobcenter', prompt)
            self.assertIn('Erfinde NICHTS', prompt)
            letter = bot.send_message.call_args
            self.assertTrue(letter.kwargs['text'].startswith('<pre>'))
            self.assertIn('&lt;bewerbe&gt;', letter.kwargs['text'])
            buttons = [b for row in letter.kwargs['reply_markup'].inline_keyboard for b in row]
            self.assertEqual(buttons[0].callback_data, f'housing:letter:again:{ref}')
            self.assertEqual(buttons[-1].url, 'https://www.kleinanzeigen.de/s-anzeige/k1')

            # Друге посилання - вже без анкети; «Інший варіант» просить інший текст.
            prompts.clear()
            self._start(f'lt{ref}')
            self._tap(context, f'housing:letter:again:{ref}')
            self.assertEqual(len(prompts), 2)
            self.assertNotIn('deutlich anders', prompts[0])
            self.assertIn('deutlich anders', prompts[1])

    def test_a_failed_generation_says_so(self):
        self._subscribe()
        self._listing()
        housing_letter_store.save_profile(777, {'full_name': 'X'})
        ref = housing_letter_store.ref_for('kleinanzeigen', 'k1')
        with mock.patch.object(housing_letter.threading, 'Thread', _SyncThread), \
                mock.patch.object(housing_letter, '_call_ai', return_value=''):
            bot, _ = self._start(f'lt{ref}')
        self.assertIn('Не вдалося', bot.edit_message_text.call_args.kwargs['text'])

    def test_unknown_reference(self):
        self._subscribe()
        bot, _ = self._start('lt999')
        self.assertIn('Не знайшов', bot.send_message.call_args.kwargs['text'])

    def test_no_open_questionnaire_leaves_text_alone(self):
        self.assertFalse(self._text(SimpleNamespace(bot=mock.Mock(), user_data={}), 'привіт'))


class FreeCurrentMatchesTests(_Db):
    def _show(self, user_id=777):
        bot = mock.Mock()
        query = SimpleNamespace(answer=mock.Mock())
        update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=user_id))
        filters = [{'source': 'kleinanzeigen', 'filter_id': 1, 'title': 'A'},
                   {'source': 'immoscout', 'filter_id': 2, 'title': 'B'}]
        matches = {'kleinanzeigen': [{'listing_key': 'a'}, {'listing_key': 'b'}], 'immoscout': [{'listing_key': 'c'}]}
        with mock.patch.object(housing_monitor, 'is_allowed', return_value=True), \
                mock.patch.object(housing_monitor, 'user_filters', return_value=filters), \
                mock.patch.object(housing_monitor, '_current_matches', side_effect=lambda s, f: matches[s]):
            housing_monitor.show_current_matches(update, SimpleNamespace(bot=bot))
        return bot

    def test_free_people_see_how_many_but_not_which(self):
        bot = self._show()
        [call] = bot.send_message.call_args_list
        self.assertIn('3 квартир', call.kwargs['text'])
        self.assertNotIn("'a'", call.kwargs['text'])
        callbacks = [b.callback_data for row in call.kwargs['reply_markup'].inline_keyboard for b in row]
        self.assertEqual(callbacks, ['housing:access_request', 'housing:menu'])

    def test_subscribers_get_the_list(self):
        self._subscribe()
        bot = self._show()
        self.assertGreater(len(bot.send_message.call_args_list), 1)
        self.assertIn('3', bot.send_message.call_args_list[0].kwargs['text'])


class UserFiltersIncludeImmoscoutTests(unittest.TestCase):
    def test_immoscout_filters_are_returned(self):
        empty = mock.Mock(list_filters=mock.Mock(return_value=[]))
        with mock.patch.object(housing_monitor, '_all_immowelt_filters', return_value=[]), \
                mock.patch.multiple(housing_monitor, propotsdam_store=empty, semmelhaack_store=empty,
                                    schoba_store=empty, regiomakler_store=empty, kleinanzeigen_store=empty,
                                    locals_store=empty, karlmarx_store=empty, vonovia_store=empty,
                                    coop_watchdog_store=empty), \
                mock.patch.object(housing_monitor.immoscout_store, 'list_filters',
                                  return_value=[{'filter_id': 5, 'user_id': 777}]):
            self.assertEqual([f.get('source') for f in housing_monitor.user_filters(777)], ['immoscout'])


if __name__ == '__main__':
    unittest.main()
