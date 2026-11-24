"""Журнал «квартиру вже зняли», возврат ушедших через 7 дней и подарок
«🎁 2 дні миттєвих» (25.11.2026)."""

import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import mock

from telegram.error import Unauthorized

import i18n
from database import (
    HousingAccessUser, HousingHeldMatch, HousingLeaveEvent, HousingPayment, HousingSearch, HousingSendLog,
    ProPotsdamListing,
)
from user_jobs.housing_pay_store import HousingGift, HousingMissedLog, HousingWinback
from pay_helpers import ADMIN, USER, PayDbCase, callback
from user_handlers import housing_pay_report, housing_winback
from user_jobs import housing_access_store, housing_missed, housing_pay_store, housing_tier


class WinbackBase(PayDbCase):
    def setUp(self):
        super().setUp()
        extra = [
            mock.patch.object(self.housing_monitor, "user_filters", return_value=[{"filter_id": 1}]),
            mock.patch.object(housing_pay_report, "relax_for_empty", return_value=(False, [], "")),
            mock.patch.object(housing_winback, "is_night", return_value=False),
            mock.patch("time.sleep"),
        ]
        for patch in extra:
            patch.start()
        self.patches.extend(extra)

    def expired_user(self, user_id=USER, days_ago=8, trial=False):
        session = self.session()
        ended = self.now() - timedelta(days=days_ago)
        session.add(HousingAccessUser(
            user_id=user_id, display_name="Olena", active=False, expires_at=ended, expiry_notice_sent=True,
            is_trial=trial, created_at=ended - timedelta(days=30), updated_at=ended,
        ))
        session.commit()
        return ended

    def add_sends(self, count, delayed, when, user_id=USER):
        session = self.session()
        for index in range(count):
            session.add(HousingSendLog(
                user_id=user_id, source="propotsdam", listing_key=f"{delayed}-{when.isoformat()}-{index}",
                delayed=delayed, sent_at=when + timedelta(minutes=index),
            ))
        session.commit()

    def add_missed(self, count, when, user_id=USER):
        for index in range(count):
            housing_pay_store.log_missed(user_id, "propotsdam", f"gone-{when.isoformat()}-{index}")
        session = self.session()
        for row in session.query(HousingMissedLog).filter(HousingMissedLog.user_id == user_id):
            row.created_at = when
        session.commit()

    def run_job(self):
        return housing_winback.winback_job(self.context)

    def first_message(self, user_id=USER):
        calls = self.sent_calls(user_id)
        return calls[0].kwargs if calls else None


class MissedLogTest(PayDbCase):
    def test_job_writes_a_journal_row_for_every_notice(self):
        now = self.now()
        session = self.session()
        session.add(HousingSearch(user_id=USER, criteria="{}", members="[]", coops="[]", created_at=now))
        session.add(ProPotsdamListing(
            listing_key="old", title="A", first_seen_at=now - timedelta(hours=1), last_seen_at=now - timedelta(minutes=50),
            is_active=False,
        ))
        session.add(ProPotsdamListing(
            listing_key="live", title="B", first_seen_at=now - timedelta(hours=1), last_seen_at=now, is_active=True,
        ))
        session.add(HousingHeldMatch(user_id=USER, source="propotsdam", listing_key="old", summary="2 Zi", held_at=now))
        session.commit()
        result = housing_missed.missed_flats_job(self.context)
        self.assertEqual(result["sent"], 1)
        rows = self.session().query(HousingMissedLog).all()
        self.assertEqual([(r.user_id, r.source, r.listing_key) for r in rows], [(USER, "propotsdam", "old")])
        self.assertLess(abs(rows[0].created_at - now), timedelta(minutes=1))

    def test_nothing_is_logged_when_the_send_fails(self):
        now = self.now()
        session = self.session()
        session.add(HousingSearch(user_id=USER, criteria="{}", members="[]", coops="[]", created_at=now))
        session.add(ProPotsdamListing(
            listing_key="old", title="A", first_seen_at=now - timedelta(hours=1), last_seen_at=now - timedelta(minutes=50),
            is_active=False,
        ))
        session.add(ProPotsdamListing(
            listing_key="live", title="B", first_seen_at=now - timedelta(hours=1), last_seen_at=now, is_active=True,
        ))
        session.add(HousingHeldMatch(user_id=USER, source="propotsdam", listing_key="old", summary="2 Zi", held_at=now))
        session.commit()
        self.bot.send_message.side_effect = RuntimeError("telegram down")
        housing_missed.missed_flats_job(self.context)
        self.assertEqual(self.session().query(HousingMissedLog).count(), 0)

    def test_logging_never_raises(self):
        broken = mock.MagicMock()
        broken.commit.side_effect = RuntimeError("disk full")
        with mock.patch.object(housing_pay_store, "DBSession", return_value=broken):
            housing_pay_store.log_missed(USER, "x", "y")      # не падает

    def test_missed_count_counts_distinct_flats_in_the_window(self):
        now = self.now()
        housing_pay_store.log_missed(USER, "propotsdam", "a")
        housing_pay_store.log_missed(USER, "propotsdam", "a")
        housing_pay_store.log_missed(USER, "kleinanzeigen", "a")
        housing_pay_store.log_missed(555, "propotsdam", "z")
        self.assertEqual(housing_pay_store.missed_count(USER, now - timedelta(hours=1), now + timedelta(hours=1)), 2)
        self.assertEqual(housing_pay_store.missed_count(USER, now + timedelta(hours=1), now + timedelta(hours=2)), 0)


class WinbackJobTest(WinbackBase):
    def test_one_message_a_week_after_the_end_with_real_numbers_and_the_gift(self):
        ended = self.expired_user(days_ago=8)
        self.add_sends(4, True, ended + timedelta(days=1))
        self.add_sends(9, True, ended + timedelta(days=9))      # после 7 дней - не считаем
        self.add_missed(2, ended + timedelta(days=2))
        self.assertEqual(self.run_job(), {"sent": 1})
        message = self.first_message()
        text = message["text"]
        self.assertIn(ended.strftime("%d.%m.%Y"), text)
        self.assertIn("Із затримкою ~2 год прийшло квартир: <b>4</b>", text)
        self.assertIn("щонайменше <b>2</b>", text)
        self.assertIn("2 дні миттєвих", text)
        buttons = self.buttons(message["reply_markup"])
        self.assertEqual([b.callback_data for b in buttons], ["housing:gift:claim"])
        self.assertEqual(buttons[0].text, "🎁 Увімкнути 2 дні миттєвих")
        # Второй запуск тому же человеку на то же окончание ничего не шлёт.
        self.bot.reset_mock()
        self.assertEqual(self.run_job(), {"sent": 0})
        self.assertEqual(self.sent_calls(USER), [])
        self.assertEqual(self.session().query(HousingWinback).count(), 1)

    def test_zero_numbers_are_not_shown(self):
        ended = self.expired_user()
        self.add_sends(3, True, ended + timedelta(days=1))
        self.run_job()
        text = self.first_message()["text"]
        self.assertIn("<b>3</b>", text)
        self.assertNotIn("Зникло з сайту", text)
        self.assertNotIn("<b>0</b>", text)

    def test_only_the_missed_line_when_nothing_came_late(self):
        ended = self.expired_user()
        self.add_missed(1, ended + timedelta(days=1))
        self.run_job()
        text = self.first_message()["text"]
        self.assertIn("Зникло з сайту ще до доставки: щонайменше <b>1</b>", text)
        self.assertNotIn("Із затримкою", text)

    def test_no_flats_and_market_confirms_gives_relax_buttons_and_still_the_gift(self):
        self.expired_user()
        row = [[SimpleNamespace(text="Ціна +100", callback_data="housing:relax:5:price")]]
        with mock.patch.object(housing_pay_report, "relax_for_empty", return_value=(True, row, "Розширити фільтр")):
            self.run_job()
        message = self.first_message()
        self.assertIn("нічого не з'явилось", message["text"])
        self.assertIn("Розширити фільтр", message["text"])
        callbacks = [b.callback_data for b in self.buttons(message["reply_markup"])]
        self.assertEqual(callbacks, ["housing:relax:5:price", "housing:gift:claim"])

    def test_no_flats_but_market_unknown_makes_no_claim(self):
        self.expired_user()
        self.run_job()
        text = self.first_message()["text"]
        self.assertNotIn("нічого не з'явилось", text)
        self.assertNotIn("<b>", text.split("\n", 1)[1].replace("🎁", ""))   # ни одной выдуманной цифры

    def test_too_early_and_too_old_endings_are_left_alone(self):
        self.expired_user(user_id=1, days_ago=6)
        self.expired_user(user_id=2, days_ago=31)
        self.assertEqual(self.run_job(), {"sent": 0})
        self.assertEqual(self.bot.send_message.call_count, 0)

    def test_first_run_picks_up_endings_from_7_to_30_days_ago_once_each(self):
        for index, days in enumerate((10, 20, 29), start=1):
            self.expired_user(user_id=index, days_ago=days)
        self.assertEqual(self.run_job(), {"sent": 3})
        self.assertEqual(self.run_job(), {"sent": 0})

    def test_someone_who_renewed_is_not_written_to(self):
        self.expired_user()
        housing_access_store.grant_access(USER, "Olena", expires_at=self.now() + timedelta(days=20))
        self.assertEqual(self.run_job(), {"sent": 0})

    def test_a_new_ending_after_a_renewal_gets_its_own_message(self):
        ended = self.expired_user(days_ago=10)
        self.run_job()
        session = self.session()
        row = session.query(HousingAccessUser).get(USER)
        row.expires_at = self.now() - timedelta(days=8)
        session.commit()
        self.assertEqual(self.run_job(), {"sent": 1})
        self.assertEqual(self.session().query(HousingWinback).count(), 2)

    def test_blocked_bot_is_skipped_but_unblocked_is_not(self):
        self.expired_user()
        session = self.session()
        session.add(HousingLeaveEvent(user_id=USER, kind="blocked", had_filters=True, created_at=self.now() - timedelta(days=3)))
        session.commit()
        self.assertEqual(self.run_job(), {"sent": 0})
        session.add(HousingLeaveEvent(user_id=USER, kind="unblocked", had_filters=True, created_at=self.now() - timedelta(days=1)))
        session.commit()
        self.assertEqual(self.run_job(), {"sent": 1})

    def test_no_active_filter_means_no_message(self):
        self.expired_user()
        with mock.patch.object(self.housing_monitor, "user_filters", return_value=[]):
            self.assertEqual(self.run_job(), {"sent": 0})

    def test_a_payment_waiting_for_the_admin_means_no_message(self):
        self.expired_user()
        session = self.session()
        session.add(HousingPayment(
            user_id=USER, display_name="O", months=1, amount_eur=5.0, amount_uah=250, eur_uah_rate=50.0,
            status="sent", created_at=self.now() - timedelta(hours=2), receipt_at=self.now() - timedelta(hours=2),
            package="m1",
        ))
        session.commit()
        self.assertEqual(self.run_job(), {"sent": 0})

    def test_trial_endings_are_not_subscription_endings(self):
        self.expired_user(trial=True)
        self.assertEqual(self.run_job(), {"sent": 0})

    def test_a_provisional_day_is_not_a_subscription_ending(self):
        ended = self.expired_user()
        session = self.session()
        session.add(HousingPayment(
            user_id=USER, display_name="O", months=1, amount_eur=5.0, amount_uah=250, eur_uah_rate=50.0,
            status="rejected", created_at=ended - timedelta(days=1), provisional_until=ended, package="m1",
        ))
        session.commit()
        self.assertEqual(self.run_job(), {"sent": 0})

    def test_an_ended_gift_is_not_a_subscription_ending(self):
        ended = self.expired_user()
        housing_pay_store.add_gift(USER, ended - timedelta(days=2), ended)
        self.assertEqual(self.run_job(), {"sent": 0})

    def test_the_admin_is_never_written_to(self):
        with mock.patch.object(housing_tier, "ADMIN_ID", USER):
            self.expired_user()
            self.assertEqual(self.run_job(), {"sent": 0})

    def test_nothing_goes_out_at_night(self):
        self.expired_user()
        with mock.patch.object(housing_winback, "is_night", return_value=True):
            self.assertEqual(self.run_job(), {"sent": 0})
        self.assertEqual(self.run_job(), {"sent": 1})

    def test_undeliverable_person_is_marked_and_not_retried(self):
        self.expired_user()
        self.bot.send_message.side_effect = Unauthorized("Forbidden: bot was blocked by the user")
        self.assertEqual(self.run_job(), {"sent": 0})
        self.assertEqual(self.session().query(HousingWinback).count(), 1)
        self.bot.send_message.side_effect = None
        self.assertEqual(self.run_job(), {"sent": 0})

    def test_a_temporary_error_is_retried_next_time(self):
        self.expired_user()
        self.bot.send_message.side_effect = RuntimeError("timeout")
        self.assertEqual(self.run_job(), {"sent": 0})
        self.assertEqual(self.session().query(HousingWinback).count(), 0)
        self.bot.send_message.side_effect = None
        self.assertEqual(self.run_job(), {"sent": 1})

    def test_off_switch(self):
        self.expired_user()
        with mock.patch.dict("os.environ", {"HOUSING_WINBACK_ENABLED": "0"}):
            self.assertEqual(self.run_job(), {"sent": 0})

    def test_message_in_every_language(self):
        from user_jobs import user_settings_store

        ended = self.expired_user()
        self.add_sends(2, True, ended + timedelta(days=1))
        self.add_missed(1, ended + timedelta(days=1))
        for lang in i18n.SUPPORTED_LANGS:
            user_settings_store.set_language(USER, lang)
            text, markup = housing_winback.build_message(USER, ended)
            self.assertNotIn("{", text, lang)
            self.assertNotIn("[[", text, lang)
            self.assertIn("<b>2</b>", text, lang)
            self.assertIn("<b>1</b>", text, lang)
            self.assertNotIn("{", markup.inline_keyboard[-1][0].text, lang)

    def test_after_a_recent_gift_the_message_offers_packages_instead(self):
        ended = self.expired_user()
        housing_pay_store.add_gift(USER, self.now() - timedelta(days=30), self.now() - timedelta(days=28))
        text, markup = housing_winback.build_message(USER, ended)
        callbacks = [b.callback_data for b in self.buttons(markup)]
        self.assertEqual(callbacks, ["housing:pay:p:m1", "housing:pay:p:m3", "housing:pay:p:until_found"])
        self.assertNotIn("🎁", text)


class DaysTextTest(unittest.TestCase):
    def test_plural_forms(self):
        self.assertEqual(housing_winback.days_text(1, "uk"), "1 день")
        self.assertEqual(housing_winback.days_text(2, "uk"), "2 дні")
        self.assertEqual(housing_winback.days_text(5, "uk"), "5 днів")
        self.assertEqual(housing_winback.days_text(11, "uk"), "11 днів")
        self.assertEqual(housing_winback.days_text(22, "uk"), "22 дні")
        self.assertEqual(housing_winback.days_text(2, "ru"), "2 дня")
        self.assertEqual(housing_winback.days_text(5, "ru"), "5 дней")
        self.assertEqual(housing_winback.days_text(2, "de"), "2 Tage")
        self.assertEqual(housing_winback.days_text(1, "de"), "1 Tag")

    def test_night_hours(self):
        # зима: Берлин = UTC+1; 21:30 UTC = 22:30 Берлин
        self.assertTrue(housing_winback.is_night(datetime(2026, 12, 1, 21, 30)))
        self.assertTrue(housing_winback.is_night(datetime(2026, 12, 1, 7, 30)))
        self.assertFalse(housing_winback.is_night(datetime(2026, 12, 1, 8, 30)))
        self.assertFalse(housing_winback.is_night(datetime(2026, 12, 1, 20, 30)))


class GiftTest(WinbackBase):
    def _claim(self, user_id=USER):
        update = callback("housing:gift:claim", user_id)
        housing_winback.handle_callback(update, self.context)
        return update

    def test_gift_turns_on_two_days_of_instant_alerts(self):
        before = self.now()
        update = self._claim()
        row = self.session().query(HousingAccessUser).get(USER)
        self.assertTrue(row.active)
        self.assertFalse(row.is_trial)
        self.assertLess(abs(row.expires_at - (before + timedelta(days=2))), timedelta(minutes=2))
        self.assertTrue(row.expiry_notice_sent)       # без «через 3 дні закінчиться»
        self.assertTrue(housing_tier.is_premium(USER))
        self.assertEqual(self.session().query(HousingGift).count(), 1)
        update.callback_query.edit_message_reply_markup.assert_called_once_with(reply_markup=None)
        text = self.sent_texts(USER)[0]
        self.assertIn("Готово", text)
        self.assertIn("час Берліна", text)

    def test_gift_cannot_be_taken_twice_within_90_days(self):
        self._claim()
        housing_access_store.set_active(USER, False)
        self.bot.reset_mock()
        update = self._claim()
        update.callback_query.answer.assert_called()
        self.assertTrue(update.callback_query.answer.call_args.kwargs.get("show_alert"))
        self.assertEqual(self.session().query(HousingGift).count(), 1)
        self.assertFalse(housing_tier.is_premium(USER))

    def test_gift_is_available_again_after_90_days(self):
        self._claim()
        housing_access_store.set_active(USER, False)
        session = self.session()
        gift = session.query(HousingGift).first()
        gift.started_at = self.now() - timedelta(days=91)
        gift.until = gift.started_at + timedelta(days=2)
        session.commit()
        self._claim()
        self.assertEqual(self.session().query(HousingGift).count(), 2)
        self.assertTrue(housing_tier.is_premium(USER))

    def test_ninety_days_exactly_is_the_boundary(self):
        housing_pay_store.add_gift(USER, self.now() - timedelta(days=89, hours=23), self.now() - timedelta(days=87))
        self.assertFalse(housing_winback.gift_allowed(USER))
        housing_pay_store.add_gift(555, self.now() - timedelta(days=90, minutes=1), self.now() - timedelta(days=88))
        self.assertTrue(housing_winback.gift_allowed(555))

    def test_current_subscribers_do_not_need_a_gift(self):
        housing_access_store.grant_access(USER, "Olena", expires_at=self.now() + timedelta(days=20))
        before = self.session().query(HousingAccessUser).get(USER).expires_at
        update = self._claim()
        self.assertEqual(self.session().query(HousingAccessUser).get(USER).expires_at, before)
        self.assertEqual(self.session().query(HousingGift).count(), 0)
        self.assertTrue(update.callback_query.answer.call_args.kwargs.get("show_alert"))

    def test_gift_off_switch(self):
        with mock.patch.dict("os.environ", {"HOUSING_WINBACK_ENABLED": "0"}):
            self._claim()
        self.assertEqual(self.session().query(HousingGift).count(), 0)

    def test_gift_length_comes_from_the_environment(self):
        with mock.patch.dict("os.environ", {"HOUSING_GIFT_DAYS": "3"}):
            before = self.now()
            self._claim()
        row = self.session().query(HousingAccessUser).get(USER)
        self.assertLess(abs(row.expires_at - (before + timedelta(days=3))), timedelta(minutes=2))

    def _end_gift(self):
        session = self.session()
        gift = session.query(HousingGift).first()
        gift.started_at -= timedelta(days=3)
        gift.until -= timedelta(days=3)
        row = session.query(HousingAccessUser).get(USER)
        row.expires_at = gift.until
        session.commit()

    def test_when_the_gift_ends_the_person_gets_one_short_message_with_three_packages(self):
        self._claim()
        self.add_sends(3, False, self.now() + timedelta(hours=1))
        self._end_gift()
        self.bot.reset_mock()
        self.assertEqual(housing_winback.expire_gifts(self.bot, self.now() + timedelta(hours=2)), 1)
        self.assertFalse(housing_tier.is_premium(USER))
        calls = self.sent_calls(USER)
        self.assertEqual(len(calls), 1)
        self.assertIn("Подарункові дні закінчились", calls[0].kwargs["text"])
        callbacks = [b.callback_data for b in self.buttons(calls[0].kwargs["reply_markup"])]
        self.assertEqual(callbacks, ["housing:pay:p:m1", "housing:pay:p:m3", "housing:pay:p:until_found"])
        # Повторный проход ничего не шлёт.
        self.bot.reset_mock()
        self.assertEqual(housing_winback.expire_gifts(self.bot), 0)
        self.assertEqual(self.bot.send_message.call_count, 0)

    def test_gift_end_counts_only_flats_received_during_the_gift(self):
        self._claim()
        self._end_gift()
        session = self.session()
        gift = session.query(HousingGift).first()
        self.add_sends(2, False, gift.started_at + timedelta(hours=3))
        self.add_sends(7, False, gift.started_at - timedelta(days=10))
        self.bot.reset_mock()
        housing_winback.expire_gifts(self.bot)
        self.assertIn("Квартир одразу: <b>2</b>", self.sent_texts(USER)[0])

    def test_daily_check_does_not_send_the_generic_expiry_message_for_a_gift(self):
        self._claim()
        self.bot.reset_mock()
        self.housing_monitor.check_access_expiry(self.context)       # подарок ещё идёт: ни предупреждения за 3 дня
        self.assertEqual(self.sent_calls(USER), [])
        self._end_gift()
        self.bot.reset_mock()
        self.housing_monitor.check_access_expiry(self.context)
        texts = self.sent_texts(USER)
        self.assertEqual(len(texts), 1)
        self.assertIn("Подарункові дні закінчились", texts[0])
        self.assertNotIn("Підписка на миттєві сповіщення закінчилась", texts[0])

    def test_payment_during_the_gift_counts_months_from_approval_and_ends_the_gift_quietly(self):
        self._claim()
        self.housing_payment.handle_callback(callback("housing:pay:p:m1"), self.context)
        self.housing_payment.handle_receipt(__import__("pay_helpers").photo(), self.context)
        payment_id = self.session().query(HousingPayment).order_by(HousingPayment.id.desc()).first().id
        before = self.now()
        self.housing_payment.handle_callback(callback(f"housing:pay:ok:{payment_id}", ADMIN), self.context)
        row = self.session().query(HousingAccessUser).get(USER)
        self.assertLess(abs(row.expires_at - self.housing_monitor._add_months(before, 1)), timedelta(minutes=2))
        # Подарок кончается по расписанию, но подписка идёт - человеку ничего не пишем.
        session = self.session()
        gift = session.query(HousingGift).first()
        gift.until = self.now() - timedelta(minutes=1)
        session.commit()
        self.bot.reset_mock()
        self.assertEqual(housing_winback.expire_gifts(self.bot), 0)
        self.assertEqual(self.sent_calls(USER), [])
        self.assertTrue(housing_tier.is_premium(USER))
        self.assertIsNotNone(self.session().query(HousingGift).first().end_handled_at)

    def test_pay_screen_does_not_count_the_gift_as_a_paid_subscription(self):
        self._claim()
        update = callback("housing:pay:start")
        self.housing_payment.handle_callback(update, self.context)
        text = update.callback_query.edit_message_text.call_args.args[0]
        self.assertNotIn("Зараз підписка діє до", text)


if __name__ == "__main__":
    unittest.main()
