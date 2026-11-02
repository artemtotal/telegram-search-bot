"""WBS у фільтрі житла (02.11.2026)."""

import unittest

from user_jobs import housing_wbs, inberlin_matching, kleinanzeigen_matching


class WbsRequirementTests(unittest.TestCase):
    def test_titles_and_fields(self):
        cases = {
            "2-Zimmer-Wohnung nur mit WBS": (True, None),
            "WBS ab 160 erforderlich": (True, 160),
            "Sozialwohnung mit WBS 140 in Spandau": (True, 140),
            "Neubau, WBS-pflichtig": (True, None),
            "Helle Wohnung ohne WBS": (False, None),
            "WBS nicht erforderlich": (False, None),
            "Schöne 3-Zimmer-Wohnung mit Balkon": (False, None),
        }
        for title, expected in cases.items():
            self.assertEqual(housing_wbs.requirement({"title": title}), expected, title)
        # Поле порталу важить більше за заголовок.
        self.assertEqual(housing_wbs.requirement({"wbs": "nicht erforderlich", "title": "Achtung!"}), (False, None))
        self.assertEqual(housing_wbs.requirement({"wbs": "erforderlich", "title": "Helle Wohnung"}), (True, None))

    def test_who_gets_what(self):
        needs_160 = {"title": "WBS 160 erforderlich"}
        needs_any = {"title": "nur mit WBS"}
        free = {"title": "Wohnung"}
        self.assertTrue(housing_wbs.allowed(needs_160, None))
        self.assertFalse(housing_wbs.allowed(needs_160, "none"))
        self.assertTrue(housing_wbs.allowed(free, "none"))
        self.assertTrue(housing_wbs.allowed(needs_160, "140"))  # дохід нижчий — підходить
        self.assertTrue(housing_wbs.allowed(needs_160, "160"))
        self.assertFalse(housing_wbs.allowed(needs_160, "180"))  # дохід вищий — не дадуть
        self.assertTrue(housing_wbs.allowed(needs_any, "220"))
        self.assertFalse(housing_wbs.allowed(needs_any, "none"))

    def test_every_portal_checks_it(self):
        flat = {"listing_key": "1", "title": "2 Zimmer, nur mit WBS", "address": "14469 Potsdam",
                "rooms": 2.0, "area_m2": 50.0, "price_eur": 500.0}
        self.assertTrue(kleinanzeigen_matching.matches_filter(flat, {}))
        self.assertFalse(kleinanzeigen_matching.matches_filter(flat, {"wbs": "none"}))
        berlin_flat = {"listing_key": "2", "title": "Achtung", "wbs": "WBS ab 160 erforderlich", "plz": "13059",
                       "region": "berlin", "rooms": 1.0}
        from user_jobs import housing_area
        berlin = housing_area.make(housing_area.find("Berlin")[0], 0)
        self.assertTrue(inberlin_matching.matches_filter(berlin_flat, {"area": berlin}))
        self.assertFalse(inberlin_matching.matches_filter(berlin_flat, {"area": berlin, "wbs": "none"}))


if __name__ == "__main__":
    unittest.main()
