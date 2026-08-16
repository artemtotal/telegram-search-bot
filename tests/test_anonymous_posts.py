import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest import mock

from user_handlers import anonymous_validation
from user_handlers import anonymous_posts


class AnonymousPostValidationTests(unittest.TestCase):
    def test_accepts_normal_question(self):
        self.assertIsNone(
            anonymous_validation.validate_submission(
                "Подскажите, пожалуйста, хорошего семейного врача в Потсдаме."
            )
        )

    def test_rejects_links_and_contacts(self):
        samples = [
            "Посмотрите подробности на https://example.com прямо сейчас",
            "Напишите мне в Telegram @example_user по этому вопросу",
            "Мой номер телефона +49 151 23456789, позвоните мне",
            "Моя почта test@example.com для ответа на вопрос",
        ]
        for sample in samples:
            with self.subTest(sample=sample):
                self.assertIsNotNone(anonymous_validation.validate_submission(sample))

    def test_fingerprint_ignores_case_and_whitespace(self):
        first = anonymous_validation.text_fingerprint("  Где найти врача?\nВ Потсдаме ")
        second = anonymous_validation.text_fingerprint("где НАЙТИ врача? в потсдаме")
        self.assertEqual(first, second)

    def test_date_is_not_mistaken_for_phone_number(self):
        self.assertIsNone(
            anonymous_validation.validate_submission(
                "Куда можно сходить с ребёнком 18.07.2026 в Потсдаме?"
            )
        )

    def test_deleted_submission_still_has_cooldown(self):
        now = datetime.utcnow()
        user = SimpleNamespace(last_submission_at=now)
        self.assertIn(
            "анонімний пост",
            anonymous_validation.cooldown_text(user.last_submission_at, 7, now).lower(),
        )

    def test_forum_message_link_contains_thread(self):
        private_message = SimpleNamespace(
            chat=SimpleNamespace(username=None),
            chat_id=-100123456,
            message_thread_id=77,
            message_id=99,
        )
        public_message = SimpleNamespace(
            chat=SimpleNamespace(username="PotsdamChat"),
            chat_id=-100123456,
            message_thread_id=77,
            message_id=99,
        )
        self.assertEqual(
            anonymous_validation.message_link(private_message),
            "https://t.me/c/123456/77/99",
        )
        self.assertEqual(
            anonymous_validation.message_link(public_message),
            "https://t.me/PotsdamChat/77/99",
        )

    def test_new_private_user_gets_anonymous_menu_buttons(self):
        keyboard = anonymous_posts.reply_menu_keyboard(user_id=123).to_dict()["keyboard"]
        flattened = [button["text"] for row in keyboard for button in row]

        self.assertIn(anonymous_posts.BTN_HOME, flattened)
        self.assertIn(anonymous_posts.BTN_ANON, flattened)
        self.assertIn(anonymous_posts.BTN_MY_POSTS, flattened)
        self.assertIn(anonymous_posts.BTN_EQUEUE, flattened)

    def test_housing_button_is_shown_for_allowed_user(self):
        with mock.patch("user_handlers.anonymous_posts.housing_monitor.is_allowed", return_value=True):
            keyboard = anonymous_posts.reply_menu_keyboard(user_id=123).to_dict()["keyboard"]
        flattened = [button["text"] for row in keyboard for button in row]

        self.assertIn(anonymous_posts.BTN_HOUSING, flattened)

    def test_feedback_button_is_shown_to_everyone(self):
        keyboard = anonymous_posts.reply_menu_keyboard(user_id=123).to_dict()["keyboard"]
        flattened = [button["text"] for row in keyboard for button in row]

        self.assertIn(anonymous_posts.BTN_FEEDBACK, flattened)


class FakeMessage:
    def __init__(self, text=""):
        self.text = text
        self.chat = SimpleNamespace(type="private")
        self.replies = []

    def reply_text(self, text, **kwargs):
        self.replies.append((text, kwargs))


class FakeBotSender:
    def __init__(self, fail=False):
        self.fail = fail
        self.sent = []

    def send_message(self, chat_id, text, **kwargs):
        if self.fail:
            raise RuntimeError("boom")
        self.sent.append((chat_id, text, kwargs))


class FeedbackTests(unittest.TestCase):
    def test_start_feedback_from_reply_keyboard_prompts_for_text(self):
        message = FakeMessage(text=anonymous_posts.BTN_FEEDBACK)
        update = SimpleNamespace(
            message=message, effective_message=message, callback_query=None,
            effective_user=SimpleNamespace(id=544675510),
        )
        context = SimpleNamespace(user_data={})

        anonymous_posts.handle_private_text(update, context)

        self.assertEqual(context.user_data["feedback"], {"step": "text"})
        self.assertIn("Зворотній звʼязок", message.replies[-1][0])

    def test_feedback_text_is_forwarded_to_admin_and_confirmed(self):
        message = FakeMessage(text="Кнопка редагування не працює на телефоні")
        update = SimpleNamespace(
            message=message, effective_message=message,
            effective_user=SimpleNamespace(id=544675510, username="katya", full_name="Katya"),
        )
        bot = FakeBotSender()
        context = SimpleNamespace(user_data={"feedback": {"step": "text"}}, bot=bot)

        with mock.patch.object(anonymous_posts, "ADMIN_ID", 312029534):
            anonymous_posts.handle_private_text(update, context)

        self.assertNotIn("feedback", context.user_data)
        self.assertEqual(len(bot.sent), 1)
        chat_id, text, _ = bot.sent[0]
        self.assertEqual(chat_id, 312029534)
        self.assertIn("не працює на телефоні", text)
        self.assertIn("544675510", text)
        self.assertIn("Дякуємо", message.replies[-1][0])

    def test_too_short_feedback_is_rejected_and_asked_again(self):
        message = FakeMessage(text="ок")
        update = SimpleNamespace(message=message, effective_message=message, effective_user=SimpleNamespace(id=1))
        context = SimpleNamespace(user_data={"feedback": {"step": "text"}}, bot=FakeBotSender())

        anonymous_posts.handle_private_text(update, context)

        self.assertEqual(context.user_data["feedback"], {"step": "text"})
        self.assertIn("Закоротко", message.replies[-1][0])

    def test_feedback_delivery_failure_still_clears_state_and_tells_the_user(self):
        message = FakeMessage(text="Довге повідомлення про помилку в боті")
        update = SimpleNamespace(
            message=message, effective_message=message, effective_user=SimpleNamespace(id=1),
        )
        context = SimpleNamespace(user_data={"feedback": {"step": "text"}}, bot=FakeBotSender(fail=True))

        with mock.patch.object(anonymous_posts, "ADMIN_ID", 312029534):
            anonymous_posts.handle_private_text(update, context)

        self.assertNotIn("feedback", context.user_data)
        self.assertIn("Не вдалося", message.replies[-1][0])

    def test_cancel_feedback_clears_state(self):
        query = SimpleNamespace(data="anon:feedback_cancel", answer=mock.Mock(), edit_message_text=mock.Mock())
        update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=1))
        context = SimpleNamespace(user_data={"feedback": {"step": "text"}})

        anonymous_posts.handle_callback(update, context)

        self.assertNotIn("feedback", context.user_data)
        query.answer.assert_called_once()


class FakeBot:
    def __init__(self):
        self.commands = []
        self.menu_buttons = []

    def set_my_commands(self, commands, **kwargs):
        self.commands.append((commands, kwargs))
        return True

    def set_chat_menu_button(self, **kwargs):
        self.menu_buttons.append(kwargs)
        return True


class BotCommandMenuTests(unittest.TestCase):
    def test_private_commands_and_menu_button_are_registered(self):
        from user_jobs.commands_set import set_bot_commands

        bot = FakeBot()
        set_bot_commands(SimpleNamespace(bot=bot))

        self.assertGreaterEqual(len(bot.commands), 2)
        private_commands, private_kwargs = bot.commands[0]
        self.assertEqual(private_commands[0][0], "start")
        self.assertEqual(private_commands[1][0], "anonymous")
        self.assertEqual(private_commands[2][0], "dps_document")
        self.assertEqual(private_commands[3][0], "housing")
        self.assertEqual(private_kwargs["scope"].type, "all_private_chats")
        self.assertTrue(bot.menu_buttons)


if __name__ == "__main__":
    unittest.main()
