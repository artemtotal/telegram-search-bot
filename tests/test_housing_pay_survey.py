"""Опрос: «✍️ Написати докладніше» после отрицательного ответа (25.11.2026)."""

import unittest
from datetime import datetime, timedelta
from unittest import mock

import i18n
from database import HousingUserJourney
from user_jobs.housing_pay_store import HousingSurveyMore
from pay_helpers import ADMIN, USER, PayDbCase, callback, text_update
from user_handlers import anonymous_posts, housing_letter, housing_pay_survey
from user_jobs import housing_journey_store, housing_pay_store, user_settings_store

MORE = "housing:survey_more"


class SurveyBase(PayDbCase):
    def setUp(self):
        super().setUp()
        extra = [
            mock.patch.object(self.housing_monitor, "ADMIN_ID", ADMIN),
            mock.patch.dict("os.environ", {"ADMIN_ID": str(ADMIN)}),
        ]
        for patch in extra:
            patch.start()
        self.patches.extend(extra)
        session = self.session()
        session.add(HousingUserJourney(user_id=USER, first_menu_at=datetime.utcnow(), survey_sent_at=datetime.utcnow()))
        session.commit()

    def answer(self, code):
        update = callback(f"housing:survey:{code}")
        self.housing_monitor._handle_callback(update, self.context)
        return update

    def detail(self, detail):
        update = callback(f"housing:survey_hard:{detail}")
        self.housing_monitor._handle_callback(update, self.context)
        return update

    def callbacks_of(self, update):
        markup = update.callback_query.edit_message_text.call_args.kwargs["reply_markup"]
        return [b.callback_data for b in self.buttons(markup) if b.callback_data]


class ButtonTest(SurveyBase):
    def test_negative_answers_are_hard_its_details_and_few(self):
        for answer in ("hard", "hard:night", "hard:many", "hard:prices", "hard:filters", "hard:other", "few"):
            self.assertTrue(housing_pay_survey.is_negative(answer), answer)
        for answer in ("found", "fine", "mismatch", "price", "", None):
            self.assertFalse(housing_pay_survey.is_negative(answer), answer)

    def test_few_gets_the_button(self):
        update = self.answer("few")
        self.assertIn(MORE, self.callbacks_of(update))

    def test_other_answers_do_not(self):
        for code in ("found", "fine", "mismatch", "price"):
            session = self.session()
            session.query(HousingUserJourney).update({"survey_answer": None})
            session.commit()
            update = self.answer(code)
            self.assertNotIn(MORE, self.callbacks_of(update), code)

    def test_hard_question_screen_has_the_button_after_the_five_details(self):
        update = self.answer("hard")
        data = self.callbacks_of(update)
        self.assertEqual(data[:5], [f"housing:survey_hard:{d}" for d in ("night", "many", "prices", "filters", "other")])
        self.assertEqual(data[-1], MORE)

    def test_every_detail_gets_the_button(self):
        for detail in ("night", "many", "prices", "filters", "other"):
            with self.subTest(detail=detail):
                session = self.session()
                session.query(HousingUserJourney).update({"survey_answer": "hard"})
                session.commit()
                update = self.detail(detail)
                self.assertIn(MORE, self.callbacks_of(update))

    def test_writing_to_the_admin_button_stays(self):
        update = self.answer("few")
        markup = update.callback_query.edit_message_text.call_args.kwargs["reply_markup"]
        self.assertTrue(any((b.url or "").startswith("tg://user?id=") for b in self.buttons(markup)))

    def test_off_switch_removes_the_button(self):
        with mock.patch.dict("os.environ", {"HOUSING_SURVEY_MORE_ENABLED": "0"}):
            update = self.answer("few")
            self.assertNotIn(MORE, self.callbacks_of(update))
            session = self.session()
            session.query(HousingUserJourney).update({"survey_answer": None})
            session.commit()
            update = self.answer("hard")
            self.assertNotIn(MORE, self.callbacks_of(update))

    def test_button_text_in_every_language(self):
        for lang in i18n.SUPPORTED_LANGS:
            self.assertNotIn("[[", i18n.t("pay.survey.btn_more", lang))
            self.assertNotIn("[[", i18n.t("pay.survey.more_prompt", lang))


class WaitForTextTest(SurveyBase):
    def _click_more_after(self, code, detail=None):
        self.answer(code)
        if detail:
            self.detail(detail)
        update = callback(MORE)
        self.housing_monitor._handle_callback(update, self.context)
        return update

    def test_click_asks_for_one_text_with_a_cancel_button(self):
        self._click_more_after("hard", "night")
        self.assertEqual(self.context.user_data[housing_pay_survey.STATE_KEY]["answer"], "hard:night")
        call = self.sent_calls(USER)[-1]
        self.assertIn("Напишіть своїми словами", call.kwargs["text"])
        buttons = self.buttons(call.kwargs["reply_markup"])
        self.assertEqual([b.callback_data for b in buttons], ["housing:survey_more_cancel"])

    def test_text_is_saved_forwarded_with_its_origin_and_thanked(self):
        self._click_more_after("hard", "night")
        self.bot.reset_mock()
        update = text_update("Уночі приходять повідомлення <b>і будять</b>")
        self.assertTrue(housing_pay_survey.handle_text(update, self.context))
        rows = self.session().query(HousingSurveyMore).all()
        self.assertEqual([(r.user_id, r.survey_answer, r.text) for r in rows],
                         [(USER, "hard:night", "Уночі приходять повідомлення <b>і будять</b>")])
        update.message.reply_text.assert_called_once()
        self.assertIn("Дякуємо", update.message.reply_text.call_args.args[0])
        admin_call = self.sent_calls(ADMIN)[0]
        self.assertIn("(з відповіді: 🤔 Незручно / незрозуміло → 🌙 Сповіщення приходять уночі)", admin_call.kwargs["text"])
        self.assertIn("&lt;b&gt;і будять&lt;/b&gt;", admin_call.kwargs["text"])
        self.assertIn(str(USER), admin_call.kwargs["text"])
        self.assertNotIn(housing_pay_survey.STATE_KEY, self.context.user_data)

    def test_origin_for_few_is_its_label(self):
        self._click_more_after("few")
        self.bot.reset_mock()
        housing_pay_survey.handle_text(text_update("Квартир надто мало приходить"), self.context)
        self.assertIn("Приходить замало квартир", self.sent_calls(ADMIN)[0].kwargs["text"])

    def test_only_one_text_is_taken(self):
        self._click_more_after("few")
        self.assertTrue(housing_pay_survey.handle_text(text_update("Перший текст"), self.context))
        self.assertFalse(housing_pay_survey.handle_text(text_update("Другий текст"), self.context))
        self.assertEqual(self.session().query(HousingSurveyMore).count(), 1)

    def test_a_too_short_text_asks_again_and_keeps_waiting(self):
        self._click_more_after("few")
        update = text_update("ок")
        self.assertTrue(housing_pay_survey.handle_text(update, self.context))
        self.assertEqual(self.session().query(HousingSurveyMore).count(), 0)
        self.assertIn(housing_pay_survey.STATE_KEY, self.context.user_data)

    def test_waiting_expires_after_24_hours(self):
        self._click_more_after("few")
        self.context.user_data[housing_pay_survey.STATE_KEY]["at"] = datetime.utcnow() - timedelta(hours=24, minutes=1)
        self.assertFalse(housing_pay_survey.handle_text(text_update("Запізно"), self.context))
        self.assertEqual(self.session().query(HousingSurveyMore).count(), 0)
        self.assertNotIn(housing_pay_survey.STATE_KEY, self.context.user_data)

    def test_just_inside_24_hours_still_counts(self):
        self._click_more_after("few")
        self.context.user_data[housing_pay_survey.STATE_KEY]["at"] = datetime.utcnow() - timedelta(hours=23, minutes=59)
        self.assertTrue(housing_pay_survey.handle_text(text_update("Встиг"), self.context))

    def test_cancel_button_stops_waiting(self):
        self._click_more_after("few")
        update = callback("housing:survey_more_cancel")
        self.housing_monitor._handle_callback(update, self.context)
        self.assertNotIn(housing_pay_survey.STATE_KEY, self.context.user_data)
        self.assertFalse(housing_pay_survey.handle_text(text_update("Після скасування"), self.context))

    def test_no_waiting_means_no_interception(self):
        self.assertFalse(housing_pay_survey.handle_text(text_update("Звичайний текст"), self.context))

    def test_long_text_is_cut(self):
        self._click_more_after("few")
        housing_pay_survey.handle_text(text_update("а" * 5000), self.context)
        self.assertEqual(len(self.session().query(HousingSurveyMore).first().text), housing_pay_survey.MAX_LENGTH)


class OtherDialogsTest(SurveyBase):
    """Ожидание текста не перехватывает чек оплаты, свою сумму доната, анкету
    письма, мастер фильтра и обратную связь."""

    def _wait(self):
        self.context.user_data[housing_pay_survey.STATE_KEY] = {"at": datetime.utcnow(), "answer": "few"}

    def _assert_yields(self, key, value):
        self._wait()
        self.context.user_data[key] = value
        self.assertFalse(housing_pay_survey.handle_text(text_update("Мій текст для іншого діалогу"), self.context))
        self.assertEqual(self.session().query(HousingSurveyMore).count(), 0)
        self.assertNotIn(housing_pay_survey.STATE_KEY, self.context.user_data)

    def test_feedback_dialog(self):
        self._assert_yields("feedback", {"step": "text"})

    def test_anonymous_post_dialog(self):
        self._assert_yields("anonymous", {"step": "text"})

    def test_filter_wizard(self):
        self._assert_yields("housing_admin", {"mode": "multi", "step": "rooms"})

    def test_filter_card_number_input(self):
        self._assert_yields("hcard", {"input": "max_price"})

    def test_letter_questionnaire(self):
        self._assert_yields(housing_letter.STATE_KEY, {"step": "name"})

    def test_custom_donation_amount(self):
        from user_handlers import support
        self._assert_yields(support._STATE_KEY, datetime.utcnow())

    def test_bottom_menu_button_is_not_taken_as_the_answer(self):
        self._wait()
        self.assertFalse(housing_pay_survey.handle_text(
            text_update("🏘 Житло"), self.context, lambda t: anonymous_posts._is_menu_text(t, "uk"),
        ))
        self.assertEqual(self.session().query(HousingSurveyMore).count(), 0)

    def test_payment_receipt_photos_are_not_text_and_stay_with_their_handler(self):
        self._wait()
        self.housing_payment.handle_callback(callback("housing:pay:p:m1"), self.context)
        from pay_helpers import photo
        self.housing_payment.handle_receipt(photo(USER, caption="оплата"), self.context)
        self.bot.copy_message.assert_called_once()
        self.assertIn(housing_pay_survey.STATE_KEY, self.context.user_data)

    def test_text_goes_through_the_normal_text_handler_in_the_right_order(self):
        self._wait()
        update = text_update("Дуже складно налаштувати фільтр")
        with mock.patch.object(self.housing_monitor, "handle_private_text") as housing_text:
            anonymous_posts.handle_private_text(update, self.context)
        housing_text.assert_not_called()
        self.assertEqual(self.session().query(HousingSurveyMore).count(), 1)

    def test_without_waiting_the_text_goes_on_to_the_other_handlers(self):
        update = text_update("Привіт")
        with mock.patch.object(self.housing_monitor, "handle_private_text", return_value=True) as housing_text:
            anonymous_posts.handle_private_text(update, self.context)
        housing_text.assert_called_once()
        self.assertEqual(self.session().query(HousingSurveyMore).count(), 0)

    def test_menu_button_while_waiting_reaches_the_menu(self):
        self._wait()
        update = text_update("🏘 Житло")
        with mock.patch.object(self.housing_monitor, "show_menu") as menu:
            anonymous_posts.handle_private_text(update, self.context)
        menu.assert_called_once()
        self.assertEqual(self.session().query(HousingSurveyMore).count(), 0)

    def test_feedback_text_still_wins_when_both_are_open(self):
        self._wait()
        self.context.user_data["feedback"] = {"step": "text"}
        update = text_update("Це відгук, а не відповідь опитування")
        with mock.patch.object(anonymous_posts, "_handle_feedback_text") as feedback:
            anonymous_posts.handle_private_text(update, self.context)
        feedback.assert_called_once()
        self.assertEqual(self.session().query(HousingSurveyMore).count(), 0)


if __name__ == "__main__":
    unittest.main()
