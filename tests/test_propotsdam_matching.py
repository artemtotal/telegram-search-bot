import unittest

from user_jobs import propotsdam_matching


class ProPotsdamMatchingTests(unittest.TestCase):
    def test_listing_matches_numeric_bounds_and_districts(self):
        listing = {
            'district': 'Babelsberg',
            'rooms': 2.0,
            'area_m2': 64.0,
            'total_rent_eur': 963.79,
        }
        filt = {
            'districts': 'babelsberg, Innenstadt',
            'min_rooms': 2.0,
            'max_rooms': 3.0,
            'min_area_m2': 50.0,
            'max_area_m2': 70.0,
            'max_total_rent_eur': 1000.0,
        }

        self.assertTrue(propotsdam_matching.matches_filter(listing, filt))

    def test_filter_only_decides_delivery_not_message_fields(self):
        listing = {
            'title': 'Große Wohnung',
            'address': 'Adresse 1',
            'district': 'Babelsberg',
            'rooms': 4.0,
            'area_m2': 120.0,
            'total_rent_eur': 1500.0,
            'available_from': 'ab sofort',
            'extra': {'Beschreibung': 'vollständiger Text'},
        }
        filt = {'districts': 'Babelsberg', 'min_rooms': None, 'max_rooms': None, 'min_area_m2': None, 'max_area_m2': None, 'max_total_rent_eur': None}

        self.assertTrue(propotsdam_matching.matches_filter(listing, filt))
        self.assertIn('Beschreibung', propotsdam_matching.format_notification(listing, 'https://portal.example/'))

    def test_rejects_out_of_budget_listing(self):
        listing = {'district': 'Babelsberg', 'rooms': 2.0, 'area_m2': 64.0, 'total_rent_eur': 1200.0}
        filt = {'districts': 'Babelsberg', 'max_total_rent_eur': 1000.0}

        self.assertFalse(propotsdam_matching.matches_filter(listing, filt))


if __name__ == '__main__':
    unittest.main()
