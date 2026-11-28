"""28.11.2026: фото в уведомлениях о квартирах. Telegram не смог скачать картинку: качаем сами; рекламный баннер не фото;
мост отдаёт сайту адрес фото (в том числе для Immowelt и «Підходящі зараз»)."""
import io
import json
import unittest
from unittest import mock

import telegram
from telegram import InputMediaPhoto
from telegram.error import BadRequest, Unauthorized as Forbidden

from user_jobs import housing_photo_fallback as hpf


def _jpeg(size=(40, 30)):
    from PIL import Image
    out = io.BytesIO()
    Image.new("RGB", size, (200, 10, 10)).save(out, "JPEG")
    return out.getvalue()


def _webp():
    from PIL import Image
    out = io.BytesIO()
    Image.new("RGB", (40, 30), (10, 10, 200)).save(out, "WEBP")
    return out.getvalue()


class FakeResponse:
    def __init__(self, status=200, body=b"", headers=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_content(self, size):
        for i in range(0, len(self._body), size):
            yield self._body[i:i + size]


class Classify(unittest.TestCase):
    def test_retriable_only_for_fetch_failures(self):
        self.assertTrue(hpf.retriable(BadRequest('Failed to send message #4 with the error message "webpage_curl_failed"')))
        self.assertTrue(hpf.retriable(BadRequest("Failed to get http url content")))
        self.assertFalse(hpf.retriable(BadRequest('Failed to send message #1 with the error message "user_is_blocked"')))
        self.assertFalse(hpf.retriable(Forbidden("bot was blocked by the user")))
        self.assertFalse(hpf.retriable(ValueError("webpage_curl_failed")))

    def test_failed_index(self):
        exc = BadRequest('Failed to send message #4 with the error message "webpage_curl_failed"')
        self.assertEqual(hpf.failed_index(exc, 10), 3)
        self.assertIsNone(hpf.failed_index(exc, 3))
        self.assertIsNone(hpf.failed_index(BadRequest("Failed to get http url content"), 5))

    def test_placeholder(self):
        self.assertTrue(hpf.is_placeholder("https://cdn.expose.vonovia.de/CAMP-Gruenstrom_v5.jpg?width=1200"))
        self.assertFalse(hpf.is_placeholder("https://cdn.expose.vonovia.de/DA00FCBA000001EC.jpg?width=1200"))
        self.assertEqual(hpf.real_photos(["", "https://x/CAMP-a.jpg", "https://x/a.jpg"]), ["https://x/a.jpg"])


class Fetch(unittest.TestCase):
    def _fetch(self, response, url="https://img.example.com/a.jpg"):
        with mock.patch("requests.get", return_value=response) as get:
            return hpf.fetch(url), get

    def test_jpeg_passes_as_is(self):
        data = _jpeg()
        got, get = self._fetch(FakeResponse(200, data))
        self.assertEqual(got, data)
        self.assertEqual(get.call_count, 1)
        self.assertEqual(get.call_args.kwargs["timeout"], (hpf.CONNECT_TIMEOUT, hpf.READ_TIMEOUT))

    def test_webp_is_recoded_to_jpeg(self):
        got, _ = self._fetch(FakeResponse(200, _webp()))
        self.assertTrue(got.startswith(b"\xff\xd8"))

    def test_404_html_oversize_and_junk_are_refused(self):
        self.assertIsNone(self._fetch(FakeResponse(404, b"x"))[0])
        self.assertIsNone(self._fetch(FakeResponse(200, b"<html>no</html>"))[0])
        self.assertIsNone(self._fetch(FakeResponse(200, b"x", {"Content-Length": str(hpf.MAX_BYTES + 1)}))[0])
        with mock.patch.object(hpf, "MAX_BYTES", 10):
            self.assertIsNone(self._fetch(FakeResponse(200, _jpeg()))[0])

    def test_network_error_is_refused(self):
        with mock.patch("requests.get", side_effect=OSError("boom")):
            self.assertIsNone(hpf.fetch("https://img.example.com/a.jpg"))

    def test_private_and_non_http_addresses_are_not_fetched(self):
        with mock.patch("requests.get") as get:
            for url in ("http://127.0.0.1/a.jpg", "http://10.0.0.5/a.jpg", "http://localhost/a.jpg", "file:///etc/passwd", "ftp://x/a.jpg"):
                self.assertIsNone(hpf.fetch(url))
            get.assert_not_called()


class Wrapper(unittest.TestCase):
    """install() на подставной telegram.Bot: оригинал отказывает так, как отказывает Telegram."""

    def setUp(self):
        self.saved = (telegram.Bot.send_photo, telegram.Bot.send_media_group)
        self.calls = []
        self.fail = []   # очередь исключений для альбома
        test = self

        def send_photo(bot, chat_id=None, photo=None, caption=None, **kwargs):
            test.calls.append(("photo", photo if isinstance(photo, (bytes, str)) else "?", caption, kwargs))
            if isinstance(photo, str) and test.photo_error is not None:
                raise test.photo_error
            return "photo-msg"

        def send_media_group(bot, chat_id=None, media=None, **kwargs):
            test.calls.append(("group", [m.media if isinstance(m.media, str) else "FILE" for m in media], [getattr(m, "caption", None) for m in media]))
            if test.fail:
                raise test.fail.pop(0)
            return ["group-msg"]

        telegram.Bot.send_photo, telegram.Bot.send_media_group = send_photo, send_media_group
        self.photo_error = BadRequest('Failed to send message #1 with the error message "webpage_curl_failed"')
        hpf.install()
        self.bot = telegram.Bot.__new__(telegram.Bot)

    def tearDown(self):
        telegram.Bot.send_photo, telegram.Bot.send_media_group = self.saved

    def test_install_is_idempotent_here_and_in_the_process(self):
        wrapped = telegram.Bot.send_photo
        self.assertFalse(hpf.install())
        self.assertIs(telegram.Bot.send_photo, wrapped)

    def test_single_photo_resent_as_file(self):
        data = _jpeg()
        with mock.patch.object(hpf, "fetch", return_value=data) as fetch:
            result = self.bot.send_photo(chat_id=1, photo="https://x/a.jpg", caption="text", parse_mode="HTML")
        self.assertEqual(result, "photo-msg")
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(self.calls[-1][1], data)
        self.assertEqual(self.calls[-1][2], "text")
        self.assertEqual(self.calls[-1][3], {"parse_mode": "HTML"})

    def test_single_photo_not_downloadable_raises_original_for_text_fallback(self):
        with mock.patch.object(hpf, "fetch", return_value=None):
            with self.assertRaises(BadRequest):
                self.bot.send_photo(chat_id=1, photo="https://x/a.jpg", caption="text")
        self.assertEqual(len(self.calls), 1)   # без повтора

    def test_other_errors_pass_through_untouched(self):
        self.photo_error = Forbidden("bot was blocked by the user")
        with mock.patch.object(hpf, "fetch") as fetch:
            with self.assertRaises(Forbidden):
                self.bot.send_photo(chat_id=1, photo="https://x/a.jpg")
        fetch.assert_not_called()

    def test_album_only_the_named_photo_is_downloaded(self):
        urls = ["https://x/%d.jpg" % i for i in range(1, 6)]
        media = [InputMediaPhoto(media=urls[0], caption="cap", parse_mode="HTML")] + [InputMediaPhoto(media=u) for u in urls[1:]]
        self.fail = [BadRequest('Failed to send message #4 with the error message "webpage_curl_failed"')]
        data = _jpeg()
        with mock.patch.object(hpf, "fetch", return_value=data) as fetch:
            result = self.bot.send_media_group(chat_id=1, media=media)
        self.assertEqual(result, ["group-msg"])
        fetch.assert_called_once_with("https://x/4.jpg")
        sent_urls, captions = self.calls[-1][1], self.calls[-1][2]
        self.assertEqual(sent_urls, urls[:3] + ["FILE", urls[4]])
        self.assertEqual(captions[0], "cap")

    def test_album_bad_photo_is_dropped_and_caption_moves(self):
        urls = ["https://x/1.jpg", "https://x/2.jpg", "https://x/3.jpg"]
        media = [InputMediaPhoto(media=urls[0], caption="cap", parse_mode="HTML")] + [InputMediaPhoto(media=u) for u in urls[1:]]
        self.fail = [BadRequest('Failed to send message #1 with the error message "webpage_curl_failed"')]
        with mock.patch.object(hpf, "fetch", return_value=None):
            self.bot.send_media_group(chat_id=1, media=media)
        self.assertEqual(self.calls[-1][1], urls[1:])
        self.assertEqual(self.calls[-1][2], ["cap", None])

    def test_album_down_to_one_photo_goes_as_single_photo_with_caption(self):
        urls = ["https://x/1.jpg", "https://x/2.jpg"]
        media = [InputMediaPhoto(media=urls[0], caption="cap", parse_mode="HTML"), InputMediaPhoto(media=urls[1])]
        self.fail = [BadRequest('Failed to send message #2 with the error message "webpage_curl_failed"')]
        self.photo_error = None
        with mock.patch.object(hpf, "fetch", return_value=None):
            result = self.bot.send_media_group(chat_id=1, media=media)
        self.assertEqual(result, ["photo-msg"])
        kind, photo, caption, kwargs = self.calls[-1]
        self.assertEqual((kind, caption, kwargs.get("parse_mode")), ("photo", "cap", "HTML"))

    def test_album_nothing_downloadable_raises_for_text_fallback(self):
        media = [InputMediaPhoto(media="https://x/1.jpg", caption="cap"), InputMediaPhoto(media="https://x/2.jpg")]
        err = BadRequest('Failed to send message #1 with the error message "webpage_curl_failed"')
        self.fail = [err, err]
        with mock.patch.object(hpf, "fetch", return_value=None):
            with self.assertRaises(BadRequest):
                self.bot.send_media_group(chat_id=1, media=media)

    def test_album_blocked_user_is_not_retried(self):
        media = [InputMediaPhoto(media="https://x/1.jpg"), InputMediaPhoto(media="https://x/2.jpg")]
        self.fail = [BadRequest('Failed to send message #1 with the error message "user_is_blocked"')]
        with mock.patch.object(hpf, "fetch") as fetch:
            with self.assertRaises(BadRequest):
                self.bot.send_media_group(chat_id=1, media=media)
        fetch.assert_not_called()
        self.assertEqual(len(self.calls), 1)


class VonoviaBanner(unittest.TestCase):
    BANNER = "https://cdn.expose.vonovia.de/CAMP-Gruenstrom_v5.jpg?width=324"

    def test_parser_does_not_take_banner_as_cover(self):
        from user_jobs import vonovia_parser
        item = {"wrk_id": "1", "slug": "s", "titel": "2 Zimmer", "preview_img_url": self.BANNER, "imageUrls": [], "groesse": "50", "preis": "700"}
        with mock.patch.object(vonovia_parser, "is_apartment", return_value=True):
            listing = vonovia_parser.parse_listings({"results": [item]})[0]
        self.assertEqual(listing["cover_image_url"], "")
        item["imageUrls"] = ["https://cdn.expose.vonovia.de/DA1.jpg?width=324"]
        with mock.patch.object(vonovia_parser, "is_apartment", return_value=True):
            listing = vonovia_parser.parse_listings({"results": [item]})[0]
        self.assertIn("DA1.jpg", listing["cover_image_url"])

    def test_monitor_sends_text_not_banner_for_old_rows(self):
        from user_jobs import vonovia_monitor
        bot = mock.Mock()
        listing = {"gallery_urls": [], "cover_image_url": "https://cdn.expose.vonovia.de/CAMP-Gruenstrom_v5.jpg?width=1200"}
        self.assertFalse(vonovia_monitor._send_listing(bot, 5, listing, "text"))
        bot.send_photo.assert_not_called()
        bot.send_media_group.assert_not_called()


class BridgeImages(unittest.TestCase):
    def test_image_prefers_real_photo_and_skips_banner(self):
        from site_bridge import housing
        self.assertEqual(housing._image({"cover_image_url": "https://cdn/CAMP-a.jpg", "gallery_urls": "https://cdn/real.jpg"}), "https://cdn/real.jpg")
        self.assertEqual(housing._image({"cover_image_url": "https://cdn/CAMP-a.jpg"}), "")
        self.assertEqual(housing._image({"gallery_urls": ["https://cdn/l1.jpg"]}), "https://cdn/l1.jpg")
        self.assertEqual(housing._image({}, {"img": "https://iw/1.jpg"}), "https://iw/1.jpg")
        self.assertEqual(housing._image({"image_url": "https://pp/2.jpg"}, {"img": "https://iw/1.jpg"}), "https://pp/2.jpg")
        self.assertEqual(housing._image(None), "")

    def test_immowelt_snapshot_carries_first_photo(self):
        from user_handlers import housing_receiver
        listing = {"rooms": 2, "area_m2": 50, "price_eur": 800, "images": ["", "https://iw/1.jpg", "https://iw/2.jpg"]}
        data = json.loads(housing_receiver._snapshot(listing))
        self.assertEqual(data["img"], "https://iw/1.jpg")
        self.assertNotIn("img", json.loads(housing_receiver._snapshot({"rooms": 2, "images": []})))
        self.assertEqual(json.loads(housing_receiver._snapshot({"images": ["https://iw/9.jpg"]})), {"img": "https://iw/9.jpg"})


if __name__ == "__main__":
    unittest.main()
