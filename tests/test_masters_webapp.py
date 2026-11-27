import unittest
from types import SimpleNamespace
from unittest import mock

import telegram
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import i18n
from database import Base
from user_handlers import anonymous_posts, masters_hint as mh, webapp_access
from user_jobs import commands_set, webapp_store

URL = "https://example.test/"


def _on():
    return mock.patch.multiple(mh, MASTERS_URL=URL, WEBAPP=True, APP_LINK="")


class StartKeyboardTests(unittest.TestCase):
    def _row(self):
        with mock.patch.object(anonymous_posts, "MASTERS_URL", URL):
            return anonymous_posts._home_keyboard(None).inline_keyboard[1][0]

    def test_off_by_default_is_plain_url(self):
        with mock.patch.multiple(mh, MASTERS_URL=URL, WEBAPP=False):
            button = self._row()
        self.assertIsNone(button.web_app)
        self.assertIn("s=bot", button.url)

    def test_on_is_web_app_button(self):
        with _on():
            button = self._row()
        self.assertIsNone(button.url)
        self.assertEqual(button.web_app.url, URL + "?s=bot&tab=masters#tab=masters")

    def test_http_url_never_web_app(self):
        with mock.patch.multiple(mh, MASTERS_URL="http://example.test/", WEBAPP=True):
            self.assertIsNone(mh.webapp_info())


class MastersCommandWebappTests(unittest.TestCase):
    def setUp(self):
        mh._cmd_last.clear()
        patcher = mock.patch.object(i18n, "get_lang", return_value="uk")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run(self, chat_type, chat_id=1):
        msg = SimpleNamespace(reply_text=mock.Mock())
        upd = SimpleNamespace(effective_message=msg, effective_chat=SimpleNamespace(type=chat_type, id=chat_id),
                              effective_user=SimpleNamespace(id=5))
        mh.masters_command(upd, None)
        return msg.reply_text.call_args.kwargs["reply_markup"].inline_keyboard[0][0]

    def test_private_gets_web_app(self):
        with _on():
            button = self._run("private")
        self.assertEqual(button.web_app.url, URL + "?s=bot")

    def test_group_never_web_app_without_app_link(self):
        with _on():
            button = self._run("supergroup", -100)
        self.assertIsNone(button.web_app)
        self.assertIn("s=hint", button.url)

    def test_group_uses_app_link_when_set(self):
        with mock.patch.multiple(mh, MASTERS_URL=URL, WEBAPP=True, APP_LINK="https://t.me/Potsdambot/maistry"):
            button = self._run("supergroup", -101)
        self.assertEqual(button.url, "https://t.me/Potsdambot/maistry")


class AppLinkTests(unittest.TestCase):
    def test_hint_links_without_app_link_unchanged(self):
        with mock.patch.multiple(mh, MASTERS_URL=URL, APP_LINK=""):
            self.assertEqual(mh.link("hint", cat="beauty"), URL + "?s=hint#cat=beauty")
            self.assertEqual(mh.link("hint", card="abc_1"), URL + "?s=hint#id=abc_1")

    def test_hint_links_with_app_link(self):
        with mock.patch.multiple(mh, MASTERS_URL=URL, APP_LINK="https://t.me/Potsdambot/maistry"):
            self.assertEqual(mh.link("hint", cat="beauty"), "https://t.me/Potsdambot/maistry?startapp=cat_beauty")
            self.assertEqual(mh.link("hint", card="abc_1"), "https://t.me/Potsdambot/maistry?startapp=id_abc_1")
            self.assertEqual(mh.link("hint"), "https://t.me/Potsdambot/maistry")
            self.assertIn("startapp=cat_home", mh.text_for("home"))
            self.assertEqual(mh.link("bot"), URL + "?s=bot")


class MenuButtonTests(unittest.TestCase):
    def _run(self):
        bot = SimpleNamespace(set_my_commands=mock.Mock(), set_chat_menu_button=mock.Mock())
        commands_set.set_bot_commands(SimpleNamespace(bot=bot))
        return bot.set_chat_menu_button.call_args.kwargs["menu_button"]

    def test_default_is_command_list(self):
        with mock.patch.multiple(mh, MASTERS_URL=URL, WEBAPP=False):
            self.assertIsInstance(self._run(), telegram.MenuButtonCommands)

    def test_webapp_on_sets_web_app_menu(self):
        with _on():
            button = self._run()
        self.assertIsInstance(button, telegram.MenuButtonWebApp)
        self.assertEqual(button.web_app.url, URL + "?s=bot")
        self.assertEqual(button.text, "Каталог")

    def test_menu_text_in_all_languages(self):
        for lang in ("uk", "ru", "de"):
            self.assertNotIn("[[", i18n.t("masters.menu.btn", lang))
            self.assertLessEqual(len(i18n.t("masters.menu.btn", lang)), 16)


class WriteAccessTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.original = webapp_store.DBSession
        webapp_store.DBSession = sessionmaker(bind=self.engine)

    def tearDown(self):
        webapp_store.DBSession = self.original
        self.engine.dispose()

    def _update(self, extra):
        message = {"message_id": 1, "date": 1, "chat": {"id": 42, "type": "private"}, "from": {"id": 42, "is_bot": False, "first_name": "A"}}
        message.update(extra)
        return telegram.Update.de_json({"update_id": 1, "message": message}, mock.Mock())

    def test_service_message_is_kept_matched_and_stored_without_reply(self):
        update = self._update({"write_access_allowed": {"web_app_name": "maistry"}})
        self.assertEqual(update.message.raw_write_access_allowed, {"web_app_name": "maistry"})
        self.assertTrue(webapp_access.handler.check_update(update))
        update.message.reply_text = mock.Mock()
        webapp_access.handle(update, None)
        self.assertTrue(webapp_store.is_write_allowed(42))
        update.message.reply_text.assert_not_called()

    def test_empty_service_message_dict_also_counts(self):
        update = self._update({"write_access_allowed": {}})
        self.assertTrue(webapp_access.handler.check_update(update))

    def test_plain_text_is_not_matched(self):
        update = self._update({"text": "привіт"})
        self.assertFalse(webapp_access.handler.check_update(update))
        self.assertFalse(webapp_store.is_write_allowed(42))

    def test_repeat_does_not_duplicate(self):
        self.assertTrue(webapp_store.mark_write_allowed(7))
        self.assertFalse(webapp_store.mark_write_allowed(7))
        self.assertEqual(webapp_store.count_allowed(), 1)


class StartParamTests(unittest.TestCase):
    def _update(self):
        msg = SimpleNamespace(from_user=SimpleNamespace(id=5), reply_text=mock.Mock())
        return SimpleNamespace(message=msg, effective_message=msg, effective_chat=SimpleNamespace(type="private", id=5),
                               effective_user=SimpleNamespace(id=5))

    def test_start_housing_opens_housing_menu(self):
        from user_handlers import chat_start, housing_monitor
        with mock.patch.object(housing_monitor, "show_menu") as menu, \
                mock.patch("user_handlers.anonymous_posts.show_home") as home:
            chat_start.start(self._update(), SimpleNamespace(args=["housing"]))
        menu.assert_called_once()
        home.assert_not_called()

    def test_start_masters_answers_like_command(self):
        from user_handlers import chat_start
        with mock.patch.object(mh, "masters_command") as cmd, mock.patch("user_handlers.anonymous_posts.show_home") as home:
            chat_start.start(self._update(), SimpleNamespace(args=["masters"]))
        cmd.assert_called_once()
        home.assert_not_called()

    def test_other_params_still_go_home(self):
        from user_handlers import chat_start
        with mock.patch("user_handlers.anonymous_posts.show_home") as home:
            chat_start.start(self._update(), SimpleNamespace(args=[]))
        home.assert_called_once()


if __name__ == "__main__":
    unittest.main()
