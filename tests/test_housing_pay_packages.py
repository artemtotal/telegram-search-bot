"""Пакеты подписки: 1 месяц 5 €, 3 месяца 12 €, «Поки не знайду житло» 15 €.
Выдача каждого пакета, продление, кнопка «Дозволити» (25.11.2026)."""

import unittest
from datetime import datetime, timedelta
from unittest import mock

import i18n
from database import HousingAccessUser, HousingPayment
from pay_helpers import ADMIN, USER, PayDbCase, callback, photo
from user_jobs import housing_access_store, housing_packages, housing_tier, user_settings_store

PACKAGES = (
    # код, месяцев, евро, гривны при курсе 48,3 (с округлением вверх до 10)
    ("m1", 1, 5, 250),
    ("m3", 3, 12, 580),
    ("until_found", 12, 15, 730),
)


class PackageConfigTest(unittest.TestCase):
    def test_three_packages_with_the_agreed_prices(self):
        self.assertEqual(housing_packages.CODES, ("m1", "m3", "until_found"))
        self.assertEqual([housing_packages.price_eur(c) for c in housing_packages.CODES], [5, 12, 15])
        self.assertEqual([housing_packages.months(c) for c in housing_packages.CODES], [1, 3, 12])

    def test_hryvnias_round_up_to_ten(self):
        for code, _months, _eur, uah in PACKAGES:
            self.assertEqual(housing_packages.amount_uah(code, 48.3), uah)
        self.assertEqual(housing_packages.amount_uah("m1", 50), 250)
        self.assertEqual(housing_packages.amount_uah("m3", 50), 600)
        self.assertEqual(housing_packages.amount_uah("until_found", 50), 750)

    def test_prices_come_from_the_environment(self):
        env = {"HOUSING_PRICE_M1_EUR": "6", "HOUSING_PRICE_M3_EUR": "14", "HOUSING_PRICE_UNTIL_FOUND_EUR": "20"}
        with mock.patch.dict("os.environ", env):
            import importlib
            module = importlib.reload(housing_packages)
            try:
                self.assertEqual([module.price_eur(c) for c in module.CODES], [6, 14, 20])
            finally:
                pass
        importlib.reload(housing_packages)
        self.assertEqual(housing_packages.price_eur("m1"), 5)

    def test_garbage_price_falls_back_to_default(self):
        with mock.patch.dict("os.environ", {"HOUSING_PRICE_M3_EUR": "abc", "HOUSING_PRICE_M1_EUR": "-3"}):
            import importlib
            module = importlib.reload(housing_packages)
            self.assertEqual((module.price_eur("m1"), module.price_eur("m3")), (5, 12))
        importlib.reload(housing_packages)

    def test_old_month_buttons_map_to_packages(self):
        self.assertEqual(housing_packages.for_months(1), "m1")
        self.assertEqual(housing_packages.for_months(3), "m3")
        self.assertIsNone(housing_packages.for_months(2))
        self.assertIsNone(housing_packages.for_months(6))

    def test_effective_months_prefers_the_package(self):
        self.assertEqual(housing_packages.effective_months("until_found", 12), 12)
        self.assertEqual(housing_packages.effective_months("m3", 99), 3)
        self.assertEqual(housing_packages.effective_months(None, 5), 5)


class PackagePaymentTest(PayDbCase):
    def _buy(self, code, user_id=USER):
        housing_payment = self.housing_payment
        housing_payment.handle_callback(callback(f"housing:pay:p:{code}", user_id), self.context)
        housing_payment.handle_receipt(photo(user_id, caption="оплата"), self.context)
        row = self.session().query(HousingPayment).order_by(HousingPayment.id.desc()).first()
        return row.id

    def _approve(self, payment_id):
        self.housing_payment.handle_callback(callback(f"housing:pay:ok:{payment_id}", ADMIN), self.context)

    def _payment(self, payment_id):
        return self.session().query(HousingPayment).get(payment_id)

    def _expiry(self, user_id=USER):
        return self.session().query(HousingAccessUser).get(user_id).expires_at

    def test_screen_offers_exactly_three_packages_with_sums(self):
        update = callback("housing:pay:start")
        self.assertTrue(self.housing_payment.handle_callback(update, self.context))
        args = update.callback_query.edit_message_text.call_args
        buttons = self.buttons(args.kwargs["reply_markup"])
        codes = [b.callback_data for b in buttons if (b.callback_data or "").startswith("housing:pay:p:")]
        self.assertEqual(codes, ["housing:pay:p:m1", "housing:pay:p:m3", "housing:pay:p:until_found"])
        self.assertIn("5 €", buttons[0].text)
        self.assertIn("250 грн", buttons[0].text)
        self.assertIn("12 €", buttons[1].text)
        self.assertIn("580 грн", buttons[1].text)
        self.assertIn("15 €", buttons[2].text)
        self.assertIn("730 грн", buttons[2].text)
        self.assertFalse([b for b in buttons if (b.callback_data or "").startswith("housing:pay:m:")])
        text = args.args[0]
        self.assertIn("Поки не знайду житло", text)
        self.assertIn("щонайбільше рік", text)
        self.assertIn("гривнева", text)

    def test_cash_hint_names_the_until_found_package(self):
        update = callback("housing:pay:start")
        self.housing_payment.handle_callback(update, self.context)
        text = update.callback_query.edit_message_text.call_args.args[0]
        self.assertIn("за пакет «Поки не знайду» (15 €)", text)
        self.assertNotIn("від 6 місяців", text)
        self.assertTrue(any((b.url or "").startswith("tg://user?id=") for b in self.buttons(
            update.callback_query.edit_message_text.call_args.kwargs["reply_markup"])))

    def test_each_package_creates_the_right_payment_row(self):
        for code, months, eur, uah in PACKAGES:
            with self.subTest(package=code):
                update = callback(f"housing:pay:p:{code}")
                self.housing_payment.handle_callback(update, self.context)
                row = self.session().query(HousingPayment).order_by(HousingPayment.id.desc()).first()
                self.assertEqual(
                    (row.package, row.months, row.amount_eur, row.amount_uah, row.status),
                    (code, months, float(eur), uah, "awaiting_receipt"),
                )
                text = update.callback_query.edit_message_text.call_args.args[0]
                self.assertIn(f"{uah} грн", text)
                self.assertIn("4111 1111 1111 1111", text)

    def test_admin_card_shows_the_package_and_the_button_grants_that_package(self):
        expected_label = {"m1": "1 міс.", "m3": "3 міс.", "until_found": "«Поки не знайду»"}
        for code, _months, eur, uah in PACKAGES:
            with self.subTest(package=code):
                self.bot.reset_mock()
                payment_id = self._buy(code)
                copy = self.bot.copy_message.call_args.kwargs
                self.assertIn(f"Пакет: <b>{expected_label[code]}</b>", copy["caption"])
                self.assertIn(f"{uah} грн", copy["caption"])
                button = copy["reply_markup"].inline_keyboard[0][0]
                self.assertEqual(button.text, f"✅ Дозволити {expected_label[code]}")
                self.assertEqual(button.callback_data, f"housing:pay:ok:{payment_id}")
                self.housing_payment.handle_callback(callback(f"housing:pay:no:{payment_id}", ADMIN), self.context)

    def test_m1_grants_one_month(self):
        payment_id = self._buy("m1")
        before = self.now()
        self._approve(payment_id)
        expected = self.housing_monitor._add_months(before, 1)
        self.assertLess(abs(self._expiry() - expected), timedelta(minutes=2))
        self.assertTrue(housing_tier.is_premium(USER))
        self.assertEqual(self._payment(payment_id).status, "approved")
        self.assertEqual(self._payment(payment_id).package, "m1")

    def test_m3_grants_three_months(self):
        payment_id = self._buy("m3")
        before = self.now()
        self._approve(payment_id)
        expected = self.housing_monitor._add_months(before, 3)
        self.assertLess(abs(self._expiry() - expected), timedelta(minutes=2))
        self.assertEqual(self._payment(payment_id).amount_eur, 12.0)

    def test_until_found_grants_twelve_months_and_explains_it(self):
        payment_id = self._buy("until_found")
        before = self.now()
        self.bot.reset_mock()
        self._approve(payment_id)
        expected = self.housing_monitor._add_months(before, 12)
        self.assertLess(abs(self._expiry() - expected), timedelta(minutes=2))
        self.assertEqual(self._payment(payment_id).amount_eur, 15.0)
        texts = self.sent_texts(USER)
        self.assertTrue(any("Поки не знайду житло" in t and expected.strftime("%d.%m.%Y") in t for t in texts), texts)

    def test_admin_card_after_decision_says_until_when(self):
        payment_id = self._buy("m3")
        update = callback(f"housing:pay:ok:{payment_id}", ADMIN)
        self.housing_payment.handle_callback(update, self.context)
        caption = update.callback_query.edit_message_caption.call_args.kwargs["caption"]
        self.assertIn("Дозволено до", caption)
        self.assertIn("Пакет: <b>3 міс.</b>", caption)

    def test_double_tap_does_not_grant_twice(self):
        payment_id = self._buy("m3")
        self._approve(payment_id)
        first = self._expiry()
        self._approve(payment_id)
        self.assertEqual(self._expiry(), first)

    def test_renewal_extends_from_the_current_end(self):
        current_end = self.now() + timedelta(days=10)
        housing_access_store.grant_access(USER, "Olena", expires_at=current_end)
        for code, months in (("m1", 1), ("m3", 3), ("until_found", 12)):
            with self.subTest(package=code):
                housing_access_store.grant_access(USER, "Olena", expires_at=current_end)
                self.housing_payment.handle_callback(callback(f"housing:pay:p:{code}"), self.context)
                self.housing_payment.handle_receipt(photo(), self.context)
                payment_id = self.session().query(HousingPayment).order_by(HousingPayment.id.desc()).first().id
                self._approve(payment_id)
                self.assertLess(
                    abs(self._expiry() - self.housing_monitor._add_months(current_end, months)), timedelta(seconds=5),
                )

    def test_renewal_on_top_of_a_package_keeps_the_days_already_paid(self):
        first = self._buy("m3")
        self._approve(first)
        end_of_first = self._expiry()
        second = self._buy("m1")
        self._approve(second)
        self.assertLess(abs(self._expiry() - self.housing_monitor._add_months(end_of_first, 1)), timedelta(seconds=5))

    def test_until_found_holder_sees_the_term_instead_of_the_pay_buttons(self):
        payment_id = self._buy("until_found")
        self._approve(payment_id)
        expiry = self._expiry()
        update = callback("housing:pay:start")
        self.housing_payment.handle_callback(update, self.context)
        args = update.callback_query.edit_message_text.call_args
        self.assertIn(expiry.strftime("%d.%m.%Y"), args.args[0])
        self.assertIn("Поки не знайду житло", args.args[0])
        codes = [b.callback_data for b in self.buttons(args.kwargs["reply_markup"])]
        self.assertFalse([c for c in codes if c and c.startswith("housing:pay:p:")])
        # Старая кнопка пакета тоже не заводит новую оплату.
        before = self.session().query(HousingPayment).count()
        self.housing_payment.handle_callback(callback("housing:pay:p:m1"), self.context)
        self.assertEqual(self.session().query(HousingPayment).count(), before)

    def test_old_month_buttons_keep_working_for_one_and_three(self):
        for months, code in ((1, "m1"), (3, "m3")):
            self.housing_payment.handle_callback(callback(f"housing:pay:m:{months}"), self.context)
            row = self.session().query(HousingPayment).order_by(HousingPayment.id.desc()).first()
            self.assertEqual((row.package, row.months), (code, months))

    def test_old_two_month_button_shows_the_packages_instead(self):
        update = callback("housing:pay:m:2")
        self.housing_payment.handle_callback(update, self.context)
        self.assertEqual(self.session().query(HousingPayment).count(), 0)
        text = update.callback_query.edit_message_text.call_args.args[0]
        self.assertIn("Оберіть пакет", text)

    def test_unknown_package_is_ignored(self):
        update = callback("housing:pay:p:forever")
        self.housing_payment.handle_callback(update, self.context)
        self.assertEqual(self.session().query(HousingPayment).count(), 0)

    def test_switching_package_cancels_the_unpaid_choice(self):
        self.housing_payment.handle_callback(callback("housing:pay:p:m1"), self.context)
        self.housing_payment.handle_callback(callback("housing:pay:p:m3"), self.context)
        rows = [(p.package, p.status) for p in self.session().query(HousingPayment).order_by(HousingPayment.id)]
        self.assertEqual(rows, [("m1", "cancelled"), ("m3", "awaiting_receipt")])

    def test_provisional_day_then_package_counts_from_approval(self):
        payment_id = self._buy("m3")  # квитанция -> временный день
        self.assertTrue(housing_tier.is_premium(USER))
        before = self.now()
        self._approve(payment_id)
        self.assertLess(abs(self._expiry() - self.housing_monitor._add_months(before, 3)), timedelta(minutes=2))

    def test_packages_off_brings_back_the_month_picker(self):
        with mock.patch.object(housing_packages, "enabled", return_value=False):
            update = callback("housing:pay:start")
            self.housing_payment.handle_callback(update, self.context)
            buttons = self.buttons(update.callback_query.edit_message_text.call_args.kwargs["reply_markup"])
            months = [b.callback_data for b in buttons if (b.callback_data or "").startswith("housing:pay:m:")]
            self.assertEqual(months, [f"housing:pay:m:{n}" for n in range(1, 7)])
            self.housing_payment.handle_callback(callback("housing:pay:m:2"), self.context)
            row = self.session().query(HousingPayment).order_by(HousingPayment.id.desc()).first()
            self.assertEqual((row.package, row.months, row.amount_uah), (None, 2, 490))

    def test_old_payment_without_package_is_granted_by_its_months(self):
        session = self.session()
        session.add(HousingPayment(
            user_id=USER, display_name="Olena", months=2, amount_eur=10.0, amount_uah=500, eur_uah_rate=50.0,
            status="sent", created_at=self.now(), receipt_at=self.now(),
        ))
        session.commit()
        payment_id = session.query(HousingPayment).first().id
        before = self.now()
        self._approve(payment_id)
        self.assertLess(abs(self._expiry() - self.housing_monitor._add_months(before, 2)), timedelta(minutes=2))

    def test_amounts_are_frozen_when_prices_change_later(self):
        payment_id = self._buy("m1")
        with mock.patch.dict(housing_packages.PRICES_EUR, {"m1": 9}):
            self._approve(payment_id)
        row = self._payment(payment_id)
        self.assertEqual((row.amount_eur, row.amount_uah), (5.0, 250))

    def test_texts_exist_in_all_languages_and_have_no_leftover_placeholders(self):
        for lang in i18n.SUPPORTED_LANGS:
            user_settings_store.set_language(USER, lang)
            update = callback("housing:pay:start")
            self.housing_payment.handle_callback(update, self.context)
            text = update.callback_query.edit_message_text.call_args.args[0]
            self.assertNotIn("{", text, lang)
            self.assertNotIn("[[", text, lang)
            for price in ("5 €", "12 €", "15 €"):
                self.assertIn(price, text, lang)
            self.housing_payment.handle_callback(callback("housing:pay:p:until_found"), self.context)
        for lang in i18n.SUPPORTED_LANGS:
            for code in housing_packages.CODES:
                for key in (f"pay.pkg.name.{code}", f"pay.pkg.term.{code}"):
                    self.assertNotIn("[[", i18n.t(key, lang))

    def test_every_price_text_names_no_fixed_five_euro_a_month(self):
        """Ни в одной строке для человека не осталось «5 €/міс» как единственной цены."""
        for lang in i18n.SUPPORTED_LANGS:
            for key, value in i18n._TRANSLATIONS[lang].items():
                if key.startswith("housing.pay.") or key.startswith("pay."):
                    continue
                for bad in ("5 €/міс", "5 €/мес", "5 €/Monat", "{price} €/міс", "{price} €/мес", "{price} €/Monat"):
                    self.assertNotIn(bad, value, f"{lang}:{key}")


if __name__ == "__main__":
    unittest.main()
