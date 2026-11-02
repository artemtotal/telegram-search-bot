"""Де шукати: місто чи індекс + радіус (етап 1 Берлін і Бранденбург, 02.11.2026)."""

import unittest

from user_jobs import housing_area as area


class FindTests(unittest.TestCase):
    def test_city_names_in_any_spelling(self):
        for text in ("Berlin", "berlin", "Берлін", "Берлин"):
            found = area.find(text)
            self.assertEqual(found[0]["name"], "Berlin", text)
        self.assertEqual(area.find("Werder (Havel)")[0]["name"], "Werder (Havel)")
        self.assertEqual(area.find("werder havel")[0]["name"], "Werder (Havel)")
        self.assertEqual(area.find("Konigs Wusterhausen")[0]["name"], "Königs Wusterhausen")
        self.assertEqual(area.find("Фалькензее")[0]["name"], "Falkensee")
        self.assertEqual(area.find("Котбус")[0]["name"], "Cottbus")

    def test_a_part_of_potsdam_is_potsdam(self):
        self.assertEqual(area.find("Golm")[0]["name"], "Potsdam")
        self.assertEqual(area.find("14476"), [area.find("Potsdam")[0]])

    def test_postcode(self):
        found = area.find("10247")
        self.assertEqual(found, [{"kind": "plz", "plz": "10247", "name": "Berlin", "code": "11000",
                                  "kreis": "Berlin", "radius_km": 0}])
        self.assertEqual(area.find("80331"), [])  # Мюнхен — поза Берліном і Бранденбургом

    def test_nothing_found(self):
        self.assertEqual(area.find("Hamburg"), [])
        self.assertEqual(area.find(""), [])


class AreaTests(unittest.TestCase):
    def setUp(self):
        self.potsdam = area.find("Potsdam")[0]
        self.berlin = area.find("Berlin")[0]
        self.werder = area.find("Werder (Havel)")[0]

    def test_potsdam_only_is_stored_as_none_like_old_filters(self):
        self.assertIsNone(area.make(self.potsdam, 0))
        self.assertTrue(area.is_default(None))
        self.assertEqual(area.make(self.potsdam, 10)["radius_km"], 10)
        self.assertFalse(area.is_default(area.make(self.potsdam, 10)))

    def test_labels(self):
        self.assertEqual(area.label(None), "Potsdam")
        self.assertEqual(area.label(area.make(self.berlin, 10)), "Berlin + 10 км")
        self.assertEqual(area.label(area.make(area.find("10247")[0], 5), "de"), "10247 Berlin + 5 km")

    def test_old_listings_from_the_potsdam_search_stay_potsdam(self):
        old = {"address": "Max-Planck-Str. 1"}  # без індексу й регіону
        self.assertTrue(area.listing_in_area(old, None))
        self.assertFalse(area.listing_in_area(old, area.make(self.berlin, 0)))

    def test_city_only(self):
        berlin_flat = {"plz": "10247", "region": "berlin"}
        self.assertTrue(area.listing_in_area(berlin_flat, area.make(self.berlin, 0)))
        self.assertFalse(area.listing_in_area(berlin_flat, None))
        teltow = {"plz": "14513", "region": "brandenburg"}
        self.assertFalse(area.listing_in_area(teltow, area.make(self.berlin, 0)))
        self.assertFalse(area.listing_in_area(teltow, None))

    def test_radius(self):
        teltow = {"plz": "14513", "region": "brandenburg"}
        self.assertTrue(area.listing_in_area(teltow, area.make(self.berlin, 5)))
        self.assertTrue(area.listing_in_area(teltow, area.make(self.potsdam, 20)))
        cottbus = {"plz": "03046", "region": "brandenburg"}
        self.assertFalse(area.listing_in_area(cottbus, area.make(self.berlin, 50)))
        # Індекс із радіусом 0 — лише сам індекс.
        own = area.make(area.find("10247")[0], 0)
        self.assertTrue(area.listing_in_area({"plz": "10247"}, own))
        self.assertFalse(area.listing_in_area({"plz": "10245"}, own))
        self.assertTrue(area.listing_in_area({"plz": "10245"}, dict(own, radius_km=5)))

    def test_potsdam_local_agencies_only_when_potsdam_is_inside(self):
        self.assertTrue(area.covers_potsdam(None))
        self.assertTrue(area.covers_potsdam(area.make(self.potsdam, 10)))
        # Поки потсдамські площадки не вміють перевіряти індекс — лише сам Потсдам.
        self.assertFalse(area.covers_potsdam(area.make(self.werder, 10)))
        self.assertFalse(area.covers_potsdam(area.make(self.berlin, 0)))


class BerlinBezirkeTests(unittest.TestCase):
    def test_twelve_districts_and_a_choice_narrows_the_postcodes(self):
        self.assertEqual(len(area.berlin_bezirke()), 12)
        self.assertEqual(area.bezirk_of_plz()["10247"], "Friedrichshain-Kreuzberg")
        berlin = area.make(area.find("Berlin")[0], 0)
        some = dict(berlin, bezirke=["Mitte", "Pankow"])
        self.assertTrue(area.listing_in_area({"plz": "10115", "region": "berlin"}, some))
        self.assertFalse(area.listing_in_area({"plz": "12679", "region": "berlin"}, some))
        self.assertTrue(area.listing_in_area({"plz": "12679", "region": "berlin"}, berlin))
        self.assertEqual(area.label(some), "Berlin (Mitte, Pankow)")
        # Округи — лише для «Берлін, лише місто».
        self.assertFalse(area.is_berlin_city(area.make(area.find("Berlin")[0], 10)))

if __name__ == "__main__":
    unittest.main()
