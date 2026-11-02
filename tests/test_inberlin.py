"""inberlinwohnen.de — міські компанії Берліна (етап 8, 02.11.2026).

Фікстура — справжня перша сторінка «Wohnungsfinder» від 02.11.2026, урізана
до атрибутів `wire:snapshot` карток."""

import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database import Base
from user_jobs import housing_area, housing_tier, inberlin_matching, inberlin_monitor, inberlin_parser, inberlin_store

PAGE = (Path(__file__).parent / "fixtures" / "inberlin_page1.html").read_text(encoding="utf-8")
BERLIN = housing_area.make(housing_area.find("Berlin")[0], 0)


class InberlinParserTests(unittest.TestCase):
    def setUp(self):
        self.flats = {item["listing_key"]: item for item in inberlin_parser.parse_listings(PAGE)}

    def test_ten_flats_with_both_rents_postcode_and_company(self):
        self.assertEqual(len(self.flats), 10)
        flat = self.flats["23645"]
        self.assertEqual((flat["price_eur"], flat["price_warm_eur"]), (452.78, 645.91))  # Gesamtmiete з опаленням
        self.assertEqual((flat["rooms"], flat["area_m2"], flat["plz"]), (3.0, 68.49, "12679"))
        self.assertEqual(flat["company"], "degewo")
        self.assertEqual(flat["address"], "Glambecker Ring 77, 12679 Berlin-Marzahn-Hellersdorf")
        self.assertTrue(flat["detail_url"].startswith("https://www.degewo.de/"))
        self.assertIn("fm=jpg", flat["cover_image_url"])

    def test_wbs(self):
        self.assertEqual(self.flats["23644"]["wbs"], "WBS ab 160 erforderlich")
        self.assertTrue(inberlin_parser.wbs_required(self.flats["23644"]))
        self.assertEqual(self.flats["23645"]["wbs"], "nicht erforderlich")
        self.assertFalse(inberlin_parser.wbs_required(self.flats["23645"]))

    def test_the_message_names_the_company_and_the_wbs(self):
        text = inberlin_parser.format_listing_message(self.flats["23644"])
        self.assertIn("HOWOGE", text)
        self.assertIn("WBS: WBS ab 160 erforderlich", text)
        self.assertIn("Gesamtmiete EUR", text)


class FakeBot:
    def __init__(self):
        self.messages = []

    def send_message(self, chat_id, text, **kwargs):
        self.messages.append(chat_id)

    def send_photo(self, chat_id, photo, caption=None, **kwargs):
        self.messages.append(chat_id)


class InberlinMonitorTests(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(engine)
        session = sessionmaker(bind=engine)
        for patch in (
            mock.patch.object(inberlin_store, "DBSession", session),
            mock.patch("user_jobs.housing_tier.DBSession", session),
            mock.patch.object(housing_tier, "is_quiet_hours", return_value=False),
            mock.patch("user_jobs.housing_tier.premium_user_ids", return_value={7, 8}),
            mock.patch.object(inberlin_monitor, "PAGE_PAUSE_SECONDS", 0),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        self.flats = inberlin_parser.parse_listings(PAGE)

    def test_the_first_scan_is_a_baseline_then_only_new_flats_go_to_berlin_filters(self):
        potsdam = inberlin_store.create_filter(user_id=7, title="t")
        berlin = inberlin_store.create_filter(user_id=8, title="t", area=BERLIN)
        pages = {1: self.flats[1:]}
        bot = FakeBot()
        with mock.patch.object(inberlin_monitor, "_fetch_page", side_effect=lambda page: pages.get(page, [])):
            inberlin_monitor.check_job(SimpleNamespace(bot=bot))
            self.assertEqual(bot.messages, [])  # усе, що вже висіло, — база
            pages[1] = self.flats  # з'явилась одна нова квартира
            inberlin_monitor.check_job(SimpleNamespace(bot=bot))
        self.assertEqual(bot.messages, [8])
        self.assertNotIn((potsdam, self.flats[0]["listing_key"]), inberlin_store.delivered_pairs())
        self.assertIn((berlin, self.flats[0]["listing_key"]), inberlin_store.delivered_pairs())

    def test_new_pages_until_one_brings_nothing_new(self):
        known = {item["listing_key"] for item in self.flats}
        with mock.patch.object(inberlin_monitor, "_fetch_page", return_value=self.flats) as fetch:
            inberlin_monitor._fetch(known, full=False)
        self.assertEqual(fetch.call_count, 1)

    def test_only_a_full_pass_takes_flats_down(self):
        inberlin_store.upsert_listings(self.flats)
        inberlin_store.upsert_listings(self.flats[:2])
        self.assertEqual(len(inberlin_store.list_active_listings()), 10)
        inberlin_store.upsert_listings(self.flats[:2], deactivate_missing=True)
        self.assertEqual(len(inberlin_store.list_active_listings()), 2)

    def test_a_full_pass_every_six_hours(self):
        self.assertTrue(inberlin_monitor._full_sweep_due())
        inberlin_store.record_status_for(inberlin_monitor.FULL_KEY, "ok")
        self.assertFalse(inberlin_monitor._full_sweep_due())
        with mock.patch.object(inberlin_store, "utc_now",
                               return_value=inberlin_store.utc_now() + timedelta(hours=7)):
            self.assertTrue(inberlin_monitor._full_sweep_due())

    def test_place_decides(self):
        flat = self.flats[0]
        self.assertFalse(inberlin_matching.matches_filter(flat, {"area": None}))
        self.assertTrue(inberlin_matching.matches_filter(flat, {"area": BERLIN}))
        werder = housing_area.make(housing_area.find("Werder (Havel)")[0], 10)
        self.assertFalse(inberlin_matching.matches_filter(flat, {"area": werder}))


if __name__ == "__main__":
    unittest.main()
