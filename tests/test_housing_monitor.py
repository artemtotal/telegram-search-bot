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
        tasks = [
            {'filter_id': 2, 'user_id': 544675510, 'title': 'Immowelt', 'source': 'immowelt'},
            {'filter_id': 2, 'user_id': 544675510, 'title': 'ProPotsdam synced', 'source': 'propotsdam'},
        ]
        propotsdam = [
            {'filter_id': 2, 'user_id': 544675510, 'title': 'ProPotsdam', 'districts': 'Drewitz'},
        ]
        with mock.patch.object(housing_monitor, '_tasks', return_value=tasks), \
             mock.patch.object(housing_monitor.propotsdam_store, 'list_filters', return_value=propotsdam):
            filters = housing_monitor.user_filters(544675510)

        self.assertEqual([item['title'] for item in filters], ['Immowelt', 'ProPotsdam'])

    def test_admin_add_flow_collects_id_name_and_immowelt_url(self):
        context = SimpleNamespace(user_data={})
        with mock.patch.object(housing_monitor, 'ADMIN_ID', 312029534), \
             mock.patch.object(housing_monitor, '_request', return_value={'ok': True, 'filter_id': 42}) as request:
            housing_monitor.start_add_flow(self._update(housing_monitor.BTN_ADMIN_ADD), context)
            self.assertEqual(context.user_data['housing_admin']['step'], 'user_id')

            housing_monitor.handle_private_text(self._update('123456789'), context)
            self.assertEqual(context.user_data['housing_admin']['step'], 'title')

            housing_monitor.handle_private_text(self._update('Іван'), context)
            self.assertEqual(context.user_data['housing_admin']['step'], 'url')

            final_update = self._update('https://www.immowelt.de/classified-search?foo=bar')
            self.assertTrue(housing_monitor.handle_private_text(final_update, context))

        request.assert_called_once_with(
            'POST',
            '/api/housing/filters',
            json={
                'user_id': 123456789,
                'title': 'Іван',
                'url': 'https://www.immowelt.de/classified-search?foo=bar',
            },
        )
        self.assertNotIn('housing_admin', context.user_data)
        self.assertIn('Фільтр житла додано', final_update.message.replies[-1][0])

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
        with mock.patch.object(housing_monitor, '_tasks', return_value=[immowelt_task]), \
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
            'last_checked_at': '2026-08-13T00:25:29.671153+00:00',
            'seen_count': 3,
        }
        propotsdam_status = {
            'last_checked_at': datetime(2026, 8, 13, 4, 50, 6),
            'last_status': 'ok',
            'listings_count': 4,
            'last_error': '',
        }

        with mock.patch.object(housing_monitor, '_tasks', return_value=[immowelt_task]), \
             mock.patch.object(housing_monitor.propotsdam_store, 'latest_status', return_value=propotsdam_status), \
             mock.patch.object(housing_monitor, '_now_berlin', return_value=now):
            rendered = '\n'.join(housing_monitor._status_lines())

        self.assertIn('Immowelt: перевірка прострочена', rendered)
        self.assertIn('ProPotsdam: перевірка прострочена', rendered)

    def test_immowelt_status_is_stale_after_30_minutes(self):
        now = datetime(2026, 8, 13, 8, 0, 0, tzinfo=housing_monitor.BERLIN_TZ)
        immowelt_task = {
            'source': 'immowelt',
            'last_checked_at': '2026-08-13T05:29:00+00:00',
            'seen_count': 3,
        }

        with mock.patch.object(housing_monitor, '_tasks', return_value=[immowelt_task]), \
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


if __name__ == '__main__':
    unittest.main()
