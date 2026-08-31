"""Кеш фото в коллекторе ProPotsdam.

Фото портала открываются только под логином, поэтому их качает браузер
коллектора, а бот в контейнере забирает уже байты. Здесь проверяется эта
половина: что качается, что не перекачивается и что наружу не отдаётся ничего
лишнего.
"""

import os
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from tools import propotsdam_receiver
from user_jobs import propotsdam_parser

JPEG = b'\xff\xd8\xff\xe0' + b'\x00' * 32
PNG = b'\x89PNG\r\n\x1a\n' + b'\x00' * 32


class FakeResponse:
    def __init__(self, body=JPEG, ok=True, status=200):
        self.ok = ok
        self.status = status
        self._body = body

    def body(self):
        return self._body


class FakeRequest:
    def __init__(self, responses=None, error=None):
        self.responses = responses or {}
        self.error = error
        self.requested = []

    def get(self, url, timeout=None):
        self.requested.append(url)
        if self.error:
            raise self.error
        return self.responses.get(url, FakeResponse())


class FakePage:
    def __init__(self, request=None):
        self.request = request or FakeRequest()


def listing_with(*resource_ids):
    return propotsdam_parser.normalize_listing({
        'title': 'Wohnung',
        'extra': {'image_resource_ids': ','.join(resource_ids)},
    })


class PhotoPathTests(unittest.TestCase):
    def test_a_resource_id_maps_to_one_cache_file(self):
        with TemporaryDirectory() as tmp:
            with mock.patch.object(propotsdam_receiver, 'PHOTO_DIR', Path(tmp)):
                path = propotsdam_receiver._photo_path('GOOD-RESOURCE-ID-0001')

            self.assertEqual(path, Path(tmp) / 'GOOD-RESOURCE-ID-0001.bin')

    def test_a_traversing_id_is_refused(self):
        """Тот же id приходит снаружи HTTP-запросом — «..» не должен читать чужое."""
        for bad in ['../../etc/passwd', '..', 'a/b', 'short', '', 'x' * 200]:
            with self.subTest(bad=bad):
                self.assertIsNone(propotsdam_receiver._photo_path(bad))

    def test_a_percent_encoded_traversal_is_refused(self):
        self.assertIsNone(propotsdam_receiver._photo_path('..%2F..%2Fsecret'))


class ContentTypeTests(unittest.TestCase):
    def test_types_come_from_the_bytes_themselves(self):
        self.assertEqual(propotsdam_receiver._content_type(JPEG), 'image/jpeg')
        self.assertEqual(propotsdam_receiver._content_type(PNG), 'image/png')
        self.assertEqual(
            propotsdam_receiver._content_type(b'RIFF\x00\x00\x00\x00WEBP'), 'image/webp')
        self.assertEqual(propotsdam_receiver._content_type(b'nonsense'), 'application/octet-stream')


class CachePhotosTests(unittest.TestCase):
    def test_every_photo_of_a_listing_is_downloaded(self):
        page = FakePage()
        listings = [listing_with('GOOD-RESOURCE-ID-0001', 'GOOD-RESOURCE-ID-0002')]
        with TemporaryDirectory() as tmp:
            with mock.patch.object(propotsdam_receiver, 'PHOTO_DIR', Path(tmp)):
                stats = propotsdam_receiver._cache_photos(page, listings)

                self.assertEqual(stats, {'wanted': 2, 'saved': 2, 'cached': 0, 'failed': 0})
                self.assertEqual(sorted(p.name for p in Path(tmp).glob('*.bin')), [
                    'GOOD-RESOURCE-ID-0001.bin', 'GOOD-RESOURCE-ID-0002.bin'])
        self.assertEqual(len(page.request.requested), 2)

    def test_an_already_cached_photo_is_not_downloaded_again(self):
        """В устоявшемся состоянии новых файлов столько же, сколько новых квартир."""
        page = FakePage()
        with TemporaryDirectory() as tmp:
            (Path(tmp) / 'GOOD-RESOURCE-ID-0001.bin').write_bytes(JPEG)
            with mock.patch.object(propotsdam_receiver, 'PHOTO_DIR', Path(tmp)):
                stats = propotsdam_receiver._cache_photos(
                    page, [listing_with('GOOD-RESOURCE-ID-0001', 'GOOD-RESOURCE-ID-0002')])

        self.assertEqual(stats, {'wanted': 2, 'saved': 1, 'cached': 1, 'failed': 0})
        self.assertEqual(len(page.request.requested), 1)

    def test_the_same_photo_in_two_listings_is_fetched_once(self):
        page = FakePage()
        listings = [listing_with('GOOD-RESOURCE-ID-0001'), listing_with('GOOD-RESOURCE-ID-0001')]
        with TemporaryDirectory() as tmp:
            with mock.patch.object(propotsdam_receiver, 'PHOTO_DIR', Path(tmp)):
                stats = propotsdam_receiver._cache_photos(page, listings)

        self.assertEqual(stats['wanted'], 1)
        self.assertEqual(len(page.request.requested), 1)

    def test_a_failed_photo_does_not_stop_the_rest(self):
        page = FakePage(FakeRequest(responses={
            propotsdam_parser.IMAGE_URL_TEMPLATE.format(resource_id='GOOD-RESOURCE-ID-0001'):
                FakeResponse(ok=False, status=403),
        }))
        with TemporaryDirectory() as tmp:
            with mock.patch.object(propotsdam_receiver, 'PHOTO_DIR', Path(tmp)):
                stats = propotsdam_receiver._cache_photos(
                    page, [listing_with('GOOD-RESOURCE-ID-0001', 'GOOD-RESOURCE-ID-0002')])

                self.assertEqual(stats, {'wanted': 2, 'saved': 1, 'cached': 0, 'failed': 1})
                self.assertEqual([p.name for p in Path(tmp).glob('*.bin')],
                                 ['GOOD-RESOURCE-ID-0002.bin'])

    def test_an_oversized_photo_is_not_stored(self):
        page = FakePage(FakeRequest(responses={
            propotsdam_parser.IMAGE_URL_TEMPLATE.format(resource_id='GOOD-RESOURCE-ID-0001'):
                FakeResponse(body=b'x' * 4096),
        }))
        with TemporaryDirectory() as tmp:
            with mock.patch.object(propotsdam_receiver, 'PHOTO_DIR', Path(tmp)), \
                 mock.patch.object(propotsdam_receiver, 'PHOTO_MAX_BYTES', 1024):
                stats = propotsdam_receiver._cache_photos(page, [listing_with('GOOD-RESOURCE-ID-0001')])

                self.assertEqual(stats['failed'], 1)
                self.assertEqual(list(Path(tmp).glob('*.bin')), [])

    def test_a_listing_without_photos_touches_no_disk(self):
        page = FakePage()
        with TemporaryDirectory() as tmp:
            missing = Path(tmp) / 'not-created-yet'
            with mock.patch.object(propotsdam_receiver, 'PHOTO_DIR', missing):
                stats = propotsdam_receiver._cache_photos(page, [listing_with()])

            self.assertEqual(stats, {'wanted': 0, 'saved': 0, 'cached': 0, 'failed': 0})
            self.assertFalse(missing.exists())


class PrunePhotosTests(unittest.TestCase):
    def test_photos_long_gone_from_the_feed_are_removed(self):
        with TemporaryDirectory() as tmp:
            fresh = Path(tmp) / 'GOOD-RESOURCE-ID-0001.bin'
            stale = Path(tmp) / 'GOOD-RESOURCE-ID-0002.bin'
            fresh.write_bytes(JPEG)
            stale.write_bytes(JPEG)
            old = time.time() - 40 * 86400
            os.utime(stale, (old, old))

            with mock.patch.object(propotsdam_receiver, 'PHOTO_DIR', Path(tmp)), \
                 mock.patch.object(propotsdam_receiver, 'PHOTO_KEEP_DAYS', 30):
                removed = propotsdam_receiver._prune_photos()

            self.assertEqual(removed, 1)
            self.assertTrue(fresh.exists())
            self.assertFalse(stale.exists())

    def test_a_photo_still_in_the_feed_is_kept_alive(self):
        """Скачали давно, но квартира ещё висит — удалять нельзя."""
        page = FakePage()
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / 'GOOD-RESOURCE-ID-0001.bin'
            path.write_bytes(JPEG)
            old = time.time() - 40 * 86400
            os.utime(path, (old, old))

            with mock.patch.object(propotsdam_receiver, 'PHOTO_DIR', Path(tmp)), \
                 mock.patch.object(propotsdam_receiver, 'PHOTO_KEEP_DAYS', 30):
                propotsdam_receiver._cache_photos(page, [listing_with('GOOD-RESOURCE-ID-0001')])
                removed = propotsdam_receiver._prune_photos()

            self.assertEqual(removed, 0)
            self.assertTrue(path.exists())

    def test_pruning_can_be_switched_off(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / 'GOOD-RESOURCE-ID-0001.bin'
            path.write_bytes(JPEG)
            old = time.time() - 400 * 86400
            os.utime(path, (old, old))

            with mock.patch.object(propotsdam_receiver, 'PHOTO_DIR', Path(tmp)), \
                 mock.patch.object(propotsdam_receiver, 'PHOTO_KEEP_DAYS', 0):
                self.assertEqual(propotsdam_receiver._prune_photos(), 0)
            self.assertTrue(path.exists())


if __name__ == '__main__':
    unittest.main()
