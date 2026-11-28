"""28.11.2026: строка журнала «housing send: source=... photo=... n=... fallback=...» на каждую отправку квартиры."""
import unittest
from unittest import mock

import telegram
from telegram import InputMediaPhoto
from telegram.error import BadRequest

from user_jobs import housing_photo_fallback as hpf
from user_jobs import housing_tier


class SendLog(unittest.TestCase):
    def setUp(self):
        self.saved = (telegram.Bot.send_photo, telegram.Bot.send_media_group)
        test = self
        self.photo_error = None
        self.group_errors = []

        def send_photo(bot, chat_id=None, photo=None, caption=None, **kw):
            if isinstance(photo, str) and test.photo_error is not None:
                raise test.photo_error
            return "m"

        def send_media_group(bot, chat_id=None, media=None, **kw):
            if test.group_errors:
                raise test.group_errors.pop(0)
            return ["m"] * len(media)

        telegram.Bot.send_photo, telegram.Bot.send_media_group = send_photo, send_media_group
        hpf.install()
        self.bot = telegram.Bot.__new__(telegram.Bot)
        hpf.reset_trace()

    def tearDown(self):
        telegram.Bot.send_photo, telegram.Bot.send_media_group = self.saved

    def line(self, source="karlmarx"):
        with self.assertLogs(hpf.logger, "INFO") as logs:
            hpf.log_send(source)
        self.assertEqual(len(logs.records), 1)
        return logs.records[0].getMessage()

    def test_photo(self):
        self.bot.send_photo(chat_id=555, photo="https://x/a.jpg", caption="Hauptstr. 1")
        self.assertEqual(self.line(), "housing send: source=karlmarx photo=photo n=1 fallback=no")

    def test_album(self):
        media = [InputMediaPhoto(media="https://x/%d.jpg" % i) for i in range(3)]
        self.bot.send_media_group(chat_id=555, media=media)
        self.assertEqual(self.line("vonovia"), "housing send: source=vonovia photo=album n=3 fallback=no")

    def test_file_after_telegram_refused_the_url(self):
        self.photo_error = BadRequest('Failed to send message #1 with the error message "webpage_curl_failed"')
        with mock.patch.object(hpf, "fetch", return_value=b"jpegbytes"):
            self.bot.send_photo(chat_id=555, photo="https://x/a.jpg")
        self.assertEqual(self.line(), "housing send: source=karlmarx photo=file n=1 fallback=yes")

    def test_album_resent_after_refusal(self):
        self.group_errors = [BadRequest('Failed to send message #2 with the error message "webpage_curl_failed"')]
        media = [InputMediaPhoto(media="https://x/%d.jpg" % i) for i in range(3)]
        with mock.patch.object(hpf, "fetch", return_value=None):      # плохая выпала, остались две
            self.bot.send_media_group(chat_id=555, media=media)
        self.assertEqual(self.line(), "housing send: source=karlmarx photo=album n=2 fallback=yes")

    def test_text_only_when_no_photo_was_tried(self):
        self.assertEqual(self.line(), "housing send: source=karlmarx photo=none n=0 fallback=no")

    def test_text_after_photo_failure(self):
        self.photo_error = BadRequest('Failed to send message #1 with the error message "webpage_curl_failed"')
        with mock.patch.object(hpf, "fetch", return_value=None):
            with self.assertRaises(BadRequest):
                self.bot.send_photo(chat_id=555, photo="https://x/a.jpg")
        self.assertEqual(self.line(), "housing send: source=karlmarx photo=none n=0 fallback=yes")

    def test_line_has_no_ids_or_addresses_and_trace_is_consumed(self):
        self.bot.send_photo(chat_id=555123, photo="https://x/Hauptstrasse1.jpg", caption="Hauptstrasse 1")
        text = self.line()
        self.assertNotIn("555123", text)
        self.assertNotIn("Hauptstrasse", text)
        self.assertIn("photo=none", self.line())          # след израсходован

    def test_gate_sent_writes_the_line_and_due_resets_the_trace(self):
        gate = housing_tier.DeliveryGate("schoba")
        self.bot.send_photo(chat_id=1, photo="https://x/a.jpg")
        with mock.patch.object(housing_tier, "log_sent"), mock.patch.object(housing_tier, "forget_held"), \
                mock.patch.object(housing_tier.DeliveryGate, "_snapshot", return_value=None), \
                mock.patch.object(housing_tier.DeliveryGate, "is_premium", return_value=True), \
                self.assertLogs(hpf.logger, "INFO") as logs:
            gate.sent(1, "k1")
        self.assertEqual(logs.records[0].getMessage(), "housing send: source=schoba photo=photo n=1 fallback=no")
        self.bot.send_photo(chat_id=1, photo="https://x/b.jpg")
        with mock.patch.object(housing_tier.DeliveryGate, "_sublet_unwanted", return_value=True):
            gate.due(1, {})
        self.assertIn("photo=none", self.line("schoba"))


if __name__ == "__main__":
    unittest.main()
