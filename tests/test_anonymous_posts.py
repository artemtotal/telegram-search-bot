import unittest
from datetime import datetime
from types import SimpleNamespace

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
        self.assertEqual(private_kwargs["scope"].type, "all_private_chats")
        self.assertTrue(bot.menu_buttons)


if __name__ == "__main__":
    unittest.main()
