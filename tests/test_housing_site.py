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


if __name__ == "__main__":
    unittest.main()
