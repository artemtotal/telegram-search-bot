import unittest
from types import SimpleNamespace
from unittest import mock

import i18n
from user_handlers import anonymous_posts, masters_hint as mh


class MastersButtonOrderTests(unittest.TestCase):
    def test_button_is_on_second_row_with_new_names(self):
        with mock.patch.object(anonymous_posts, "MASTERS_URL", "https://example.test/"), \
                mock.patch.object(mh, "MASTERS_URL", "https://example.test/"):
            rows = anonymous_posts._home_keyboard(None).inline_keyboard
        self.assertEqual(rows[0][0].callback_data, "anon:menu")
        self.assertEqual(rows[1][0].text, "🧰 Контакти майстрів і послуг")
        self.assertIn("s=bot", rows[1][0].url)

    def test_names_in_three_languages(self):
        self.assertEqual(i18n.t("anon.btn.masters", "ru"), "🧰 Контакты мастеров и услуг")
        self.assertEqual(i18n.t("anon.btn.masters", "de"), "🧰 Kontakte: Handwerker & Dienste")
        for lang in ("uk", "ru", "de"):
            self.assertNotIn("[[", i18n.t("masters.cmd.text", lang))
            self.assertNotIn("[[", i18n.t("masters.cmd.btn", lang))


class MastersCommandTests(unittest.TestCase):
    def setUp(self):
        mh._cmd_last.clear()
        self.patch = mock.patch.object(mh, "MASTERS_URL", "https://example.test/")
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.lang = mock.patch.object(i18n, "get_lang", return_value="ru")
        self.lang.start()
        self.addCleanup(self.lang.stop)

    def _update(self, chat_type, chat_id=1):
        msg = SimpleNamespace(reply_text=mock.Mock())
        return SimpleNamespace(effective_message=msg, effective_chat=SimpleNamespace(type=chat_type, id=chat_id),
                               effective_user=SimpleNamespace(id=5)), msg

    def test_private_reply_uses_bot_source(self):
        upd, msg = self._update("private")
        mh.masters_command(upd, None)
        kw = msg.reply_text.call_args.kwargs
        self.assertIn("Контакты мастеров", msg.reply_text.call_args.args[0])
        self.assertIn("s=bot", kw["reply_markup"].inline_keyboard[0][0].url)

    def test_group_reply_once_per_gap_and_hint_source(self):
        upd, msg = self._update("supergroup", -100)
        mh.masters_command(upd, None)
        mh.masters_command(upd, None)
        self.assertEqual(msg.reply_text.call_count, 1)
        self.assertIn("s=hint", msg.reply_text.call_args.kwargs["reply_markup"].inline_keyboard[0][0].url)

    def test_no_url_no_reply(self):
        upd, msg = self._update("private")
        with mock.patch.object(mh, "MASTERS_URL", ""):
            mh.masters_command(upd, None)
        msg.reply_text.assert_not_called()

    def test_command_names(self):
        self.assertEqual(sorted(mh.command_handler.command), ["maistry", "mastera", "masters"])


if __name__ == "__main__":
    unittest.main()
