"""Кеш фото в коллекторе ProPotsdam.

Фото портала открываются только под логином, поэтому их качает браузер
коллектора, а бот в контейнере забирает уже байты. Здесь проверяется эта
половина: что качается, что не перекачивается и что наружу не отдаётся ничего
лишнего.
"""

import inspect
import os
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

try:
    from tools import propotsdam_receiver
except ModuleNotFoundError as exc:  # playwright живе на хості, не в контейнері бота
    propotsdam_receiver = None
    _IMPORT_ERROR = exc
else:
    _IMPORT_ERROR = None

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


@unittest.skipIf(
    propotsdam_receiver is None,
    f"колектор ProPotsdam доступний лише на хості: {_IMPORT_ERROR}",
)
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


@unittest.skipIf(
    propotsdam_receiver is None,
    f"колектор ProPotsdam доступний лише на хості: {_IMPORT_ERROR}",
)
class ContentTypeTests(unittest.TestCase):
    def test_types_come_from_the_bytes_themselves(self):
        self.assertEqual(propotsdam_receiver._content_type(JPEG), 'image/jpeg')
        self.assertEqual(propotsdam_receiver._content_type(PNG), 'image/png')
        self.assertEqual(
            propotsdam_receiver._content_type(b'RIFF\x00\x00\x00\x00WEBP'), 'image/webp')
        self.assertEqual(propotsdam_receiver._content_type(b'nonsense'), 'application/octet-stream')


@unittest.skipIf(
    propotsdam_receiver is None,
    f"колектор ProPotsdam доступний лише на хості: {_IMPORT_ERROR}",
)
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


@unittest.skipIf(
    propotsdam_receiver is None,
    f"колектор ProPotsdam доступний лише на хості: {_IMPORT_ERROR}",
)
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


class FakeDetailResponse:
    url = 'https://portal/prorex/xmlforms?command=detail'

    def __init__(self, resource_ids):
        self._resource_ids = resource_ids

    def text(self):
        return ''.join('<image resourceId="{}"/>'.format(rid) for rid in self._resource_ids)


class FakeDetailContext:
    """Браузер, у которого снимок карточки просит собственную вкладку.

    Своя вкладка тут не деталь реализации: первый же снимок посреди обхода
    сбил страницу со списком, и квартира осталась без фотографий.
    """

    def __init__(self, page):
        self._page = page
        self.pages_opened = 0

    def new_page(self):
        self.pages_opened += 1
        return self._page


class FakeDetailPage:
    """Портал, который послушно открывает карточку и отдаёт её галерею."""

    url = 'https://portal/detail'

    def __init__(self, resource_ids=(), openable=True):
        self._resource_ids = list(resource_ids)
        self._openable = openable
        self._handlers = []
        self.opened = 0
        self.closed = False

    def on(self, event, handler):
        self._handlers.append(handler)

    def remove_listener(self, event, handler):
        self._handlers.remove(handler)

    def get_by_text(self, needle, exact=False):
        if not self._openable:
            raise RuntimeError('такого текста на странице нет')
        return self

    @property
    def first(self):
        return self

    def dispatch_event(self, name):
        self.opened += 1
        for handler in list(self._handlers):
            handler(FakeDetailResponse(self._resource_ids))

    def wait_for_timeout(self, ms):
        pass

    def evaluate(self, script):
        if 'img' in script:
            return ['https://portal/cover.jpg']
        return 'Kaltmiete 700,00 EUR Betriebskosten 180,00 EUR Gesamtmiete 880,00 EUR Zimmer 3'

    def content(self):
        return '<html>деталь</html>'

    def go_back(self, timeout=None):
        pass

    def goto(self, url, **kwargs):
        pass

    def wait_for_load_state(self, *args, **kwargs):
        pass

    def get_by_text(self, needle, exact=False):
        if not self._openable:
            raise RuntimeError("такого тексту на сторінці немає")
        return self

    def close(self):
        self.closed = True


LISTING = {
    'listing_key': '8151604D-B656-690E-6DDC-347467A96C0E',
    'title': 'sanierter Altbau mit Weitblick ins Grüne',
    'address': 'Ribbeckstr. 27, 14469 Potsdam',
}


@unittest.skipIf(
    propotsdam_receiver is None,
    f"колектор ProPotsdam доступний лише на хості: {_IMPORT_ERROR}",
)
class CaptureDetailsTests(unittest.TestCase):
    """Открытие карточки объявления: список показывает одну обложку и
    Gesamtmiete, а внутри лежат и вся галерея, и разбивка цены."""

    def test_the_gallery_inside_the_listing_is_collected(self):
        page = FakeDetailPage(['AAAA1111-BBBB-2222-CCCC-333344445555',
                               'DDDD9999-EEEE-8888-FFFF-777766665555'])
        with TemporaryDirectory() as tmp, \
             mock.patch.object(propotsdam_receiver, 'DETAIL_DIR', Path(tmp)), \
             mock.patch.object(propotsdam_receiver, '_open_offer_list', lambda page: True):
            result = propotsdam_receiver._capture_details(page, [LISTING])

            self.assertEqual(result['opened'], 1)
            self.assertEqual(len(result['resource_ids']), 2)
            self.assertTrue((Path(tmp) / '{}.json'.format(LISTING['listing_key'])).exists())

    def test_a_listing_is_opened_only_once_ever(self):
        """Снимок уже есть — значит фото из него давно забраны, лезть в портал
        второй раз не за чем."""
        page = FakeDetailPage(['AAAA1111-BBBB-2222-CCCC-333344445555'])
        with TemporaryDirectory() as tmp, \
             mock.patch.object(propotsdam_receiver, 'DETAIL_DIR', Path(tmp)), \
             mock.patch.object(propotsdam_receiver, '_open_offer_list', lambda page: True):
            propotsdam_receiver._capture_details(page, [LISTING])
            again = propotsdam_receiver._capture_details(page, [LISTING])

            self.assertEqual(page.opened, 1)
            self.assertEqual(again['opened'], 0)
            self.assertEqual(again['skipped'], 1)

    def test_a_portal_that_refuses_to_open_the_card_does_not_break_the_scan(self):
        page = FakeDetailPage(openable=False)
        with TemporaryDirectory() as tmp, \
             mock.patch.object(propotsdam_receiver, 'DETAIL_DIR', Path(tmp)), \
             mock.patch.object(propotsdam_receiver, '_open_offer_list', lambda page: True):
            result = propotsdam_receiver._capture_details(page, [LISTING])

            self.assertEqual(result['opened'], 0)
            self.assertEqual(result['resource_ids'], [])

    def test_the_step_can_be_switched_off(self):
        page = FakeDetailPage(['AAAA1111-BBBB-2222-CCCC-333344445555'])
        with mock.patch.object(propotsdam_receiver, 'DETAIL_ENABLED', False):
            result = propotsdam_receiver._capture_details(page, [LISTING])

        self.assertEqual(page.opened, 0)
        self.assertEqual(result['opened'], 0)

    def test_photos_found_inside_the_card_are_cached_too(self):
        """Ради этого карточка и открывается: в списке обложка одна, а в
        объявлении — вся галерея."""
        with TemporaryDirectory() as tmp, \
             mock.patch.object(propotsdam_receiver, 'PHOTO_DIR', Path(tmp)):
            page = FakePage(FakeRequest())
            stats = propotsdam_receiver._cache_photos(
                page, [], ['AAAA1111-BBBB-2222-CCCC-333344445555'],
            )

            self.assertEqual(stats['wanted'], 1)
            self.assertEqual(stats['saved'], 1)


if __name__ == '__main__':
    unittest.main()


@unittest.skipIf(
    propotsdam_receiver is None,
    f"колектор ProPotsdam доступний лише на хості: {_IMPORT_ERROR}",
)
class DetailTabIsolationTests(unittest.TestCase):
    """Знімок картки не має заважати основній роботі обходу.

    Перша ж спроба зняти картку на живому порталі клацнула повз, збила
    сторінку зі списку — і квартира приїхала до людини без жодного фото.
    Тому знімок робиться у власній вкладці й лише після того, як фото вже
    завантажені.
    """

    def test_the_snapshot_never_touches_the_page_before_photos_are_cached(self):
        """Знімок робиться останнім кроком обходу — саме тому він більше не
        може відібрати в квартири фотографії, як сталось першого ж разу."""
        source = inspect.getsource(propotsdam_receiver.scan)
        photos_at = source.index("_cache_photos(page, listings)")
        details_at = source.index("_capture_details(page, listings)")

        self.assertLess(photos_at, details_at)

    def test_a_view_that_cannot_reach_the_list_gives_up_quietly(self):
        page = FakeDetailPage()
        context = FakeDetailContext(page)

        def refuse(_page):
            raise RuntimeError("портал не відкрив перелік квартир")

        with TemporaryDirectory() as tmp, \
             mock.patch.object(propotsdam_receiver, 'DETAIL_DIR', Path(tmp)), \
             mock.patch.object(propotsdam_receiver, '_open_offer_list', refuse):
            result = propotsdam_receiver._capture_details(page, [LISTING])

        self.assertEqual(result, {"opened": 0, "skipped": 0, "resource_ids": []})
        self.assertEqual(page.opened, 0)

@unittest.skipIf(
    propotsdam_receiver is None,
    f"колектор ProPotsdam доступний лише на хості: {_IMPORT_ERROR}",
)
class SecondCardTests(unittest.TestCase):
    """Після першої картки обхід має повернутись до переліку й відкрити наступну.

    Повернення йшло через `_navigate_to_list`, який доводить лише до підменю —
    і другу квартиру за той самий прохід не відкривали взагалі: користувач
    побачив на екрані, що портал відкрив рівно одну картку й пішов.
    """

    def _listings(self):
        return [
            {"listing_key": "AAAA1111-BBBB-2222-CCCC-333344445555",
             "title": "1-Zimmer-Wohnung", "address": "Alt Nowawes 84, 14482 Potsdam"},
            {"listing_key": "DDDD9999-EEEE-8888-FFFF-777766665555",
             "title": "Babelsberg", "address": "Großbeerenstr. 43, 14482 Potsdam"},
        ]

    def test_the_run_returns_to_the_list_and_opens_the_next_card(self):
        page = FakeDetailPage(['AAAA1111-BBBB-2222-CCCC-333344445555'])
        page.evaluate = lambda script: (  # після картки на екрані вже не перелік
            ['https://portal/cover.jpg'] if 'img' in script else 'Kosten\nKaltmiete\n326,48 EUR'
        )
        returns = []

        with TemporaryDirectory() as tmp, \
             mock.patch.object(propotsdam_receiver, 'DETAIL_DIR', Path(tmp)), \
             mock.patch.object(propotsdam_receiver, '_open_offer_list',
                               lambda page: returns.append(1) or True):
            result = propotsdam_receiver._capture_details(page, self._listings())

        self.assertEqual(result['opened'], 2)
        # Один раз — щоб узагалі дійти до переліку, ще раз — щоб повернутись.
        self.assertGreaterEqual(len(returns), 2)

    def test_a_list_that_will_not_reopen_stops_the_run_instead_of_looping(self):
        page = FakeDetailPage(['AAAA1111-BBBB-2222-CCCC-333344445555'])
        page.evaluate = lambda script: (
            ['https://portal/cover.jpg'] if 'img' in script else 'Kosten\nKaltmiete\n326,48 EUR'
        )
        calls = []

        def reopen(_page):
            calls.append(1)
            return len(calls) == 1  # перший раз відкрився, далі — ні

        with TemporaryDirectory() as tmp, \
             mock.patch.object(propotsdam_receiver, 'DETAIL_DIR', Path(tmp)), \
             mock.patch.object(propotsdam_receiver, '_open_offer_list', reopen):
            result = propotsdam_receiver._capture_details(page, self._listings())

        self.assertEqual(result['opened'], 1)
