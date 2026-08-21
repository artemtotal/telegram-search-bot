import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database import Base
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

        self.assertIn('➕ Додати фільтр', labels)
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

        self.assertIn('➕ Додати фільтр', labels)
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

        context = SimpleNamespace(user_data={}, bot_data={}, bot=mock.Mock())
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

        # The name step no longer grants immediately - it asks for a duration
        # first (see HousingAccessExpiryTests for the months picker itself).
        grant.assert_not_called()
        self.assertNotIn('housing_access_admin', context.user_data)
        months_callbacks = [
            b.callback_data
            for row in update.message.replies[-1][1]['reply_markup'].inline_keyboard
            for b in row
        ]
        self.assertIn('housing:access_months:777:1', months_callbacks)

        # Picking a duration is what actually grants access and tells the user.
        query = SimpleNamespace(
            data='housing:access_months:777:1', answer=mock.Mock(), edit_message_text=mock.Mock(),
        )
        finalize_update = SimpleNamespace(
            callback_query=query, effective_user=SimpleNamespace(id=312029534),
        )
        with mock.patch.object(housing_monitor, 'ADMIN_ID', 312029534), \
             mock.patch.object(housing_monitor.housing_access_store, 'grant_access') as grant:
            housing_monitor.handle_callback(finalize_update, context)

        grant.assert_called_once()
        self.assertEqual(grant.call_args.args[:2], (777, 'Новий користувач'))
        # Раніше адмін вручну додавав доступ, а сама людина про це не дізнавалась —
        # бачила відкритий пункт меню лише випадково.
        context.bot.send_message.assert_called_once()
        self.assertEqual(context.bot.send_message.call_args.kwargs['chat_id'], 777)
        self.assertIn('відкрито', context.bot.send_message.call_args.kwargs['text'])

    def test_access_list_shows_a_delete_button_per_user_and_can_revoke_access(self):
        """Раніше цей екран був лише списком без жодної дії над записом —
        прибрати чийсь доступ можна було тільки вручну в базі."""
        with mock.patch.object(housing_monitor, 'ADMIN_ID', 312029534), \
             mock.patch.object(
                 housing_monitor.housing_access_store, 'list_users',
                 return_value=[{'user_id': 777, 'display_name': 'Хтось', 'active': True}],
             ):
            buttons = [
                button
                for row in housing_monitor._access_users_keyboard().inline_keyboard
                for button in row
            ]
        delete_button = next(b for b in buttons if b.callback_data == 'housing:access_delete:777')
        self.assertIn('777', delete_button.text)

        query = SimpleNamespace(
            data='housing:access_delete:777', answer=mock.Mock(), edit_message_text=mock.Mock(),
        )
        update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=312029534))
        context = SimpleNamespace(user_data={}, bot=mock.Mock())
        with mock.patch.object(housing_monitor, 'ADMIN_ID', 312029534):
            housing_monitor.handle_callback(update, context)
        confirm_callbacks = [
            b.callback_data
            for row in query.edit_message_text.call_args.kwargs['reply_markup'].inline_keyboard
            for b in row
        ]
        self.assertIn('housing:access_delete_confirm:777', confirm_callbacks)

        query = SimpleNamespace(
            data='housing:access_delete_confirm:777', answer=mock.Mock(), edit_message_text=mock.Mock(),
        )
        update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=312029534))
        with mock.patch.object(housing_monitor, 'ADMIN_ID', 312029534), \
             mock.patch.object(housing_monitor.housing_access_store, 'revoke_access', return_value=True) as revoke, \
             mock.patch.object(housing_monitor, '_delete_all_filters_for_user', return_value=3) as delete_filters:
            housing_monitor.handle_callback(update, context)
        revoke.assert_called_once_with(777)
        delete_filters.assert_called_once_with(777)
        self.assertIn('3', query.answer.call_args.args[0])
        # Раніше видалення доступу проходило мовчки для самої людини —
        # сповіщення бачив тільки адмін.
        context.bot.send_message.assert_called_once()
        self.assertEqual(context.bot.send_message.call_args.kwargs['chat_id'], 777)
        self.assertIn('закінчився', context.bot.send_message.call_args.kwargs['text'])

    def test_deleting_a_user_not_in_the_access_list_does_not_send_a_notification(self):
        """Адмін міг натиснути видалення на записі, якого вже немає — тоді
        нічого й не змінилось, і людину турбувати не варто."""
        query = SimpleNamespace(
            data='housing:access_delete_confirm:777', answer=mock.Mock(), edit_message_text=mock.Mock(),
        )
        update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=312029534))
        context = SimpleNamespace(user_data={}, bot=mock.Mock())
        with mock.patch.object(housing_monitor, 'ADMIN_ID', 312029534), \
             mock.patch.object(housing_monitor.housing_access_store, 'revoke_access', return_value=False), \
             mock.patch.object(housing_monitor, '_delete_all_filters_for_user', return_value=0):
            housing_monitor.handle_callback(update, context)

        context.bot.send_message.assert_not_called()

    def test_deleting_access_removes_the_users_filters_on_every_source(self):
        """Розсилка не звіряється зі списком доступу — саме лише видалення
        доступу нічого не змінило б, старі фільтри й далі отримували б новини."""
        immowelt = [
            {'filter_id': 5, 'user_id': 777, 'title': 'Immowelt'},
            {'filter_id': 6, 'user_id': 999, 'title': 'Not this one'},
        ]
        with mock.patch.object(housing_monitor, '_all_immowelt_filters', return_value=immowelt), \
             mock.patch.object(housing_monitor, '_request', return_value={'ok': True}) as request, \
             mock.patch.object(housing_monitor, '_sync_propot_filters') as sync_propot, \
             mock.patch.object(
                 housing_monitor.propotsdam_store, 'list_filters',
                 return_value=[{'filter_id': 1}],
             ), \
             mock.patch.object(housing_monitor.propotsdam_store, 'delete_filter', return_value=True), \
             mock.patch.object(
                 housing_monitor.schoba_store, 'list_filters',
                 return_value=[{'filter_id': 2}],
             ), \
             mock.patch.object(housing_monitor.schoba_store, 'delete_filter', return_value=True), \
             mock.patch.object(housing_monitor.semmelhaack_store, 'list_filters', return_value=[]), \
             mock.patch.object(housing_monitor.regiomakler_store, 'list_filters', return_value=[]), \
             mock.patch.object(housing_monitor.kleinanzeigen_store, 'list_filters', return_value=[]), \
             mock.patch.object(housing_monitor.locals_store, 'list_filters', return_value=[]), \
             mock.patch.object(housing_monitor.karlmarx_store, 'list_filters', return_value=[]):
            removed = housing_monitor._delete_all_filters_for_user(777)

        request.assert_called_once_with('DELETE', '/api/housing/filters/5')
        sync_propot.assert_called_once()
        self.assertEqual(removed, 3)

    def test_allowed_user_add_flow_uses_own_telegram_id(self):
        context = SimpleNamespace(user_data={})
        update = self._update('', user_id=544675510)
        with mock.patch.object(housing_monitor, 'ADMIN_ID', 312029534), \
             mock.patch.object(housing_monitor, 'ALLOWED_USER_IDS', {544675510}):
            housing_monitor.start_self_add_flow(update, context)

        self.assertEqual(context.user_data['housing_admin'], {
            'step': 'sources',
            'user_id': 544675510,
            'sources_selected': [],
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
        """Усі джерела мокнуті явно — цей user_id використовується і живими
        людьми в проді, тож реальні фільтри інших джерел інакше просочувались
        би в підрахунок і ламали тест без жодної зміни коду."""
        immowelt = [
            {'filter_id': 2, 'user_id': 544675510, 'title': 'Immowelt', 'source': 'immowelt', 'active': True},
        ]
        propotsdam = [
            {'filter_id': 2, 'user_id': 544675510, 'title': 'ProPotsdam', 'districts': 'Drewitz'},
        ]
        with mock.patch.object(housing_monitor, '_all_immowelt_filters', return_value=immowelt), \
             mock.patch.object(housing_monitor.propotsdam_store, 'list_filters', return_value=propotsdam), \
             mock.patch.object(housing_monitor.semmelhaack_store, 'list_filters', return_value=[]), \
             mock.patch.object(housing_monitor.schoba_store, 'list_filters', return_value=[]), \
             mock.patch.object(housing_monitor.regiomakler_store, 'list_filters', return_value=[]), \
             mock.patch.object(housing_monitor.kleinanzeigen_store, 'list_filters', return_value=[]), \
             mock.patch.object(housing_monitor.locals_store, 'list_filters', return_value=[]), \
             mock.patch.object(housing_monitor.karlmarx_store, 'list_filters', return_value=[]), \
             mock.patch.object(housing_monitor.coop_watchdog_store, 'list_filters', return_value=[]):
            filters = housing_monitor.user_filters(544675510)

        self.assertEqual([item['title'] for item in filters], ['Immowelt', 'ProPotsdam'])

    def test_admin_add_flow_asks_target_user_then_reuses_the_shared_source_wizard(self):
        """Раніше додавання йшло двома різними майстрами — окремо для Immowelt і
        окремо для ProPotsdam, з двома різними кнопками в адмінці. Тепер один
        вхід «➕ Додати користувача» питає лише «для кого», а далі веде той
        самий майстер вибору порталів, що й самообслуговування для себе."""
        context = SimpleNamespace(user_data={})
        with mock.patch.object(housing_monitor, 'ADMIN_ID', 312029534):
            housing_monitor.start_admin_add_flow(self._update(housing_monitor.BTN_ADMIN_ADD), context)
            self.assertEqual(context.user_data['housing_admin']['step'], 'admin_target_user_id')

            housing_monitor.handle_private_text(self._update('987654321'), context)
            state = context.user_data['housing_admin']
            self.assertEqual(state['step'], 'sources')
            self.assertEqual(state['user_id'], 987654321)

            housing_monitor._toggle_source(self._cb_update(), context, 'schoba')
            housing_monitor._finish_sources(self._cb_update(), context)
            # SCHOBA не знає районів — крок вибору району тут пропускається.
            self.assertEqual(state['step'], 'min_rooms')

            with mock.patch('user_handlers.housing_monitor.schoba_store.create_filter', return_value=42) as create_filter:
                for text in ['2', '-', '-', '-', '-', '-']:
                    self.assertTrue(housing_monitor.handle_private_text(self._update(text), context))

        self.assertNotIn('housing_admin', context.user_data)
        create_filter.assert_called_once_with(
            user_id=987654321, title=mock.ANY,
            min_rooms=2.0, max_rooms=None, min_area_m2=None, max_area_m2=None,
            min_price_eur=None, max_price_eur=None,
        )

    def test_admin_add_flow_rejects_a_non_numeric_target_id(self):
        context = SimpleNamespace(user_data={})
        with mock.patch.object(housing_monitor, 'ADMIN_ID', 312029534):
            housing_monitor.start_admin_add_flow(self._update(housing_monitor.BTN_ADMIN_ADD), context)
            self.assertTrue(housing_monitor.handle_private_text(self._update('not a number'), context))

        state = context.user_data['housing_admin']
        self.assertEqual(state['step'], 'admin_target_user_id')
        self.assertNotIn('user_id', state)

    def test_dash_skips_a_field_and_moves_to_the_next_one(self):
        context = SimpleNamespace(user_data={'housing_admin': {
            'mode': 'immowelt', 'step': 'min_price_eur', 'user_id': 123456789,
            'title': 'Іван', 'districts_selected': ['Golm'],
        }})
        state = context.user_data['housing_admin']

        with mock.patch.object(housing_monitor, 'ADMIN_ID', 312029534):
            self.assertTrue(housing_monitor.handle_private_text(self._update('-'), context))

        self.assertIsNone(state['min_price_eur'])
        self.assertEqual(state['step'], 'max_price_eur')

    def test_all_six_dashes_still_reach_preview(self):
        context = SimpleNamespace(user_data={'housing_admin': {
            'mode': 'immowelt', 'step': 'min_price_eur', 'user_id': 123456789,
            'title': 'Іван', 'districts_selected': ['Golm'],
        }})
        state = context.user_data['housing_admin']

        with mock.patch.object(housing_monitor, 'ADMIN_ID', 312029534), \
             mock.patch.object(housing_monitor, '_preview_criteria', return_value={}):
            for _ in range(6):
                self.assertTrue(housing_monitor.handle_private_text(self._update('-'), context))

        self.assertEqual(state['step'], 'preview')
        for key in ('min_price_eur', 'max_price_eur', 'min_rooms', 'max_rooms', 'min_area_m2', 'max_area_m2'):
            self.assertIsNone(state[key])

    def test_invalid_text_reprompts_the_same_field(self):
        context = SimpleNamespace(user_data={'housing_admin': {
            'mode': 'immowelt', 'step': 'min_rooms', 'user_id': 123456789,
            'title': 'Іван', 'districts_selected': ['Golm'],
        }})
        update = self._update('дуже дорого')

        with mock.patch.object(housing_monitor, 'ADMIN_ID', 312029534):
            self.assertTrue(housing_monitor.handle_private_text(update, context))

        self.assertEqual(context.user_data['housing_admin']['step'], 'min_rooms')
        self.assertIn('Незрозуміле значення', update.message.replies[-1][0])

    def test_sibling_bound_violation_is_rejected_and_keeps_asking_the_same_field(self):
        context = SimpleNamespace(user_data={'housing_admin': {
            'mode': 'immowelt', 'step': 'min_price_eur', 'user_id': 123456789,
            'title': 'Іван', 'districts_selected': ['Golm'],
        }})
        state = context.user_data['housing_admin']

        with mock.patch.object(housing_monitor, 'ADMIN_ID', 312029534):
            self.assertTrue(housing_monitor.handle_private_text(self._update('1500'), context))
            self.assertEqual(state['step'], 'max_price_eur')

            update = self._update('800')
            self.assertTrue(housing_monitor.handle_private_text(update, context))

        self.assertEqual(state['step'], 'max_price_eur')
        self.assertIn('Мінімум не може бути більшим за максимум', update.message.replies[-1][0])

    def test_preview_has_a_back_button_not_only_cancel(self):
        """Раніше на етапі перевірки можна було лише скасувати весь фільтр."""
        keyboard = housing_monitor._preview_keyboard()
        callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]

        self.assertIn('housing:imm_back', callbacks)
        self.assertIn('housing:imm_cancel', callbacks)

    def test_back_from_preview_returns_to_districts_and_keeps_the_title(self):
        context = SimpleNamespace(user_data={'housing_admin': {
            'mode': 'immowelt', 'step': 'preview', 'user_id': 123456789,
            'title': 'Іван', 'districts_selected': ['Golm'],
            'min_price_eur': 800, 'max_price_eur': None,
            'min_rooms': None, 'max_rooms': None, 'min_area_m2': None, 'max_area_m2': None,
        }})
        state = context.user_data['housing_admin']
        update = self._cb_update()

        housing_monitor._back_from_immowelt_preview(update, context)

        self.assertEqual(state['step'], 'districts')
        self.assertEqual(state['title'], 'Іван')
        self.assertEqual(state['districts_selected'], ['Golm'])
        update.callback_query.edit_message_text.assert_called_once()

    def test_cancel_returns_to_the_menu_instead_of_a_dead_end(self):
        """Раніше «Скасовано.» не мало жодної кнопки — єдиний вихід був почати спочатку."""
        context = SimpleNamespace(user_data={'housing_admin': {'mode': 'immowelt', 'step': 'min_price_eur'}})
        query = SimpleNamespace(data='housing:imm_cancel', answer=mock.Mock(), edit_message_text=mock.Mock())
        update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=312029534))

        housing_monitor.handle_callback(update, context)

        self.assertNotIn('housing_admin', context.user_data)
        kwargs = query.edit_message_text.call_args.kwargs
        callbacks = [button.callback_data for row in kwargs['reply_markup'].inline_keyboard for button in row]
        self.assertIn('housing:menu', callbacks)

    def test_propot_cancel_also_returns_to_the_menu(self):
        context = SimpleNamespace(user_data={'housing_admin': {'mode': 'propotsdam', 'step': 'min_rooms'}})
        query = SimpleNamespace(data='housing:propot_cancel', answer=mock.Mock(), edit_message_text=mock.Mock())
        update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=312029534))

        housing_monitor.handle_callback(update, context)

        self.assertNotIn('housing_admin', context.user_data)
        kwargs = query.edit_message_text.call_args.kwargs
        callbacks = [button.callback_data for row in kwargs['reply_markup'].inline_keyboard for button in row]
        self.assertIn('housing:menu', callbacks)

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

    def test_clone_propot_from_immowelt_asks_price_then_saves(self):
        """Район/кімнати/площу перенесено без питань, оренду питає окремо — двома числами.

        ProPotsdam рахує повну оренду (Gesamtmiete), Immowelt — холодну
        (Kaltmiete): перенесене число означало б інакшу умову, тож замість
        мовчазного пропуску клон питає його наново.
        """
        context = SimpleNamespace(user_data={'housing_clone_source': {
            'target': 'propotsdam', 'user_id': 544675510, 'title': 'Катя',
            'districts': ['Golm', 'Waldstadt 1'],
            'min_rooms': 2.0, 'max_rooms': None, 'min_area_m2': 50.0, 'max_area_m2': None,
        }})
        update = self._cb_update(544675510)

        with mock.patch.object(housing_monitor, 'ALLOWED_USER_IDS', {544675510}):
            housing_monitor._clone_propot_from_immowelt(update, context)

        state = context.user_data['housing_admin']
        self.assertEqual(state['mode'], 'propotsdam')
        self.assertEqual(state['step'], 'clone_price_min')
        self.assertEqual(state['districts'], 'Golm,Waldstadt 1')
        self.assertEqual(state['min_rooms'], 2.0)
        prompt = update.callback_query.edit_message_text.call_args.args[0]
        self.assertIn('Gesamtmiete', prompt)

        with mock.patch.object(housing_monitor, 'ADMIN_ID', 312029534), \
             mock.patch.object(housing_monitor, 'ALLOWED_USER_IDS', {544675510}), \
             mock.patch('user_handlers.housing_monitor.propotsdam_store.create_filter', return_value=42) as create_filter, \
             mock.patch.object(housing_monitor, '_sync_propot_filters') as sync:
            self.assertTrue(housing_monitor.handle_private_text(self._update('800', user_id=544675510), context))
            self.assertEqual(state['step'], 'clone_price_max')
            self.assertTrue(housing_monitor.handle_private_text(self._update('1200', user_id=544675510), context))

        create_filter.assert_called_once_with(
            user_id=544675510, title=mock.ANY, districts='Golm,Waldstadt 1',
            min_rooms=2.0, max_rooms=None, min_area_m2=50.0, max_area_m2=None,
            min_total_rent_eur=800.0, max_total_rent_eur=1200.0,
        )
        sync.assert_called_once()
        self.assertNotIn('housing_admin', context.user_data)

    def test_clone_immowelt_from_propot_asks_price_then_shows_preview(self):
        context = SimpleNamespace(user_data={'housing_clone_source': {
            'target': 'immowelt', 'user_id': 123456789, 'title': 'Іван',
            'districts': ['Golm', 'Waldstadt I'],
            'min_rooms': 2.0, 'max_rooms': None, 'min_area_m2': None, 'max_area_m2': 90.0,
        }})
        update = self._cb_update(123456789)

        with mock.patch.object(housing_monitor, 'ALLOWED_USER_IDS', {123456789}):
            housing_monitor._clone_immowelt_from_propot(update, context)

        state = context.user_data['housing_admin']
        self.assertEqual(state['mode'], 'immowelt')
        self.assertEqual(state['step'], 'clone_price_min')
        self.assertEqual(state['districts_selected'], ['Golm', 'Waldstadt I'])
        prompt = update.callback_query.edit_message_text.call_args.args[0]
        self.assertIn('Kaltmiete', prompt)

        with mock.patch.object(housing_monitor, 'ADMIN_ID', 123456789), \
             mock.patch.object(housing_monitor, '_preview_criteria', return_value={}):
            self.assertTrue(housing_monitor.handle_private_text(self._update('700', user_id=123456789), context))
            self.assertEqual(state['step'], 'clone_price_max')
            self.assertTrue(housing_monitor.handle_private_text(self._update('-', user_id=123456789), context))

        self.assertEqual(state['step'], 'preview')
        self.assertEqual(state['min_price_eur'], 700.0)
        self.assertIsNone(state['max_price_eur'])
        self.assertEqual(state['min_rooms'], 2.0)
        self.assertEqual(state['max_area_m2'], 90.0)

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
            {'districts': [], 'max_price_eur': 100},
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
        """Перелік друкувався цілком і впирався б у ліміт Telegram у 4096 знаків.

        Усі джерела мокнуті явно (не лише ProPotsdam) — інакше кількість
        реальних фільтрів живих користувачів бота міняється з часом і тихо
        зсуває підрахунок сторінок, ламаючи тест без жодної зміни коду.
        """
        immowelt = [
            {'filter_id': index, 'user_id': 500 + index, 'title': f'Фільтр {index}', 'active': True}
            for index in range(1, 51)
        ]
        with mock.patch.object(housing_monitor, '_all_immowelt_filters', return_value=immowelt), \
             mock.patch.object(housing_monitor.propotsdam_store, 'list_filters', return_value=[]), \
             mock.patch.object(housing_monitor.semmelhaack_store, 'list_filters', return_value=[]), \
             mock.patch.object(housing_monitor.schoba_store, 'list_filters', return_value=[]), \
             mock.patch.object(housing_monitor.regiomakler_store, 'list_filters', return_value=[]), \
             mock.patch.object(housing_monitor.kleinanzeigen_store, 'list_filters', return_value=[]), \
             mock.patch.object(housing_monitor.locals_store, 'list_filters', return_value=[]), \
             mock.patch.object(housing_monitor.karlmarx_store, 'list_filters', return_value=[]), \
             mock.patch.object(housing_monitor.coop_watchdog_store, 'list_filters', return_value=[]):
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
        context = SimpleNamespace(user_data={'housing_admin': {'step': 'admin_target_user_id'}})
        update = self._update('123456789')

        with mock.patch.object(housing_monitor, 'ADMIN_ID', 312029534):
            anonymous_posts.handle_private_text(update, context)

        self.assertEqual(context.user_data['housing_admin']['user_id'], 123456789)
        self.assertEqual(context.user_data['housing_admin']['step'], 'sources')

    def test_admin_add_flow_can_create_a_propotsdam_filter_for_the_target_user(self):
        """Той самий уніфікований вхід має покривати й district-aware джерела
        (ProPotsdam), не лише ті, що районів не знають."""
        context = SimpleNamespace(user_data={})
        with mock.patch.object(housing_monitor, 'ADMIN_ID', 312029534), \
             mock.patch.object(housing_monitor, '_request', return_value={'ok': True}), \
             mock.patch('user_handlers.housing_monitor.propotsdam_store.create_filter', return_value=77) as create_filter:
            housing_monitor.start_admin_add_flow(self._update(housing_monitor.BTN_ADMIN_ADD), context)
            housing_monitor.handle_private_text(self._update('123456789'), context)
            state = context.user_data['housing_admin']
            self.assertEqual(state['step'], 'sources')

            housing_monitor._toggle_source(self._cb_update(), context, 'propotsdam')
            housing_monitor._finish_sources(self._cb_update(), context)
            self.assertEqual(state['step'], 'districts')
            housing_monitor._finish_multi_districts(self._cb_update(), context, all_districts=True)
            self.assertEqual(state['step'], 'min_rooms')

            for text in ['2', '3', '50', '80', '800', '1000']:
                update = self._update(text)
                self.assertTrue(housing_monitor.handle_private_text(update, context))

        create_filter.assert_called_once_with(
            user_id=123456789,
            title=mock.ANY,
            districts='',
            min_rooms=2.0,
            max_rooms=3.0,
            min_area_m2=50.0,
            max_area_m2=80.0,
            min_total_rent_eur=800.0,
            max_total_rent_eur=1000.0,
        )
        self.assertIn('ProPotsdam', create_filter.call_args.kwargs['title'])
        self.assertNotIn('housing_admin', context.user_data)
        self.assertIn('ProPotsdam', update.message.replies[-1][0])

    def test_propot_dash_skips_a_field_and_still_creates_the_filter(self):
        context = SimpleNamespace(user_data={
            'housing_admin': {
                'mode': 'propotsdam', 'step': 'min_rooms', 'user_id': 123456789,
                'title': 'Ivan', 'districts': '',
            }
        })

        with mock.patch.object(housing_monitor, 'ADMIN_ID', 312029534), \
             mock.patch('user_handlers.housing_monitor.propotsdam_store.create_filter', return_value=77) as create_filter:
            for text in ['2', '-', '-', '-', '-', '-']:
                update = self._update(text)
                self.assertTrue(housing_monitor.handle_private_text(update, context))

        create_filter.assert_called_once_with(
            user_id=123456789,
            title=mock.ANY,
            districts='',
            min_rooms=2.0,
            max_rooms=None,
            min_area_m2=None,
            max_area_m2=None,
            min_total_rent_eur=None,
            max_total_rent_eur=None,
        )
        self.assertNotIn('housing_admin', context.user_data)

    def test_propot_sibling_bound_violation_is_rejected(self):
        context = SimpleNamespace(user_data={
            'housing_admin': {
                'mode': 'propotsdam', 'step': 'min_rooms', 'user_id': 123456789,
                'title': 'Ivan', 'districts': '',
            }
        })
        state = context.user_data['housing_admin']

        with mock.patch.object(housing_monitor, 'ADMIN_ID', 312029534):
            self.assertTrue(housing_monitor.handle_private_text(self._update('5'), context))
            self.assertEqual(state['step'], 'max_rooms')

            update = self._update('2')
            self.assertTrue(housing_monitor.handle_private_text(update, context))

        self.assertEqual(state['step'], 'max_rooms')
        self.assertIn('Мінімум не може бути більшим за максимум', update.message.replies[-1][0])

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
        self.assertEqual(state['districts_selected'], ['Golm'])
        self.assertEqual(state['max_price_eur'], 800.0)
        self.assertEqual(state['step'], 'districts')

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

    def test_self_manage_keyboard_offers_edit_for_both_sources(self):
        """Раніше ProPotsdam-фільтр можна було лише поставити на паузу чи видалити."""
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
        self.assertIn('housing:edit:propotsdam:2', callbacks)
        self.assertIn('housing:delete:propotsdam:2', callbacks)

    def test_self_manage_keyboard_groups_filters_under_source_headers(self):
        """Раніше однакові на вигляд кнопки в одному списку не казали, чий фільтр який."""
        immowelt = {
            'filter_id': 1, 'user_id': 544675510, 'title': 'Golm', 'source': 'immowelt',
            'active': True, 'districts': ('Golm',),
        }
        propotsdam = {
            'filter_id': 2, 'user_id': 544675510, 'title': 'Golm', 'source': 'propotsdam',
            'districts': 'Golm', 'active': True,
        }

        with mock.patch.object(housing_monitor, 'manageable_filters', return_value=[immowelt, propotsdam]):
            keyboard = housing_monitor._self_manage_keyboard(544675510)

        labels = [button.text for row in keyboard.inline_keyboard for button in row]
        self.assertTrue(any('Immowelt' in label and '──' in label for label in labels))
        self.assertTrue(any('ProPotsdam' in label and '──' in label for label in labels))
        # Заголовок іде РАНІШЕ фільтра, якого стосується.
        header_index = next(i for i, label in enumerate(labels) if 'Immowelt' in label and '──' in label)
        filter_index = next(i for i, label in enumerate(labels) if 'Golm' in label and '✅' in label)
        self.assertLess(header_index, filter_index)

    def test_self_manage_keyboard_omits_a_source_with_no_filters(self):
        immowelt = {'filter_id': 1, 'user_id': 544675510, 'title': 'Immowelt', 'source': 'immowelt', 'active': True}

        with mock.patch.object(housing_monitor, 'manageable_filters', return_value=[immowelt]):
            keyboard = housing_monitor._self_manage_keyboard(544675510)

        labels = [button.text for row in keyboard.inline_keyboard for button in row]
        self.assertTrue(any('Immowelt' in label and '──' in label for label in labels))
        self.assertFalse(any('ProPotsdam' in label and '──' in label for label in labels))

    def test_header_button_is_a_no_op(self):
        query = SimpleNamespace(data='housing:noop', answer=mock.Mock())
        update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=544675510))

        housing_monitor.handle_callback(update, SimpleNamespace(user_data={}))

        query.answer.assert_called_once()

    def test_edit_callback_routes_propotsdam_to_its_own_edit_flow(self):
        propotsdam = {
            'filter_id': 2, 'user_id': 544675510, 'title': 'Пошук Каті', 'source': 'propotsdam',
            'districts': 'Golm,Drewitz', 'active': True, 'min_rooms': 2.0,
        }
        query = SimpleNamespace(
            data='housing:edit:propotsdam:2', answer=mock.Mock(), edit_message_text=mock.Mock(),
        )
        update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=544675510))
        context = SimpleNamespace(user_data={})

        with mock.patch.object(housing_monitor, 'ALLOWED_USER_IDS', {544675510}), \
             mock.patch.object(housing_monitor, 'manageable_filters', return_value=[propotsdam]):
            housing_monitor.handle_callback(update, context)

        state = context.user_data['housing_admin']
        self.assertEqual(state['mode'], 'propotsdam')
        self.assertEqual(state['edit_filter_id'], 2)
        self.assertEqual(state['districts_selected'], ['Golm', 'Drewitz'])
        self.assertEqual(state['min_rooms'], 2.0)
        self.assertEqual(state['step'], 'districts')

    def test_saving_a_propotsdam_edit_updates_instead_of_creating(self):
        state = {
            'mode': 'propotsdam', 'step': 'min_rooms', 'user_id': 544675510,
            'title': 'Пошук Каті', 'districts': 'Golm', 'edit_filter_id': 2,
        }
        context = SimpleNamespace(user_data={'housing_admin': state})

        with mock.patch.object(housing_monitor, 'ALLOWED_USER_IDS', {544675510}), \
             mock.patch('user_handlers.housing_monitor.propotsdam_store.update_filter', return_value=True) as update_filter, \
             mock.patch('user_handlers.housing_monitor.propotsdam_store.create_filter') as create_filter, \
             mock.patch.object(housing_monitor, '_sync_propot_filters'):
            for text in ['3', '-', '-', '-', '-', '-']:
                update = self._update(text, user_id=544675510)
                self.assertTrue(housing_monitor.handle_private_text(update, context))

        update_filter.assert_called_once_with(
            filter_id=2, user_id=544675510, title=mock.ANY, districts='Golm',
            min_rooms=3.0, max_rooms=None, min_area_m2=None, max_area_m2=None,
            min_total_rent_eur=None, max_total_rent_eur=None,
        )
        create_filter.assert_not_called()
        self.assertIn('оновлено', update.message.replies[-1][0])
        self.assertNotIn('housing_admin', context.user_data)

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


class HousingMultiSourceWizardTests(unittest.TestCase):
    """Один вхід «Додати фільтр» веде по всіх обраних порталах одразу.

    Раніше Immowelt і ProPotsdam заводились двома окремими кнопками й майстрами,
    хоча користувач хотів «1 фільтр для всіх сервісів» — тепер спершу питає,
    де шукати (галочками, портали з часом поповнюватимуться), а вже потім веде
    спільними районом/кімнатами/площею і окремою оренду під кожен обраний портал.
    """

    def _update(self, text, user_id=544675510):
        message = FakeMessage(text=text, user_id=user_id)
        return SimpleNamespace(message=message, effective_message=message, effective_user=SimpleNamespace(id=user_id))

    def _cb_update(self, user_id=544675510):
        query = SimpleNamespace(answer=mock.Mock(), edit_message_text=mock.Mock())
        return SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=user_id))

    def test_toggling_a_source_updates_selection(self):
        context = SimpleNamespace(user_data={'housing_admin': {
            'step': 'sources', 'user_id': 544675510, 'sources_selected': [],
        }})
        query = SimpleNamespace(data='housing:src:immowelt', answer=mock.Mock(), edit_message_text=mock.Mock())
        update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=544675510))

        housing_monitor.handle_callback(update, context)

        self.assertEqual(context.user_data['housing_admin']['sources_selected'], ['immowelt'])

    def test_finishing_with_no_source_selected_shows_an_alert(self):
        context = SimpleNamespace(user_data={'housing_admin': {
            'step': 'sources', 'user_id': 544675510, 'sources_selected': [],
        }})
        query = SimpleNamespace(data='housing:src_done', answer=mock.Mock(), edit_message_text=mock.Mock())
        update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=544675510))

        housing_monitor.handle_callback(update, context)

        query.answer.assert_called_once_with('Оберіть хоча б один портал.', show_alert=True)
        self.assertEqual(context.user_data['housing_admin']['step'], 'sources')

    def test_finishing_sources_with_only_propotsdam_shows_its_own_district_list(self):
        context = SimpleNamespace(user_data={'housing_admin': {
            'step': 'sources', 'user_id': 544675510, 'sources_selected': ['propotsdam'],
        }})
        query = SimpleNamespace(data='housing:src_done', answer=mock.Mock(), edit_message_text=mock.Mock())
        update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=544675510))

        housing_monitor.handle_callback(update, context)

        state = context.user_data['housing_admin']
        self.assertEqual(state['mode'], 'multi')
        self.assertEqual(state['step'], 'districts')
        keyboard = query.edit_message_text.call_args.kwargs['reply_markup']
        labels = [button.text for row in keyboard.inline_keyboard for button in row]
        # ProPotsdam-специфічний район, якого нема в словнику Immowelt.
        self.assertTrue(any('Babelsberg Nord' in label for label in labels))

    def test_multi_cancel_returns_to_the_menu(self):
        context = SimpleNamespace(user_data={'housing_admin': {
            'mode': 'multi', 'step': 'districts', 'sources_selected': ['immowelt'],
        }})
        query = SimpleNamespace(data='housing:multi_cancel', answer=mock.Mock(), edit_message_text=mock.Mock())
        update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=544675510))

        housing_monitor.handle_callback(update, context)

        self.assertNotIn('housing_admin', context.user_data)
        callbacks = [b.callback_data for row in query.edit_message_text.call_args.kwargs['reply_markup'].inline_keyboard for b in row]
        self.assertIn('housing:menu', callbacks)

    def test_both_sources_selected_creates_two_filters_with_separate_rent_answers(self):
        context = SimpleNamespace(user_data={'housing_admin': {
            'mode': 'multi', 'step': 'min_rooms', 'user_id': 544675510,
            'sources_selected': ['immowelt', 'propotsdam'],
            'districts_selected': ['Waldstadt I', 'Golm'],
        }})
        state = context.user_data['housing_admin']

        with mock.patch.object(housing_monitor, 'ADMIN_ID', 312029534), \
             mock.patch.object(housing_monitor, 'ALLOWED_USER_IDS', {544675510}), \
             mock.patch.object(housing_monitor, '_request', return_value={'ok': True, 'filter_id': 11}) as request, \
             mock.patch('user_handlers.housing_monitor.propotsdam_store.create_filter', return_value=22) as create_filter, \
             mock.patch.object(housing_monitor, '_sync_propot_filters') as sync:
            for text in ['2', '-', '-', '-', '800', '1200', '900', '1400']:
                self.assertTrue(housing_monitor.handle_private_text(self._update(text), context))

        self.assertNotIn('housing_admin', context.user_data)
        request.assert_called_once_with(
            'POST', '/api/housing/filters',
            json={
                'user_id': 544675510, 'title': mock.ANY,
                'districts': ['Waldstadt I', 'Golm'],
                'min_price_eur': 800.0, 'max_price_eur': 1200.0,
                'min_rooms': 2.0, 'max_rooms': None,
                'min_area_m2': None, 'max_area_m2': None,
            },
        )
        create_filter.assert_called_once_with(
            user_id=544675510, title=mock.ANY,
            districts='Waldstadt 1,Golm',
            min_rooms=2.0, max_rooms=None, min_area_m2=None, max_area_m2=None,
            min_total_rent_eur=900.0, max_total_rent_eur=1400.0,
        )
        sync.assert_called_once()

    def test_creating_two_sources_at_once_sends_the_first_filter_congrats_only_once(self):
        context = SimpleNamespace(user_data={'housing_admin': {
            'mode': 'multi', 'step': 'min_rooms', 'user_id': 544675510,
            'sources_selected': ['immowelt', 'propotsdam'],
            'districts_selected': ['Waldstadt I', 'Golm'],
        }}, bot_data={}, bot=mock.Mock())

        with mock.patch.object(housing_monitor, 'ADMIN_ID', 312029534), \
             mock.patch.object(housing_monitor, 'ALLOWED_USER_IDS', {544675510}), \
             mock.patch.object(housing_monitor, '_request', return_value={'ok': True, 'filter_id': 11}), \
             mock.patch('user_handlers.housing_monitor.propotsdam_store.create_filter', return_value=22), \
             mock.patch.object(housing_monitor, '_sync_propot_filters'):
            for text in ['2', '-', '-', '-', '800', '1200', '900', '1400']:
                self.assertTrue(housing_monitor.handle_private_text(self._update(text), context))

        congrats_calls = [
            call for call in context.bot.send_message.call_args_list
            if 'молодець' in call.kwargs.get('text', '')
        ]
        self.assertEqual(len(congrats_calls), 1)
        self.assertEqual(congrats_calls[0].kwargs['chat_id'], 544675510)

    def test_only_propotsdam_selected_skips_immowelt_entirely(self):
        context = SimpleNamespace(user_data={'housing_admin': {
            'mode': 'multi', 'step': 'min_rooms', 'user_id': 544675510,
            'sources_selected': ['propotsdam'],
            'districts_selected': ['Babelsberg Nord'],
        }})

        with mock.patch.object(housing_monitor, 'ADMIN_ID', 312029534), \
             mock.patch.object(housing_monitor, 'ALLOWED_USER_IDS', {544675510}), \
             mock.patch.object(housing_monitor, '_request') as request, \
             mock.patch('user_handlers.housing_monitor.propotsdam_store.create_filter', return_value=5) as create_filter, \
             mock.patch.object(housing_monitor, '_sync_propot_filters'):
            for text in ['-', '-', '-', '-', '700', '-']:
                self.assertTrue(housing_monitor.handle_private_text(self._update(text), context))

        request.assert_not_called()
        create_filter.assert_called_once_with(
            user_id=544675510, title=mock.ANY,
            districts='Babelsberg Nord',
            min_rooms=None, max_rooms=None, min_area_m2=None, max_area_m2=None,
            min_total_rent_eur=700.0, max_total_rent_eur=None,
        )
        self.assertNotIn('housing_admin', context.user_data)

    def test_semmelhaack_has_no_districts_so_the_district_step_is_skipped(self):
        context = SimpleNamespace(user_data={'housing_admin': {
            'step': 'sources', 'user_id': 544675510, 'sources_selected': ['semmelhaack'],
        }})
        query = SimpleNamespace(data='housing:src_done', answer=mock.Mock(), edit_message_text=mock.Mock())
        update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=544675510))

        housing_monitor.handle_callback(update, context)

        state = context.user_data['housing_admin']
        self.assertEqual(state['mode'], 'multi')
        self.assertEqual(state['step'], 'min_rooms')
        prompt = query.edit_message_text.call_args.args[0]
        self.assertIn('кімнат', prompt)

    def test_immowelt_and_semmelhaack_together_ask_kaltmiete_only_once(self):
        """Обидва джерела рахують ту саму холодну оренду — не варто питати двічі поспіль."""
        context = SimpleNamespace(user_data={'housing_admin': {
            'mode': 'multi', 'step': 'min_rooms', 'user_id': 544675510,
            'sources_selected': ['immowelt', 'semmelhaack'],
            'districts_selected': ['Golm'],
        }})

        with mock.patch.object(housing_monitor, 'ADMIN_ID', 312029534), \
             mock.patch.object(housing_monitor, 'ALLOWED_USER_IDS', {544675510}), \
             mock.patch.object(housing_monitor, '_request', return_value={'ok': True, 'filter_id': 11}) as request, \
             mock.patch('user_handlers.housing_monitor.semmelhaack_store.create_filter', return_value=33) as create_filter:
            # 4 shared fields (rooms/area) + exactly 2 price answers, not 4 —
            # confirms the Kaltmiete question was not repeated for the second source.
            for text in ['2', '-', '-', '-', '800', '1200']:
                self.assertTrue(housing_monitor.handle_private_text(self._update(text), context))

        self.assertNotIn('housing_admin', context.user_data)
        request.assert_called_once_with(
            'POST', '/api/housing/filters',
            json={
                'user_id': 544675510, 'title': mock.ANY, 'districts': ['Golm'],
                'min_price_eur': 800.0, 'max_price_eur': 1200.0,
                'min_rooms': 2.0, 'max_rooms': None, 'min_area_m2': None, 'max_area_m2': None,
            },
        )
        create_filter.assert_called_once_with(
            user_id=544675510, title=mock.ANY,
            min_rooms=2.0, max_rooms=None, min_area_m2=None, max_area_m2=None,
            min_price_eur=800.0, max_price_eur=1200.0,
        )

    def test_semmelhaack_edit_flow_prefills_current_bounds_and_skips_straight_to_rooms(self):
        own_filter = {
            'filter_id': 3, 'user_id': 544675510, 'title': 'SEMMELHAACK: 4 кімн.', 'source': 'semmelhaack',
            'active': True, 'min_rooms': 3.0, 'max_price_eur': 1900.0,
        }
        query = SimpleNamespace(
            data='housing:edit:semmelhaack:3', answer=mock.Mock(), edit_message_text=mock.Mock(),
        )
        update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=544675510))
        context = SimpleNamespace(user_data={})

        with mock.patch.object(housing_monitor, 'ALLOWED_USER_IDS', {544675510}), \
             mock.patch.object(housing_monitor, 'manageable_filters', return_value=[own_filter]):
            housing_monitor.handle_callback(update, context)

        state = context.user_data['housing_admin']
        self.assertEqual(state['mode'], 'semmelhaack')
        self.assertEqual(state['edit_filter_id'], 3)
        self.assertEqual(state['min_rooms'], 3.0)
        self.assertEqual(state['max_price_eur'], 1900.0)
        self.assertEqual(state['step'], 'min_rooms')

    def test_saving_a_semmelhaack_edit_updates_instead_of_creating(self):
        state = {
            'mode': 'semmelhaack', 'step': 'min_rooms', 'user_id': 544675510, 'edit_filter_id': 4,
        }
        context = SimpleNamespace(user_data={'housing_admin': state})

        with mock.patch.object(housing_monitor, 'ALLOWED_USER_IDS', {544675510}), \
             mock.patch('user_handlers.housing_monitor.semmelhaack_store.update_filter', return_value=True) as update_filter, \
             mock.patch('user_handlers.housing_monitor.semmelhaack_store.create_filter') as create_filter:
            for text in ['3', '-', '-', '-', '-', '-']:
                update = self._update(text, user_id=544675510)
                self.assertTrue(housing_monitor.handle_private_text(update, context))

        update_filter.assert_called_once_with(
            filter_id=4, user_id=544675510, title=mock.ANY,
            min_rooms=3.0, max_rooms=None, min_area_m2=None, max_area_m2=None,
            min_price_eur=None, max_price_eur=None,
        )
        create_filter.assert_not_called()
        self.assertIn('оновлено', update.message.replies[-1][0])
        self.assertNotIn('housing_admin', context.user_data)

    def test_toggle_and_delete_dispatch_to_the_semmelhaack_store(self):
        own_filter = {
            'filter_id': 5, 'user_id': 544675510, 'title': 'SEMMELHAACK', 'source': 'semmelhaack', 'active': True,
        }
        toggle_query = SimpleNamespace(
            data='housing:toggle:semmelhaack:5:0', answer=mock.Mock(), edit_message_text=mock.Mock(),
        )
        update = SimpleNamespace(callback_query=toggle_query, effective_user=SimpleNamespace(id=544675510))
        context = SimpleNamespace(user_data={})

        with mock.patch.object(housing_monitor, 'ALLOWED_USER_IDS', {544675510}), \
             mock.patch.object(housing_monitor, 'manageable_filters', return_value=[own_filter]), \
             mock.patch('user_handlers.housing_monitor.semmelhaack_store.set_filter_active', return_value=True) as set_active:
            housing_monitor.handle_callback(update, context)
        set_active.assert_called_once_with(5, False, user_id=544675510)

        delete_query = SimpleNamespace(
            data='housing:delete_confirm:semmelhaack:5', answer=mock.Mock(), edit_message_text=mock.Mock(),
        )
        update = SimpleNamespace(callback_query=delete_query, effective_user=SimpleNamespace(id=544675510))
        with mock.patch.object(housing_monitor, 'ALLOWED_USER_IDS', {544675510}), \
             mock.patch.object(housing_monitor, 'manageable_filters', return_value=[own_filter]), \
             mock.patch('user_handlers.housing_monitor.semmelhaack_store.delete_filter', return_value=True) as delete_filter:
            housing_monitor.handle_callback(update, context)
        delete_filter.assert_called_once_with(5, user_id=544675510)


class HousingSchobaWizardTests(unittest.TestCase):
    def _update(self, text, user_id=544675510):
        message = FakeMessage(text=text, user_id=user_id)
        return SimpleNamespace(message=message, effective_message=message, effective_user=SimpleNamespace(id=user_id))

    def _cb_update(self, user_id=544675510):
        query = SimpleNamespace(answer=mock.Mock(), edit_message_text=mock.Mock())
        return SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=user_id))

    def test_available_sources_include_schoba(self):
        self.assertIn('schoba', housing_monitor.AVAILABLE_SOURCE_KEYS)

    def test_immowelt_semmelhaack_and_schoba_together_ask_kaltmiete_only_once(self):
        """Всі три джерела рахують ту саму холодну оренду — питання одне на всіх."""
        context = SimpleNamespace(user_data={'housing_admin': {
            'mode': 'multi', 'step': 'min_rooms', 'user_id': 544675510,
            'sources_selected': ['immowelt', 'semmelhaack', 'schoba'],
            'districts_selected': ['Golm'],
        }})

        with mock.patch.object(housing_monitor, 'ADMIN_ID', 312029534), \
             mock.patch.object(housing_monitor, 'ALLOWED_USER_IDS', {544675510}), \
             mock.patch.object(housing_monitor, '_request', return_value={'ok': True, 'filter_id': 11}), \
             mock.patch('user_handlers.housing_monitor.semmelhaack_store.create_filter', return_value=22), \
             mock.patch('user_handlers.housing_monitor.schoba_store.create_filter', return_value=33) as create_filter:
            for text in ['2', '-', '-', '-', '800', '1200']:
                self.assertTrue(housing_monitor.handle_private_text(self._update(text), context))

        self.assertNotIn('housing_admin', context.user_data)
        create_filter.assert_called_once_with(
            user_id=544675510, title=mock.ANY,
            min_rooms=2.0, max_rooms=None, min_area_m2=None, max_area_m2=None,
            min_price_eur=800.0, max_price_eur=1200.0,
        )

    def test_schoba_edit_flow_prefills_current_bounds_and_skips_straight_to_rooms(self):
        own_filter = {
            'filter_id': 6, 'user_id': 544675510, 'title': 'SCHOBA', 'source': 'schoba',
            'active': True, 'min_rooms': 3.0, 'max_price_eur': 900.0,
        }
        query = SimpleNamespace(
            data='housing:edit:schoba:6', answer=mock.Mock(), edit_message_text=mock.Mock(),
        )
        update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=544675510))
        context = SimpleNamespace(user_data={})

        with mock.patch.object(housing_monitor, 'ALLOWED_USER_IDS', {544675510}), \
             mock.patch.object(housing_monitor, 'manageable_filters', return_value=[own_filter]):
            housing_monitor.handle_callback(update, context)

        state = context.user_data['housing_admin']
        self.assertEqual(state['mode'], 'schoba')
        self.assertEqual(state['edit_filter_id'], 6)
        self.assertEqual(state['min_rooms'], 3.0)
        self.assertEqual(state['step'], 'min_rooms')
        self.assertNotIn('reply_markup', query.edit_message_text.call_args.kwargs)

    def test_saving_a_schoba_edit_updates_instead_of_creating(self):
        state = {'mode': 'schoba', 'step': 'min_rooms', 'user_id': 544675510, 'edit_filter_id': 7}
        context = SimpleNamespace(user_data={'housing_admin': state})

        with mock.patch.object(housing_monitor, 'ALLOWED_USER_IDS', {544675510}), \
             mock.patch('user_handlers.housing_monitor.schoba_store.update_filter', return_value=True) as update_filter, \
             mock.patch('user_handlers.housing_monitor.schoba_store.create_filter') as create_filter:
            for text in ['3', '-', '-', '-', '-', '-']:
                update = self._update(text)
                self.assertTrue(housing_monitor.handle_private_text(update, context))

        update_filter.assert_called_once_with(
            filter_id=7, user_id=544675510, title=mock.ANY,
            min_rooms=3.0, max_rooms=None, min_area_m2=None, max_area_m2=None,
            min_price_eur=None, max_price_eur=None,
        )
        create_filter.assert_not_called()
        self.assertIn('оновлено', update.message.replies[-1][0])

    def test_toggle_and_delete_dispatch_to_the_schoba_store(self):
        own_filter = {'filter_id': 8, 'user_id': 544675510, 'source': 'schoba', 'active': True}
        toggle_query = SimpleNamespace(
            data='housing:toggle:schoba:8:0', answer=mock.Mock(), edit_message_text=mock.Mock(),
        )
        update = SimpleNamespace(callback_query=toggle_query, effective_user=SimpleNamespace(id=544675510))
        context = SimpleNamespace(user_data={})

        with mock.patch.object(housing_monitor, 'ALLOWED_USER_IDS', {544675510}), \
             mock.patch.object(housing_monitor, 'manageable_filters', return_value=[own_filter]), \
             mock.patch('user_handlers.housing_monitor.schoba_store.set_filter_active', return_value=True) as set_active:
            housing_monitor.handle_callback(update, context)
        set_active.assert_called_once_with(8, False, user_id=544675510)

        delete_query = SimpleNamespace(
            data='housing:delete_confirm:schoba:8', answer=mock.Mock(), edit_message_text=mock.Mock(),
        )
        update = SimpleNamespace(callback_query=delete_query, effective_user=SimpleNamespace(id=544675510))
        with mock.patch.object(housing_monitor, 'ALLOWED_USER_IDS', {544675510}), \
             mock.patch.object(housing_monitor, 'manageable_filters', return_value=[own_filter]), \
             mock.patch('user_handlers.housing_monitor.schoba_store.delete_filter', return_value=True) as delete_filter:
            housing_monitor.handle_callback(update, context)
        delete_filter.assert_called_once_with(8, user_id=544675510)

    def test_back_from_second_schoba_field_recaps_the_first(self):
        context = SimpleNamespace(user_data={'housing_admin': {
            'mode': 'schoba', 'step': 'max_rooms', 'user_id': 544675510, 'min_rooms': 3.0,
        }})
        update = self._cb_update()

        housing_monitor._step_back(update, context)

        state = context.user_data['housing_admin']
        self.assertEqual(state['step'], 'min_rooms')


class HousingRegiomaklerWizardTests(unittest.TestCase):
    def _update(self, text, user_id=544675510):
        message = FakeMessage(text=text, user_id=user_id)
        return SimpleNamespace(message=message, effective_message=message, effective_user=SimpleNamespace(id=user_id))

    def test_available_sources_include_regiomakler(self):
        self.assertIn('regiomakler', housing_monitor.AVAILABLE_SOURCE_KEYS)

    def test_regiomakler_joins_the_shared_kaltmiete_question(self):
        context = SimpleNamespace(user_data={'housing_admin': {
            'mode': 'multi', 'step': 'min_rooms', 'user_id': 544675510,
            'sources_selected': ['schoba', 'regiomakler'],
            'districts_selected': [],
        }})

        with mock.patch.object(housing_monitor, 'ADMIN_ID', 312029534), \
             mock.patch.object(housing_monitor, 'ALLOWED_USER_IDS', {544675510}), \
             mock.patch('user_handlers.housing_monitor.schoba_store.create_filter', return_value=1), \
             mock.patch('user_handlers.housing_monitor.regiomakler_store.create_filter', return_value=2) as create_filter:
            for text in ['2', '-', '-', '-', '800', '1200']:
                self.assertTrue(housing_monitor.handle_private_text(self._update(text), context))

        self.assertNotIn('housing_admin', context.user_data)
        create_filter.assert_called_once_with(
            user_id=544675510, title=mock.ANY,
            min_rooms=2.0, max_rooms=None, min_area_m2=None, max_area_m2=None,
            min_price_eur=800.0, max_price_eur=1200.0,
        )

    def test_regiomakler_edit_flow_prefills_current_bounds(self):
        own_filter = {
            'filter_id': 9, 'user_id': 544675510, 'title': 'ImmoTeam/alpha', 'source': 'regiomakler',
            'active': True, 'min_rooms': 2.0, 'max_price_eur': 1500.0,
        }
        query = SimpleNamespace(
            data='housing:edit:regiomakler:9', answer=mock.Mock(), edit_message_text=mock.Mock(),
        )
        update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=544675510))
        context = SimpleNamespace(user_data={})

        with mock.patch.object(housing_monitor, 'ALLOWED_USER_IDS', {544675510}), \
             mock.patch.object(housing_monitor, 'manageable_filters', return_value=[own_filter]):
            housing_monitor.handle_callback(update, context)

        state = context.user_data['housing_admin']
        self.assertEqual(state['mode'], 'regiomakler')
        self.assertEqual(state['edit_filter_id'], 9)
        self.assertEqual(state['min_rooms'], 2.0)
        self.assertEqual(state['step'], 'min_rooms')

    def test_saving_a_regiomakler_edit_updates_instead_of_creating(self):
        state = {'mode': 'regiomakler', 'step': 'min_rooms', 'user_id': 544675510, 'edit_filter_id': 11}
        context = SimpleNamespace(user_data={'housing_admin': state})

        with mock.patch.object(housing_monitor, 'ALLOWED_USER_IDS', {544675510}), \
             mock.patch('user_handlers.housing_monitor.regiomakler_store.update_filter', return_value=True) as update_filter, \
             mock.patch('user_handlers.housing_monitor.regiomakler_store.create_filter') as create_filter:
            for text in ['2', '-', '-', '-', '-', '-']:
                update = self._update(text)
                self.assertTrue(housing_monitor.handle_private_text(update, context))

        update_filter.assert_called_once_with(
            filter_id=11, user_id=544675510, title=mock.ANY,
            min_rooms=2.0, max_rooms=None, min_area_m2=None, max_area_m2=None,
            min_price_eur=None, max_price_eur=None,
        )
        create_filter.assert_not_called()
        self.assertIn('оновлено', update.message.replies[-1][0])

    def test_toggle_and_delete_dispatch_to_the_regiomakler_store(self):
        own_filter = {'filter_id': 12, 'user_id': 544675510, 'source': 'regiomakler', 'active': True}
        toggle_query = SimpleNamespace(
            data='housing:toggle:regiomakler:12:0', answer=mock.Mock(), edit_message_text=mock.Mock(),
        )
        update = SimpleNamespace(callback_query=toggle_query, effective_user=SimpleNamespace(id=544675510))
        context = SimpleNamespace(user_data={})

        with mock.patch.object(housing_monitor, 'ALLOWED_USER_IDS', {544675510}), \
             mock.patch.object(housing_monitor, 'manageable_filters', return_value=[own_filter]), \
             mock.patch('user_handlers.housing_monitor.regiomakler_store.set_filter_active', return_value=True) as set_active:
            housing_monitor.handle_callback(update, context)
        set_active.assert_called_once_with(12, False, user_id=544675510)

        delete_query = SimpleNamespace(
            data='housing:delete_confirm:regiomakler:12', answer=mock.Mock(), edit_message_text=mock.Mock(),
        )
        update = SimpleNamespace(callback_query=delete_query, effective_user=SimpleNamespace(id=544675510))
        with mock.patch.object(housing_monitor, 'ALLOWED_USER_IDS', {544675510}), \
             mock.patch.object(housing_monitor, 'manageable_filters', return_value=[own_filter]), \
             mock.patch('user_handlers.housing_monitor.regiomakler_store.delete_filter', return_value=True) as delete_filter:
            housing_monitor.handle_callback(update, context)
        delete_filter.assert_called_once_with(12, user_id=544675510)


class HousingKleinanzeigenWizardTests(unittest.TestCase):
    def _update(self, text, user_id=544675510):
        message = FakeMessage(text=text, user_id=user_id)
        return SimpleNamespace(message=message, effective_message=message, effective_user=SimpleNamespace(id=user_id))

    def test_available_sources_include_kleinanzeigen(self):
        self.assertIn('kleinanzeigen', housing_monitor.AVAILABLE_SOURCE_KEYS)

    def test_kleinanzeigen_price_is_asked_separately_even_alongside_a_kaltmiete_source(self):
        """Kleinanzeigen's price has no reliable Kalt/Warm label — it must NOT
        share Immowelt/SEMMELHAACK/SCHOBA/regiomakler's Kaltmiete question."""
        context = SimpleNamespace(user_data={'housing_admin': {
            'mode': 'multi', 'step': 'min_rooms', 'user_id': 544675510,
            'sources_selected': ['immowelt', 'kleinanzeigen'],
            'districts_selected': ['Golm'],
        }})

        with mock.patch.object(housing_monitor, 'ADMIN_ID', 312029534), \
             mock.patch.object(housing_monitor, 'ALLOWED_USER_IDS', {544675510}), \
             mock.patch.object(housing_monitor, '_request', return_value={'ok': True, 'filter_id': 1}) as request, \
             mock.patch('user_handlers.housing_monitor.kleinanzeigen_store.create_filter', return_value=2) as create_filter:
            # 4 shared fields, then Immowelt's Kaltmiete (2), then Kleinanzeigen's own price (2) = 8 answers.
            for text in ['2', '-', '-', '-', '800', '1200', '500', '900']:
                self.assertTrue(housing_monitor.handle_private_text(self._update(text), context))

        self.assertNotIn('housing_admin', context.user_data)
        self.assertEqual(request.call_args.kwargs['json']['min_price_eur'], 800.0)
        self.assertEqual(request.call_args.kwargs['json']['max_price_eur'], 1200.0)
        create_filter.assert_called_once_with(
            user_id=544675510, title=mock.ANY,
            min_rooms=2.0, max_rooms=None, min_area_m2=None, max_area_m2=None,
            min_price_eur=500.0, max_price_eur=900.0,
        )

    def test_kleinanzeigen_edit_flow_prefills_current_bounds(self):
        own_filter = {
            'filter_id': 14, 'user_id': 544675510, 'title': 'Kleinanzeigen', 'source': 'kleinanzeigen',
            'active': True, 'min_rooms': 3.0, 'max_price_eur': 900.0,
        }
        query = SimpleNamespace(
            data='housing:edit:kleinanzeigen:14', answer=mock.Mock(), edit_message_text=mock.Mock(),
        )
        update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=544675510))
        context = SimpleNamespace(user_data={})

        with mock.patch.object(housing_monitor, 'ALLOWED_USER_IDS', {544675510}), \
             mock.patch.object(housing_monitor, 'manageable_filters', return_value=[own_filter]):
            housing_monitor.handle_callback(update, context)

        state = context.user_data['housing_admin']
        self.assertEqual(state['mode'], 'kleinanzeigen')
        self.assertEqual(state['edit_filter_id'], 14)
        self.assertEqual(state['min_rooms'], 3.0)
        self.assertEqual(state['step'], 'min_rooms')

    def test_saving_a_kleinanzeigen_edit_updates_instead_of_creating(self):
        state = {'mode': 'kleinanzeigen', 'step': 'min_rooms', 'user_id': 544675510, 'edit_filter_id': 15}
        context = SimpleNamespace(user_data={'housing_admin': state})

        with mock.patch.object(housing_monitor, 'ALLOWED_USER_IDS', {544675510}), \
             mock.patch('user_handlers.housing_monitor.kleinanzeigen_store.update_filter', return_value=True) as update_filter, \
             mock.patch('user_handlers.housing_monitor.kleinanzeigen_store.create_filter') as create_filter:
            for text in ['2', '-', '-', '-', '-', '-']:
                update = self._update(text)
                self.assertTrue(housing_monitor.handle_private_text(update, context))

        update_filter.assert_called_once_with(
            filter_id=15, user_id=544675510, title=mock.ANY,
            min_rooms=2.0, max_rooms=None, min_area_m2=None, max_area_m2=None,
            min_price_eur=None, max_price_eur=None,
        )
        create_filter.assert_not_called()
        self.assertIn('оновлено', update.message.replies[-1][0])

    def test_toggle_and_delete_dispatch_to_the_kleinanzeigen_store(self):
        own_filter = {'filter_id': 16, 'user_id': 544675510, 'source': 'kleinanzeigen', 'active': True}
        toggle_query = SimpleNamespace(
            data='housing:toggle:kleinanzeigen:16:0', answer=mock.Mock(), edit_message_text=mock.Mock(),
        )
        update = SimpleNamespace(callback_query=toggle_query, effective_user=SimpleNamespace(id=544675510))
        context = SimpleNamespace(user_data={})

        with mock.patch.object(housing_monitor, 'ALLOWED_USER_IDS', {544675510}), \
             mock.patch.object(housing_monitor, 'manageable_filters', return_value=[own_filter]), \
             mock.patch('user_handlers.housing_monitor.kleinanzeigen_store.set_filter_active', return_value=True) as set_active:
            housing_monitor.handle_callback(update, context)
        set_active.assert_called_once_with(16, False, user_id=544675510)

        delete_query = SimpleNamespace(
            data='housing:delete_confirm:kleinanzeigen:16', answer=mock.Mock(), edit_message_text=mock.Mock(),
        )
        update = SimpleNamespace(callback_query=delete_query, effective_user=SimpleNamespace(id=544675510))
        with mock.patch.object(housing_monitor, 'ALLOWED_USER_IDS', {544675510}), \
             mock.patch.object(housing_monitor, 'manageable_filters', return_value=[own_filter]), \
             mock.patch('user_handlers.housing_monitor.kleinanzeigen_store.delete_filter', return_value=True) as delete_filter:
            housing_monitor.handle_callback(update, context)
        delete_filter.assert_called_once_with(16, user_id=544675510)


class HousingKarlmarxWizardTests(unittest.TestCase):
    def _update(self, text, user_id=544675510):
        message = FakeMessage(text=text, user_id=user_id)
        return SimpleNamespace(message=message, effective_message=message, effective_user=SimpleNamespace(id=user_id))

    def test_available_sources_include_karlmarx(self):
        self.assertIn('karlmarx', housing_monitor.AVAILABLE_SOURCE_KEYS)

    def test_karlmarx_price_is_asked_separately_even_alongside_a_kaltmiete_source(self):
        """Karl Marx counts Warmmiete, not Kaltmiete — it must NOT share
        Immowelt/SEMMELHAACK/SCHOBA/regiomakler/locals's Kaltmiete question."""
        context = SimpleNamespace(user_data={'housing_admin': {
            'mode': 'multi', 'step': 'min_rooms', 'user_id': 544675510,
            'sources_selected': ['immowelt', 'karlmarx'],
            'districts_selected': ['Golm'],
        }})

        with mock.patch.object(housing_monitor, 'ADMIN_ID', 312029534), \
             mock.patch.object(housing_monitor, 'ALLOWED_USER_IDS', {544675510}), \
             mock.patch.object(housing_monitor, '_request', return_value={'ok': True, 'filter_id': 1}) as request, \
             mock.patch('user_handlers.housing_monitor.karlmarx_store.create_filter', return_value=2) as create_filter:
            # 4 shared fields, then Immowelt's Kaltmiete (2), then Karl Marx's own price (2) = 8 answers.
            for text in ['2', '-', '-', '-', '800', '1200', '500', '900']:
                self.assertTrue(housing_monitor.handle_private_text(self._update(text), context))

        self.assertNotIn('housing_admin', context.user_data)
        self.assertEqual(request.call_args.kwargs['json']['min_price_eur'], 800.0)
        self.assertEqual(request.call_args.kwargs['json']['max_price_eur'], 1200.0)
        create_filter.assert_called_once_with(
            user_id=544675510, title=mock.ANY,
            min_rooms=2.0, max_rooms=None, min_area_m2=None, max_area_m2=None,
            min_price_eur=500.0, max_price_eur=900.0,
        )

    def test_karlmarx_edit_flow_prefills_current_bounds(self):
        own_filter = {
            'filter_id': 20, 'user_id': 544675510, 'title': 'Karl Marx', 'source': 'karlmarx',
            'active': True, 'min_rooms': 3.0, 'max_price_eur': 3000.0,
        }
        query = SimpleNamespace(
            data='housing:edit:karlmarx:20', answer=mock.Mock(), edit_message_text=mock.Mock(),
        )
        update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=544675510))
        context = SimpleNamespace(user_data={})

        with mock.patch.object(housing_monitor, 'ALLOWED_USER_IDS', {544675510}), \
             mock.patch.object(housing_monitor, 'manageable_filters', return_value=[own_filter]):
            housing_monitor.handle_callback(update, context)

        state = context.user_data['housing_admin']
        self.assertEqual(state['mode'], 'karlmarx')
        self.assertEqual(state['edit_filter_id'], 20)
        self.assertEqual(state['min_rooms'], 3.0)
        self.assertEqual(state['step'], 'min_rooms')

    def test_saving_a_karlmarx_edit_updates_instead_of_creating(self):
        state = {'mode': 'karlmarx', 'step': 'min_rooms', 'user_id': 544675510, 'edit_filter_id': 21}
        context = SimpleNamespace(user_data={'housing_admin': state})

        with mock.patch.object(housing_monitor, 'ALLOWED_USER_IDS', {544675510}), \
             mock.patch('user_handlers.housing_monitor.karlmarx_store.update_filter', return_value=True) as update_filter, \
             mock.patch('user_handlers.housing_monitor.karlmarx_store.create_filter') as create_filter:
            for text in ['2', '-', '-', '-', '-', '-']:
                update = self._update(text)
                self.assertTrue(housing_monitor.handle_private_text(update, context))

        update_filter.assert_called_once_with(
            filter_id=21, user_id=544675510, title=mock.ANY,
            min_rooms=2.0, max_rooms=None, min_area_m2=None, max_area_m2=None,
            min_price_eur=None, max_price_eur=None,
        )
        create_filter.assert_not_called()
        self.assertIn('оновлено', update.message.replies[-1][0])

    def test_toggle_and_delete_dispatch_to_the_karlmarx_store(self):
        own_filter = {'filter_id': 22, 'user_id': 544675510, 'source': 'karlmarx', 'active': True}
        toggle_query = SimpleNamespace(
            data='housing:toggle:karlmarx:22:0', answer=mock.Mock(), edit_message_text=mock.Mock(),
        )
        update = SimpleNamespace(callback_query=toggle_query, effective_user=SimpleNamespace(id=544675510))
        context = SimpleNamespace(user_data={})

        with mock.patch.object(housing_monitor, 'ALLOWED_USER_IDS', {544675510}), \
             mock.patch.object(housing_monitor, 'manageable_filters', return_value=[own_filter]), \
             mock.patch('user_handlers.housing_monitor.karlmarx_store.set_filter_active', return_value=True) as set_active:
            housing_monitor.handle_callback(update, context)
        set_active.assert_called_once_with(22, False, user_id=544675510)

        delete_query = SimpleNamespace(
            data='housing:delete_confirm:karlmarx:22', answer=mock.Mock(), edit_message_text=mock.Mock(),
        )
        update = SimpleNamespace(callback_query=delete_query, effective_user=SimpleNamespace(id=544675510))
        with mock.patch.object(housing_monitor, 'ALLOWED_USER_IDS', {544675510}), \
             mock.patch.object(housing_monitor, 'manageable_filters', return_value=[own_filter]), \
             mock.patch('user_handlers.housing_monitor.karlmarx_store.delete_filter', return_value=True) as delete_filter:
            housing_monitor.handle_callback(update, context)
        delete_filter.assert_called_once_with(22, user_id=544675510)


class HousingRecentMatchesOfferTests(unittest.TestCase):
    """After a filter is created, two buttons offer to search listings first
    seen in the last hour/day — bypassing the create-time baseline that
    otherwise hides everything already in the catalog at that moment."""

    def _update(self, text, user_id=544675510):
        message = FakeMessage(text=text, user_id=user_id)
        return SimpleNamespace(message=message, effective_message=message, effective_user=SimpleNamespace(id=user_id))

    def _cb_update(self, data, user_id=544675510):
        query = SimpleNamespace(
            data=data, answer=mock.Mock(), edit_message_text=mock.Mock(), edit_message_reply_markup=mock.Mock(),
        )
        update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=user_id))
        context = SimpleNamespace(user_data={}, bot=mock.Mock())
        return update, context

    def test_creating_a_single_source_filter_offers_the_recent_buttons(self):
        state = {'mode': 'schoba', 'step': 'min_rooms', 'user_id': 544675510}
        context = SimpleNamespace(user_data={'housing_admin': state})

        with mock.patch.object(housing_monitor, 'ALLOWED_USER_IDS', {544675510}), \
             mock.patch('user_handlers.housing_monitor.schoba_store.create_filter', return_value=30):
            for text in ['2', '-', '-', '-', '-', '-']:
                update = self._update(text)
                self.assertTrue(housing_monitor.handle_private_text(update, context))

        self.assertEqual(context.user_data['recent_offer_filters'], [('schoba', 30)])
        reply_text, reply_kwargs = update.message.replies[-1]
        callbacks = [
            b.callback_data for row in reply_kwargs['reply_markup'].inline_keyboard for b in row
        ]
        self.assertIn('housing:recent:1', callbacks)
        self.assertIn('housing:recent:24', callbacks)

    def test_editing_a_filter_does_not_offer_the_recent_buttons(self):
        state = {'mode': 'schoba', 'step': 'min_rooms', 'user_id': 544675510, 'edit_filter_id': 31}
        context = SimpleNamespace(user_data={'housing_admin': state})

        with mock.patch.object(housing_monitor, 'ALLOWED_USER_IDS', {544675510}), \
             mock.patch('user_handlers.housing_monitor.schoba_store.update_filter', return_value=True):
            for text in ['2', '-', '-', '-', '-', '-']:
                update = self._update(text)
                self.assertTrue(housing_monitor.handle_private_text(update, context))

        self.assertNotIn('recent_offer_filters', context.user_data)
        reply_text, reply_kwargs = update.message.replies[-1]
        self.assertIsNone(reply_kwargs.get('reply_markup'))

    def test_multi_source_creation_stashes_every_created_filter(self):
        context = SimpleNamespace(user_data={'housing_admin': {
            'mode': 'multi', 'step': 'min_rooms', 'user_id': 544675510,
            'sources_selected': ['schoba', 'locals'],
            'districts_selected': [],
        }})

        with mock.patch.object(housing_monitor, 'ADMIN_ID', 312029534), \
             mock.patch.object(housing_monitor, 'ALLOWED_USER_IDS', {544675510}), \
             mock.patch('user_handlers.housing_monitor.schoba_store.create_filter', return_value=40), \
             mock.patch('user_handlers.housing_monitor.locals_store.create_filter', return_value=41):
            for text in ['2', '-', '-', '-', '800', '1200']:
                self.assertTrue(housing_monitor.handle_private_text(self._update(text), context))

        self.assertEqual(
            sorted(context.user_data['recent_offer_filters']),
            [('locals', 41), ('schoba', 40)],
        )

    def test_clicking_a_window_sends_matches_first_seen_since_the_cutoff_to_the_filter_owner(self):
        update, context = self._cb_update('housing:recent:1')
        context.user_data['recent_offer_filters'] = [('schoba', 30)]

        filt = {'filter_id': 30, 'user_id': 544675510, 'min_rooms': 2.0}
        recent_listing = {'listing_key': 'fresh', 'rooms': 2.0, 'title': 'Свіже'}

        with mock.patch('user_handlers.housing_monitor.schoba_store.list_filters', return_value=[filt]), \
             mock.patch(
                 'user_handlers.housing_monitor.schoba_store.list_active_listings_since',
                 return_value=[recent_listing],
             ) as list_since, \
             mock.patch('user_handlers.housing_monitor.schoba_store.mark_delivered') as mark_delivered:
            housing_monitor._send_recent_matches(update, context, 1)

        list_since.assert_called_once()
        cutoff = list_since.call_args.args[0]
        self.assertLess(abs((datetime.utcnow() - cutoff).total_seconds() - 3600), 5)
        context.bot.send_message.assert_called_once()
        self.assertEqual(context.bot.send_message.call_args.kwargs['chat_id'], 544675510)
        mark_delivered.assert_called_once_with(30, 'fresh')
        self.assertNotIn('recent_offer_filters', context.user_data)

    def test_no_recent_matches_tells_the_clicking_user_nothing_was_found(self):
        update, context = self._cb_update('housing:recent:24', user_id=999)
        context.user_data['recent_offer_filters'] = [('schoba', 30)]
        filt = {'filter_id': 30, 'user_id': 544675510, 'min_rooms': 2.0}

        with mock.patch('user_handlers.housing_monitor.schoba_store.list_filters', return_value=[filt]), \
             mock.patch('user_handlers.housing_monitor.schoba_store.list_active_listings_since', return_value=[]):
            housing_monitor._send_recent_matches(update, context, 24)

        context.bot.send_message.assert_called_once_with(chat_id=999, text=mock.ANY)

    def test_skip_clears_the_stash_without_sending_anything(self):
        update, context = self._cb_update('housing:recent_skip')
        context.user_data['recent_offer_filters'] = [('schoba', 30)]

        housing_monitor.handle_callback(update, context)

        self.assertNotIn('recent_offer_filters', context.user_data)
        context.bot.send_message.assert_not_called()


class HousingLocalsWizardTests(unittest.TestCase):
    def _update(self, text, user_id=544675510):
        message = FakeMessage(text=text, user_id=user_id)
        return SimpleNamespace(message=message, effective_message=message, effective_user=SimpleNamespace(id=user_id))

    def test_available_sources_include_locals(self):
        self.assertIn('locals', housing_monitor.AVAILABLE_SOURCE_KEYS)

    def test_locals_joins_the_shared_kaltmiete_question(self):
        context = SimpleNamespace(user_data={'housing_admin': {
            'mode': 'multi', 'step': 'min_rooms', 'user_id': 544675510,
            'sources_selected': ['schoba', 'locals'],
            'districts_selected': [],
        }})

        with mock.patch.object(housing_monitor, 'ADMIN_ID', 312029534), \
             mock.patch.object(housing_monitor, 'ALLOWED_USER_IDS', {544675510}), \
             mock.patch('user_handlers.housing_monitor.schoba_store.create_filter', return_value=1), \
             mock.patch('user_handlers.housing_monitor.locals_store.create_filter', return_value=2) as create_filter:
            for text in ['2', '-', '-', '-', '800', '1200']:
                self.assertTrue(housing_monitor.handle_private_text(self._update(text), context))

        self.assertNotIn('housing_admin', context.user_data)
        create_filter.assert_called_once_with(
            user_id=544675510, title=mock.ANY,
            min_rooms=2.0, max_rooms=None, min_area_m2=None, max_area_m2=None,
            min_price_eur=800.0, max_price_eur=1200.0,
        )

    def test_locals_edit_flow_prefills_current_bounds(self):
        own_filter = {
            'filter_id': 17, 'user_id': 544675510, 'title': 'locals®', 'source': 'locals',
            'active': True, 'min_rooms': 2.0, 'max_price_eur': 1500.0,
        }
        query = SimpleNamespace(
            data='housing:edit:locals:17', answer=mock.Mock(), edit_message_text=mock.Mock(),
        )
        update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=544675510))
        context = SimpleNamespace(user_data={})

        with mock.patch.object(housing_monitor, 'ALLOWED_USER_IDS', {544675510}), \
             mock.patch.object(housing_monitor, 'manageable_filters', return_value=[own_filter]):
            housing_monitor.handle_callback(update, context)

        state = context.user_data['housing_admin']
        self.assertEqual(state['mode'], 'locals')
        self.assertEqual(state['edit_filter_id'], 17)
        self.assertEqual(state['min_rooms'], 2.0)
        self.assertEqual(state['step'], 'min_rooms')

    def test_saving_a_locals_edit_updates_instead_of_creating(self):
        state = {'mode': 'locals', 'step': 'min_rooms', 'user_id': 544675510, 'edit_filter_id': 18}
        context = SimpleNamespace(user_data={'housing_admin': state})

        with mock.patch.object(housing_monitor, 'ALLOWED_USER_IDS', {544675510}), \
             mock.patch('user_handlers.housing_monitor.locals_store.update_filter', return_value=True) as update_filter, \
             mock.patch('user_handlers.housing_monitor.locals_store.create_filter') as create_filter:
            for text in ['2', '-', '-', '-', '-', '-']:
                update = self._update(text)
                self.assertTrue(housing_monitor.handle_private_text(update, context))

        update_filter.assert_called_once_with(
            filter_id=18, user_id=544675510, title=mock.ANY,
            min_rooms=2.0, max_rooms=None, min_area_m2=None, max_area_m2=None,
            min_price_eur=None, max_price_eur=None,
        )
        create_filter.assert_not_called()
        self.assertIn('оновлено', update.message.replies[-1][0])

    def test_toggle_and_delete_dispatch_to_the_locals_store(self):
        own_filter = {'filter_id': 19, 'user_id': 544675510, 'source': 'locals', 'active': True}
        toggle_query = SimpleNamespace(
            data='housing:toggle:locals:19:0', answer=mock.Mock(), edit_message_text=mock.Mock(),
        )
        update = SimpleNamespace(callback_query=toggle_query, effective_user=SimpleNamespace(id=544675510))
        context = SimpleNamespace(user_data={})

        with mock.patch.object(housing_monitor, 'ALLOWED_USER_IDS', {544675510}), \
             mock.patch.object(housing_monitor, 'manageable_filters', return_value=[own_filter]), \
             mock.patch('user_handlers.housing_monitor.locals_store.set_filter_active', return_value=True) as set_active:
            housing_monitor.handle_callback(update, context)
        set_active.assert_called_once_with(19, False, user_id=544675510)

        delete_query = SimpleNamespace(
            data='housing:delete_confirm:locals:19', answer=mock.Mock(), edit_message_text=mock.Mock(),
        )
        update = SimpleNamespace(callback_query=delete_query, effective_user=SimpleNamespace(id=544675510))
        with mock.patch.object(housing_monitor, 'ALLOWED_USER_IDS', {544675510}), \
             mock.patch.object(housing_monitor, 'manageable_filters', return_value=[own_filter]), \
             mock.patch('user_handlers.housing_monitor.locals_store.delete_filter', return_value=True) as delete_filter:
            housing_monitor.handle_callback(update, context)
        delete_filter.assert_called_once_with(19, user_id=544675510)


class HousingWizardBackButtonTests(unittest.TestCase):
    """Люди намагалися виправити відповідь, редагуючи своє старе повідомлення —
    бот такі редагування не бачить. Recap показує вже введене, а «⬅ Назад»
    дозволяє переправити конкретне поле, не заводячи фільтр наново."""

    def _update(self, text, user_id=544675510):
        message = FakeMessage(text=text, user_id=user_id)
        return SimpleNamespace(message=message, effective_message=message, effective_user=SimpleNamespace(id=user_id))

    def _cb_update(self, user_id=544675510):
        query = SimpleNamespace(answer=mock.Mock(), edit_message_text=mock.Mock())
        return SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=user_id))

    def test_second_field_prompt_actually_contains_the_recap_line(self):
        context = SimpleNamespace(user_data={'housing_admin': {
            'mode': 'immowelt', 'step': 'min_price_eur', 'user_id': 123456789,
            'districts_selected': ['Golm'],
        }})
        update = self._update('800', user_id=544675510)

        with mock.patch.object(housing_monitor, 'ADMIN_ID', 312029534), \
             mock.patch.object(housing_monitor, 'ALLOWED_USER_IDS', {544675510}):
            housing_monitor.handle_private_text(update, context)

        text, kwargs = update.message.replies[-1]
        self.assertIn('Ціна: мінімум (від): 800', text)
        callbacks = [b.callback_data for row in kwargs['reply_markup'].inline_keyboard for b in row]
        self.assertIn(housing_monitor.BACK_CALLBACK, callbacks)

    def test_numeric_prompts_are_hand_holding_and_actually_render_as_html(self):
        """The old one-liner ("Мінімальна кількість кімнат: Або «-», щоб
        пропустити.") was too easy to skim past — people didn't realize they
        could just type a number and hit send. The new prompt spells that
        out and uses <b>/<code> markup, which only renders if the message is
        actually sent with parse_mode="HTML" (otherwise Telegram shows the
        literal tags)."""
        context = SimpleNamespace(user_data={})
        finish_update = self._cb_update(user_id=544675510)
        with mock.patch.object(housing_monitor, 'ALLOWED_USER_IDS', {544675510}):
            housing_monitor.start_self_add_flow(self._update(''), context)
            housing_monitor._toggle_source(self._cb_update(user_id=544675510), context, 'schoba')
            housing_monitor._finish_sources(finish_update, context)

        query = finish_update.callback_query
        query.edit_message_text.assert_called_once()
        text = query.edit_message_text.call_args.args[0]
        kwargs = query.edit_message_text.call_args.kwargs
        self.assertIn('Напишіть', text)
        self.assertIn('Надіслати', text)
        self.assertIn('<b>', text)
        self.assertEqual(kwargs.get('parse_mode'), 'HTML')

    def test_back_from_second_immowelt_field_returns_to_the_first_with_its_value_intact(self):
        context = SimpleNamespace(user_data={'housing_admin': {
            'mode': 'immowelt', 'step': 'max_price_eur', 'user_id': 123456789,
            'districts_selected': ['Golm'], 'min_price_eur': 800.0,
        }})
        update = self._cb_update()

        housing_monitor._step_back(update, context)

        state = context.user_data['housing_admin']
        self.assertEqual(state['step'], 'min_price_eur')
        self.assertEqual(state['min_price_eur'], 800.0)
        prompt = update.callback_query.edit_message_text.call_args.args[0]
        self.assertNotIn('мінімум (від): 800', prompt)  # editing the field itself doesn't recap itself

    def test_back_from_the_first_immowelt_field_returns_to_district_picker(self):
        context = SimpleNamespace(user_data={'housing_admin': {
            'mode': 'immowelt', 'step': 'min_price_eur', 'user_id': 123456789,
            'districts_selected': ['Golm'],
        }})
        update = self._cb_update()

        housing_monitor._step_back(update, context)

        state = context.user_data['housing_admin']
        self.assertEqual(state['step'], 'districts')
        callbacks = [
            b.callback_data
            for row in update.callback_query.edit_message_text.call_args.kwargs['reply_markup'].inline_keyboard
            for b in row
        ]
        self.assertIn('housing:imm_district_done', callbacks)

    def test_back_from_the_first_propotsdam_field_returns_to_district_picker(self):
        context = SimpleNamespace(user_data={'housing_admin': {
            'mode': 'propotsdam', 'step': 'min_rooms', 'user_id': 123456789,
            'districts_selected': ['Golm'],
        }})
        update = self._cb_update()

        housing_monitor._step_back(update, context)

        self.assertEqual(context.user_data['housing_admin']['step'], 'districts')

    def test_semmelhaack_first_field_has_no_back_button(self):
        query = SimpleNamespace(answer=mock.Mock(), edit_message_text=mock.Mock())
        update = SimpleNamespace(
            callback_query=query, effective_user=SimpleNamespace(id=544675510),
        )
        own_filter = {'filter_id': 3, 'user_id': 544675510, 'source': 'semmelhaack', 'active': True}
        context = SimpleNamespace(user_data={})

        with mock.patch.object(housing_monitor, 'ALLOWED_USER_IDS', {544675510}), \
             mock.patch.object(housing_monitor, 'manageable_filters', return_value=[own_filter]):
            housing_monitor.start_semmelhaack_edit_flow(update, context, 3)

        call_kwargs = query.edit_message_text.call_args.kwargs
        self.assertNotIn('reply_markup', call_kwargs)

    def test_semmelhaack_second_field_offers_back_and_recaps_the_first(self):
        context = SimpleNamespace(user_data={'housing_admin': {
            'mode': 'semmelhaack', 'step': 'min_rooms', 'user_id': 544675510,
        }})
        update = self._update('3', user_id=544675510)

        with mock.patch.object(housing_monitor, 'ALLOWED_USER_IDS', {544675510}):
            housing_monitor.handle_private_text(update, context)

        text, kwargs = update.message.replies[-1]
        self.assertIn('Кімнати: мінімум (від): 3', text)
        callbacks = [b.callback_data for row in kwargs['reply_markup'].inline_keyboard for b in row]
        self.assertIn(housing_monitor.BACK_CALLBACK, callbacks)

    def test_back_from_first_shared_multi_field_returns_to_districts_when_district_aware_source_picked(self):
        context = SimpleNamespace(user_data={'housing_admin': {
            'mode': 'multi', 'step': 'min_rooms', 'user_id': 544675510,
            'sources_selected': ['immowelt'], 'districts_selected': ['Golm'],
        }})
        update = self._cb_update()

        housing_monitor._step_back(update, context)

        self.assertEqual(context.user_data['housing_admin']['step'], 'districts')

    def test_back_from_first_shared_multi_field_returns_to_sources_when_only_semmelhaack_picked(self):
        context = SimpleNamespace(user_data={'housing_admin': {
            'mode': 'multi', 'step': 'min_rooms', 'user_id': 544675510,
            'sources_selected': ['semmelhaack'], 'districts_selected': [],
        }})
        update = self._cb_update()

        housing_monitor._step_back(update, context)

        self.assertEqual(context.user_data['housing_admin']['step'], 'sources')

    def test_back_from_first_price_step_returns_to_last_shared_field(self):
        context = SimpleNamespace(user_data={'housing_admin': {
            'mode': 'multi', 'step': 'min_price_eur', 'user_id': 544675510,
            'sources_selected': ['immowelt'], 'districts_selected': ['Golm'],
            '_price_steps': ['min_price_eur', 'max_price_eur'],
            'min_rooms': 2.0, 'max_rooms': None, 'min_area_m2': None, 'max_area_m2': 80.0,
        }})
        update = self._cb_update()

        housing_monitor._step_back(update, context)

        state = context.user_data['housing_admin']
        self.assertEqual(state['step'], 'max_area_m2')
        prompt = update.callback_query.edit_message_text.call_args.args[0]
        self.assertIn('Кімнати: мінімум (від): 2', prompt)

    def test_back_from_second_price_step_recaps_the_first_price_answer(self):
        context = SimpleNamespace(user_data={'housing_admin': {
            'mode': 'multi', 'step': 'max_price_eur', 'user_id': 544675510,
            'sources_selected': ['immowelt'], 'districts_selected': ['Golm'],
            '_price_steps': ['min_price_eur', 'max_price_eur'],
            'min_rooms': None, 'max_rooms': None, 'min_area_m2': None, 'max_area_m2': None,
            'min_price_eur': 800.0,
        }})
        update = self._cb_update()

        housing_monitor._step_back(update, context)

        state = context.user_data['housing_admin']
        self.assertEqual(state['step'], 'min_price_eur')

    def test_back_from_multi_districts_returns_to_sources(self):
        context = SimpleNamespace(user_data={'housing_admin': {
            'mode': 'multi', 'step': 'districts', 'user_id': 544675510,
            'sources_selected': ['immowelt', 'propotsdam'], 'districts_selected': [],
        }})
        update = self._cb_update()

        housing_monitor._step_back(update, context)

        self.assertEqual(context.user_data['housing_admin']['step'], 'sources')


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

    def test_locked_menu_also_offers_the_faq(self):
        """People without access yet should still be able to read what the
        feature does and how much it costs before requesting it."""
        with mock.patch.object(housing_monitor, 'is_allowed', return_value=False):
            labels = [
                button.text
                for row in housing_monitor._locked_keyboard().inline_keyboard
                for button in row
            ]

        self.assertIn('❓ Довідка / Часті питання', labels)

    def test_allowed_menu_offers_the_faq(self):
        with mock.patch.object(housing_monitor, 'ADMIN_ID', 312029534), \
             mock.patch.object(housing_monitor, 'ALLOWED_USER_IDS', {544675510}):
            labels = [
                button.text
                for row in housing_monitor._menu_keyboard(544675510).inline_keyboard
                for button in row
            ]

        self.assertIn('❓ Довідка / Часті питання', labels)

    def test_faq_screen_mentions_the_price_and_returns_to_the_menu(self):
        query = SimpleNamespace(data='housing:faq', answer=mock.Mock(), edit_message_text=mock.Mock())
        update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=544675510))
        context = SimpleNamespace(user_data={})

        housing_monitor.handle_callback(update, context)

        query.answer.assert_called_once()
        text, kwargs = query.edit_message_text.call_args.args[0], query.edit_message_text.call_args.kwargs
        self.assertIn('10 €', text)
        callbacks = [b.callback_data for row in kwargs['reply_markup'].inline_keyboard for b in row]
        self.assertIn('housing:menu', callbacks)

    def test_locked_screen_explains_the_value_and_the_price(self):
        context = SimpleNamespace()
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=999),
            effective_message=SimpleNamespace(reply_text=mock.Mock()),
            callback_query=None,
        )
        with mock.patch.object(housing_monitor, 'is_allowed', return_value=False):
            housing_monitor.show_menu(update, context)

        text = update.effective_message.reply_text.call_args.args[0]
        self.assertIn('8 порталами', text)
        self.assertIn('10 €', text)

    def test_district_pickers_bold_the_current_selection(self):
        # Раніше вибрані райони губилися серед звичайного тексту — людина не
        # одразу бачила, що вже позначила.
        self.assertIn('<b>Golm</b>', housing_monitor._immowelt_district_text(['Golm']))
        self.assertIn('<b>Golm</b>', housing_monitor._district_text(['Golm']))
        self.assertIn('<b>Golm</b>', housing_monitor._multi_district_text(['Golm']))

    def test_back_button_text_explains_what_it_does(self):
        callback = housing_monitor._field_keyboard().inline_keyboard[0][0]
        self.assertEqual(callback.callback_data, housing_monitor.BACK_CALLBACK)
        self.assertIn('виправити', callback.text)

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
            user_data={}, bot_data={'housing_access_pending': {777: True}}, bot=mock.Mock()
        )

        with mock.patch.object(housing_monitor, 'ADMIN_ID', 312029534), \
             mock.patch.object(housing_monitor, 'is_allowed', return_value=False):
            housing_monitor.handle_callback(self._update(), context)

        context.bot.send_message.assert_not_called()

    def test_denial_clears_the_pending_flag_so_the_user_can_ask_again(self):
        # Regression: the pending flag used to live in the ADMIN's own
        # user_data (set via the requester's context, cleared - incorrectly -
        # via the admin's), so it never actually cleared and the requester
        # stayed locked out until the container restarted.
        context = SimpleNamespace(
            user_data={},
            bot_data={
                'housing_access_pending': {777: True},
                'housing_access_names': {777: 'Іван (@ivan)'},
            },
            bot=mock.Mock(),
        )
        deny_update = self._update(user_id=312029534, data='housing:access_deny:777')

        with mock.patch.object(housing_monitor, 'ADMIN_ID', 312029534), \
             mock.patch.object(housing_monitor.housing_access_store, 'grant_access') as grant:
            housing_monitor.handle_callback(deny_update, context)

        grant.assert_not_called()
        self.assertNotIn(777, context.bot_data.get('housing_access_pending', {}))

        context.bot.send_message.reset_mock()
        with mock.patch.object(housing_monitor, 'ADMIN_ID', 312029534), \
             mock.patch.object(housing_monitor, 'is_allowed', return_value=False):
            housing_monitor.handle_callback(self._update(), context)

        context.bot.send_message.assert_called_once()

    def test_admin_approval_asks_how_many_months_before_granting_anything(self):
        context = SimpleNamespace(
            user_data={}, bot_data={'housing_access_names': {777: 'Іван (@ivan)'}}, bot=mock.Mock()
        )
        update = self._update(user_id=312029534, data='housing:access_grant:777')

        with mock.patch.object(housing_monitor, 'ADMIN_ID', 312029534), \
             mock.patch.object(housing_monitor.housing_access_store, 'grant_access') as grant:
            housing_monitor.handle_callback(update, context)

        grant.assert_not_called()
        context.bot.send_message.assert_not_called()
        months_callbacks = [
            b.callback_data
            for row in update.callback_query.edit_message_text.call_args.kwargs['reply_markup'].inline_keyboard
            for b in row
        ]
        self.assertIn('housing:access_months:777:1', months_callbacks)
        self.assertIn('housing:access_months:777:12', months_callbacks)

    def test_picking_months_grants_access_with_an_expiry_and_tells_the_user(self):
        context = SimpleNamespace(
            user_data={}, bot_data={'housing_access_names': {777: 'Іван (@ivan)'}}, bot=mock.Mock()
        )
        update = self._update(user_id=312029534, data='housing:access_months:777:3')

        with mock.patch.object(housing_monitor, 'ADMIN_ID', 312029534), \
             mock.patch.object(housing_monitor.housing_access_store, 'grant_access') as grant:
            housing_monitor.handle_callback(update, context)

        grant.assert_called_once()
        args = grant.call_args.args
        self.assertEqual(args[0], 777)
        self.assertEqual(args[1], 'Іван (@ivan)')
        expires_at = grant.call_args.kwargs['expires_at']
        # Calendar-accurate months, not a flat 30*N days (see _add_months).
        self.assertAlmostEqual(
            expires_at, housing_monitor._add_months(datetime.utcnow(), 3), delta=timedelta(minutes=1),
        )
        self.assertEqual(context.bot.send_message.call_args.kwargs['chat_id'], 777)
        # The pending flag and stashed name are for THIS user - a second
        # grant attempt for someone else must not be blocked by leftovers.
        self.assertNotIn(777, context.bot_data.get('housing_access_pending', {}))
        self.assertNotIn(777, context.bot_data.get('housing_access_names', {}))

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


class HousingAccessExpiryTests(unittest.TestCase):
    """На скільки місяців дати доступ, попередження за 3 дні, автозакриття."""

    def _query_update(self, data, user_id):
        query = SimpleNamespace(data=data, answer=mock.Mock(), edit_message_text=mock.Mock())
        return SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(
            id=user_id, first_name='Іван', last_name='', username='ivan',
        ))

    def test_add_months_clamps_to_the_end_of_a_shorter_month(self):
        # Jan 31 + 1 month must not explode trying to build Feb 31.
        start = datetime(2026, 1, 31, 12, 0)
        self.assertEqual(housing_monitor._add_months(start, 1), datetime(2026, 2, 28, 12, 0))
        self.assertEqual(housing_monitor._add_months(start, 12), datetime(2027, 1, 31, 12, 0))

    def test_continue_button_notifies_the_admin_with_a_renew_shortcut(self):
        update = self._query_update('housing:access_continue:777', user_id=777)
        context = SimpleNamespace(bot_data={}, bot=mock.Mock())

        with mock.patch.object(housing_monitor, 'ADMIN_ID', 312029534):
            housing_monitor.handle_callback(update, context)

        kwargs = context.bot.send_message.call_args.kwargs
        self.assertEqual(kwargs['chat_id'], 312029534)
        self.assertIn('777', kwargs['text'])
        callbacks = [b.callback_data for row in kwargs['reply_markup'].inline_keyboard for b in row]
        self.assertIn('housing:access_renew:777', callbacks)

    def test_continue_button_is_a_no_op_for_someone_elses_notice(self):
        # Buttons are only ever delivered to the person's own chat, but the
        # handler still shouldn't trust the callback data blindly.
        update = self._query_update('housing:access_continue:777', user_id=999)
        context = SimpleNamespace(bot_data={}, bot=mock.Mock())

        with mock.patch.object(housing_monitor, 'ADMIN_ID', 312029534):
            housing_monitor.handle_callback(update, context)

        context.bot.send_message.assert_not_called()

    def test_stop_button_leaves_access_open_until_the_paid_period_actually_ends(self):
        # Clicking "не продовжувати" must NOT cut the person off early - they
        # already paid through the expiry date. It only turns off the
        # question; the actual close happens later via check_access_expiry's
        # list_expired() pass (see the tests below), exactly as if they'd
        # never answered the warning at all.
        update = self._query_update('housing:access_stop:777', user_id=777)
        context = SimpleNamespace(bot_data={}, bot=mock.Mock())

        with mock.patch.object(housing_monitor, 'ADMIN_ID', 312029534), \
             mock.patch.object(housing_monitor.housing_access_store, 'revoke_access') as revoke, \
             mock.patch.object(housing_monitor, '_delete_all_filters_for_user') as delete_filters:
            housing_monitor.handle_callback(update, context)

        revoke.assert_not_called()
        delete_filters.assert_not_called()
        self.assertIn('автоматично закрито', update.callback_query.edit_message_text.call_args.args[0])
        context.bot.send_message.assert_not_called()

    def test_check_access_expiry_warns_the_user_and_the_admin_once(self):
        expires_at = datetime.utcnow() + timedelta(days=2)
        context = SimpleNamespace(bot=mock.Mock())

        with mock.patch.object(housing_monitor, 'ADMIN_ID', 312029534), \
             mock.patch.object(
                 housing_monitor.housing_access_store, 'list_expiring_soon',
                 return_value=[{'user_id': 777, 'display_name': 'Іван', 'expires_at': expires_at}],
             ), \
             mock.patch.object(housing_monitor.housing_access_store, 'list_expired', return_value=[]), \
             mock.patch.object(housing_monitor.housing_access_store, 'mark_notice_sent') as mark_sent:
            housing_monitor.check_access_expiry(context)

        calls = {call.kwargs['chat_id']: call for call in context.bot.send_message.call_args_list}
        self.assertIn(777, calls)
        user_callbacks = [
            b.callback_data for row in calls[777].kwargs['reply_markup'].inline_keyboard for b in row
        ]
        self.assertIn('housing:access_continue:777', user_callbacks)
        self.assertIn('housing:access_stop:777', user_callbacks)
        self.assertIn(312029534, calls)
        mark_sent.assert_called_once_with(777)

    def test_check_access_expiry_closes_access_once_the_date_has_passed(self):
        context = SimpleNamespace(bot=mock.Mock())

        with mock.patch.object(housing_monitor, 'ADMIN_ID', 312029534), \
             mock.patch.object(housing_monitor.housing_access_store, 'list_expiring_soon', return_value=[]), \
             mock.patch.object(
                 housing_monitor.housing_access_store, 'list_expired',
                 return_value=[{'user_id': 888, 'display_name': 'Стара підписка', 'expires_at': datetime.utcnow()}],
             ), \
             mock.patch.object(housing_monitor.housing_access_store, 'revoke_access') as revoke, \
             mock.patch.object(housing_monitor, '_delete_all_filters_for_user', return_value=0):
            housing_monitor.check_access_expiry(context)

        revoke.assert_called_once_with(888)
        goodbye_calls = [
            call for call in context.bot.send_message.call_args_list
            if call.kwargs.get('chat_id') == 888
        ]
        self.assertEqual(len(goodbye_calls), 1)
        self.assertIn('Дякуємо', goodbye_calls[0].kwargs['text'])


class HousingFirstFilterCongratsTests(unittest.TestCase):
    """Одноразове «ви впорались, молодець» після створення фільтра."""

    def _state(self, user_id=544675510):
        return {
            'user_id': user_id, 'min_rooms': None, 'max_rooms': None,
            'min_area_m2': None, 'max_area_m2': None,
            'min_price_eur': None, 'max_price_eur': 800,
        }

    def _congrats_calls(self, bot):
        return [
            call for call in bot.send_message.call_args_list
            if 'молодець' in call.kwargs.get('text', '')
        ]

    def test_first_filter_sends_the_congrats_message(self):
        message = FakeMessage(text='', user_id=544675510)
        context = SimpleNamespace(user_data={}, bot_data={}, bot=mock.Mock())

        with mock.patch('user_handlers.housing_monitor.semmelhaack_store.create_filter', return_value=1):
            housing_monitor._finalize_semmelhaack_filter(message, context, self._state())

        calls = self._congrats_calls(context.bot)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].kwargs['chat_id'], 544675510)

    def test_a_second_filter_for_the_same_user_does_not_repeat_it(self):
        message = FakeMessage(text='', user_id=544675510)
        context = SimpleNamespace(user_data={}, bot_data={}, bot=mock.Mock())

        with mock.patch('user_handlers.housing_monitor.semmelhaack_store.create_filter', return_value=1):
            housing_monitor._finalize_semmelhaack_filter(message, context, self._state())
        context.bot.send_message.reset_mock()

        with mock.patch('user_handlers.housing_monitor.schoba_store.create_filter', return_value=2):
            housing_monitor._finalize_schoba_filter(message, context, self._state())

        self.assertEqual(self._congrats_calls(context.bot), [])

    def test_editing_a_filter_does_not_trigger_it(self):
        message = FakeMessage(text='', user_id=544675510)
        context = SimpleNamespace(user_data={}, bot_data={}, bot=mock.Mock())
        state = self._state()
        state['edit_filter_id'] = 7

        with mock.patch('user_handlers.housing_monitor.semmelhaack_store.update_filter', return_value=True):
            housing_monitor._finalize_semmelhaack_filter(message, context, state)

        self.assertEqual(self._congrats_calls(context.bot), [])


class HousingCurrentMatchesTests(unittest.TestCase):
    """«\U0001f50d Квартири, що підходять» — жива перевірка замість світлофорів свіжості."""

    def test_menu_offers_the_current_matches_button(self):
        with mock.patch.object(housing_monitor, 'ADMIN_ID', 312029534), \
             mock.patch.object(housing_monitor, 'ALLOWED_USER_IDS', {544675510}):
            labels = [b.text for row in housing_monitor._menu_keyboard(544675510).inline_keyboard for b in row]

        self.assertIn(housing_monitor.BTN_CURRENT_MATCHES, labels)

    def test_admin_menu_also_offers_the_current_matches_button(self):
        with mock.patch.object(housing_monitor, 'ADMIN_ID', 312029534):
            labels = [b.text for row in housing_monitor._menu_keyboard(312029534).inline_keyboard for b in row]

        self.assertIn(housing_monitor.BTN_CURRENT_MATCHES, labels)

    def test_current_matches_for_a_local_source_filters_active_listings(self):
        filt = {'user_id': 544675510, 'min_rooms': 2}
        listings = [{'listing_key': 'a'}, {'listing_key': 'b'}]
        with mock.patch.object(housing_monitor.semmelhaack_store, 'list_active_listings', return_value=listings), \
             mock.patch.object(
                 housing_monitor.semmelhaack_matching, 'matches_filter',
                 side_effect=lambda listing, _filt: listing['listing_key'] == 'b',
             ):
            result = housing_monitor._current_matches('semmelhaack', filt)

        self.assertEqual(result, [{'listing_key': 'b'}])

    def test_current_matches_for_immowelt_uses_the_live_preview_endpoint(self):
        filt = {
            'districts': ['Golm'], 'min_price_eur': None, 'max_price_eur': 800,
            'min_rooms': 2, 'max_rooms': None, 'min_area_m2': None, 'max_area_m2': None,
        }
        with mock.patch.object(
            housing_monitor, '_preview_criteria',
            return_value={'match_count': 1, 'matches': [{'title': 'X'}]},
        ) as preview:
            result = housing_monitor._current_matches('immowelt', filt)

        preview.assert_called_once_with({
            'districts': ['Golm'], 'min_price_eur': None, 'max_price_eur': 800,
            'min_rooms': 2, 'max_rooms': None, 'min_area_m2': None, 'max_area_m2': None,
        })
        self.assertEqual(result, [{'title': 'X'}])

    def test_match_line_falls_back_to_the_propotsdam_portal_when_theres_no_direct_link(self):
        # ProPotsdam's easysquare portal is a JS SPA - there's rarely a
        # per-listing detail_url, so the fallback needs a real destination.
        line = housing_monitor._match_line('propotsdam', {
            'title': 'Helle 3-Raum-Wohnung!', 'district': 'Waldstadt 2',
            'rooms': 3, 'area_m2': 54, 'total_rent_eur': 650.4,
        })

        self.assertIn('Helle 3-Raum-Wohnung!', line)
        self.assertIn('Waldstadt 2', line)
        self.assertIn('650.4 €', line)
        self.assertIn(housing_monitor.propotsdam_parser.PORTAL_URL, line)

    def test_show_current_matches_reports_a_total_and_the_notification_reassurance(self):
        context = SimpleNamespace(bot=mock.Mock())
        query = SimpleNamespace(data='housing:current_matches', answer=mock.Mock())
        update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=544675510))
        filters = [{'source': 'semmelhaack', 'filter_id': 4, 'title': 'до 800 €'}]

        with mock.patch.object(housing_monitor, 'is_allowed', return_value=True), \
             mock.patch.object(housing_monitor, 'user_filters', return_value=filters), \
             mock.patch.object(housing_monitor, '_current_matches', return_value=[
                 {'title': 'Nice flat', 'rooms': 2, 'area_m2': 50, 'price_eur': 700, 'detail_url': 'https://example.test/1'},
             ]):
            housing_monitor.show_current_matches(update, context)

        texts = [call.kwargs.get('text', '') for call in context.bot.send_message.call_args_list]
        self.assertTrue(any('Квартири, що підходять зараз: 1' in t for t in texts))
        self.assertTrue(any('Nice flat' in t for t in texts))
        self.assertTrue(any('одразу напишемо вам сюди' in t for t in texts))

    def test_show_current_matches_asks_to_add_a_filter_first_when_there_are_none(self):
        context = SimpleNamespace(bot=mock.Mock())
        query = SimpleNamespace(data='housing:current_matches', answer=mock.Mock())
        update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=544675510))

        with mock.patch.object(housing_monitor, 'is_allowed', return_value=True), \
             mock.patch.object(housing_monitor, 'user_filters', return_value=[]):
            housing_monitor.show_current_matches(update, context)

        text = context.bot.send_message.call_args.kwargs['text']
        self.assertIn('немає жодного фільтра', text)

    def test_show_current_matches_lists_sources_that_have_no_filter_yet(self):
        # Regression: someone with filters on only 3 of the 8 sources read
        # the report as buggy/selective, not realizing the other 5 sources
        # simply had no filter to check at all.
        context = SimpleNamespace(bot=mock.Mock())
        query = SimpleNamespace(data='housing:current_matches', answer=mock.Mock())
        update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=5115109366))
        filters = [
            {'source': 'kleinanzeigen', 'filter_id': 4, 'title': 'Kleinanzeigen: ...'},
            {'source': 'locals', 'filter_id': 4, 'title': 'locals: ...'},
            {'source': 'karlmarx', 'filter_id': 6, 'title': 'Karl Marx: ...'},
        ]

        with mock.patch.object(housing_monitor, 'is_allowed', return_value=True), \
             mock.patch.object(housing_monitor, 'user_filters', return_value=filters), \
             mock.patch.object(housing_monitor, '_current_matches', return_value=[]):
            housing_monitor.show_current_matches(update, context)

        footer = context.bot.send_message.call_args_list[-1].kwargs.get('text', '')
        self.assertIn('Фільтра ще немає на', footer)
        self.assertIn('Immowelt', footer)
        self.assertIn('ProPotsdam', footer)
        self.assertIn('SEMMELHAACK', footer)
        self.assertIn('SCHOBA', footer)
        self.assertIn('ImmoTeam/alpha', footer)
        self.assertNotIn('Kleinanzeigen', footer)
        self.assertNotIn('Karl Marx', footer)

    def test_show_current_matches_omits_the_missing_note_once_every_source_has_a_filter(self):
        context = SimpleNamespace(bot=mock.Mock())
        query = SimpleNamespace(data='housing:current_matches', answer=mock.Mock())
        update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=544675510))
        filters = [
            {'source': source, 'filter_id': index, 'title': 'x'}
            for index, source in enumerate(housing_monitor.ALL_HOUSING_SOURCES, start=1)
        ]

        with mock.patch.object(housing_monitor, 'is_allowed', return_value=True), \
             mock.patch.object(housing_monitor, 'user_filters', return_value=filters), \
             mock.patch.object(housing_monitor, '_current_matches', return_value=[]):
            housing_monitor.show_current_matches(update, context)

        footer = context.bot.send_message.call_args_list[-1].kwargs.get('text', '')
        self.assertNotIn('Фільтра ще немає', footer)


class HousingCoopSubscriptionTests(unittest.TestCase):
    """Gewoba/WBG 1903/WBG «Daheim» - subscribe-only, no rooms/price/area
    criteria yet (see CoopWatchdogFilter's docstring for why)."""

    def _query_update(self, data, user_id=544675510):
        query = SimpleNamespace(data=data, answer=mock.Mock(), edit_message_text=mock.Mock())
        return SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=user_id))

    def test_menu_offers_the_coops_button_for_self_service_users(self):
        with mock.patch.object(housing_monitor, 'ADMIN_ID', 312029534), \
             mock.patch.object(housing_monitor, 'ALLOWED_USER_IDS', {544675510}):
            labels = [b.text for row in housing_monitor._menu_keyboard(544675510).inline_keyboard for b in row]

        self.assertIn(housing_monitor.BTN_COOPS, labels)

    def test_admin_menu_also_offers_the_coops_button(self):
        with mock.patch.object(housing_monitor, 'ADMIN_ID', 312029534):
            labels = [b.text for row in housing_monitor._menu_keyboard(312029534).inline_keyboard for b in row]

        self.assertIn(housing_monitor.BTN_COOPS, labels)

    def test_all_eleven_sources_are_listed(self):
        self.assertEqual(len(housing_monitor.ALL_HOUSING_SOURCES), 11)
        for key in ('gewoba', 'wbg1903', 'wbg_daheim'):
            self.assertIn(key, housing_monitor.ALL_HOUSING_SOURCES)

    def test_toggling_on_subscribes_and_toggling_off_pauses(self):
        # Isolated in-memory DB - this must NOT touch the real coop_watchdog_filter
        # table, which has real users' subscriptions in it.
        engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
        Base.metadata.create_all(engine)
        original_session = housing_monitor.coop_watchdog_store.DBSession
        housing_monitor.coop_watchdog_store.DBSession = sessionmaker(bind=engine)
        try:
            update_on = self._query_update('housing:coop_toggle:gewoba')
            context = SimpleNamespace(bot=mock.Mock())

            with mock.patch.object(housing_monitor, 'ADMIN_ID', 312029534), \
                 mock.patch.object(housing_monitor, 'ALLOWED_USER_IDS', {544675510}):
                housing_monitor.handle_callback(update_on, context)

            subs = housing_monitor.coop_watchdog_store.list_filters(user_id=544675510)
            self.assertEqual(len(subs), 1)
            self.assertEqual(subs[0]['coop_key'], 'gewoba')
            self.assertTrue(subs[0]['active'])

            update_off = self._query_update('housing:coop_toggle:gewoba')
            with mock.patch.object(housing_monitor, 'ADMIN_ID', 312029534), \
                 mock.patch.object(housing_monitor, 'ALLOWED_USER_IDS', {544675510}):
                housing_monitor.handle_callback(update_off, context)

            subs = housing_monitor.coop_watchdog_store.list_filters(user_id=544675510)
            self.assertFalse(subs[0]['active'])
        finally:
            housing_monitor.coop_watchdog_store.DBSession = original_session
            engine.dispose()

    def test_a_locked_out_user_cannot_toggle(self):
        engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
        Base.metadata.create_all(engine)
        original_session = housing_monitor.coop_watchdog_store.DBSession
        housing_monitor.coop_watchdog_store.DBSession = sessionmaker(bind=engine)
        try:
            update = self._query_update('housing:coop_toggle:gewoba', user_id=999999)
            context = SimpleNamespace(bot=mock.Mock())

            with mock.patch.object(housing_monitor, 'ADMIN_ID', 312029534), \
                 mock.patch.object(housing_monitor, 'ALLOWED_USER_IDS', set()), \
                 mock.patch.object(housing_monitor, 'is_allowed', return_value=False):
                housing_monitor.handle_callback(update, context)

            self.assertEqual(housing_monitor.coop_watchdog_store.list_filters(user_id=999999), [])
        finally:
            housing_monitor.coop_watchdog_store.DBSession = original_session
            engine.dispose()

    def test_user_filters_includes_active_coop_subscriptions_with_the_right_source(self):
        with mock.patch.object(housing_monitor, '_all_immowelt_filters', return_value=[]), \
             mock.patch.object(housing_monitor.propotsdam_store, 'list_filters', return_value=[]), \
             mock.patch.object(housing_monitor.semmelhaack_store, 'list_filters', return_value=[]), \
             mock.patch.object(housing_monitor.schoba_store, 'list_filters', return_value=[]), \
             mock.patch.object(housing_monitor.regiomakler_store, 'list_filters', return_value=[]), \
             mock.patch.object(housing_monitor.kleinanzeigen_store, 'list_filters', return_value=[]), \
             mock.patch.object(housing_monitor.locals_store, 'list_filters', return_value=[]), \
             mock.patch.object(housing_monitor.karlmarx_store, 'list_filters', return_value=[]), \
             mock.patch.object(
                 housing_monitor.coop_watchdog_store, 'list_filters',
                 return_value=[{'filter_id': 3, 'user_id': 544675510, 'coop_key': 'wbg1903',
                                 'title': 'WBG 1903 Potsdam', 'active': True}],
             ):
            filters = housing_monitor.user_filters(544675510)

        self.assertEqual(len(filters), 1)
        self.assertEqual(filters[0]['source'], 'wbg1903')
        self.assertEqual(housing_monitor.SOURCE_LABEL[filters[0]['source']], 'WBG 1903 Potsdam')

    def test_admin_panel_lists_coop_subscriptions_with_distinct_prefixes(self):
        with mock.patch.object(housing_monitor, '_all_immowelt_filters', return_value=[]), \
             mock.patch.object(housing_monitor.propotsdam_store, 'list_filters', return_value=[]), \
             mock.patch.object(housing_monitor.semmelhaack_store, 'list_filters', return_value=[]), \
             mock.patch.object(housing_monitor.schoba_store, 'list_filters', return_value=[]), \
             mock.patch.object(housing_monitor.regiomakler_store, 'list_filters', return_value=[]), \
             mock.patch.object(housing_monitor.kleinanzeigen_store, 'list_filters', return_value=[]), \
             mock.patch.object(housing_monitor.locals_store, 'list_filters', return_value=[]), \
             mock.patch.object(housing_monitor.karlmarx_store, 'list_filters', return_value=[]), \
             mock.patch.object(
                 housing_monitor.coop_watchdog_store, 'list_filters',
                 return_value=[
                     {'filter_id': 1, 'user_id': 544675510, 'coop_key': 'gewoba', 'title': 'Gewoba eG Babelsberg', 'active': True},
                     {'filter_id': 2, 'user_id': 544675510, 'coop_key': 'wbg1903', 'title': 'WBG 1903 Potsdam', 'active': True},
                     {'filter_id': 3, 'user_id': 544675510, 'coop_key': 'wbg_daheim', 'title': 'WBG „Daheim" eG', 'active': False},
                 ],
             ):
            rows = housing_monitor._admin_rows()

        labels = {row['label'] for row in rows}
        self.assertEqual(labels, {'G#1', 'W#2', 'D#3'})
        paused = next(row for row in rows if row['label'] == 'D#3')
        self.assertIn('призупинено', paused['title'])


class HousingLanguageSwitcherTests(unittest.TestCase):
    def _query_update(self, data, user_id=999999):
        query = SimpleNamespace(data=data, answer=mock.Mock(), edit_message_text=mock.Mock())
        return SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=user_id))

    def setUp(self):
        self.engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.original_session = housing_monitor.user_settings_store.DBSession
        housing_monitor.user_settings_store.DBSession = sessionmaker(bind=self.engine)

    def tearDown(self):
        housing_monitor.user_settings_store.DBSession = self.original_session
        self.engine.dispose()

    def test_menu_offers_a_language_button(self):
        labels = [b.text for row in housing_monitor._menu_keyboard(999999).inline_keyboard for b in row]

        self.assertIn("🌐 Мова / Язык / Sprache", labels)

    def test_opening_the_picker_shows_all_three_languages(self):
        update = self._query_update('housing:lang:menu')
        context = SimpleNamespace()

        housing_monitor.handle_callback(update, context)

        update.callback_query.answer.assert_called_once()
        keyboard = update.callback_query.edit_message_text.call_args.kwargs["reply_markup"]
        labels = [b.text for row in keyboard.inline_keyboard for b in row]
        self.assertIn("🇺🇦 Українська", labels)
        self.assertIn("🇷🇺 Русский", labels)
        self.assertIn("🇩🇪 Deutsch", labels)

    def test_picking_a_language_stores_it(self):
        update = self._query_update('housing:lang:set:de', user_id=777)
        context = SimpleNamespace()

        housing_monitor.handle_callback(update, context)

        self.assertEqual(housing_monitor.user_settings_store.get_language(777), 'de')

    def test_an_unsupported_language_code_is_ignored(self):
        update = self._query_update('housing:lang:set:fr', user_id=777)
        context = SimpleNamespace()

        housing_monitor.handle_callback(update, context)

        self.assertEqual(housing_monitor.user_settings_store.get_language(777), 'uk')


class HousingTranslationSmokeTests(unittest.TestCase):
    """Not a full duplicate of every uk-language assertion above - just enough
    per converted screen to prove lang actually reaches the rendered text."""

    def test_relative_time_in_russian_and_german(self):
        base = datetime(2026, 8, 13, 12, 0, 0, tzinfo=housing_monitor.BERLIN_TZ)
        with mock.patch.object(housing_monitor, '_now_berlin', return_value=base):
            self.assertEqual(
                housing_monitor._relative_time((base - timedelta(minutes=5)).isoformat(), lang='ru'),
                '5 мин назад',
            )
            self.assertEqual(
                housing_monitor._relative_time((base - timedelta(minutes=5)).isoformat(), lang='de'),
                'vor 5 Min.',
            )

    def test_status_lines_in_russian_and_german(self):
        with mock.patch.object(housing_monitor, '_all_immowelt_filters', return_value=[]), \
             mock.patch.object(housing_monitor.propotsdam_store, 'latest_status', return_value=None), \
             mock.patch.object(housing_monitor.semmelhaack_store, 'latest_status', return_value=None), \
             mock.patch.object(housing_monitor.schoba_store, 'latest_status', return_value=None), \
             mock.patch.object(housing_monitor.regiomakler_store, 'latest_status', return_value=None), \
             mock.patch.object(housing_monitor.kleinanzeigen_store, 'latest_status', return_value=None), \
             mock.patch.object(housing_monitor.locals_store, 'latest_status', return_value=None), \
             mock.patch.object(housing_monitor.karlmarx_store, 'latest_status', return_value=None), \
             mock.patch.object(housing_monitor.coop_watchdog_store, 'get_status', return_value={}):
            ru_lines = '\n'.join(housing_monitor._status_lines(lang='ru'))
            de_lines = '\n'.join(housing_monitor._status_lines(lang='de'))

        self.assertIn('проверка ещё не запускалась', ru_lines)
        self.assertIn('noch nicht geprüft', de_lines)

    def test_menu_screen_in_russian_and_german(self):
        with mock.patch.object(housing_monitor, 'ADMIN_ID', 312029534), \
             mock.patch.object(housing_monitor, 'ALLOWED_USER_IDS', {544675510}), \
             mock.patch.object(housing_monitor, '_status_lines', return_value=[]), \
             mock.patch.object(housing_monitor, 'user_filters', return_value=[]):
            ru_text = housing_monitor._render_menu(544675510, lang='ru')
            de_text = housing_monitor._render_menu(544675510, lang='de')
            ru_labels = [b.text for row in housing_monitor._menu_keyboard(544675510, lang='ru').inline_keyboard for b in row]
            de_labels = [b.text for row in housing_monitor._menu_keyboard(544675510, lang='de').inline_keyboard for b in row]

        self.assertIn('Мониторинг жилья', ru_text)
        self.assertIn('Wohnungs-Monitoring', de_text)
        self.assertIn('➕ Добавить фильтр', ru_labels)
        self.assertIn('➕ Filter hinzufügen', de_labels)

    def test_locked_screen_in_russian_and_german(self):
        ru_text = housing_monitor.i18n.t('housing.locked.text', 'ru')
        de_text = housing_monitor.i18n.t('housing.locked.text', 'de')

        self.assertIn('Мониторинг жилья в Потсдаме', ru_text)
        self.assertIn('Wohnungs-Monitoring in Potsdam', de_text)

    def test_coops_screen_in_russian_and_german(self):
        with mock.patch.object(housing_monitor, '_coop_subscription_state', return_value={}):
            ru_text = housing_monitor._coops_text(544675510, lang='ru')
            de_text = housing_monitor._coops_text(544675510, lang='de')
            ru_labels = [
                b.text for row in housing_monitor._coops_keyboard(544675510, lang='ru').inline_keyboard for b in row
            ]

        self.assertIn('Кооперативы (Gewoba/WBG)', ru_text)
        self.assertIn('Genossenschaften (Gewoba/WBG)', de_text)
        self.assertIn('⬅ К мониторингу', ru_labels)

    def test_wizard_field_prompt_in_russian_and_german(self):
        ru = housing_monitor._field_prompt({}, housing_monitor.IMMOWELT_CRITERIA_FIELDS, 'min_rooms', lang='ru')
        de = housing_monitor._field_prompt({}, housing_monitor.IMMOWELT_CRITERIA_FIELDS, 'min_rooms', lang='de')

        self.assertIn('Минимальное количество комнат', ru)
        self.assertIn('Mindestanzahl Zimmer', de)

    def test_wizard_recap_uses_the_translated_label(self):
        state = {'min_rooms': 2}
        ru = housing_monitor._field_prompt(state, housing_monitor.IMMOWELT_CRITERIA_FIELDS, 'max_rooms', lang='ru')

        self.assertIn('Комнаты: минимум (от): 2', ru)
        self.assertIn('Максимальное количество комнат', ru)

    def test_edit_flow_first_question_in_german(self):
        query = SimpleNamespace(answer=mock.Mock(), edit_message_text=mock.Mock())
        update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=777))
        context = SimpleNamespace(user_data={})

        with mock.patch.object(housing_monitor, 'ADMIN_ID', 312029534), \
             mock.patch.object(housing_monitor, 'ALLOWED_USER_IDS', {777}), \
             mock.patch.object(housing_monitor, '_own_filter', return_value={'min_rooms': None}), \
             mock.patch.object(housing_monitor.user_settings_store, 'get_language', return_value='de'):
            housing_monitor.start_semmelhaack_edit_flow(update, context, 5)

        text = query.edit_message_text.call_args[0][0]
        self.assertIn('Mindestanzahl Zimmer', text)

    def test_describe_criteria_in_russian_and_german(self):
        criteria = {'districts': [], 'min_rooms': 2, 'max_rooms': 4, 'min_price_eur': 600, 'max_price_eur': 1200}

        ru = housing_monitor._describe_criteria(criteria, lang='ru')
        de = housing_monitor._describe_criteria(criteria, lang='de')

        self.assertIn('все районы', ru)
        self.assertIn('комн.', ru)
        self.assertIn('alle Stadtteile', de)
        self.assertIn('Zi.', de)

    def test_finalize_semmelhaack_filter_in_german(self):
        message = FakeMessage(user_id=777)
        context = SimpleNamespace(user_data={}, bot_data={})
        state = {
            'user_id': 777, 'min_rooms': 2, 'max_rooms': 4,
            'min_area_m2': 50, 'max_area_m2': 90, 'min_price_eur': 600, 'max_price_eur': 1200,
        }

        with mock.patch.object(housing_monitor.semmelhaack_store, 'create_filter', return_value=9), \
             mock.patch.object(housing_monitor, '_offer_recent_matches'), \
             mock.patch.object(housing_monitor, '_maybe_send_first_filter_congrats'), \
             mock.patch.object(housing_monitor.user_settings_store, 'get_language', return_value='de'):
            housing_monitor._finalize_semmelhaack_filter(message, context, state)

        text = message.replies[-1][0]
        self.assertIn('Filter SEMMELHAACK hinzugefügt', text)
        self.assertIn('ID: S9', text)


if __name__ == '__main__':
    unittest.main()
