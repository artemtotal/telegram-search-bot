import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import i18n
from database import Base
from user_handlers import anonymous_validation
from user_handlers import anonymous_posts
from user_jobs import user_settings_store


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
        self.assertIn(anonymous_posts.i18n.t("anon.btn.menu"), flattened)
        self.assertIn(anonymous_posts.BTN_EQUEUE, flattened)

    def test_housing_button_is_shown_for_allowed_user(self):
        with mock.patch("user_handlers.anonymous_posts.housing_monitor.is_allowed", return_value=True):
            keyboard = anonymous_posts.reply_menu_keyboard(user_id=123).to_dict()["keyboard"]
        flattened = [button["text"] for row in keyboard for button in row]

        self.assertIn(anonymous_posts.BTN_HOUSING, flattened)

    def test_housing_button_is_shown_even_without_access(self):
        # The bottom keyboard used to hide this button until access was
        # granted, leaving it reachable only from the top inline menu.
        # housing_monitor.show_menu() already renders its own locked
        # screen (pricing + request-access button) for people without
        # access, so there's no reason to hide the shortcut too.
        with mock.patch("user_handlers.anonymous_posts.housing_monitor.is_allowed", return_value=False):
            keyboard = anonymous_posts.reply_menu_keyboard(user_id=123).to_dict()["keyboard"]
        flattened = [button["text"] for row in keyboard for button in row]

        self.assertIn(anonymous_posts.BTN_HOUSING, flattened)

    def test_housing_button_opens_the_menu_even_without_access(self):
        # Same reasoning as the keyboard test above: the text dispatcher
        # used to gate this on is_allowed() too, so tapping the (now always
        # visible) button silently fell through to the home screen instead
        # of housing_monitor's own locked/request-access screen.
        message = SimpleNamespace(text=anonymous_posts.BTN_HOUSING, chat=SimpleNamespace(type="private"))
        update = SimpleNamespace(
            message=message, effective_message=message, callback_query=None,
            effective_user=SimpleNamespace(id=999),
        )
        context = SimpleNamespace(user_data={})

        with mock.patch("user_handlers.anonymous_posts.housing_monitor.is_allowed", return_value=False), \
             mock.patch("user_handlers.anonymous_posts.housing_monitor.show_menu") as show_menu:
            anonymous_posts.handle_private_text(update, context)

        show_menu.assert_called_once_with(update, context)

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


class LanguageSwitcherTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            'sqlite://',
            connect_args={'check_same_thread': False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.original_session = user_settings_store.DBSession
        user_settings_store.DBSession = sessionmaker(bind=self.engine)

    def tearDown(self):
        user_settings_store.DBSession = self.original_session
        self.engine.dispose()

    def test_lang_menu_button_is_on_the_home_screen(self):
        keyboard = anonymous_posts._home_keyboard(544675510)
        labels = [button.text for row in keyboard.inline_keyboard for button in row]

        self.assertIn("🌐 Мова / Язык / Sprache", labels)

    def test_opening_the_picker_shows_all_three_languages(self):
        query = SimpleNamespace(data="anon:lang:menu", answer=mock.Mock(), edit_message_text=mock.Mock())
        update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=1))
        context = SimpleNamespace(user_data={})

        anonymous_posts.handle_callback(update, context)

        query.answer.assert_called_once()
        keyboard = query.edit_message_text.call_args.kwargs["reply_markup"]
        labels = [button.text for row in keyboard.inline_keyboard for button in row]
        self.assertIn("🇺🇦 Українська", labels)
        self.assertIn("🇷🇺 Русский", labels)
        self.assertIn("🇩🇪 Deutsch", labels)

    def test_picking_a_language_stores_it_and_returns_home(self):
        query = SimpleNamespace(
            data="anon:lang:set:ru", answer=mock.Mock(), edit_message_text=mock.Mock(),
            from_user=SimpleNamespace(id=777),
        )
        update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=777))
        context = SimpleNamespace(user_data={})

        anonymous_posts.handle_callback(update, context)

        self.assertEqual(user_settings_store.get_language(777), 'ru')
        query.edit_message_text.assert_called_once()

    def test_an_unsupported_language_code_is_ignored(self):
        query = SimpleNamespace(
            data="anon:lang:set:fr", answer=mock.Mock(), edit_message_text=mock.Mock(),
            from_user=SimpleNamespace(id=777),
        )
        update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=777))
        context = SimpleNamespace(user_data={})

        anonymous_posts.handle_callback(update, context)

        self.assertEqual(user_settings_store.get_language(777), 'uk')


class AnonSubmenuTests(unittest.TestCase):
    """"✍️ Анонімні запитання" - "ask" and "my posts" used to be two separate
    top-level home-screen buttons; now they're grouped one level down."""

    def setUp(self):
        self.engine = create_engine(
            'sqlite://',
            connect_args={'check_same_thread': False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.original_session = user_settings_store.DBSession
        user_settings_store.DBSession = sessionmaker(bind=self.engine)

    def tearDown(self):
        user_settings_store.DBSession = self.original_session
        self.engine.dispose()

    def test_home_screen_offers_one_consolidated_anon_button(self):
        keyboard = anonymous_posts._home_keyboard(544675510)
        callbacks = [b.callback_data for row in keyboard.inline_keyboard for b in row]

        self.assertIn("anon:menu", callbacks)
        self.assertNotIn("anon:new", callbacks)
        self.assertNotIn("anon:mine", callbacks)

    def test_submenu_offers_ask_my_posts_and_a_way_back(self):
        callbacks = [
            b.callback_data for row in anonymous_posts._anon_submenu_keyboard().inline_keyboard for b in row
        ]

        self.assertIn("anon:new", callbacks)
        self.assertIn("anon:mine", callbacks)
        self.assertIn("anon:home", callbacks)

    def test_callback_anon_menu_opens_the_submenu(self):
        query = SimpleNamespace(data="anon:menu", answer=mock.Mock(), edit_message_text=mock.Mock())
        update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=544675510))
        context = SimpleNamespace(user_data={})

        anonymous_posts.handle_callback(update, context)

        query.answer.assert_called_once()
        text, kwargs = query.edit_message_text.call_args.args[0], query.edit_message_text.call_args.kwargs
        self.assertIn("Анонімні запитання", text)
        # Explains what this even is for people new to the bot - it's not
        # obvious "anonymous questions" means "posted into the Potsdam group
        # chat" without saying so explicitly.
        self.assertIn("чат Потсдама", text)
        callbacks = [b.callback_data for row in kwargs["reply_markup"].inline_keyboard for b in row]
        self.assertIn("anon:new", callbacks)
        self.assertIn("anon:mine", callbacks)

    def test_submenu_explains_the_potsdam_chat_in_russian_and_german(self):
        ru = anonymous_posts.i18n.t("anon.submenu.text", "ru")
        de = anonymous_posts.i18n.t("anon.submenu.text", "de")

        self.assertIn("чате Потсдама", ru)
        self.assertIn("Potsdam-Chat", de)

    def test_submenu_in_russian_and_german(self):
        ru = anonymous_posts._anon_submenu_keyboard(lang='ru')
        de = anonymous_posts._anon_submenu_keyboard(lang='de')

        self.assertIn('Задать анонимный вопрос', ru.inline_keyboard[0][0].text)
        self.assertIn('Anonyme Frage stellen', de.inline_keyboard[0][0].text)

    def test_reply_keyboard_has_one_consolidated_anon_button(self):
        labels = [
            b for row in anonymous_posts.reply_menu_keyboard(544675510).to_dict()["keyboard"] for b in row
        ]
        texts = [b["text"] for b in labels]

        self.assertIn(anonymous_posts.i18n.t("anon.btn.menu"), texts)
        self.assertNotIn("📋 Мої публікації", texts)

    def test_tapping_the_reply_keyboard_button_opens_the_submenu(self):
        message = FakeMessage(text=anonymous_posts.i18n.t("anon.btn.menu"))
        update = SimpleNamespace(
            message=message, effective_message=message, callback_query=None,
            effective_user=SimpleNamespace(id=544675510),
        )
        context = SimpleNamespace(user_data={})

        with mock.patch("user_handlers.anonymous_posts.housing_monitor.handle_private_text", return_value=False):
            anonymous_posts.handle_private_text(update, context)

        self.assertTrue(message.replies)
        text, kwargs = message.replies[0]
        self.assertIn("Анонімні запитання", text)
        callbacks = [b.callback_data for row in kwargs["reply_markup"].inline_keyboard for b in row]
        self.assertIn("anon:new", callbacks)
        self.assertIn("anon:mine", callbacks)


class AnonymousPostsTranslationSmokeTests(unittest.TestCase):
    """Not a full duplicate of every uk-language assertion - just enough per
    converted screen to prove lang actually reaches the rendered text."""

    def test_validation_errors_in_russian_and_german(self):
        ru = anonymous_validation.validate_submission("short", lang='ru')
        de = anonymous_validation.validate_submission("short", lang='de')

        self.assertIn('слишком короткий', ru)
        self.assertIn('zu kurz', de)

    def test_cooldown_text_in_russian_and_german(self):
        now = datetime(2026, 8, 21, 12, 0, 0)
        last = now - timedelta(days=6, hours=1)

        ru = anonymous_validation.cooldown_text(last, 7, now, lang='ru')
        de = anonymous_validation.cooldown_text(last, 7, now, lang='de')

        self.assertIn('Новый анонимный пост', ru)
        self.assertIn('anonymer Beitrag', de)

    def test_home_screen_in_russian_and_german(self):
        ru = i18n.t('anon.home.text', 'ru', days=7)
        de = i18n.t('anon.home.text', 'de', days=7)

        self.assertIn('Анонимный вопрос в чате Потсдама', ru)
        self.assertIn('Anonyme Frage im Potsdam-Chat', de)

    def test_reply_keyboard_in_german(self):
        with mock.patch.object(user_settings_store, 'get_language', return_value='uk'):
            keyboard_uk = anonymous_posts.reply_menu_keyboard(544675510)
        with mock.patch.object(user_settings_store, 'get_language', return_value='de'):
            keyboard_de = anonymous_posts.reply_menu_keyboard(544675510)

        labels_uk = [button.text for row in keyboard_uk.keyboard for button in row]
        labels_de = [button.text for row in keyboard_de.keyboard for button in row]
        self.assertIn('🏠 Меню', labels_uk)
        self.assertIn('🏠 Menü', labels_de)


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
