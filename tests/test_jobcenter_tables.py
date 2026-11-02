"""Ліміти Jobcenter поза Потсдамом: Берлін і округи Бранденбургу (02.11.2026)."""

import unittest

from user_jobs import housing_area, jobcenter_tables


def place(name):
    return housing_area.make(housing_area.find(name)[0], 0)


class JobcenterTablesTests(unittest.TestCase):
    def test_every_named_place_exists_in_the_postcode_table(self):
        cities = {(row[1], row[4]) for row in housing_area._rows()}
        self.assertEqual([key for key in jobcenter_tables.PLACE_TABLE if key not in cities], [])

    def test_district_tables(self):
        # Havelland, gültig ab 01.01.2026: Ost 2 Personen — 820 €.
        self.assertEqual(jobcenter_tables.for_place(place("Falkensee"), 2)[0], 820.00)
        # Potsdam-Mittelmark, Planregion 1: 1 Person — 619 €; 5 — 15,88 €/m² × 100 m².
        self.assertEqual(jobcenter_tables.for_place(place("Teltow"), 1)[0], 619.00)
        self.assertEqual(jobcenter_tables.for_place(place("Teltow"), 5)[0], 1588.00)
        # Oberhavel, Markt 2: 3 Personen — 836 €; 6 — +125 €.
        self.assertEqual(jobcenter_tables.for_place(place("Oranienburg"), 3)[0], 836.00)
        self.assertEqual(jobcenter_tables.for_place(place("Oranienburg"), 6)[0], 1372.00)

    def test_warm_adds_heating_for_the_appropriate_area(self):
        cold, warm = jobcenter_tables.for_place(place("Rathenow"), 2)
        self.assertEqual(round(warm - cold, 2), round(65 * 22.30 / 12, 2))

    def test_places_without_a_table(self):
        self.assertIsNone(jobcenter_tables.for_place(place("Cottbus"), 2))
        self.assertIsNone(jobcenter_tables.for_place(place("Ludwigsfelde"), 2))


if __name__ == "__main__":
    unittest.main()
