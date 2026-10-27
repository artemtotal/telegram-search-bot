"""Картка фільтра житла (housing_card, 27.10.2026): одне повідомлення з
умовами й кнопками, зміни зберігаються одразу, записи площадок - на місці."""

import unittest
from types import SimpleNamespace
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database import Base
from user_handlers import housing_card, housing_monitor as hm
from user_jobs import coop_watchdog_store, housing_journey_store, housing_search_store, user_settings_store

USER = 544675510
ADMIN = 312029534


class FakeMessage:
    def __init__(self, text='', user_id=USER):
        self.text = text
        self.from_user = SimpleNamespace(id=user_id)
        self.chat = SimpleNamespace(type='private')
        self.replies = []

    def reply_text(self, text, **kwargs):
        self.replies.append((text, kwargs))


class FakeImmowelt:
    """Приймач check-Wohnung: фільтри Immowelt живуть там, а не в базі бота."""

    def __init__(self):
        self.filters = {}
        self.next_id = 100

    def request(self, method, path, timeout=None, json=None, **_):
        if method == 'POST' and path == '/api/housing/filters':
            self.next_id += 1
            self.filters[self.next_id] = {**json, 'filter_id': self.next_id, 'source': 'immowelt', 'active': True}
            return {'ok': True, 'filter_id': self.next_id}
        filter_id = int(path.split('/')[4])
        if method == 'PATCH' and path.endswith('/active'):
            self.filters[filter_id]['active'] = json['active']
        elif method == 'PATCH':
            self.filters[filter_id].update(json)
        elif method == 'DELETE':
            self.filters.pop(filter_id, None)
        return {'ok': True}


class HousingCardTest(unittest.TestCase):
    def setUp(self):
        engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
        Base.metadata.create_all(engine)
        session = sessionmaker(bind=engine)
        modules = [housing_search_store, coop_watchdog_store, user_settings_store, housing_journey_store, hm]
        modules += [store for store, _matching in hm._LOCAL_SOURCE_MODULES.values()]
        modules += [matching for _store, matching in hm._LOCAL_SOURCE_MODULES.values() if hasattr(matching, 'DBSession')]
        self.immowelt = FakeImmowelt()
        patches = [mock.patch.object(module, 'DBSession', session) for module in modules]
        patches += [
            mock.patch.object(housing_search_store, 'engine', engine),
            mock.patch.object(hm, 'ADMIN_ID', ADMIN),
            mock.patch.object(hm, 'ALLOWED_USER_IDS', {USER}),
            mock.patch.object(hm, '_request', side_effect=self.immowelt.request),
            mock.patch.object(hm, '_all_immowelt_filters', side_effect=lambda: list(self.immowelt.filters.values())),
            mock.patch.object(hm, '_sync_propot_filters'),
            mock.patch.object(hm, '_after_filter_saved'),
            mock.patch.object(hm, '_maybe_send_first_filter_congrats'),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.context = SimpleNamespace(user_data={}, bot_data={}, bot=mock.MagicMock())

    def _tap(self, data, user_id=USER):
        message = FakeMessage(user_id=user_id)
        query = SimpleNamespace(data=data, answer=mock.Mock(), edit_message_text=mock.Mock(), message=message,
                                from_user=SimpleNamespace(id=user_id))
        update = SimpleNamespace(callback_query=query, effective_message=message, effective_user=SimpleNamespace(id=user_id))
        if data.startswith('hcard:'):
            housing_card.handle_callback(update, self.context)
        else:
            hm.handle_callback(update, self.context)
        return query

    def _type(self, text, user_id=USER):
        message = FakeMessage(text=text, user_id=user_id)
        update = SimpleNamespace(message=message, effective_message=message, callback_query=None,
                                 effective_user=SimpleNamespace(id=user_id))
        handled = housing_card.handle_text(update, self.context)
        return handled, message

    def _search(self):
        [search] = hm.user_searches(USER)
        return search

    @staticmethod
    def _callbacks(query):
        markup = query.edit_message_text.call_args.kwargs['reply_markup']
        return [b.callback_data for row in markup.inline_keyboard for b in row]

    def _new(self, raw='3'):
        return self._tap(f'hcard:new:{raw}')

    # --- новий фільтр ---------------------------------------------------------------

    def test_new_search_covers_every_portal_and_starts_paused(self):
        query = self._new('3')
        search = self._search()
        self.assertEqual(sorted(hm._search_sources(search)), sorted(hm.AVAILABLE_SOURCE_KEYS))
        self.assertEqual(search['coops'], hm.COOP_KEYS)
        self.assertTrue(hm._search_paused(search))
        _area, price, warm = hm.jobcenter_limits(3)
        self.assertEqual(search['criteria']['max_price_eur'], price)
        self.assertEqual(search['criteria']['max_price_warm_eur'], warm)
        text = query.edit_message_text.call_args.args[0]
        self.assertIn('на паузі', text)
        self.assertIn('Почати пошук', text)
        self.assertEqual(self._callbacks(query)[0], 'hcard:on')

    def test_card_fits_a_phone_screen(self):
        query = self._new('no')
        markup = query.edit_message_text.call_args.kwargs['reply_markup']
        self.assertLessEqual(len(markup.inline_keyboard), 7)
        self.assertTrue(all(len(row) <= 2 for row in markup.inline_keyboard))
        self.assertLessEqual(query.edit_message_text.call_args.args[0].count('\n'), 14)

    def test_start_switches_every_portal_on_and_counts_as_the_first_filter(self):
        self._new('no')
        query = self._tap('hcard:on')
        search = self._search()
        self.assertFalse(hm._search_paused(search))
        self.assertTrue(all(item['active'] for item in search['items']))
        self.assertEqual(len(search['items']), len(hm.AVAILABLE_SOURCE_KEYS))  # ImmoScout24 теж
        hm._after_filter_saved.assert_called_once()
        self.assertNotIn('edited', hm._after_filter_saved.call_args.kwargs)
        self.assertIn('працює', query.edit_message_text.call_args.args[0])

    # --- поля ------------------------------------------------------------------------

    def test_price_button_updates_every_portal_in_place(self):
        self._new('no')
        before = sorted(self._search()['members'])
        self._tap('hcard:f:max_price_eur')
        self._tap('hcard:v:max_price_eur:800')
        search = self._search()
        self.assertEqual(sorted(search['members']), before)  # ті самі записи, не нові
        self.assertEqual(search['criteria']['max_price_eur'], 800)
        by_source = {item['source']: item for item in search['items']}
        for source in ('immowelt', 'schoba', 'kleinanzeigen', 'propotsdam', 'immoscout', 'karlmarx'):
            self.assertEqual(by_source[source]['max_price_eur'], 800, source)

    def test_warm_rent_reaches_propotsdam_as_gesamtmiete(self):
        self._new('no')
        self._tap('hcard:v:max_price_warm_eur:1100')
        by_source = {item['source']: item for item in self._search()['items']}
        self.assertEqual(by_source['propotsdam']['max_total_rent_eur'], 1100)
        self.assertEqual(by_source['vonovia']['max_price_warm_eur'], 1100)

    def test_rooms_min_over_max_is_refused(self):
        self._new('no')
        self._tap('hcard:v:max_rooms:2')
        query = self._tap('hcard:v:min_rooms:3')
        query.answer.assert_called_with(housing_card._t('min_over_max', 'uk'), show_alert=True)
        self.assertIsNone(self._search()['criteria'].get('min_rooms'))

    def test_own_number_typed_into_the_chat(self):
        self._new('no')
        self._tap('hcard:f:min_area_m2')
        handled, message = self._type('55,5')
        self.assertTrue(handled)
        self.assertEqual(self._search()['criteria']['min_area_m2'], 55.5)
        self.assertIn('від 55,50 м²', message.replies[-1][0])

    def test_menu_button_text_is_not_taken_for_a_number(self):
        self._new('no')
        self._tap('hcard:f:min_area_m2')
        handled, _message = self._type('🏘 Житло')
        self.assertFalse(handled)
        self.assertNotIn('input', self.context.user_data['hcard'])

    def test_jobcenter_limit_is_offered_on_the_price_picker(self):
        self._new('2')
        query = self._tap('hcard:f:max_price_eur')
        _area, price, _warm = hm.jobcenter_limits(2)
        self.assertEqual(self._callbacks(query)[0], f'hcard:v:max_price_eur:{price}')

    def test_no_limit_clears_the_price(self):
        self._new('3')
        self._tap('hcard:v:max_price_eur:-')
        self.assertIsNone(self._search()['criteria']['max_price_eur'])

    # --- райони й портали --------------------------------------------------------------

    def test_new_filter_shows_every_district_ticked(self):
        self._new('no')
        query = self._tap('hcard:dist')
        markup = query.edit_message_text.call_args.kwargs['reply_markup']
        district_buttons = [b.text for row in markup.inline_keyboard for b in row if b.callback_data.startswith('hcard:dt:')]
        self.assertTrue(district_buttons)
        self.assertTrue(all(label.startswith('✅') for label in district_buttons))
        # Зберегти все як є - лишається «усі райони».
        self._tap('hcard:dok')
        self.assertEqual(self._search()['criteria']['districts'], [])

    def test_no_district_at_all_cannot_be_saved(self):
        self._new('no')
        self._tap('hcard:dist')
        self._tap('hcard:dnone')
        query = self._tap('hcard:dok')
        query.answer.assert_called_with(housing_card._t('pick_a_district', 'uk'), show_alert=True)
        self.assertEqual(self._search()['criteria']['districts'], [])

    def test_districts_are_saved_for_each_portal_in_its_own_names(self):
        self._new('no')
        self._tap('hcard:dist')
        self._tap('hcard:dnone')
        names = hm._canonical_districts(hm.AVAILABLE_SOURCE_KEYS)
        for name in ('Babelsberg', 'Waldstadt II'):
            self._tap(f'hcard:dt:{names.index(name)}')
        self._tap('hcard:dok')
        by_source = {item['source']: item for item in self._search()['items']}
        self.assertEqual(self._search()['criteria']['districts'], ['Babelsberg', 'Waldstadt II'])
        self.assertIn('Waldstadt 2', by_source['propotsdam']['districts'])
        self.assertEqual(by_source['immowelt']['districts'], ['Babelsberg', 'Waldstadt II'])

    def test_portals_can_be_dropped_and_added_back(self):
        self._new('no')
        self._tap('hcard:src')
        self._tap('hcard:st:kleinanzeigen')
        self._tap('hcard:ct:gewoba')
        self._tap('hcard:sok')
        search = self._search()
        self.assertNotIn('kleinanzeigen', hm._search_sources(search))
        self.assertNotIn('gewoba', search['coops'])
        self._tap('hcard:src')
        self._tap('hcard:st:kleinanzeigen')
        self._tap('hcard:sok')
        self.assertIn('kleinanzeigen', hm._search_sources(self._search()))

    def test_at_least_one_portal_stays(self):
        self._new('no')
        self._tap('hcard:src')
        for key in hm.AVAILABLE_SOURCE_KEYS:
            self._tap(f'hcard:st:{key}')
        query = self._tap('hcard:sok')
        query.answer.assert_called_with(hm.i18n.t('housing.toast.pick_a_source', 'uk'), show_alert=True)
        self.assertEqual(len(self._search()['items']), len(hm.AVAILABLE_SOURCE_KEYS))

    # --- готово й чужі фільтри -------------------------------------------------------------

    def test_done_after_editing_a_running_search_sends_one_report(self):
        self._new('no')
        self._tap('hcard:on')
        hm._after_filter_saved.reset_mock()
        self._tap('hcard:v:min_rooms:2')
        self._tap('hcard:v:max_rooms:4')
        hm._after_filter_saved.assert_not_called()  # не на кожен тап
        with mock.patch.object(hm, 'show_menu'):
            self._tap('hcard:done')
        hm._after_filter_saved.assert_called_once()
        self.assertTrue(hm._after_filter_saved.call_args.kwargs['edited'])

    def test_someone_else_cannot_open_the_card(self):
        self._new('no')
        search_id = self._search()['search_id']
        with mock.patch.object(hm, 'ALLOWED_USER_IDS', {USER, 777}):
            self.context.user_data['hcard'] = {'sid': search_id}
            self._tap('hcard:v:min_rooms:2', user_id=777)
        self.assertIsNone(self._search()['criteria'].get('min_rooms'))

    def test_admin_edits_someone_elses_card(self):
        self._new('no')
        search_id = self._search()['search_id']
        self.context.user_data['hcard'] = {'sid': search_id}
        self._tap('hcard:v:min_rooms:2', user_id=ADMIN)
        self.assertEqual(self._search()['criteria']['min_rooms'], 2)


if __name__ == '__main__':
    unittest.main()
