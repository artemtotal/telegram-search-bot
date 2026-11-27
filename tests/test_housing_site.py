import hashlib
import hmac
import os
import re
import tempfile
import time
import unittest
from unittest import mock

from user_handlers import housing_site


class SiteCodeTests(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp()
        os.write(fd, b"test-secret\n")
        os.close(fd)
        self.patch = mock.patch.object(housing_site, "SECRET_FILE", self.path)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        os.remove(self.path)

    def test_code_format_and_signature_match_the_site(self):
        code = housing_site.make_code(1001, now=1_800_000_000)
        match = re.fullmatch(r"(\d+)\.(\d+)\.([0-9a-f]{8})\.([0-9a-f]{40})", code)
        self.assertTrue(match)
        uid, ts, nonce, sig = match.groups()
        calc = hmac.new(b"test-secret", f"login:{uid}.{ts}.{nonce}".encode(), hashlib.sha256).hexdigest()[:40]
        self.assertEqual((uid, ts, sig), ("1001", "1800000000", calc))

    def test_codes_differ(self):
        self.assertNotEqual(housing_site.make_code(1, now=5), housing_site.make_code(1, now=5))

    def test_no_secret_file_means_no_link(self):
        with mock.patch.object(housing_site, "SECRET_FILE", self.path + ".missing"):
            self.assertIsNone(housing_site.make_code(1))
            self.assertIsNone(housing_site.site_link(1))

    def test_link_has_code_on_the_site_origin(self):
        with mock.patch.object(housing_site, "site_origin", return_value="https://example.org"):
            link = housing_site.site_link(7)
        self.assertTrue(link.startswith("https://example.org/?k=7."))

    def test_texts_exist_in_all_languages(self):
        import i18n
        for lang in ("uk", "ru", "de"):
            for key in ("housing.site.text", "housing.site.btn", "housing.site.btn_old", "housing.site.off"):
                self.assertNotEqual(i18n.t(key, lang), key)
        self.assertNotEqual(i18n.t("housing.site.text", "de"), i18n.t("housing.site.text", "uk"))


class MiniAppEntryTests(unittest.TestCase):
    """27.11.2026: /site и кнопки жилья открывают Mini App на вкладке «Житло», а не ссылку входа для Chrome."""

    def _send(self, webapp):
        from types import SimpleNamespace
        from user_handlers import masters_hint as mh
        bot = mock.Mock()
        context = SimpleNamespace(bot=bot)
        with mock.patch.multiple(mh, MASTERS_URL="https://example.test/", WEBAPP=webapp, APP_LINK="https://t.me/Potsdambot/potsdam"):
            housing_site.send_link(context, 5, with_old=True)
        return bot.send_message.call_args.kwargs

    def test_site_command_gives_web_app_button_on_housing_tab(self):
        kwargs = self._send(True)
        rows = kwargs["reply_markup"].inline_keyboard
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0].web_app.url, "https://example.test/?s=bot&tab=housing#tab=housing")
        self.assertIsNone(rows[0][0].url)

    def test_text_has_mini_app_link_not_login_code(self):
        kwargs = self._send(True)
        self.assertIn("https://t.me/Potsdambot/potsdam?startapp=tab_housing", kwargs["text"])
        self.assertNotIn("?k=", kwargs["text"])

    def test_without_webapp_plain_url_on_housing_tab_never_login_code(self):
        rows = self._send(False)["reply_markup"].inline_keyboard
        self.assertTrue(rows[0][0].url.endswith("#tab=housing"))
        self.assertNotIn("?k=", rows[0][0].url)

    def test_home_button_is_web_app_when_on_and_menu_callback_when_off(self):
        from user_handlers import housing_monitor, masters_hint as mh
        with mock.patch.multiple(mh, MASTERS_URL="https://example.test/", WEBAPP=True):
            button = housing_monitor.private_home_rows(5)[0][0]
        self.assertEqual(button.web_app.url, "https://example.test/?s=bot&tab=housing#tab=housing")
        with mock.patch.multiple(mh, MASTERS_URL="https://example.test/", WEBAPP=False):
            button = housing_monitor.private_home_rows(5)[0][0]
        self.assertEqual(button.callback_data, "housing:menu")

    def test_text_link_is_in_all_languages(self):
        import i18n
        for lang in ("uk", "ru", "de"):
            self.assertIn("https://x.y", i18n.t("housing.site.text", lang, link="https://x.y"))


if __name__ == "__main__":
    unittest.main()
