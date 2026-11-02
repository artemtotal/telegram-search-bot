"""Етап 2 «Берлін і Бранденбург» (02.11.2026): Kleinanzeigen по регіонах і місце
в записах усіх площадок — потсдамські агентства теж звіряють індекс."""

import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database import Base, KleinanzeigenListing
from user_jobs import (
    housing_area, kleinanzeigen_matching, kleinanzeigen_monitor, kleinanzeigen_parser, kleinanzeigen_store,
    semmelhaack_matching, semmelhaack_store,
)

BERLIN = housing_area.make(housing_area.find("Berlin")[0], 0)
WERDER_10 = housing_area.make(housing_area.find("Werder (Havel)")[0], 10)


def _flat(key, address, title="2-Zimmer-Wohnung", **extra):
    return {"listing_key": key, "title": title, "address": address, "rooms": 2.0, "area_m2": 55.0,
            "price_eur": 800.0, "detail_url": f"https://x/{key}", **extra}


class _Db(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        session = sessionmaker(bind=self.engine)
        for module in (kleinanzeigen_store, semmelhaack_store):
            patch = mock.patch.object(module, "DBSession", session)
            patch.start()
            self.addCleanup(patch.stop)


class RegionUrlTests(unittest.TestCase):
    def test_berlin_and_brandenburg_pages(self):
        self.assertEqual(kleinanzeigen_parser.region_url("berlin"),
                         "https://www.kleinanzeigen.de/s-wohnung-mieten/berlin/c203l3331")
        self.assertEqual(kleinanzeigen_parser.region_url("brandenburg", 2),
                         "https://www.kleinanzeigen.de/s-wohnung-mieten/brandenburg/seite:2/c203l7711")


class KleinanzeigenRegionTests(_Db):
    def test_one_page_unless_it_is_almost_all_new(self):
        pages = {
            1: [_flat("n1", "10247 Friedrichshain"), _flat("old", "12059 Neukölln"),
                _flat("swap", "10115 Mitte", title="Tauschwohnung 2 Zimmer"),
                _flat("room", "10115 Mitte", title="Möbliertes Zimmer zur Untermiete"),
                _flat("pots", "14469 Potsdam")],
        }
        kleinanzeigen_store.upsert_listings([_flat("old", "12059 Neukölln")], region="berlin", deactivate_missing=False)
        with mock.patch.object(kleinanzeigen_monitor, "REGIONS", ("berlin",)), \
             mock.patch.object(kleinanzeigen_monitor, "REGION_PAUSE_SECONDS", 0), \
             mock.patch.object(kleinanzeigen_monitor, "_fetch_region_page", side_effect=lambda url: pages[1]) as fetch:
            found = kleinanzeigen_monitor._scan_regions(SimpleNamespace(bot=None), skip={"pots"})
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual([item["listing_key"] for item in found], ["n1", "old"])
        self.assertTrue(all(item["region"] == "berlin" for item in found))

    def test_the_first_scan_of_a_region_is_a_baseline(self):
        filter_id = kleinanzeigen_store.create_filter(user_id=1, title="t")  # Потсдам
        pages = [[_flat("old-p", "14480 Potsdam")], [_flat("old-p", "14480 Potsdam"), _flat("new-p", "14469 Potsdam")]]
        with mock.patch.object(kleinanzeigen_monitor, "REGIONS", ("brandenburg",)), \
             mock.patch.object(kleinanzeigen_monitor, "REGION_PAUSE_SECONDS", 0), \
             mock.patch.object(kleinanzeigen_monitor, "_fetch_region_page", side_effect=lambda url: pages.pop(0)):
            kleinanzeigen_monitor._scan_regions(SimpleNamespace(bot=None))
            self.assertIn((filter_id, "old-p"), kleinanzeigen_store.delivered_pairs())
            kleinanzeigen_monitor._scan_regions(SimpleNamespace(bot=None))
        # Друга сторінка — уже не перша: нова квартира чекає на відправку.
        self.assertNotIn((filter_id, "new-p"), kleinanzeigen_store.delivered_pairs())

    def test_a_region_page_takes_down_nothing_and_stale_flats_go_after_three_days(self):
        kleinanzeigen_store.upsert_listings([_flat("p", "14469 Potsdam")])
        kleinanzeigen_store.upsert_listings([_flat("b1", "10247 Berlin")], region="berlin", deactivate_missing=False)
        kleinanzeigen_store.upsert_listings([_flat("b2", "10247 Berlin")], region="berlin", deactivate_missing=False)
        active = {item["listing_key"] for item in kleinanzeigen_store.list_active_listings()}
        self.assertEqual(active, {"p", "b1", "b2"})
        session = sessionmaker(bind=self.engine)()
        session.query(KleinanzeigenListing).filter(KleinanzeigenListing.listing_key == "b1").update(
            {"last_seen_at": datetime.utcnow() - timedelta(days=4)})
        session.commit()
        session.close()
        self.assertEqual(kleinanzeigen_store.deactivate_stale("berlin", datetime.utcnow() - timedelta(days=3)), 1)
        # Потсдамська сторінка знімає лише потсдамські.
        kleinanzeigen_store.upsert_listings([_flat("p2", "14469 Potsdam")])
        active = {item["listing_key"] for item in kleinanzeigen_store.list_active_listings()}
        self.assertEqual(active, {"p2", "b2"})

    def test_place_decides_who_gets_a_regional_flat(self):
        old = kleinanzeigen_store.create_filter(user_id=1, title="t")
        berlin = kleinanzeigen_store.create_filter(user_id=2, title="t", area=BERLIN)
        filters = {item["filter_id"]: item for item in kleinanzeigen_store.list_filters()}
        berlin_flat = _flat("b", "10247 Friedrichshain", region="berlin")
        potsdam_flat = _flat("p", "14469 Potsdam")
        self.assertFalse(kleinanzeigen_matching.matches_filter(berlin_flat, filters[old]))
        self.assertTrue(kleinanzeigen_matching.matches_filter(berlin_flat, filters[berlin]))
        self.assertTrue(kleinanzeigen_matching.matches_filter(potsdam_flat, filters[old]))
        self.assertFalse(kleinanzeigen_matching.matches_filter(potsdam_flat, filters[berlin]))
        # Підказка «трохи дорожче» теж поважає місце.
        self.assertIsNone(kleinanzeigen_matching.near_miss_over(dict(potsdam_flat, price_eur=900),
                                                                dict(filters[berlin], max_price_eur=850)))

    def test_listing_pages_only_for_regional_flats_that_fit_someone(self):
        kleinanzeigen_store.create_filter(user_id=2, title="t", min_rooms=2, area=BERLIN)
        fits, small = _flat("f", "10247 Friedrichshain", region="berlin"), _flat("s", "10247 Mitte", region="berlin", rooms=1.0)
        far = _flat("c", "03046 Cottbus", region="brandenburg")
        wanted = kleinanzeigen_monitor._wanted_regional([fits, small, far], kleinanzeigen_store.list_filters(active_only=True))
        self.assertEqual([item["listing_key"] for item in wanted], ["f"])

    def test_an_old_edit_path_keeps_the_place(self):
        filter_id = kleinanzeigen_store.create_filter(user_id=2, title="t", area=BERLIN)
        kleinanzeigen_store.update_filter(filter_id, 2, "t", min_rooms=2)
        self.assertEqual(kleinanzeigen_store.list_filters()[0]["area"]["name"], "Berlin")


class VonoviaRegionTests(_Db):
    def setUp(self):
        super().setUp()
        from user_jobs import vonovia_store
        patch = mock.patch.object(vonovia_store, "DBSession", sessionmaker(bind=self.engine))
        patch.start()
        self.addCleanup(patch.stop)

    def test_each_city_is_scanned_whole_and_takes_down_only_its_own_flats(self):
        from user_jobs import vonovia_monitor, vonovia_store
        vonovia_store.upsert_listings([_flat("p", "Schwedische Allee 7, 14476 Potsdam OT Krampnitz")])
        cities = {"Berlin": [_flat("b1", "Weg 10, 12355 Berlin OT Rudow")], "Oranienburg": []}
        with mock.patch.object(vonovia_monitor, "REGION_CITIES", ("Berlin", "Oranienburg")), \
             mock.patch.object(vonovia_monitor, "_session"), \
             mock.patch.object(vonovia_monitor, "_search_token", return_value="t"), \
             mock.patch.object(vonovia_monitor, "_add_full_rent"), \
             mock.patch.object(vonovia_monitor, "_fetch_city", side_effect=lambda s, t, city: cities[city]):
            vonovia_monitor._scan_regions()
            cities["Berlin"] = [_flat("b2", "Str. 1, 10115 Berlin OT Mitte")]
            vonovia_monitor._scan_regions()
        active = {item["listing_key"]: item["region"] for item in vonovia_store.list_active_listings()}
        self.assertEqual(active, {"p": "potsdam", "b2": "Berlin"})
        berlin = vonovia_store.create_filter(user_id=5, title="t", area=BERLIN)
        filt = next(item for item in vonovia_store.list_filters() if item["filter_id"] == berlin)
        from user_jobs import vonovia_matching
        self.assertTrue(vonovia_matching.matches_filter(dict(_flat("b2", "Str. 1, 10115 Berlin OT Mitte"), region="Berlin"), filt))


class PotsdamAgencyAreaTests(_Db):
    def test_werder_with_radius_gets_the_near_part_of_potsdam_only(self):
        filter_id = semmelhaack_store.create_filter(user_id=3, title="t", area=WERDER_10)
        filt = next(item for item in semmelhaack_store.list_filters() if item["filter_id"] == filter_id)
        near = {"listing_key": "1", "title": "Wohnung", "address": "Kurt-von-Plettenberg-Straße 14, 14469 Potsdam",
                "rooms": 2.0, "area_m2": 50.0, "price_eur": 600.0}
        far = dict(near, listing_key="2", address="Gaußstr. 16, 14480 Potsdam")
        self.assertTrue(semmelhaack_matching.matches_filter(near, filt))
        self.assertFalse(semmelhaack_matching.matches_filter(far, filt))
        self.assertTrue(housing_area.touches_potsdam(WERDER_10))
        self.assertFalse(housing_area.touches_potsdam(housing_area.make(housing_area.find("Werder (Havel)")[0], 0)))


if __name__ == "__main__":
    unittest.main()
