"""Личный отчёт перед концом подписки: только настоящие цифры человека,
нулевые строки не показываются (25.11.2026)."""

import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import mock

import i18n
from database import (
    HousingAccessUser, HousingLetterLog, HousingPayment, HousingSendLog, KleinanzeigenListing,
)
from pay_helpers import ADMIN, USER, PayDbCase
from user_handlers import housing_pay_report
from user_jobs import housing_access_store, user_settings_store


class ReportBase(PayDbCase):
    def setUp(self):
        super().setUp()
        self.t0 = self.now() - timedelta(days=27)   # оплата 27 дней назад, конец через 3 дня
        self.end = self.now() + timedelta(days=3)

    def add_payment(self, user_id=USER, decided_at=None, expires_at=None, package="m1", months=1,
                    provisional=False, status="approved"):
        decided_at = decided_at or self.t0
        session = self.session()
        session.add(HousingPayment(
            user_id=user_id, display_name="Olena", months=months, amount_eur=5.0, amount_uah=250, eur_uah_rate=50.0,
            status=status, created_at=decided_at - timedelta(minutes=30),
            receipt_at=decided_at - timedelta(minutes=20), decided_at=decided_at,
            expires_at=expires_at or self.end, package=package,
            provisional_until=(decided_at - timedelta(minutes=20) + timedelta(days=1)) if provisional else None,
        ))
        session.commit()

    def add_sends(self, count, delayed=False, when=None, source="propotsdam", user_id=USER, prefix="k"):
        session = self.session()
        for index in range(count):
            session.add(HousingSendLog(
                user_id=user_id, source=source, listing_key=f"{prefix}{index}-{delayed}-{source}",
                delayed=delayed, sent_at=when or (self.t0 + timedelta(days=2, minutes=index)),
            ))
        session.commit()

    def add_letters(self, count, when=None, user_id=USER):
        session = self.session()
        for index in range(count):
            session.add(HousingLetterLog(
                user_id=user_id, source="propotsdam", listing_key=f"l{index}", created_at=when or (self.t0 + timedelta(days=3)),
            ))
        session.commit()

    def add_listing(self, key, first_seen, last_seen, active):
        session = self.session()
        session.add(KleinanzeigenListing(
            listing_key=key, title="Wohnung", first_seen_at=first_seen, last_seen_at=last_seen, is_active=active,
        ))
        session.commit()

    def block(self, **kwargs):
        return housing_pay_report.build_block(USER, self.t0, self.now(), "uk", **kwargs)


class PeriodTest(ReportBase):
    def test_period_starts_at_the_last_payment(self):
        self.add_payment(decided_at=self.t0)
        self.assertEqual(housing_pay_report.period_start(USER), self.t0)

    def test_back_to_back_renewals_form_one_period(self):
        first_start = self.t0 - timedelta(days=30)
        self.add_payment(decided_at=first_start, expires_at=self.t0 + timedelta(days=1), months=1)
        self.add_payment(decided_at=self.t0, expires_at=self.end, months=1)
        self.assertEqual(housing_pay_report.period_start(USER), first_start)

    def test_a_gap_between_payments_starts_a_new_period(self):
        self.add_payment(decided_at=self.t0 - timedelta(days=90), expires_at=self.t0 - timedelta(days=60))
        self.add_payment(decided_at=self.t0, expires_at=self.end)
        self.assertEqual(housing_pay_report.period_start(USER), self.t0)

    def test_receipt_time_counts_when_a_provisional_day_was_given(self):
        self.add_payment(provisional=True)
        self.assertEqual(housing_pay_report.period_start(USER), self.t0 - timedelta(minutes=20))

    def test_manual_grant_without_payments_starts_after_the_last_delayed_flat(self):
        self.assertIsNone(housing_pay_report.period_start(USER))
        self.add_sends(2, delayed=True, when=self.t0)
        self.add_sends(3, delayed=False, when=self.t0 + timedelta(days=1))
        start = housing_pay_report.subscription_period(USER)
        self.assertEqual(start, self.t0)
        self.assertEqual(len(housing_pay_report.instant_sends(USER, start, self.now())), 3)

    def test_only_approved_payments_count(self):
        self.add_payment(status="rejected")
        self.assertIsNone(housing_pay_report.period_start(USER))


class NumbersTest(ReportBase):
    def test_counts_only_instant_flats_of_this_person_in_the_period(self):
        self.add_payment()
        self.add_sends(5, delayed=False)
        self.add_sends(3, delayed=True, when=self.t0 - timedelta(days=5))     # раньше - бесплатный уровень
        self.add_sends(4, delayed=False, user_id=555)                         # чужие
        result = self.block()
        self.assertEqual(result["sends"], 5)
        self.assertIn("Квартир одразу: <b>5</b>", result["text"])

    def test_instant_before_the_period_is_not_counted(self):
        self.add_payment()
        self.add_sends(2, delayed=False, when=self.t0 - timedelta(days=1))
        self.add_sends(1, delayed=False)
        self.assertEqual(self.block()["sends"], 1)

    def test_same_flat_sent_twice_is_one_flat(self):
        self.add_payment()
        session = self.session()
        for _ in range(2):
            session.add(HousingSendLog(
                user_id=USER, source="propotsdam", listing_key="same", delayed=False, sent_at=self.t0 + timedelta(days=1),
            ))
        session.commit()
        self.assertEqual(self.block()["sends"], 1)

    def test_letters_line_and_no_hint_for_someone_who_wrote_letters(self):
        self.add_payment()
        self.add_sends(2)
        self.add_letters(3)
        result = self.block()
        self.assertEqual(result["letters"], 3)
        self.assertIn("Листів орендодавцям: <b>3</b>", result["text"])
        self.assertNotIn("✍️ Лист»", result["text"])

    def test_hint_about_letters_only_for_those_who_never_used_them(self):
        self.add_payment()
        self.add_sends(2)
        text = self.block()["text"]
        self.assertIn("«✍️ Лист»", text)
        self.assertNotIn("Листів орендодавцям", text)   # нулевая строка не показывается

    def test_letters_from_before_the_period_still_mean_no_hint(self):
        self.add_payment()
        self.add_sends(1)
        self.add_letters(1, when=self.t0 - timedelta(days=40))
        result = self.block()
        self.assertEqual(result["letters"], 0)
        self.assertNotIn("«✍️ Лист»", result["text"])

    def test_zero_lines_are_never_shown(self):
        self.add_payment()
        self.add_sends(2)
        text = self.block()["text"]
        for zero in (": <b>0</b>", "щонайменше <b>0</b>"):
            self.assertNotIn(zero, text)

    def _vanishing_setup(self):
        self.add_payment()
        base = self.now() - timedelta(days=10)
        # A: прожило 1 час и снято, сканы шли дальше; B: прожило 5 часов; C: ещё висит;
        # D: прожило 1 час, но это самое свежее, что видели на площадке (мог быть сбой).
        self.add_listing("A", base, base + timedelta(hours=1), False)
        self.add_listing("B", base, base + timedelta(hours=5), False)
        self.add_listing("C", base, self.now(), True)
        self.add_listing("fresh", self.now() - timedelta(days=1), self.now(), True)
        session = self.session()
        for key in ("A", "B", "C"):
            session.add(HousingSendLog(
                user_id=USER, source="kleinanzeigen", listing_key=key, delayed=False, sent_at=self.t0 + timedelta(days=1),
            ))
        session.commit()

    def test_vanished_early_counts_only_flats_taken_down_within_the_free_delay(self):
        self._vanishing_setup()
        result = self.block()
        self.assertEqual(result["sends"], 3)
        self.assertEqual(result["vanished"], 1)
        self.assertIn("щонайменше <b>1</b>", result["text"])

    def test_possible_scan_glitch_is_not_counted_as_vanished(self):
        self.add_payment()
        newest = self.now() - timedelta(minutes=5)
        self.add_listing("D", newest - timedelta(hours=1), newest, False)
        session = self.session()
        session.add(HousingSendLog(
            user_id=USER, source="kleinanzeigen", listing_key="D", delayed=False, sent_at=self.t0 + timedelta(days=1),
        ))
        session.commit()
        self.assertEqual(self.block()["vanished"], 0)

    def test_sources_that_cannot_be_checked_give_no_vanished_line(self):
        self.add_payment()
        self.add_sends(4, source="immowelt")
        result = self.block()
        self.assertEqual(result["sends"], 4)
        self.assertEqual(result["vanished"], 0)
        self.assertNotIn("зникли", result["text"])

    def test_flat_keys_of_unknown_listings_give_no_vanished_line(self):
        self.add_payment()
        self.add_sends(2, source="kleinanzeigen")      # объявлений в таблице нет
        self.assertEqual(self.block()["vanished"], 0)


class EmptyPeriodTest(ReportBase):
    def test_nothing_came_and_market_confirms_it(self):
        self.add_payment()
        row = [[SimpleNamespace(text="relax", callback_data="housing:relax:5:price")]]
        with mock.patch.object(housing_pay_report, "relax_for_empty", return_value=(True, row, "Розширити")):
            result = self.block()
        self.assertEqual(result["sends"], 0)
        self.assertIn("нічого не з'явилось", result["text"])
        self.assertIn("Розширити", result["text"])
        self.assertEqual(result["relax_rows"], row)
        self.assertNotIn("Квартир одразу", result["text"])

    def test_long_period_says_how_many_days_the_market_was_checked(self):
        self.add_payment(decided_at=self.now() - timedelta(days=80), expires_at=self.end, months=3)
        with mock.patch.object(housing_pay_report, "relax_for_empty", return_value=(True, [], "")):
            text = housing_pay_report.build_block(USER, self.now() - timedelta(days=80), self.now(), "uk")["text"]
        self.assertIn("За останні 30 дн.", text)

    def test_nothing_claimed_when_market_has_flats_we_did_not_send(self):
        self.add_payment()
        with mock.patch.object(housing_pay_report, "relax_for_empty", return_value=(False, [], "")):
            result = self.block()
        self.assertNotIn("нічого не з'явилось", result["text"])
        self.assertNotIn("Квартир одразу", result["text"])

    def test_relax_buttons_come_from_the_real_relaxation_offers(self):
        self.add_payment()
        search = {"search_id": 5, "members": [("propotsdam", 1)], "criteria": {"max_price_eur": 800}}
        fake_market = mock.MagicMock()
        fake_market.member_filters.return_value = [("propotsdam", {"active": True})]
        with mock.patch("user_jobs.housing_search_store.list_for_user", return_value=[search]), \
                mock.patch("user_handlers.housing_relax.offers", return_value=(0, [("price", 4)])):
            verified, rows, text = housing_pay_report.relax_for_empty(USER, fake_market)
        self.assertTrue(verified)
        self.assertEqual(rows[0][0].callback_data, "housing:relax:5:price")
        self.assertIn("100", rows[0][0].text)
        self.assertIn("+4", rows[0][0].text)
        self.assertTrue(text)

    def test_market_with_flats_is_not_reported_as_empty(self):
        search = {"search_id": 5, "members": [("propotsdam", 1)], "criteria": {"max_price_eur": 800}}
        fake_market = mock.MagicMock()
        fake_market.member_filters.return_value = [("propotsdam", {"active": True})]
        with mock.patch("user_jobs.housing_search_store.list_for_user", return_value=[search]), \
                mock.patch("user_handlers.housing_relax.offers", return_value=(7, [])):
            verified, rows, _text = housing_pay_report.relax_for_empty(USER, fake_market)
        self.assertFalse(verified)
        self.assertEqual(rows, [])

    def test_a_failing_market_never_breaks_the_report(self):
        with mock.patch("user_handlers.housing_relax.Market", side_effect=RuntimeError("immowelt down")):
            self.assertEqual(housing_pay_report.relax_for_empty(USER), (False, [], ""))


class MessagesTest(ReportBase):
    def _subscriber(self, expires=None):
        self.add_payment()
        self.add_sends(6)
        self.add_letters(2)
        housing_access_store.grant_access(USER, "Olena", expires_at=expires or self.end)

    def _package_callbacks(self, markup):
        return [b.callback_data for b in self.buttons(markup) if (b.callback_data or "").startswith("housing:pay:p:")]

    def test_warning_has_real_numbers_the_date_and_three_package_buttons(self):
        self._subscriber()
        text, markup = housing_pay_report.warning_message(USER, self.end)
        self.assertIn(self.end.strftime("%d.%m.%Y"), text)
        self.assertIn("Квартир одразу: <b>6</b>", text)
        self.assertIn("Листів орендодавцям: <b>2</b>", text)
        self.assertEqual(self._package_callbacks(markup), ["housing:pay:p:m1", "housing:pay:p:m3", "housing:pay:p:until_found"])
        labels = [b.text for b in self.buttons(markup)]
        self.assertTrue(any("5 €" in t for t in labels) and any("12 €" in t for t in labels) and any("15 €" in t for t in labels))

    def test_warning_in_every_language(self):
        self._subscriber()
        for lang in i18n.SUPPORTED_LANGS:
            user_settings_store.set_language(USER, lang)
            text, markup = housing_pay_report.warning_message(USER, self.end)
            self.assertNotIn("{", text, lang)
            self.assertNotIn("[[", text, lang)
            self.assertIn("<b>6</b>", text, lang)
            self.assertEqual(len(self._package_callbacks(markup)), 3, lang)

    def test_warning_is_none_when_the_report_is_off_or_card_missing(self):
        self._subscriber()
        with mock.patch.object(housing_pay_report, "enabled", return_value=False):
            self.assertIsNone(housing_pay_report.warning_message(USER, self.end))
        with mock.patch.object(self.housing_payment, "PAY_CARD", ""):
            self.assertIsNone(housing_pay_report.warning_message(USER, self.end))

    def test_daily_job_sends_the_report_three_days_before_the_end(self):
        self._subscriber(expires=self.now() + timedelta(days=2))
        self.housing_monitor.check_access_expiry(self.context)
        calls = self.sent_calls(USER)
        self.assertEqual(len(calls), 1)
        self.assertIn("Квартир одразу: <b>6</b>", calls[0].kwargs["text"])
        self.assertEqual(len(self._package_callbacks(calls[0].kwargs["reply_markup"])), 3)
        # Второй запуск то же предупреждение не повторяет.
        self.bot.reset_mock()
        self.housing_monitor.check_access_expiry(self.context)
        self.assertEqual(self.sent_calls(USER), [])

    def test_daily_job_keeps_the_old_text_when_the_report_is_off(self):
        self._subscriber(expires=self.now() + timedelta(days=2))
        with mock.patch.object(housing_pay_report, "enabled", return_value=False):
            self.housing_monitor.check_access_expiry(self.context)
        text = self.sent_calls(USER)[0].kwargs["text"]
        self.assertIn(i18n.t("housing.access.expiry_warning", "uk"), text)

    def test_end_day_message_has_numbers_and_packages(self):
        self._subscriber(expires=self.now() - timedelta(minutes=1))
        housing_access_store.mark_notice_sent(USER)        # предупреждение за 3 дня уже было
        self.housing_monitor.check_access_expiry(self.context)
        calls = self.sent_calls(USER)
        self.assertEqual(len(calls), 1)
        text = calls[0].kwargs["text"]
        self.assertIn("Підписка на миттєві сповіщення закінчилась", text)
        self.assertIn("Квартир одразу: <b>6</b>", text)
        self.assertEqual(len(self._package_callbacks(calls[0].kwargs["reply_markup"])), 3)
        self.assertFalse(self.session().query(HousingAccessUser).get(USER).active)

    def test_end_day_message_for_a_manual_grant(self):
        housing_access_store.grant_access(USER, "Olena", expires_at=self.now() - timedelta(minutes=1))
        housing_access_store.mark_notice_sent(USER)
        self.add_sends(3, delayed=True, when=self.now() - timedelta(days=60))
        self.add_sends(4, delayed=False, when=self.now() - timedelta(days=20))
        self.housing_monitor.check_access_expiry(self.context)
        text = self.sent_calls(USER)[0].kwargs["text"]
        self.assertIn("Квартир одразу: <b>4</b>", text)

    def test_trial_end_message_is_unchanged(self):
        housing_access_store.grant_trial(USER, "Olena", expires_at=self.now() - timedelta(minutes=1))
        self.housing_monitor.check_access_expiry(self.context)
        text = self.sent_calls(USER)[0].kwargs["text"]
        self.assertNotIn("Квартир одразу", text)

    def test_gift_end_message_counts_from_the_gift_start(self):
        started = self.now() - timedelta(days=2)
        self.add_sends(3, delayed=False, when=started + timedelta(hours=3))
        self.add_sends(5, delayed=False, when=started - timedelta(days=30))
        text, markup = housing_pay_report.gift_end_message(USER, started)
        self.assertIn("Подарункові дні закінчились", text)
        self.assertIn("Квартир одразу: <b>3</b>", text)
        self.assertEqual(len(self._package_callbacks(markup)), 3)


if __name__ == "__main__":
    unittest.main()
