"""ohne-makler.net (етап 6, 02.11.2026). Фікстура — справжня перша сторінка
Берліна від 02.11.2026, урізана до карток."""

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database import Base
from user_jobs import housing_area, housing_tier, ohnemakler_matching, ohnemakler_monitor, ohnemakler_parser, ohnemakler_store

PAGE = (Path(__file__).parent / "fixtures" / "ohnemakler_berlin_page1.html").read_text(encoding="utf-8")
BERLIN = housing_area.make(housing_area.find("Berlin")[0], 0)


class OhnemaklerParserTests(unittest.TestCase):
    def test_cards(self):
        flats = {item["listing_key"]: item for item in ohnemakler_parser.parse_listings(PAGE)}
        self.assertEqual(len(flats), 24)
        flat = flats["210414"]
        self.assertEqual((flat["price_eur"], flat["rooms"], flat["area_m2"]), (819.0, 2.0, 50.0))
        self.assertEqual((flat["plz"], flat["address"], flat["region"]), ("13591", "13591 Berlin (Staaken)", "berlin"))
        self.assertEqual(flats["235423"]["rooms"], 3.5)
        self.assertTrue(flat["cover_image_url"].endswith("/immobilie/210414/picture/0/medium.jpg"))

    def test_total_count_and_pages(self):
        self.assertEqual(ohnemakler_parser.total_count("<b>172 Immobilien</b>"), 172)
        self.assertTrue(ohnemakler_parser.page_url("brandenburg", 3).endswith("/wohnung-mieten/brandenburg/?page=3"))


class OhnemaklerMonitorTests(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(engine)
        session = sessionmaker(bind=engine)
        for patch in (
            mock.patch.object(ohnemakler_store, "DBSession", session),
            mock.patch("user_jobs.housing_tier.DBSession", session),
            mock.patch.object(housing_tier, "is_quiet_hours", return_value=False),
            mock.patch("user_jobs.housing_tier.premium_user_ids", return_value={7, 8}),
            mock.patch.object(ohnemakler_monitor, "PAGE_PAUSE_SECONDS", 0),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        self.flats = ohnemakler_parser.parse_listings(PAGE)

    def test_pages_follow_the_total(self):
        calls = []

        def fetch(region, page):
            calls.append((region, page))
            return [dict(item, listing_key=f"{region}{page}-{item['listing_key']}") for item in self.flats], 30

        with mock.patch.object(ohnemakler_monitor, "_fetch_page", side_effect=fetch):
            listings = ohnemakler_monitor._fetch(set())
        self.assertEqual(calls, [("berlin", 1), ("berlin", 2), ("brandenburg", 1), ("brandenburg", 2)])
        self.assertEqual(len(listings), 96)

    def test_first_scan_is_a_baseline_and_the_place_decides(self):
        ohnemakler_store.create_filter(user_id=7, title="t")  # Потсдам
        berlin = ohnemakler_store.create_filter(user_id=8, title="t", area=BERLIN)
        pages = {"berlin": self.flats[1:]}
        bot = SimpleNamespace(messages=[])
        bot.send_message = lambda chat_id, text, **kw: bot.messages.append(chat_id)
        bot.send_photo = lambda chat_id, photo, caption=None, **kw: bot.messages.append(chat_id)
        fetch = lambda region, page: (pages.get(region, []) if page == 1 else [], 0)  # noqa: E731
        with mock.patch.object(ohnemakler_monitor, "_fetch_page", side_effect=fetch):
            ohnemakler_monitor.check_job(SimpleNamespace(bot=bot))
            self.assertEqual(bot.messages, [])
            pages["berlin"] = self.flats
            ohnemakler_monitor.check_job(SimpleNamespace(bot=bot))
        self.assertEqual(bot.messages, [8])
        self.assertIn((berlin, self.flats[0]["listing_key"]), ohnemakler_store.delivered_pairs())

    def test_matching(self):
        flat = self.flats[0]
        self.assertFalse(ohnemakler_matching.matches_filter(flat, {}))
        self.assertTrue(ohnemakler_matching.matches_filter(flat, {"area": BERLIN}))
        self.assertFalse(ohnemakler_matching.matches_filter(flat, {"area": BERLIN, "max_price_eur": 500}))


if __name__ == "__main__":
    unittest.main()
