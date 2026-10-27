"""«☕ Підтримати проєкт», «✅ Я записався» і розклад посольства за кількістю
підписників (27.10.2026)."""

import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database import Base, EqueueSubscription
from user_handlers import econsul_monitor, equeue_monitor, housing_payment, support
from user_jobs import user_settings_store

ADMIN = 999
USER = 777


def _user(user_id=USER):
    return SimpleNamespace(id=user_id, first_name="Olena", last_name="", username="olena", full_name="Olena")


def _callback(data, user_id=USER):
    query = mock.MagicMock()
    query.data = data
    query.from_user = _user(user_id)
    return SimpleNamespace(callback_query=query, effective_user=_user(user_id), effective_message=query.message)


def _text(text, user_id=USER):
    message = mock.MagicMock()
    message.text = text
    return SimpleNamespace(callback_query=None, effective_user=_user(user_id), message=message, effective_message=message)


class _Base(unittest.TestCase):
    MODULES = (econsul_monitor, equeue_monitor, user_settings_store)

    def setUp(self):
        self.engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self._saved = [(m, m.DBSession) for m in self.MODULES]
        for module in self.MODULES:
            module.DBSession = sessionmaker(bind=self.engine)
        self.patches = [
            mock.patch.object(housing_payment, 'PAY_CARD', '4111111111111111'),
            mock.patch.object(housing_payment, 'PAY_CARD_HOLDER', 'Тарасюк'),
            mock.patch.object(housing_payment, 'ADMIN_ID', ADMIN),
            mock.patch.object(housing_payment, 'EUR_UAH_RATE', 50.0),
        ]
        for patch in self.patches:
            patch.start()
        self.context = SimpleNamespace(bot=mock.MagicMock(), bot_data={}, user_data={})

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        for module, session in self._saved:
            module.DBSession = session
        self.engine.dispose()

    def _subscribe(self, service, user_id=USER, active=True):
        session = sessionmaker(bind=self.engine)()
        now = datetime.utcnow()
        session.add(EqueueSubscription(
            user_id=user_id, username="", display_name="", service=service, active=active,
            last_status="new", created_at=now, updated_at=now,
        ))
        session.commit()
        session.close()

    def _active(self, prefix):
        session = sessionmaker(bind=self.engine)()
        try:
            return [
                row.service for row in session.query(EqueueSubscription)
                .filter(EqueueSubscription.service.like(prefix + "%"), EqueueSubscription.active == 1)
            ]
        finally:
            session.close()


class SupportTest(_Base):
    def test_amounts_in_euro_with_hryvnias(self):
        update = _callback("support:start")
        support.handle_callback(update, self.context)
        keyboard = update.callback_query.edit_message_text.call_args.kwargs["reply_markup"]
        buttons = [b for row in keyboard.inline_keyboard for b in row]
        data = [b.callback_data for b in buttons]
        self.assertEqual(data[:5], [f"support:a:{eur * 100}" for eur in (1, 2, 3, 5, 10)])
        self.assertIn("support:custom", data)
        self.assertEqual(buttons[0].text, "1 € · 50 грн")
        self.assertEqual(buttons[4].text, "10 € · 500 грн")

    def test_amount_shows_card_and_done_tells_admin(self):
        update = _callback("support:a:300")
        support.handle_callback(update, self.context)
        text = update.callback_query.edit_message_text.call_args.args[0]
        self.assertIn("3 € = 150 грн", text)
        self.assertIn("<code>4111 1111 1111 1111</code>", text)
        self.assertIn("Тарасюк", text)
        support.handle_callback(_callback("support:done:300"), self.context)
        sent = self.context.bot.send_message.call_args.kwargs
        self.assertEqual(sent["chat_id"], ADMIN)
        self.assertIn("3 € = 150 грн", sent["text"])

    def test_custom_amount_is_converted(self):
        support.handle_callback(_callback("support:custom"), self.context)
        update = _text("7,5")
        self.assertTrue(support.handle_text(update, self.context))
        text = update.message.reply_text.call_args.args[0]
        self.assertIn("7,50 € = 375 грн", text)
        # Відповідь прийнято - наступне число вже не сума.
        self.assertFalse(support.handle_text(_text("4"), self.context))

    def test_non_amount_text_is_left_to_other_handlers(self):
        support.handle_callback(_callback("support:custom"), self.context)
        self.assertFalse(support.handle_text(_text("🏠 Житло"), self.context))
        self.assertNotIn(support._STATE_KEY, self.context.user_data)

    def test_custom_amount_waits_only_half_an_hour(self):
        self.context.user_data[support._STATE_KEY] = datetime.utcnow() - timedelta(hours=1)
        self.assertFalse(support.handle_text(_text("5"), self.context))

    def test_parse_eur(self):
        self.assertEqual(support.parse_eur("4"), 400)
        self.assertEqual(support.parse_eur("4.5 €"), 450)
        self.assertEqual(support.parse_eur("10 євро"), 1000)
        self.assertIsNone(support.parse_eur("0,5"))
        self.assertIsNone(support.parse_eur("5000"))
        self.assertIsNone(support.parse_eur("привіт"))

    def test_no_card_no_support_button(self):
        with mock.patch.object(housing_payment, 'PAY_CARD', ''):
            self.assertEqual(support.home_rows(USER), [])


class BookedTest(_Base):
    def test_dp_document_booked_unsubscribes_and_offers_support(self):
        self._subscribe(equeue_monitor.SERVICE_KEY)
        with mock.patch.object(equeue_monitor, 'is_allowed', return_value=True):
            equeue_monitor.handle_callback(_callback("equeue:booked"), self.context)
        self.assertEqual(self._active(equeue_monitor.SERVICE_KEY), [])
        calls = self.context.bot.send_message.call_args_list
        to_user = calls[0].kwargs
        self.assertEqual(to_user["chat_id"], USER)
        self.assertEqual(to_user["reply_markup"].inline_keyboard[0][0].callback_data, "support:start")
        self.assertEqual(calls[1].kwargs["chat_id"], ADMIN)
        self.assertIn("записався", calls[1].kwargs["text"])

    def test_alerts_carry_the_booked_button(self):
        self._subscribe(equeue_monitor.SERVICE_KEY)
        equeue_monitor._notify_available(self.context.bot, [(USER, "none", None)], {})
        keyboard = self.context.bot.send_message.call_args.kwargs["reply_markup"]
        self.assertIn("equeue:booked", [b.callback_data for row in keyboard.inline_keyboard for b in row])

    def test_embassy_booked_unsubscribes_from_every_service(self):
        self._subscribe(econsul_monitor.SUB_PREFIX + "1")
        self._subscribe(econsul_monitor.SUB_PREFIX + "40")
        with mock.patch.object(econsul_monitor, 'is_allowed', return_value=True):
            econsul_monitor.handle_callback(_callback("econsul:booked"), self.context)
        self.assertEqual(self._active(econsul_monitor.SUB_PREFIX), [])
        self.assertEqual(self.context.bot.send_message.call_args_list[0].kwargs["chat_id"], USER)


class EmbassyScheduleTest(_Base):
    def test_hours_follow_the_number_of_people(self):
        self.assertEqual(econsul_monitor.wanted_hours(0), econsul_monitor.SCHEDULE_FEW)
        self.assertEqual(econsul_monitor.wanted_hours(4), [6, 20])
        self.assertEqual(econsul_monitor.wanted_hours(5), [6, 11, 16, 20])

    def test_people_are_counted_once(self):
        self._subscribe(econsul_monitor.SUB_PREFIX + "1", user_id=1)
        self._subscribe(econsul_monitor.SUB_PREFIX + "40", user_id=1)
        self._subscribe(econsul_monitor.SUB_PREFIX + "*", user_id=2)
        self._subscribe(econsul_monitor.SUB_PREFIX + "1", user_id=3, active=False)
        self._subscribe(equeue_monitor.SERVICE_KEY, user_id=4)
        self.assertEqual(econsul_monitor.active_people(), 2)

    def test_schedule_is_pushed_only_on_change(self):
        econsul_monitor._pushed_hours = None
        with mock.patch.object(econsul_monitor, 'push_schedule', return_value=True) as push:
            econsul_monitor.schedule_job(self.context)
            econsul_monitor.schedule_job(self.context)
        push.assert_called_once_with([6, 20])
        econsul_monitor._pushed_hours = None

    def test_no_login_prompts_without_subscribers(self):
        with mock.patch.object(econsul_monitor, 'ADMIN_ID', ADMIN):
            result = econsul_monitor.handle_browser_result(
                self.context.bot, {"source": econsul_monitor.SOURCE, "status": "login_reminder", "login_id": "x"},
            )
            self.assertEqual(result.get("suppressed"), "no subscribers")
            self.context.bot.send_message.assert_not_called()

            self._subscribe(econsul_monitor.SUB_PREFIX + "1")
            self.context.bot.send_message.return_value = SimpleNamespace(message_id=5)
            econsul_monitor.handle_browser_result(
                self.context.bot, {"source": econsul_monitor.SOURCE, "status": "login_reminder", "login_id": "x"},
            )
            self.assertEqual(self.context.bot.send_message.call_args.args[0], ADMIN)


if __name__ == '__main__':
    unittest.main()
