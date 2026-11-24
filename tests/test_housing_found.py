"""«🏠 Я знайшов(ла) житло» и mark_found для потока B (25.11.2026)."""

import unittest
from datetime import datetime, timedelta
from unittest import mock

import i18n
from database import HousingAccessUser, HousingLeaveEvent
from pay_helpers import ADMIN, USER, PayDbCase, callback
from user_handlers import housing_found, housing_leave
from user_jobs import housing_access_store, housing_pay_store, housing_tier, user_settings_store


class FoundBase(PayDbCase):
    def setUp(self):
        super().setUp()
        patch = mock.patch.object(self.housing_monitor, "user_filters", return_value=[{"filter_id": 1}])
        patch.start()
        self.patches.append(patch)
        self.paused = self.housing_monitor._set_all_filters_active_for_user

    def events(self, kind=None):
        query = self.session().query(HousingLeaveEvent).filter(HousingLeaveEvent.user_id == USER)
        if kind:
            query = query.filter(HousingLeaveEvent.kind == kind)
        return query.all()

    def subscribe(self, days=20):
        end = datetime.utcnow() + timedelta(days=days)
        housing_access_store.grant_access(USER, "Olena", expires_at=end)
        return end


class MarkFoundTest(FoundBase):
    def test_marks_found_records_the_event_and_pauses_the_searches(self):
        result = housing_found.mark_found(USER)
        self.assertEqual(result, {"paused": 1, "had_filters": True})
        events = self.events("found")
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0].had_filters)
        self.paused.assert_called_once_with(USER, False)
        self.assertTrue(housing_found.is_found(USER))

    def test_the_subscription_is_not_taken_away(self):
        end = self.subscribe()
        housing_found.mark_found(USER)
        row = self.session().query(HousingAccessUser).get(USER)
        self.assertTrue(row.active)
        self.assertEqual(row.expires_at, end)
        self.assertTrue(housing_tier.is_premium(USER))

    def test_event_remembers_whether_the_person_had_filters(self):
        with mock.patch.object(self.housing_monitor, "user_filters", return_value=[]):
            result = housing_found.mark_found(USER)
        self.assertFalse(result["had_filters"])
        self.assertFalse(self.events("found")[0].had_filters)

    def test_a_failing_pause_still_records_everything_else(self):
        self.paused.side_effect = RuntimeError("immowelt down")
        result = housing_found.mark_found(USER)
        self.assertEqual(result["paused"], 0)
        self.assertEqual(len(self.events("found")), 1)
        self.assertTrue(housing_found.is_found(USER))

    def test_works_for_someone_without_any_access_row(self):
        housing_found.mark_found(USER)
        self.assertIsNone(self.session().query(HousingAccessUser).get(USER))

    def test_resume_restores_the_searches_once(self):
        housing_found.mark_found(USER)
        self.assertTrue(housing_found.resume(USER))
        self.paused.assert_called_with(USER, True)
        self.assertFalse(housing_found.is_found(USER))
        self.assertFalse(housing_found.resume(USER))

    def test_paying_again_resumes_the_searches_and_clears_the_mark(self):
        housing_found.mark_found(USER)
        self.housing_monitor.grant_paid_months(USER, "Olena", 1)
        self.paused.assert_called_with(USER, True)
        self.assertFalse(housing_found.is_found(USER))

    def test_extending_without_resuming_keeps_the_pause(self):
        housing_found.mark_found(USER)
        self.paused.reset_mock()
        self.housing_monitor.grant_paid_months(USER, "Olena", 1, resume_filters=False)
        self.paused.assert_not_called()
        self.assertTrue(housing_found.is_found(USER))

    def test_found_again_after_resume_pauses_again(self):
        housing_found.mark_found(USER)
        housing_found.resume(USER)
        housing_found.mark_found(USER)
        self.assertTrue(housing_found.is_found(USER))
        self.assertEqual(len(self.events("found")), 2)

    def test_evening_summary_gets_a_line_about_found(self):
        housing_found.mark_found(USER)
        bot = mock.MagicMock()
        bot.get_chat.side_effect = RuntimeError("no chat")
        now = datetime.utcnow()
        lines = housing_leave.summary_lines(bot, now - timedelta(hours=1), now + timedelta(hours=1), lambda chat: "x")
        joined = "\n".join(lines)
        self.assertIn("Знайшли житло (позначили в боті): <b>1</b>", joined)
        self.assertIn(str(USER), joined)

    def test_the_admin_is_not_told_instantly(self):
        housing_found.mark_found(USER)
        self.bot.send_message.assert_not_called()


class FoundFlowTest(FoundBase):
    def test_button_asks_for_confirmation_first(self):
        update = callback("housing:found")
        self.assertTrue(housing_found.handle_callback(update, self.context))
        args = update.callback_query.edit_message_text.call_args
        self.assertIn("Знайшли житло?", args.args[0])
        data = [b.callback_data for b in self.buttons(args.kwargs["reply_markup"])]
        self.assertEqual(data, ["housing:found:yes", "housing:menu"])
        self.assertFalse(housing_found.is_found(USER))

    def test_confirmation_pauses_and_congratulates_with_the_donation_block(self):
        self.context.bot = self.bot
        update = callback("housing:found:yes")
        housing_found.handle_callback(update, self.context)
        self.assertTrue(housing_found.is_found(USER))
        calls = self.sent_calls(USER)
        self.assertEqual(len(calls), 1)
        self.assertIn("Вітаємо з новим житлом", calls[0].kwargs["text"])
        data = [b.callback_data for b in self.buttons(calls[0].kwargs["reply_markup"])]
        self.assertEqual(data, [
            "support:a:100", "support:a:200", "support:a:300", "support:a:400", "support:a:500",
            "support:custom", "housing:found:resume",
        ])
        update.callback_query.edit_message_reply_markup.assert_called_once_with(reply_markup=None)

    def test_without_a_donation_card_the_congratulation_is_plain(self):
        with mock.patch.object(self.housing_payment, "PAY_CARD", ""):
            housing_found.handle_callback(callback("housing:found:yes"), self.context)
        calls = self.sent_calls(USER)
        data = [b.callback_data for b in self.buttons(calls[0].kwargs["reply_markup"])]
        self.assertEqual(data, ["housing:found:resume"])
        self.assertNotIn("донатом", calls[0].kwargs["text"])

    def test_double_tap_congratulates_once(self):
        housing_found.handle_callback(callback("housing:found:yes"), self.context)
        housing_found.handle_callback(callback("housing:found:yes"), self.context)
        self.assertEqual(len(self.sent_calls(USER)), 1)
        self.assertEqual(len(self.events("found")), 1)

    def test_resume_button_unpauses_and_shows_the_menu(self):
        housing_found.handle_callback(callback("housing:found:yes"), self.context)
        update = callback("housing:found:resume")
        with mock.patch.object(self.housing_monitor, "show_menu") as menu:
            housing_found.handle_callback(update, self.context)
        self.assertFalse(housing_found.is_found(USER))
        self.paused.assert_called_with(USER, True)
        menu.assert_called_once()
        self.assertIn("відновлено", update.callback_query.answer.call_args.args[0])

    def test_routed_through_the_housing_callbacks(self):
        update = callback("housing:found")
        self.housing_monitor._handle_callback(update, self.context)
        self.assertIn("Знайшли житло?", update.callback_query.edit_message_text.call_args.args[0])

    def test_off_switch_hides_everything(self):
        with mock.patch.dict("os.environ", {"HOUSING_FOUND_ENABLED": "0"}):
            self.subscribe()
            update = callback("housing:found:yes")
            housing_found.handle_callback(update, self.context)
            self.assertFalse(housing_found.is_found(USER))
            markup = self.housing_monitor._menu_keyboard(USER, "uk")
        self.assertNotIn("housing:found", [b.callback_data for b in self.buttons(markup)])

    def test_texts_in_every_language(self):
        for lang in i18n.SUPPORTED_LANGS:
            user_settings_store.set_language(USER, lang)
            for key in ("pay.found.confirm", "pay.found.congrats", "pay.found.congrats_plain",
                        "pay.found.resumed", "housing.menu.btn.found", "housing.menu.btn.resume"):
                text = i18n.t(key, lang)
                self.assertNotIn("[[", text)
                self.assertNotIn("{", text)


class MenuTest(FoundBase):
    def _data(self):
        return [b.callback_data for b in self.buttons(self.housing_monitor._menu_keyboard(USER, "uk"))]

    def test_found_button_is_for_subscribers_only(self):
        self.assertNotIn("housing:found", self._data())
        self.subscribe()
        self.assertIn("housing:found", self._data())

    def test_invite_button_is_for_everyone(self):
        self.assertIn("housing:invite", self._data())
        self.subscribe()
        self.assertIn("housing:invite", self._data())

    def test_paused_person_sees_resume_instead_of_found(self):
        self.subscribe()
        housing_found.mark_found(USER)
        data = self._data()
        self.assertIn("housing:found:resume", data)
        self.assertNotIn("housing:found", data)

    def test_paused_person_without_a_subscription_can_resume_too(self):
        housing_found.mark_found(USER)
        data = self._data()
        self.assertIn("housing:found:resume", data)
        self.assertNotIn("housing:found", data)

    def test_menu_does_not_grow_past_seven_rows(self):
        for premium in (False, True):
            if premium:
                self.subscribe()
            rows = self.housing_monitor._menu_keyboard(USER, "uk").inline_keyboard
            self.assertLessEqual(len(rows), 7, premium)
            housing_found.mark_found(USER)
            self.assertLessEqual(len(self.housing_monitor._menu_keyboard(USER, "uk").inline_keyboard), 7, premium)
            housing_found.resume(USER)

    def test_subscribers_pause_does_not_hide_the_menu(self):
        self.subscribe()
        housing_found.mark_found(USER)
        self.assertTrue(len(self._data()) > 5)


class WorkflowForStreamB(FoundBase):
    """Поток B вызывает mark_found(user_id) из трекера заявок."""

    def test_signature_and_import_path(self):
        from user_handlers.housing_found import mark_found

        self.assertEqual(mark_found.__code__.co_varnames[:1], ("user_id",))
        self.assertEqual(mark_found(USER)["had_filters"], True)

    def test_mark_found_sends_nothing_to_the_person_itself(self):
        # Поздравление и донат показывает сам трекер заявок (или housing_found.congrats).
        housing_found.mark_found(USER)
        self.assertEqual(self.sent_calls(USER), [])


if __name__ == "__main__":
    unittest.main()
