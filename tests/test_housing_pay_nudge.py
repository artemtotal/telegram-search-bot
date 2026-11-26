"""Одне нагадування тим, хто кинув оплату на екрані з карткою (26.11.2026)."""

import json
import os
import unittest
from datetime import timedelta
from unittest import mock

from telegram.error import Unauthorized

from database import HousingLeaveEvent, HousingPayment
from user_jobs.housing_pay_store import HousingPayNudge
from pay_helpers import ADMIN, USER, FRIEND, PayDbCase
from user_handlers import housing_pay_nudge
from user_jobs import housing_pay_store


class PayNudgeTest(PayDbCase):
    def setUp(self):
        super().setUp()
        extra = [
            mock.patch.object(housing_pay_nudge.housing_winback, "is_night", return_value=False),
            mock.patch("time.sleep"),
            mock.patch.dict(os.environ, {"HOUSING_PAY_NUDGE_ENABLED": "1"}),
        ]
        for patch in extra:
            patch.start()
        self.patches.extend(extra)

    def pay(self, user_id=USER, hours_ago=30, status="awaiting_receipt"):
        session = self.session()
        row = HousingPayment(
            user_id=user_id, display_name="Olena", months=1, amount_eur=5.0, amount_uah=250, eur_uah_rate=50.0,
            status=status, created_at=self.now() - timedelta(hours=hours_ago), package="m1",
        )
        session.add(row)
        session.commit()
        return row.id

    def run_job(self):
        return housing_pay_nudge.pay_nudge_job(self.context)

    def test_off_by_default(self):
        self.pay()
        with mock.patch.dict(os.environ):
            os.environ.pop("HOUSING_PAY_NUDGE_ENABLED", None)
            self.assertFalse(housing_pay_nudge.enabled())
            self.assertEqual(self.run_job(), {"sent": 0})
        self.assertEqual(self.sent_texts(USER), [])

    def test_sends_once_after_a_day_with_package_buttons(self):
        self.pay()
        self.assertEqual(self.run_job()["sent"], 1)
        self.assertEqual(self.run_job()["sent"], 0)
        calls = self.sent_calls(USER)
        self.assertEqual(len(calls), 1)
        data = [b.callback_data for b in self.buttons(calls[0].kwargs["reply_markup"]) if b.callback_data]
        self.assertEqual(data, ["housing:pay:p:m1", "housing:pay:p:m3", "housing:pay:p:until_found"])
        self.assertTrue(housing_pay_store.pay_nudge_sent(USER))

    def test_not_before_a_day_not_after_receipt_window(self):
        self.pay(user_id=USER, hours_ago=5)
        self.pay(user_id=FRIEND, hours_ago=100)
        self.assertEqual(self.run_job()["sent"], 0)

    def test_skips_receipt_sent_cancelled_and_paid(self):
        self.pay(user_id=USER, status="sent")
        self.pay(user_id=FRIEND, status="cancelled")
        self.assertEqual(self.run_job()["sent"], 0)
        self.pay(user_id=USER, hours_ago=40)
        self.pay(user_id=USER, hours_ago=30, status="approved")
        self.assertEqual(self.run_job()["sent"], 0)

    def test_never_to_a_user_who_blocked_the_bot(self):
        self.pay()
        session = self.session()
        session.add(HousingLeaveEvent(user_id=USER, kind="blocked", had_filters=True, created_at=self.now()))
        session.commit()
        self.assertEqual(self.run_job()["sent"], 0)
        self.assertEqual(self.sent_texts(USER), [])
        self.assertEqual(self.session().query(HousingPayNudge).count(), 1)   # и больше не пробуем

    def test_unreachable_recipient_is_marked_and_not_retried(self):
        self.pay()
        self.bot.send_message.side_effect = Unauthorized("Forbidden: bot was blocked by the user")
        self.run_job()
        self.bot.send_message.side_effect = None
        self.assertEqual(self.run_job()["sent"], 0)
        self.assertEqual(len(self.sent_calls(USER)), 1)

    def test_other_errors_are_retried_next_run(self):
        self.pay()
        self.bot.send_message.side_effect = RuntimeError("telegram down")
        self.run_job()
        self.bot.send_message.side_effect = None
        self.assertEqual(self.run_job()["sent"], 1)

    def test_not_at_night(self):
        self.pay()
        with mock.patch.object(housing_pay_nudge.housing_winback, "is_night", return_value=True):
            self.assertEqual(self.run_job()["sent"], 0)

    def test_texts_exist_in_all_languages(self):
        base = os.path.join(os.path.dirname(__file__), "..", "translations")
        for lang in ("uk", "ru", "de"):
            with open(os.path.join(base, f"pay.{lang}.json"), encoding="utf-8") as handle:
                self.assertTrue(json.load(handle).get("pay.nudge.text"), lang)

    def test_scheduled_but_off(self):
        source = open(os.path.join(os.path.dirname(__file__), "..", "robot.py"), encoding="utf-8").read()
        self.assertIn("housing_pay_nudge.pay_nudge_job", source)


if __name__ == "__main__":
    unittest.main()
