import unittest
from datetime import datetime
from types import SimpleNamespace

from user_handlers import anonymous_validation


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
            "Новый анонимный пост",
            anonymous_validation.cooldown_text(user.last_submission_at, 7, now),
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


if __name__ == "__main__":
    unittest.main()
