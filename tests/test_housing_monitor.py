import unittest
from datetime import datetime
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
            state['step'] = 'max_price_eur'

            for text in ['800', '2', '-']:
                self.assertTrue(housing_monitor.handle_private_text(self._update(text), context))

        state = context.user_data['housing_admin']
        self.assertEqual(state['step'], 'preview')
        self.assertEqual(state['max_price_eur'], 800)
        self.assertEqual(state['min_rooms'], 2)
        self.assertIsNone(state['min_area_m2'])

    def test_saving_a_filter_sends_its_criteria(self):
        context = SimpleNamespace(user_data={'housing_admin': {
            'mode': 'immowelt', 'step': 'preview', 'user_id': 123456789,
            'title': 'Іван', 'districts_selected': ['Golm'],
            'max_price_eur': 800, 'min_rooms': 2, 'min_area_m2': None,
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
                'max_price_eur': 800,
                'min_rooms': 2,
                'min_area_m2': None,
            },
        )
        self.assertNotIn('housing_admin', context.user_data)

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
            housing_monitor._finish_districts(
                SimpleNamespace(
                    callback_query=SimpleNamespace(
                        answer=mock.Mock(),
                        edit_message_text=mock.Mock(),
                    ),
                    effective_user=SimpleNamespace(id=312029534),
                ),
                context,
                all_districts=True,
            )
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
        self.assertIn('Immowelt: остання перевірка 13.08.2026 02:25', rendered)
        self.assertIn('ProPotsdam: остання перевірка 13.08.2026 06:50', rendered)
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

        self.assertIn('Immowelt: перевірка прострочена', rendered)
        self.assertIn('ProPotsdam: перевірка прострочена', rendered)

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

        self.assertIn('Immowelt: перевірка прострочена', rendered)

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
