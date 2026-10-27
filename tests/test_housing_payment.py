"""«⚡ Підписка»: місяці -> сума в гривнях і карта -> квитанція -> адмін
дозволяє одним натиском (27.10.2026)."""

import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database import Base, HousingAccessUser, HousingPayment
from user_handlers import housing_monitor, housing_payment
from user_jobs import housing_access_store, housing_tier, user_settings_store

ADMIN = 999
USER = 777


def _user(user_id=USER):
    return SimpleNamespace(id=user_id, first_name="Olena", last_name="", username="olena")


def _callback(data, user_id=USER):
    query = mock.MagicMock()
    query.data = data
    return SimpleNamespace(callback_query=query, effective_user=_user(user_id), effective_message=query.message)


def _photo(user_id=USER, caption=None):
    message = mock.MagicMock()
    message.chat_id = user_id
    message.message_id = 55
    message.caption = caption
    return SimpleNamespace(callback_query=None, effective_user=_user(user_id), effective_message=message)


class HousingPaymentTest(unittest.TestCase):
    MODULES = (housing_payment, housing_access_store, housing_tier, user_settings_store)

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
            mock.patch.object(housing_payment, 'eur_uah_rate', return_value=48.3),
            mock.patch.object(housing_monitor, '_set_all_filters_active_for_user'),
            mock.patch.object(housing_monitor, '_notify_user_access_granted'),
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

    def _session(self):
        return sessionmaker(bind=self.engine)()

    def _payments(self):
        session = self._session()
        try:
            return [(p.months, p.amount_uah, p.status) for p in session.query(HousingPayment).order_by(HousingPayment.id)]
        finally:
            session.close()

    def _pay_and_send_receipt(self, months=3):
        housing_payment.handle_callback(_callback(f"housing:pay:m:{months}"), self.context)
        housing_payment.handle_receipt(_photo(caption="оплатила"), self.context)
        return self._session().query(HousingPayment).order_by(HousingPayment.id.desc()).first().id

    def test_amount_is_rounded_up_to_tens_of_hryvnias(self):
        self.assertEqual(housing_payment.amount_uah(1, 48.3), 250)    # 241.5
        self.assertEqual(housing_payment.amount_uah(6, 48.3), 1450)   # 1449
        self.assertEqual(housing_payment.amount_uah(2, 50.0), 500)

    def test_fixed_rate_gives_250_hryvnias_a_month(self):
        self.patches[3].stop()
        try:
            self.assertEqual(housing_payment.eur_uah_rate(), 50)
            self.assertEqual(housing_payment.amount_uah(1, housing_payment.eur_uah_rate()), 250)
            self.assertEqual(housing_payment._format_rate(50.0), "50")
        finally:
            self.patches[3].start()

    def test_picker_offers_one_to_six_months_with_sums(self):
        update = _callback("housing:pay:start")
        self.assertTrue(housing_payment.handle_callback(update, self.context))
        keyboard = update.callback_query.edit_message_text.call_args.kwargs["reply_markup"]
        buttons = [b for row in keyboard.inline_keyboard for b in row]
        months = [b.callback_data for b in buttons if b.callback_data.startswith("housing:pay:m:")]
        self.assertEqual(months, [f"housing:pay:m:{n}" for n in range(1, 7)])
        self.assertIn("250", buttons[0].text)

    def test_month_choice_shows_card_and_sum(self):
        update = _callback("housing:pay:m:2")
        housing_payment.handle_callback(update, self.context)
        text = update.callback_query.edit_message_text.call_args.args[0]
        self.assertIn("<code>4111 1111 1111 1111</code>", text)
        self.assertIn("Тарасюк", text)
        self.assertIn("490", text)  # 2 × 5 € × 48.3 = 483 -> 490
        self.assertEqual(self._payments(), [(2, 490, "awaiting_receipt")])

    def test_new_choice_replaces_the_previous_unpaid_one(self):
        housing_payment.handle_callback(_callback("housing:pay:m:1"), self.context)
        housing_payment.handle_callback(_callback("housing:pay:m:4"), self.context)
        self.assertEqual(self._payments(), [(1, 250, "cancelled"), (4, 970, "awaiting_receipt")])

    def test_receipt_goes_to_admin_with_approve_buttons(self):
        payment_id = self._pay_and_send_receipt(months=3)
        copy = self.context.bot.copy_message.call_args.kwargs
        self.assertEqual(copy["chat_id"], ADMIN)
        self.assertIn("3 міс.", copy["caption"])
        self.assertIn("730 грн", copy["caption"])
        self.assertIn("оплатила", copy["caption"])
        data = [b.callback_data for row in copy["reply_markup"].inline_keyboard for b in row if b.callback_data]
        self.assertEqual(data, [f"housing:pay:ok:{payment_id}", f"housing:pay:no:{payment_id}"])
        self.assertEqual(self._payments()[-1][2], "sent")

    def test_photo_without_open_payment_is_ignored(self):
        housing_payment.handle_receipt(_photo(), self.context)
        self.context.bot.copy_message.assert_not_called()

    def test_admin_approval_opens_subscription_for_paid_months(self):
        payment_id = self._pay_and_send_receipt(months=3)
        before = datetime.utcnow()
        update = _callback(f"housing:pay:ok:{payment_id}", user_id=ADMIN)
        housing_payment.handle_callback(update, self.context)
        self.assertTrue(housing_tier.is_premium(USER))
        row = self._session().query(HousingAccessUser).get(USER)
        self.assertGreater(row.expires_at, before + timedelta(days=88))
        self.assertLess(row.expires_at, before + timedelta(days=93))
        self.assertEqual(self._payments()[-1][2], "approved")
        housing_monitor._notify_user_access_granted.assert_called_once()

        # Повторний натиск не додає ще 3 місяці.
        housing_payment.handle_callback(_callback(f"housing:pay:ok:{payment_id}", user_id=ADMIN), self.context)
        self.assertEqual(self._session().query(HousingAccessUser).get(USER).expires_at, row.expires_at)

    def test_renewal_extends_from_current_expiry(self):
        current_end = datetime.utcnow() + timedelta(days=10)
        housing_access_store.grant_access(USER, "Olena", expires_at=current_end)
        payment_id = self._pay_and_send_receipt(months=1)
        housing_payment.handle_callback(_callback(f"housing:pay:ok:{payment_id}", user_id=ADMIN), self.context)
        row = self._session().query(HousingAccessUser).get(USER)
        self.assertGreater(row.expires_at, current_end + timedelta(days=27))

    def test_only_admin_can_approve(self):
        payment_id = self._pay_and_send_receipt()
        housing_payment.handle_callback(_callback(f"housing:pay:ok:{payment_id}", user_id=USER), self.context)
        self.assertFalse(housing_tier.is_premium(USER))
        self.assertEqual(self._payments()[-1][2], "sent")

    def test_rejection_tells_the_person_and_offers_retry(self):
        payment_id = self._pay_and_send_receipt()
        housing_payment.handle_callback(_callback(f"housing:pay:no:{payment_id}", user_id=ADMIN), self.context)
        self.assertFalse(housing_tier.is_premium(USER))
        self.assertEqual(self._payments()[-1][2], "rejected")
        sent = self.context.bot.send_message.call_args.kwargs
        self.assertEqual(sent["chat_id"], USER)
        self.assertEqual(sent["reply_markup"].inline_keyboard[0][0].callback_data, "housing:pay:start")

    def test_text_right_after_the_card_asks_for_a_screenshot(self):
        housing_payment.handle_callback(_callback("housing:pay:m:1"), self.context)
        message = _photo()
        self.assertTrue(housing_payment.remind_receipt(message, self.context))
        housing_payment.handle_receipt(_photo(), self.context)
        # Квитанцію вже надіслано - звичайний текст більше не перехоплюємо.
        self.assertFalse(housing_payment.remind_receipt(_photo(), self.context))

    def test_subscribe_button_opens_the_picker_when_card_is_set(self):
        update = _callback("housing:access_request")
        with mock.patch.object(housing_payment, 'show_months') as show:
            housing_monitor.request_access(update, self.context)
        show.assert_called_once()
        self.context.bot.send_message.assert_not_called()


if __name__ == '__main__':
    unittest.main()
