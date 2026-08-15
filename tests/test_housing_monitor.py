import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import mock

from user_handlers import anonymous_posts, housing_monitor


class FakeMessage:
    def __init__(self, text='', user_id=312029534):
        self.text = text
        self.from_user = SimpleNamespace(id=user_id)
        self.chat = SimpleNamespace(type='private')
        self.replies = []

    def reply_text(self, text, **kwargs):
        self.replies.append((text, kwargs))


class HousingAdminFlowTests(unittest.TestCase):
    def _update(self, text, user_id=312029534):
        message = FakeMessage(text=text, user_id=user_id)
        return SimpleNamespace(
            message=message,
            effective_message=message,
            effective_user=SimpleNamespace(id=user_id),
        )

    def _cb_update(self, user_id=312029534):
        query = SimpleNamespace(
            answer=mock.Mock(),
            edit_message_text=mock.Mock(),
            message=mock.Mock(),
        )
        return SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=user_id))

    def test_allowed_user_sees_housing_before_filters_are_created(self):
        with mock.patch.object(housing_monitor, 'ADMIN_ID', 312029534), \
             mock.patch.object(housing_monitor, 'ALLOWED_USER_IDS', {544675510}), \
             mock.patch.object(housing_monitor, 'user_filters', return_value=[]):
            self.assertTrue(housing_monitor.is_allowed(544675510))
            rows = list(housing_monitor.private_home_rows(544675510))

        self.assertEqual(rows[0][0].text, '🏠 Моніторинг житла')

    def test_allowed_user_menu_has_self_service_filter_controls(self):
        with mock.patch.object(housing_monitor, 'ADMIN_ID', 312029534), \
             mock.patch.object(housing_monitor, 'ALLOWED_USER_IDS', {544675510}):
            labels = [button.text for row in housing_monitor._menu_keyboard(544675510).inline_keyboard for button in row]

        self.assertIn('➕ Додати Immowelt', labels)
        self.assertIn('🏢 Додати ProPotsdam', labels)
        self.assertIn('⚙️ Мої фільтри', labels)
        self.assertNotIn('⚙️ Адмінка житла', labels)

    def test_database_allowed_user_gets_self_service_controls_without_env_access(self):
        with mock.patch.object(housing_monitor, 'ADMIN_ID', 312029534), \
             mock.patch.object(housing_monitor, 'ALLOWED_USER_IDS', set()), \
             mock.patch.object(housing_monitor.housing_access_store, 'is_allowed', return_value=True), \
             mock.patch.object(housing_monitor, 'user_filters', return_value=[]):
            self.assertTrue(housing_monitor.is_allowed(777))
            labels = [
                button.text
                for row in housing_monitor._menu_keyboard(777).inline_keyboard
                for button in row
            ]

        self.assertIn('➕ Додати Immowelt', labels)
        self.assertIn('🏢 Додати ProPotsdam', labels)
        self.assertIn('⚙️ Мої фільтри', labels)

    def test_admin_can_add_housing_access_user(self):
        with mock.patch.object(housing_monitor, 'ADMIN_ID', 312029534):
            labels = [
                button.text
                for row in housing_monitor._admin_keyboard().inline_keyboard
                for button in row
            ]
        self.assertIn('👤 Додати доступ користувачу', labels)
        self.assertIn('👥 Доступ до моніторингу', labels)

        context = SimpleNamespace(user_data={})
        update = self._update('', user_id=312029534)
        with mock.patch.object(housing_monitor, 'ADMIN_ID', 312029534):
            housing_monitor.start_access_add_flow(update, context)

        self.assertEqual(
            context.user_data['housing_access_admin'],
            {'step': 'user_id'},
        )

        update = self._update('777', user_id=312029534)
        with mock.patch.object(housing_monitor, 'ADMIN_ID', 312029534):
            self.assertTrue(housing_monitor.handle_private_text(update, context))
        self.assertEqual(context.user_data['housing_access_admin']['step'], 'name')

        update = self._update('Новий користувач', user_id=312029534)
        with mock.patch.object(housing_monitor, 'ADMIN_ID', 312029534), \
             mock.patch.object(housing_monitor.housing_access_store, 'grant_access') as grant:
            self.assertTrue(housing_monitor.handle_private_text(update, context))

        grant.assert_called_once_with(777, 'Новий користувач')
        self.assertNotIn('housing_access_admin', context.user_data)
        self.assertIn('Доступ до моніторингу житла надано', update.message.replies[-1][0])

    def test_allowed_user_add_flow_uses_own_telegram_id(self):
        context = SimpleNamespace(user_data={})
        update = self._update('', user_id=544675510)
        with mock.patch.object(housing_monitor, 'ADMIN_ID', 312029534), \
             mock.patch.object(housing_monitor, 'ALLOWED_USER_IDS', {544675510}):
            housing_monitor.start_self_add_flow(update, context)

        self.assertEqual(context.user_data['housing_admin'], {
            'mode': 'immowelt',
            'step': 'title',
            'user_id': 544675510,
        })

    def test_allowed_user_can_toggle_only_own_filter(self):
        own_filter = {
            'filter_id': 2,
            'user_id': 544675510,
            'title': 'Пошук Каті',
            'source': 'immowelt',
            'active': True,
        }
        foreign_filter = {
            'filter_id': 1,
            'user_id': 312029534,
            'title': 'Пошук Артема',
            'source': 'immowelt',
            'active': True,
        }
        query = SimpleNamespace(
            data='housing:toggle:immowelt:2:0',
            answer=mock.Mock(),
            edit_message_text=mock.Mock(),
        )
        update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=544675510))
        context = SimpleNamespace(user_data={})
        with mock.patch.object(housing_monitor, 'ADMIN_ID', 312029534), \
             mock.patch.object(housing_monitor, 'ALLOWED_USER_IDS', {544675510}), \
             mock.patch.object(housing_monitor, 'manageable_filters', return_value=[own_filter]), \
             mock.patch.object(housing_monitor, '_request', return_value={'ok': True}) as request:
            housing_monitor.handle_callback(update, context)

        request.assert_called_once_with(
            'PATCH',
            '/api/housing/filters/2/active',
            json={'active': False},
        )

        query.data = 'housing:toggle:immowelt:1:0'
        query.answer.reset_mock()
        with mock.patch.object(housing_monitor, 'ADMIN_ID', 312029534), \
             mock.patch.object(housing_monitor, 'ALLOWED_USER_IDS', {544675510}), \
             mock.patch.object(housing_monitor, 'manageable_filters', return_value=[foreign_filter]), \
             mock.patch.object(housing_monitor, '_request') as request:
            housing_monitor.handle_callback(update, context)

        request.assert_not_called()
        query.answer.assert_called_with('Цей фільтр вам не належить.', show_alert=True)

    def test_user_filters_do_not_duplicate_synced_propotsdam_tasks(self):
        immowelt = [
            {'filter_id': 2, 'user_id': 544675510, 'title': 'Immowelt', 'source': 'immowelt', 'active': True},
        ]
        propotsdam = [
            {'filter_id': 2, 'user_id': 544675510, 'title': 'ProPotsdam', 'districts': 'Drewitz'},
        ]
        with mock.patch.object(housing_monitor, '_all_immowelt_filters', return_value=immowelt), \
             mock.patch.object(housing_monitor.propotsdam_store, 'list_filters', return_value=propotsdam):
            filters = housing_monitor.user_filters(544675510)

        self.assertEqual([item['title'] for item in filters], ['Immowelt', 'ProPotsdam'])

    def test_admin_add_flow_collects_id_name_and_search_bounds(self):
        """Майстер збирає умови, а не посилання.

        Обхід Immowelt ходить своєю адресою й посилання фільтра не відкриває, а
        відбір іде за умовами в самому записі. Фільтр без умов збігається з
        будь-якою квартирою, тож людина з «до 800 €» отримувала весь Потсдам.
        """
        context = SimpleNamespace(user_data={})
        with mock.patch.object(housing_monitor, 'ADMIN_ID', 312029534), \
             mock.patch.object(housing_monitor, '_preview_criteria', return_value={}):
            housing_monitor.start_add_flow(self._update(housing_monitor.BTN_ADMIN_ADD), context)
            self.assertEqual(context.user_data['housing_admin']['step'], 'user_id')

            housing_monitor.handle_private_text(self._update('123456789'), context)
            self.assertEqual(context.user_data['housing_admin']['step'], 'title')

            housing_monitor.handle_private_text(self._update('Іван'), context)
            self.assertEqual(context.user_data['housing_admin']['step'], 'districts')

            state = context.user_data['housing_admin']
            state['districts_selected'] = ['Golm']
            housing_monitor._finish_immowelt_districts(self._cb_update(), context)
            self.assertEqual(state['step'], 'criteria')

            housing_monitor._toggle_immowelt_criteria(self._cb_update(), context, 'max_price_eur')
            housing_monitor._toggle_immowelt_criteria(self._cb_update(), context, 'min_rooms')
            housing_monitor._finish_immowelt_criteria(self._cb_update(), context)
            self.assertEqual(state['step'], 'max_price_eur')

            for text in ['800', '2']:
                self.assertTrue(housing_monitor.handle_private_text(self._update(text), context))

        state = context.user_data['housing_admin']
        self.assertEqual(state['step'], 'preview')
        # Питали лише те, що позначили галочкою — решта в стані просто
        # відсутня, її взагалі не питали.
        self.assertIsNone(state.get('min_price_eur'))
        self.assertEqual(state['max_price_eur'], 800)
        self.assertEqual(state['min_rooms'], 2)
        self.assertIsNone(state.get('max_rooms'))
        self.assertIsNone(state.get('min_area_m2'))
        self.assertIsNone(state.get('max_area_m2'))

    def test_criteria_checkbox_skips_unselected_fields_entirely(self):
        """Позначили лише площу — про ціну й кімнати майстер навіть не питає."""
        context = SimpleNamespace(user_data={'housing_admin': {
            'mode': 'immowelt', 'step': 'criteria', 'user_id': 123456789,
            'title': 'Іван', 'districts_selected': ['Golm'], 'criteria_selected': [],
        }})
        state = context.user_data['housing_admin']

        with mock.patch.object(housing_monitor, 'ADMIN_ID', 312029534), \
             mock.patch.object(housing_monitor, '_preview_criteria', return_value={}):
            housing_monitor._toggle_immowelt_criteria(self._cb_update(), context, 'min_area_m2')
            housing_monitor._toggle_immowelt_criteria(self._cb_update(), context, 'max_area_m2')
            housing_monitor._finish_immowelt_criteria(self._cb_update(), context)
            self.assertEqual(state['step'], 'min_area_m2')

            self.assertTrue(housing_monitor.handle_private_text(self._update('50'), context))
            self.assertEqual(state['step'], 'max_area_m2')
            self.assertTrue(housing_monitor.handle_private_text(self._update('90'), context))

        self.assertEqual(state['step'], 'preview')
        self.assertEqual(state['min_area_m2'], 50)
        self.assertEqual(state['max_area_m2'], 90)
        for untouched in ('min_price_eur', 'max_price_eur', 'min_rooms', 'max_rooms'):
            self.assertNotIn(untouched, state)

    def test_selecting_nothing_skips_straight_to_preview(self):
        context = SimpleNamespace(user_data={'housing_admin': {
            'mode': 'immowelt', 'step': 'criteria', 'user_id': 123456789,
            'title': 'Іван', 'districts_selected': ['Golm'], 'criteria_selected': [],
        }})
        state = context.user_data['housing_admin']

        with mock.patch.object(housing_monitor, '_preview_criteria', return_value={}):
            housing_monitor._finish_immowelt_criteria(self._cb_update(), context)

        self.assertEqual(state['step'], 'preview')

    def test_sibling_bound_violation_is_rejected_and_keeps_asking_the_same_field(self):
        context = SimpleNamespace(user_data={'housing_admin': {
            'mode': 'immowelt', 'step': 'criteria', 'user_id': 123456789,
            'title': 'Іван', 'districts_selected': ['Golm'], 'criteria_selected': [],
        }})
        state = context.user_data['housing_admin']

        with mock.patch.object(housing_monitor, 'ADMIN_ID', 312029534):
            housing_monitor._toggle_immowelt_criteria(self._cb_update(), context, 'min_price_eur')
            housing_monitor._toggle_immowelt_criteria(self._cb_update(), context, 'max_price_eur')
            housing_monitor._finish_immowelt_criteria(self._cb_update(), context)
            self.assertTrue(housing_monitor.handle_private_text(self._update('1500'), context))
            self.assertEqual(state['step'], 'max_price_eur')

            update = self._update('800')
            self.assertTrue(housing_monitor.handle_private_text(update, context))

        self.assertEqual(state['step'], 'max_price_eur')
        self.assertIn('Мінімум не може бути більшим за максимум', update.message.replies[-1][0])

    def test_saving_a_filter_sends_its_criteria(self):
        context = SimpleNamespace(user_data={'housing_admin': {
            'mode': 'immowelt', 'step': 'preview', 'user_id': 123456789,
            'title': 'Іван', 'districts_selected': ['Golm'],
            'min_price_eur': None, 'max_price_eur': 800,
            'min_rooms': 2, 'max_rooms': None,
            'min_area_m2': None, 'max_area_m2': None,
        }})
        update = SimpleNamespace(
            callback_query=mock.Mock(),
            effective_user=SimpleNamespace(id=312029534),
        )

        with mock.patch.object(housing_monitor, '_request', return_value={'ok': True, 'filter_id': 42}) as request:
            housing_monitor._save_immowelt_filter(update, context)

        request.assert_called_once_with(
            'POST',
            '/api/housing/filters',
            json={
                'user_id': 123456789,
                'title': 'Іван',
                'districts': ['Golm'],
                'min_price_eur': None, 'max_price_eur': 800,
                'min_rooms': 2, 'max_rooms': None,
                'min_area_m2': None, 'max_area_m2': None,
            },
        )
        self.assertNotIn('housing_admin', context.user_data)

    def test_saving_own_immowelt_filter_suggests_propotsdam_when_none_exists(self):
        context = SimpleNamespace(user_data={'housing_admin': {
            'mode': 'immowelt', 'step': 'preview', 'user_id': 544675510,
            'title': 'Катя', 'districts_selected': ['Golm'],
            'min_price_eur': None, 'max_price_eur': 800,
            'min_rooms': None, 'max_rooms': None,
            'min_area_m2': None, 'max_area_m2': None,
        }})
        update = SimpleNamespace(
            callback_query=mock.Mock(),
            effective_user=SimpleNamespace(id=544675510),
        )

        with mock.patch.object(housing_monitor, '_request', return_value={'ok': True, 'filter_id': 5}), \
             mock.patch.object(housing_monitor.propotsdam_store, 'list_filters', return_value=[]) as list_filters:
            housing_monitor._save_immowelt_filter(update, context)

        list_filters.assert_called_once_with(user_id=544675510, active_only=True)
        text, kwargs = update.callback_query.edit_message_text.call_args.args[0], update.callback_query.edit_message_text.call_args.kwargs
        self.assertIn('ще немає фільтра ProPotsdam', text)
        buttons = [btn for row in kwargs['reply_markup'].inline_keyboard for btn in row]
        self.assertTrue(any(btn.callback_data == 'housing:clone_propot' for btn in buttons))
        stashed = context.user_data['housing_clone_source']
        self.assertEqual(stashed['target'], 'propotsdam')
        self.assertEqual(stashed['districts'], ['Golm'])
        self.assertEqual(stashed['title'], 'Катя')

    def test_saving_own_immowelt_filter_skips_suggestion_if_propotsdam_already_exists(self):
        context = SimpleNamespace(user_data={'housing_admin': {
            'mode': 'immowelt', 'step': 'preview', 'user_id': 544675510,
            'title': 'Катя', 'districts_selected': ['Golm'],
            'min_price_eur': None, 'max_price_eur': 800,
            'min_rooms': None, 'max_rooms': None,
            'min_area_m2': None, 'max_area_m2': None,
        }})
        update = SimpleNamespace(
            callback_query=mock.Mock(),
            effective_user=SimpleNamespace(id=544675510),
        )

        with mock.patch.object(housing_monitor, '_request', return_value={'ok': True, 'filter_id': 5}), \
             mock.patch.object(housing_monitor.propotsdam_store, 'list_filters', return_value=[{'filter_id': 1}]):
            housing_monitor._save_immowelt_filter(update, context)

        text = update.callback_query.edit_message_text.call_args.args[0]
        kwargs = update.callback_query.edit_message_text.call_args.kwargs
        self.assertNotIn('ProPotsdam', text)
        buttons = [btn for row in kwargs['reply_markup'].inline_keyboard for btn in row]
        self.assertEqual(len(buttons), 1)

    def test_saving_someone_elses_immowelt_filter_never_touches_propotsdam_lookup(self):
        """Адміну, що додає фільтр іншій людині, чужа підказка не потрібна."""
        context = SimpleNamespace(user_data={'housing_admin': {
            'mode': 'immowelt', 'step': 'preview', 'user_id': 123456789,
            'title': 'Іван', 'districts_selected': ['Golm'],
            'min_price_eur': None, 'max_price_eur': 800,
            'min_rooms': None, 'max_rooms': None,
            'min_area_m2': None, 'max_area_m2': None,
        }})
        update = SimpleNamespace(
            callback_query=mock.Mock(),
            effective_user=SimpleNamespace(id=312029534),
        )

        with mock.patch.object(housing_monitor, '_request', return_value={'ok': True, 'filter_id': 5}), \
             mock.patch.object(housing_monitor.propotsdam_store, 'list_filters') as list_filters:
            housing_monitor._save_immowelt_filter(update, context)

        list_filters.assert_not_called()

    def test_finishing_own_propotsdam_filter_suggests_immowelt_when_none_exists(self):
        state = {
            'mode': 'propotsdam', 'step': 'max_total_rent_eur', 'user_id': 544675510,
            'title': 'Катя', 'districts': '', 'criteria_queue': ['max_total_rent_eur'],
            'min_rooms': None, 'max_rooms': None, 'min_area_m2': None, 'max_area_m2': None,
        }
        context = SimpleNamespace(user_data={'housing_admin': state})
        update = self._update('1500', user_id=544675510)

        with mock.patch('user_handlers.housing_monitor.propotsdam_store.create_filter', return_value=9), \
             mock.patch.object(housing_monitor, '_sync_propot_filters'), \
             mock.patch.object(housing_monitor, '_all_immowelt_filters', return_value=[]) as all_immo:
            housing_monitor._handle_propot_flow(update, context, state, '1500')

        all_immo.assert_called_once()
        text, kwargs = update.message.replies[-1]
        self.assertIn('ще немає фільтра Immowelt', text)
        buttons = [btn for row in kwargs['reply_markup'].inline_keyboard for btn in row]
        self.assertTrue(any(btn.callback_data == 'housing:clone_immo' for btn in buttons))
        self.assertEqual(context.user_data['housing_clone_source']['target'], 'immowelt')

    def test_finishing_propotsdam_filter_for_someone_else_never_touches_immowelt_lookup(self):
        state = {
            'mode': 'propotsdam', 'step': 'max_total_rent_eur', 'user_id': 123456789,
            'title': 'Іван', 'districts': '', 'criteria_queue': ['max_total_rent_eur'],
            'min_rooms': None, 'max_rooms': None, 'min_area_m2': None, 'max_area_m2': None,
        }
        context = SimpleNamespace(user_data={'housing_admin': state})
        update = self._update('1500', user_id=312029534)

        with mock.patch('user_handlers.housing_monitor.propotsdam_store.create_filter', return_value=9), \
             mock.patch.object(housing_monitor, '_sync_propot_filters'), \
             mock.patch.object(housing_monitor, '_all_immowelt_filters') as all_immo:
            housing_monitor._handle_propot_flow(update, context, state, '1500')

        all_immo.assert_not_called()
        self.assertNotIn('Immowelt', update.message.replies[-1][0])

    def test_clone_propot_from_immowelt_saves_immediately_with_transferred_criteria(self):
        """Кнопка «Створити такий самий» не веде майстром — зберігає одразу."""
        context = SimpleNamespace(user_data={'housing_clone_source': {
            'target': 'propotsdam', 'user_id': 544675510, 'title': 'Катя',
            'districts': ['Golm', 'Waldstadt 1'],
            'min_rooms': 2.0, 'max_rooms': None, 'min_area_m2': 50.0, 'max_area_m2': None,
        }})
        update = self._cb_update(544675510)

        with mock.patch('user_handlers.housing_monitor.propotsdam_store.create_filter', return_value=42) as create_filter, \
             mock.patch.object(housing_monitor, '_sync_propot_filters') as sync:
            housing_monitor._clone_propot_from_immowelt(update, context)

        create_filter.assert_called_once_with(
            user_id=544675510, title='Катя', districts='Golm,Waldstadt 1',
            min_rooms=2.0, max_rooms=None, min_area_m2=50.0, max_area_m2=None,
        )
        sync.assert_called_once()
        self.assertNotIn('housing_clone_source', context.user_data)
        text = update.callback_query.edit_message_text.call_args.args[0]
        self.assertIn('P42', text)
        self.assertIn('Оренду не переносив', text)

    def test_clone_immowelt_from_propot_saves_immediately_with_transferred_criteria(self):
        context = SimpleNamespace(user_data={'housing_clone_source': {
            'target': 'immowelt', 'user_id': 123456789, 'title': 'Іван',
            'districts': ['Golm', 'Waldstadt I'],
            'min_rooms': 2.0, 'max_rooms': None, 'min_area_m2': None, 'max_area_m2': 90.0,
        }})
        update = self._cb_update(123456789)

        with mock.patch.object(housing_monitor, '_request', return_value={'ok': True, 'filter_id': 7}) as request:
            housing_monitor._clone_immowelt_from_propot(update, context)

        request.assert_called_once_with('POST', '/api/housing/filters', json={
            'user_id': 123456789, 'title': 'Іван', 'districts': ['Golm', 'Waldstadt I'],
            'min_price_eur': None, 'max_price_eur': None,
            'min_rooms': 2.0, 'max_rooms': None, 'min_area_m2': None, 'max_area_m2': 90.0,
        })
        self.assertNotIn('housing_clone_source', context.user_data)
        text = update.callback_query.edit_message_text.call_args.args[0]
        self.assertIn('ID: 7', text)
        self.assertIn('Ціну не переносив', text)

    def test_waldstadt_district_name_translates_between_sources(self):
        self.assertEqual(
            housing_monitor._translate_districts(
                ['Waldstadt I', 'Golm'], housing_monitor.IMMOWELT_TO_PROPOT_DISTRICT, set(housing_monitor.PROPOT_DISTRICTS),
            ),
            ['Waldstadt 1', 'Golm'],
        )
        self.assertEqual(
            housing_monitor._translate_districts(
                ['Waldstadt 2', 'Babelsberg Nord'], housing_monitor.PROPOT_TO_IMMOWELT_DISTRICT, set(housing_monitor.IMMOWELT_DISTRICTS),
            ),
            ['Waldstadt II'],
        )

    def test_preview_reports_what_matches_right_now(self):
        """Перший обхід мовчки збирає базову лінію, тож людині потрібен доказ."""
        text = housing_monitor._preview_text(
            'Іван',
            {'districts': ['Golm'], 'max_price_eur': 800},
            {'catalog_size': 120, 'match_count': 3, 'matches': [
                {'url': 'https://www.immowelt.de/expose/abc', 'title': 'Wohnung',
                 'price_eur': 700, 'rooms': 2, 'area_m2': 55},
            ]},
        )

        self.assertIn('підходить 3 з 120', text)
        self.assertIn('immowelt.de/expose/abc', text)

    def test_preview_warns_when_nothing_matches(self):
        text = housing_monitor._preview_text(
            'Іван', {'districts': [], 'max_price_eur': 100},
            {'catalog_size': 120, 'match_count': 0, 'matches': []},
        )

        self.assertIn('не підходить жодна', text)

    def test_describe_criteria_shows_both_bounds_when_both_are_set(self):
        description = housing_monitor._describe_criteria({
            'districts': ['Golm'],
            'min_price_eur': 800, 'max_price_eur': 1200,
            'min_rooms': 2, 'max_rooms': 4,
            'min_area_m2': 50, 'max_area_m2': None,
        })

        self.assertIn('800–1200 €', description)
        self.assertIn('2–4 кімн.', description)
        self.assertIn('від 50 м²', description)

    def test_admin_panel_pages_a_long_filter_list(self):
        """Перелік друкувався цілком і впирався б у ліміт Telegram у 4096 знаків."""
        immowelt = [
            {'filter_id': index, 'user_id': 500 + index, 'title': f'Фільтр {index}', 'active': True}
            for index in range(1, 51)
        ]
        with mock.patch.object(housing_monitor, '_all_immowelt_filters', return_value=immowelt), \
             mock.patch.object(housing_monitor.propotsdam_store, 'list_filters', return_value=[]):
            first = housing_monitor._render_admin(0)
            second = housing_monitor._render_admin(1)
            beyond = housing_monitor._render_admin(99)

        self.assertLess(len(first), 4096)
        self.assertIn('сторінка 1 з 3', first)
        self.assertIn('#1 ', first)
        self.assertNotIn('#21 ', first)
        self.assertIn('#21 ', second)
        # Сторінка поза межами має впиратися в останню, а не падати.
        self.assertIn('сторінка 3 з 3', beyond)

    def test_anonymous_private_text_delegates_active_housing_admin_flow(self):
        context = SimpleNamespace(user_data={'housing_admin': {'step': 'user_id'}})
        update = self._update('123456789')

        with mock.patch.object(housing_monitor, 'ADMIN_ID', 312029534):
            anonymous_posts.handle_private_text(update, context)

        self.assertEqual(context.user_data['housing_admin']['user_id'], 123456789)
        self.assertEqual(context.user_data['housing_admin']['step'], 'title')

    def test_admin_add_propot_flow_collects_filter_bounds(self):
        context = SimpleNamespace(user_data={})
        with mock.patch.object(housing_monitor, 'ADMIN_ID', 312029534), \
             mock.patch('user_handlers.housing_monitor.propotsdam_store.create_filter', return_value=77) as create_filter:
            housing_monitor.start_propot_add_flow(self._update(housing_monitor.BTN_ADMIN_ADD_PROPOT), context)
            for text in ['123456789', 'Pro Potsdam Ivan']:
                update = self._update(text)
                self.assertTrue(housing_monitor.handle_private_text(update, context))
            self.assertEqual(context.user_data['housing_admin']['step'], 'districts')
            state = context.user_data['housing_admin']
            housing_monitor._finish_districts(self._cb_update(), context, all_districts=True)
            self.assertEqual(state['step'], 'criteria')

            housing_monitor._toggle_propot_criteria(self._cb_update(), context, 'min_rooms')
            housing_monitor._toggle_propot_criteria(self._cb_update(), context, 'max_rooms')
            housing_monitor._toggle_propot_criteria(self._cb_update(), context, 'min_area_m2')
            housing_monitor._toggle_propot_criteria(self._cb_update(), context, 'max_area_m2')
            housing_monitor._toggle_propot_criteria(self._cb_update(), context, 'min_total_rent_eur')
            housing_monitor._toggle_propot_criteria(self._cb_update(), context, 'max_total_rent_eur')
            housing_monitor._finish_propot_criteria(self._cb_update(), context)
            self.assertEqual(state['step'], 'min_rooms')

            for text in ['2', '3', '50', '80', '800', '1000']:
                update = self._update(text)
                self.assertTrue(housing_monitor.handle_private_text(update, context))

        create_filter.assert_called_once_with(
            user_id=123456789,
            title='Pro Potsdam Ivan',
            districts='',
            min_rooms=2.0,
            max_rooms=3.0,
            min_area_m2=50.0,
            max_area_m2=80.0,
            min_total_rent_eur=800.0,
            max_total_rent_eur=1000.0,
        )
        self.assertNotIn('housing_admin', context.user_data)
        self.assertIn('ProPotsdam', update.message.replies[-1][0])

    def test_propot_criteria_checkbox_skips_unselected_fields_entirely(self):
        """Позначили лише кімнати — про площу й оренду майстер навіть не питає."""
        context = SimpleNamespace(user_data={
            'housing_admin': {
                'mode': 'propotsdam', 'step': 'criteria', 'user_id': 123456789,
                'title': 'Ivan', 'districts': '', 'criteria_selected': [],
            }
        })
        state = context.user_data['housing_admin']

        with mock.patch.object(housing_monitor, 'ADMIN_ID', 312029534), \
             mock.patch('user_handlers.housing_monitor.propotsdam_store.create_filter', return_value=77) as create_filter:
            housing_monitor._toggle_propot_criteria(self._cb_update(), context, 'min_rooms')
            housing_monitor._finish_propot_criteria(self._cb_update(), context)
            self.assertEqual(state['step'], 'min_rooms')

            update = self._update('2')
            self.assertTrue(housing_monitor.handle_private_text(update, context))

        create_filter.assert_called_once_with(
            user_id=123456789,
            title='Ivan',
            districts='',
            min_rooms=2.0,
            max_rooms=None,
            min_area_m2=None,
            max_area_m2=None,
            min_total_rent_eur=None,
            max_total_rent_eur=None,
        )

    def test_propot_sibling_bound_violation_is_rejected(self):
        context = SimpleNamespace(user_data={
            'housing_admin': {
                'mode': 'propotsdam', 'step': 'criteria', 'user_id': 123456789,
                'title': 'Ivan', 'districts': '', 'criteria_selected': [],
            }
        })
        state = context.user_data['housing_admin']

        with mock.patch.object(housing_monitor, 'ADMIN_ID', 312029534):
            housing_monitor._toggle_propot_criteria(self._cb_update(), context, 'min_rooms')
            housing_monitor._toggle_propot_criteria(self._cb_update(), context, 'max_rooms')
            housing_monitor._finish_propot_criteria(self._cb_update(), context)
            self.assertTrue(housing_monitor.handle_private_text(self._update('5'), context))
            self.assertEqual(state['step'], 'max_rooms')

            update = self._update('2')
            self.assertTrue(housing_monitor.handle_private_text(update, context))

        self.assertEqual(state['step'], 'max_rooms')
        self.assertIn('Мінімум не може бути більшим за максимум', update.message.replies[-1][0])

    def test_propot_selecting_nothing_creates_the_filter_right_away(self):
        context = SimpleNamespace(user_data={
            'housing_admin': {
                'mode': 'propotsdam', 'step': 'criteria', 'user_id': 123456789,
                'title': 'Ivan', 'districts': '', 'criteria_selected': [],
            }
        })

        with mock.patch('user_handlers.housing_monitor.propotsdam_store.create_filter', return_value=77) as create_filter:
            housing_monitor._finish_propot_criteria(self._cb_update(), context)

        create_filter.assert_called_once_with(
            user_id=123456789, title='Ivan', districts='',
            min_rooms=None, max_rooms=None, min_area_m2=None, max_area_m2=None,
            min_total_rent_eur=None, max_total_rent_eur=None,
        )
        self.assertNotIn('housing_admin', context.user_data)

    def test_housing_status_shows_local_times_and_never_dp_document(self):
        immowelt_task = {
            'source': 'immowelt',
            'active': True,
            'last_checked_at': '2026-08-13T00:25:29.671153+00:00',
            'seen_count': 3,
        }
        propotsdam_status = {
            'last_checked_at': datetime(2026, 8, 13, 4, 50, 6),
            'last_status': 'ok',
            'listings_count': 4,
            'last_error': '',
        }

        now = datetime(2026, 8, 13, 2, 40, 0, tzinfo=housing_monitor.BERLIN_TZ)
        with mock.patch.object(housing_monitor, '_all_immowelt_filters', return_value=[immowelt_task]), \
             mock.patch.object(housing_monitor, '_receiver_status', return_value={}), \
             mock.patch.object(housing_monitor.propotsdam_store, 'latest_status', return_value=propotsdam_status), \
             mock.patch.object(housing_monitor, '_now_berlin', return_value=now):
            lines = housing_monitor._status_lines()

        rendered = '\n'.join(lines)
        self.assertIn('🟢 Immowelt: перевірка 14 хв тому', rendered)
        self.assertIn('ProPotsdam: перевірка', rendered)
        self.assertNotIn('DP Document', rendered)
        self.assertNotIn('2026-08-13T', rendered)

    def test_housing_status_marks_stale_checks(self):
        now = datetime(2026, 8, 13, 8, 0, 0, tzinfo=housing_monitor.BERLIN_TZ)
        immowelt_task = {
            'source': 'immowelt',
            'active': True,
            'last_checked_at': '2026-08-13T00:25:29.671153+00:00',
            'seen_count': 3,
        }
        propotsdam_status = {
            'last_checked_at': datetime(2026, 8, 13, 4, 50, 6),
            'last_status': 'ok',
            'listings_count': 4,
            'last_error': '',
        }

        with mock.patch.object(housing_monitor, '_all_immowelt_filters', return_value=[immowelt_task]), \
             mock.patch.object(housing_monitor, '_receiver_status', return_value={}), \
             mock.patch.object(housing_monitor.propotsdam_store, 'latest_status', return_value=propotsdam_status), \
             mock.patch.object(housing_monitor, '_now_berlin', return_value=now):
            rendered = '\n'.join(housing_monitor._status_lines())

        self.assertIn('🔴 Immowelt: перевірка 5 год тому', rendered)
        self.assertIn('🔴 ProPotsdam: перевірка 1 год тому', rendered)

    def test_immowelt_status_is_stale_after_30_minutes(self):
        now = datetime(2026, 8, 13, 8, 0, 0, tzinfo=housing_monitor.BERLIN_TZ)
        immowelt_task = {
            'source': 'immowelt',
            'active': True,
            'last_checked_at': '2026-08-13T05:29:00+00:00',
            'seen_count': 3,
        }

        with mock.patch.object(housing_monitor, '_all_immowelt_filters', return_value=[immowelt_task]), \
             mock.patch.object(housing_monitor, '_receiver_status', return_value={}), \
             mock.patch.object(housing_monitor.propotsdam_store, 'latest_status', return_value=None), \
             mock.patch.object(housing_monitor, '_now_berlin', return_value=now):
            rendered = '\n'.join(housing_monitor._status_lines())

        self.assertIn('🔴 Immowelt: перевірка 31 хв тому', rendered)

    def test_relative_time_reads_naturally(self):
        base = datetime(2026, 8, 13, 12, 0, 0, tzinfo=housing_monitor.BERLIN_TZ)
        with mock.patch.object(housing_monitor, '_now_berlin', return_value=base):
            self.assertEqual(housing_monitor._relative_time(base.isoformat()), 'щойно')
            self.assertEqual(
                housing_monitor._relative_time((base - timedelta(minutes=5)).isoformat()), '5 хв тому'
            )
            self.assertEqual(
                housing_monitor._relative_time((base - timedelta(hours=3)).isoformat()), '3 год тому'
            )
            self.assertEqual(
                housing_monitor._relative_time((base - timedelta(days=1, hours=1)).isoformat()),
                f'учора о {(base - timedelta(days=1, hours=1)).strftime("%H:%M")}',
            )
            self.assertEqual(housing_monitor._relative_time(None), 'ще не було')

    def test_traffic_light_has_three_states(self):
        base = datetime(2026, 8, 13, 12, 0, 0, tzinfo=housing_monitor.BERLIN_TZ)
        max_age = timedelta(minutes=30)
        with mock.patch.object(housing_monitor, '_now_berlin', return_value=base):
            fresh = (base - timedelta(minutes=5)).isoformat()
            aging = (base - timedelta(minutes=20)).isoformat()
            stale = (base - timedelta(minutes=40)).isoformat()
            self.assertEqual(housing_monitor._traffic_light(fresh, max_age), '🟢')
            self.assertEqual(housing_monitor._traffic_light(aging, max_age), '🟡')
            self.assertEqual(housing_monitor._traffic_light(stale, max_age), '🔴')
            self.assertEqual(housing_monitor._traffic_light(None, max_age), '🔴')

    def test_propot_district_callback_toggles_checkbox_selection(self):
        context = SimpleNamespace(user_data={'housing_admin': {'mode': 'propotsdam', 'step': 'districts', 'districts_selected': []}})
        query = SimpleNamespace(
            data='housing:propot_district:Babelsberg',
            answer=mock.Mock(),
            edit_message_text=mock.Mock(),
        )
        update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=312029534))

        with mock.patch.object(housing_monitor, 'ADMIN_ID', 312029534):
            housing_monitor.handle_callback(update, context)

        self.assertEqual(context.user_data['housing_admin']['districts_selected'], ['Babelsberg'])
        query.edit_message_text.assert_called_once()

    def test_toggle_routes_an_immowelt_filter_with_districts_to_the_right_table(self):
        """Регресія: Immowelt-фільтр з критеріями плутався з ProPotsdam.

        Обидва джерела тепер несуть ключ `districts`, і стара перевірка
        `"districts" in item` бачила його в Immowelt-записі й вирішувала, що
        це ProPotsdam — пауза йшла в чужу таблицю й тихо нічого не міняла.
        """
        own_filter = {
            'filter_id': 2, 'user_id': 544675510, 'title': 'Пошук Каті', 'source': 'immowelt',
            'active': True, 'districts': ('Golm',), 'max_price_eur': 800.0,
        }
        query = SimpleNamespace(
            data='housing:toggle:immowelt:2:0', answer=mock.Mock(), edit_message_text=mock.Mock(),
        )
        update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=544675510))
        context = SimpleNamespace(user_data={})

        with mock.patch.object(housing_monitor, 'ADMIN_ID', 312029534), \
             mock.patch.object(housing_monitor, 'ALLOWED_USER_IDS', {544675510}), \
             mock.patch.object(housing_monitor, 'manageable_filters', return_value=[own_filter]), \
             mock.patch.object(housing_monitor, '_request', return_value={'ok': True}) as request, \
             mock.patch.object(housing_monitor.propotsdam_store, 'set_filter_active') as set_propot_active:
            housing_monitor.handle_callback(update, context)

        request.assert_called_once_with('PATCH', '/api/housing/filters/2/active', json={'active': False})
        set_propot_active.assert_not_called()

    def test_edit_flow_prefills_the_wizard_with_current_criteria(self):
        """Раніше в «Мої фільтри» можна було лише поставити фільтр на паузу."""
        own_filter = {
            'filter_id': 2, 'user_id': 544675510, 'title': 'Пошук Каті', 'source': 'immowelt',
            'active': True, 'districts': ('Golm',), 'max_price_eur': 800.0,
            'min_rooms': 2.0, 'min_area_m2': None,
        }
        query = SimpleNamespace(
            data='housing:edit:immowelt:2', answer=mock.Mock(), edit_message_text=mock.Mock(),
        )
        update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=544675510))
        context = SimpleNamespace(user_data={})

        with mock.patch.object(housing_monitor, 'ADMIN_ID', 312029534), \
             mock.patch.object(housing_monitor, 'ALLOWED_USER_IDS', {544675510}), \
             mock.patch.object(housing_monitor, 'manageable_filters', return_value=[own_filter]):
            housing_monitor.handle_callback(update, context)

        state = context.user_data['housing_admin']
        self.assertEqual(state['edit_filter_id'], 2)
        self.assertEqual(state['title'], 'Пошук Каті')
        self.assertEqual(state['districts_selected'], ['Golm'])
        self.assertEqual(state['max_price_eur'], 800.0)
        self.assertEqual(state['step'], 'districts')
        # Раніше задані умови показуємо вже позначеними галочками.
        self.assertEqual(set(state['criteria_selected']), {'max_price_eur', 'min_rooms'})

    def test_edit_flow_rejects_someone_elses_filter(self):
        foreign_filter = {
            'filter_id': 1, 'user_id': 312029534, 'title': 'Пошук Артема', 'source': 'immowelt', 'active': True,
        }
        query = SimpleNamespace(
            data='housing:edit:immowelt:1', answer=mock.Mock(), edit_message_text=mock.Mock(),
        )
        update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=544675510))
        context = SimpleNamespace(user_data={})

        with mock.patch.object(housing_monitor, 'ADMIN_ID', 312029534), \
             mock.patch.object(housing_monitor, 'ALLOWED_USER_IDS', {544675510}), \
             mock.patch.object(housing_monitor, 'manageable_filters', return_value=[foreign_filter]):
            housing_monitor.handle_callback(update, context)

        query.answer.assert_called_with('Цей фільтр вам не належить.', show_alert=True)
        self.assertNotIn('housing_admin', context.user_data)

    def test_saving_an_edit_patches_the_existing_filter(self):
        context = SimpleNamespace(user_data={'housing_admin': {
            'mode': 'immowelt', 'step': 'preview', 'user_id': 544675510,
            'title': 'Пошук Каті', 'districts_selected': ['Golm'],
            'min_price_eur': None, 'max_price_eur': 900,
            'min_rooms': 2, 'max_rooms': None,
            'min_area_m2': None, 'max_area_m2': None,
            'edit_filter_id': 2,
        }})
        update = SimpleNamespace(
            callback_query=mock.Mock(), effective_user=SimpleNamespace(id=544675510),
        )

        with mock.patch.object(housing_monitor, '_request', return_value={'ok': True}) as request:
            housing_monitor._save_immowelt_filter(update, context)

        request.assert_called_once_with(
            'PATCH', '/api/housing/filters/2',
            json={
                'title': 'Пошук Каті', 'districts': ['Golm'],
                'min_price_eur': None, 'max_price_eur': 900,
                'min_rooms': 2, 'max_rooms': None,
                'min_area_m2': None, 'max_area_m2': None,
            },
        )
        self.assertNotIn('housing_admin', context.user_data)
        text = update.callback_query.edit_message_text.call_args.args[0]
        self.assertIn('Фільтр оновлено', text)

    def test_delete_asks_for_confirmation_first(self):
        own_filter = {'filter_id': 2, 'user_id': 544675510, 'title': 'Пошук Каті', 'source': 'immowelt', 'active': True}
        query = SimpleNamespace(
            data='housing:delete:immowelt:2', answer=mock.Mock(), edit_message_text=mock.Mock(),
        )
        update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=544675510))
        context = SimpleNamespace(user_data={})

        with mock.patch.object(housing_monitor, 'ADMIN_ID', 312029534), \
             mock.patch.object(housing_monitor, 'ALLOWED_USER_IDS', {544675510}), \
             mock.patch.object(housing_monitor, 'manageable_filters', return_value=[own_filter]), \
             mock.patch.object(housing_monitor, '_request') as request:
            housing_monitor.handle_callback(update, context)

        request.assert_not_called()
        keyboard_data = [
            button.callback_data
            for row in query.edit_message_text.call_args.kwargs['reply_markup'].inline_keyboard
            for button in row
        ]
        self.assertIn('housing:delete_confirm:immowelt:2', keyboard_data)

    def test_delete_confirm_removes_an_immowelt_filter(self):
        own_filter = {'filter_id': 2, 'user_id': 544675510, 'title': 'Пошук Каті', 'source': 'immowelt', 'active': True}
        query = SimpleNamespace(
            data='housing:delete_confirm:immowelt:2', answer=mock.Mock(), edit_message_text=mock.Mock(),
        )
        update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=544675510))
        context = SimpleNamespace(user_data={})

        with mock.patch.object(housing_monitor, 'ADMIN_ID', 312029534), \
             mock.patch.object(housing_monitor, 'ALLOWED_USER_IDS', {544675510}), \
             mock.patch.object(housing_monitor, 'manageable_filters', return_value=[own_filter]), \
             mock.patch.object(housing_monitor, '_request', return_value={'ok': True}) as request:
            housing_monitor.handle_callback(update, context)

        request.assert_called_once_with('DELETE', '/api/housing/filters/2')
        query.answer.assert_called_with('Фільтр видалено.')

    def test_delete_confirm_rejects_someone_elses_filter(self):
        foreign_filter = {'filter_id': 1, 'user_id': 312029534, 'title': 'Пошук Артема', 'source': 'immowelt', 'active': True}
        query = SimpleNamespace(
            data='housing:delete_confirm:immowelt:1', answer=mock.Mock(), edit_message_text=mock.Mock(),
        )
        update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=544675510))
        context = SimpleNamespace(user_data={})

        with mock.patch.object(housing_monitor, 'ADMIN_ID', 312029534), \
             mock.patch.object(housing_monitor, 'ALLOWED_USER_IDS', {544675510}), \
             mock.patch.object(housing_monitor, 'manageable_filters', return_value=[foreign_filter]), \
             mock.patch.object(housing_monitor, '_request') as request:
            housing_monitor.handle_callback(update, context)

        request.assert_not_called()
        query.answer.assert_called_with('Цей фільтр вам не належить.', show_alert=True)

    def test_delete_confirm_removes_a_propotsdam_filter(self):
        own_filter = {
            'filter_id': 5, 'user_id': 544675510, 'title': 'ProPotsdam', 'source': 'propotsdam',
            'districts': 'Drewitz', 'active': True,
        }
        query = SimpleNamespace(
            data='housing:delete_confirm:propotsdam:5', answer=mock.Mock(), edit_message_text=mock.Mock(),
        )
        update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=544675510))
        context = SimpleNamespace(user_data={})

        with mock.patch.object(housing_monitor, 'ADMIN_ID', 312029534), \
             mock.patch.object(housing_monitor, 'ALLOWED_USER_IDS', {544675510}), \
             mock.patch.object(housing_monitor, 'manageable_filters', return_value=[own_filter]), \
             mock.patch.object(housing_monitor.propotsdam_store, 'delete_filter', return_value=True) as delete_filter, \
             mock.patch.object(housing_monitor, '_sync_propot_filters') as sync_filters:
            housing_monitor.handle_callback(update, context)

        delete_filter.assert_called_once_with(5, user_id=544675510)
        sync_filters.assert_called_once()
        query.answer.assert_called_with('Фільтр видалено.')

    def test_self_manage_keyboard_offers_edit_only_for_immowelt(self):
        immowelt = {'filter_id': 1, 'user_id': 544675510, 'title': 'Immowelt', 'source': 'immowelt', 'active': True}
        propotsdam = {
            'filter_id': 2, 'user_id': 544675510, 'title': 'ProPotsdam', 'source': 'propotsdam',
            'districts': 'Drewitz', 'active': True,
        }

        with mock.patch.object(housing_monitor, 'manageable_filters', return_value=[immowelt, propotsdam]):
            keyboard = housing_monitor._self_manage_keyboard(544675510)

        callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]
        self.assertIn('housing:edit:immowelt:1', callbacks)
        self.assertIn('housing:delete:immowelt:1', callbacks)
        self.assertNotIn('housing:edit:propotsdam:2', callbacks)
        self.assertIn('housing:delete:propotsdam:2', callbacks)

    def test_immowelt_district_callback_toggles_checkbox_selection(self):
        context = SimpleNamespace(user_data={'housing_admin': {
            'mode': 'immowelt', 'step': 'districts', 'districts_selected': [],
        }})
        query = SimpleNamespace(
            data='housing:imm_district:Golm',
            answer=mock.Mock(),
            edit_message_text=mock.Mock(),
        )
        update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=312029534))

        housing_monitor.handle_callback(update, context)

        self.assertEqual(context.user_data['housing_admin']['districts_selected'], ['Golm'])


class HousingNotificationSettingsTests(unittest.TestCase):
    """Тиха ніч і денний дайджест — вибирає користувач, не адмін."""

    def _update(self, user_id=544675510, data='housing:notify_settings'):
        query = SimpleNamespace(data=data, answer=mock.Mock(), edit_message_text=mock.Mock())
        return SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=user_id))

    def test_settings_screen_reads_prefs_via_query_params(self):
        update = self._update()
        context = SimpleNamespace(user_data={})

        with mock.patch.object(housing_monitor, 'ADMIN_ID', 312029534), \
             mock.patch.object(housing_monitor, 'ALLOWED_USER_IDS', {544675510}), \
             mock.patch.object(
                 housing_monitor, '_request',
                 return_value={'ok': True, 'quiet_hours_enabled': True, 'digest_mode': 'daily'},
             ) as request:
            housing_monitor.handle_callback(update, context)

        request.assert_called_once_with(
            'GET', '/api/housing/notification-prefs', params={'user_id': 544675510}
        )
        text = update.callback_query.edit_message_text.call_args.args[0]
        self.assertIn('увімкнена', text)
        self.assertIn('раз на день', text)

    def test_toggle_quiet_hours_sends_the_new_value(self):
        update = self._update(data='housing:notify_quiet:1')
        context = SimpleNamespace(user_data={})

        with mock.patch.object(housing_monitor, 'ADMIN_ID', 312029534), \
             mock.patch.object(housing_monitor, 'ALLOWED_USER_IDS', {544675510}), \
             mock.patch.object(
                 housing_monitor, '_request',
                 return_value={'ok': True, 'quiet_hours_enabled': True, 'digest_mode': 'instant'},
             ) as request:
            housing_monitor.handle_callback(update, context)

        calls = [c for c in request.call_args_list if c.args[0] == 'POST']
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            calls[0].kwargs['json'],
            {'user_id': 544675510, 'quiet_hours_enabled': True},
        )

    def test_set_digest_mode_sends_daily(self):
        update = self._update(data='housing:notify_digest:daily')
        context = SimpleNamespace(user_data={})

        with mock.patch.object(housing_monitor, 'ADMIN_ID', 312029534), \
             mock.patch.object(housing_monitor, 'ALLOWED_USER_IDS', {544675510}), \
             mock.patch.object(
                 housing_monitor, '_request',
                 return_value={'ok': True, 'quiet_hours_enabled': False, 'digest_mode': 'daily'},
             ) as request:
            housing_monitor.handle_callback(update, context)

        calls = [c for c in request.call_args_list if c.args[0] == 'POST']
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            calls[0].kwargs['json'],
            {'user_id': 544675510, 'digest_mode': 'daily'},
        )

    def test_a_user_without_access_cannot_reach_settings(self):
        update = self._update(user_id=999)
        context = SimpleNamespace(user_data={})

        with mock.patch.object(housing_monitor, 'ADMIN_ID', 312029534), \
             mock.patch.object(housing_monitor, 'ALLOWED_USER_IDS', {544675510}), \
             mock.patch.object(housing_monitor, 'is_allowed', return_value=False), \
             mock.patch.object(housing_monitor, '_request') as request:
            housing_monitor.handle_callback(update, context)

        request.assert_not_called()

    def test_settings_button_is_offered_to_self_service_users(self):
        with mock.patch.object(housing_monitor, 'ADMIN_ID', 312029534), \
             mock.patch.object(housing_monitor, 'ALLOWED_USER_IDS', {544675510}):
            labels = [
                button.text
                for row in housing_monitor._menu_keyboard(544675510).inline_keyboard
                for button in row
            ]

        self.assertIn('🔔 Сповіщення', labels)


class HousingAccessRequestTests(unittest.TestCase):
    """Доступ видавався лише тим, що адмін вручну вбивав Telegram ID."""

    def _update(self, user_id=777, data='housing:access_request'):
        query = SimpleNamespace(data=data, answer=mock.Mock(), edit_message_text=mock.Mock())
        return SimpleNamespace(
            callback_query=query,
            effective_user=SimpleNamespace(
                id=user_id, first_name='Іван', last_name='', username='ivan',
            ),
        )

    def test_locked_menu_offers_a_way_to_ask_for_access(self):
        with mock.patch.object(housing_monitor, 'is_allowed', return_value=False):
            labels = [
                button.text
                for row in housing_monitor._locked_keyboard().inline_keyboard
                for button in row
            ]

        self.assertIn('📩 Запросити доступ', labels)

    def test_housing_button_is_visible_without_access(self):
        # Без кнопки людина без доступу не могла навіть попросити про нього.
        with mock.patch.object(housing_monitor, 'is_allowed', return_value=False):
            rows = list(housing_monitor.private_home_rows(777))

        self.assertEqual(rows[0][0].text, '🏠 Моніторинг житла')

    def test_request_reaches_the_admin_with_decision_buttons(self):
        context = SimpleNamespace(user_data={}, bot_data={}, bot=mock.Mock())
        update = self._update()

        with mock.patch.object(housing_monitor, 'ADMIN_ID', 312029534), \
             mock.patch.object(housing_monitor, 'is_allowed', return_value=False):
            housing_monitor.handle_callback(update, context)

        context.bot.send_message.assert_called_once()
        kwargs = context.bot.send_message.call_args.kwargs
        self.assertEqual(kwargs['chat_id'], 312029534)
        self.assertIn('777', kwargs['text'])
        payloads = [
            button.callback_data
            for row in kwargs['reply_markup'].inline_keyboard
            for button in row
        ]
        self.assertIn('housing:access_grant:777', payloads)
        self.assertIn('housing:access_deny:777', payloads)

    def test_second_request_is_not_forwarded_again(self):
        context = SimpleNamespace(
            user_data={'housing_access_requested': True}, bot_data={}, bot=mock.Mock()
        )

        with mock.patch.object(housing_monitor, 'ADMIN_ID', 312029534), \
             mock.patch.object(housing_monitor, 'is_allowed', return_value=False):
            housing_monitor.handle_callback(self._update(), context)

        context.bot.send_message.assert_not_called()

    def test_admin_approval_grants_access_and_tells_the_user(self):
        context = SimpleNamespace(
            user_data={}, bot_data={'housing_access_names': {777: 'Іван (@ivan)'}}, bot=mock.Mock()
        )
        update = self._update(user_id=312029534, data='housing:access_grant:777')

        with mock.patch.object(housing_monitor, 'ADMIN_ID', 312029534), \
             mock.patch.object(housing_monitor.housing_access_store, 'grant_access') as grant:
            housing_monitor.handle_callback(update, context)

        grant.assert_called_once_with(777, 'Іван (@ivan)')
        self.assertEqual(context.bot.send_message.call_args.kwargs['chat_id'], 777)

    def test_denial_does_not_grant_anything(self):
        context = SimpleNamespace(user_data={}, bot_data={}, bot=mock.Mock())
        update = self._update(user_id=312029534, data='housing:access_deny:777')

        with mock.patch.object(housing_monitor, 'ADMIN_ID', 312029534), \
             mock.patch.object(housing_monitor.housing_access_store, 'grant_access') as grant:
            housing_monitor.handle_callback(update, context)

        grant.assert_not_called()

    def test_only_the_admin_can_decide(self):
        context = SimpleNamespace(user_data={}, bot_data={}, bot=mock.Mock())
        update = self._update(user_id=999, data='housing:access_grant:777')

        with mock.patch.object(housing_monitor, 'ADMIN_ID', 312029534), \
             mock.patch.object(housing_monitor.housing_access_store, 'grant_access') as grant:
            housing_monitor.handle_callback(update, context)

        grant.assert_not_called()
        context.bot.send_message.assert_not_called()


if __name__ == '__main__':
    unittest.main()
