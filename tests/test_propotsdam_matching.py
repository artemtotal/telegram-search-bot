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
            'min_total_rent_eur': 900.0,
            'max_total_rent_eur': 1000.0,
        }

        self.assertTrue(propotsdam_matching.matches_filter(listing, filt))

    def test_rejects_listing_below_minimum_rent(self):
        listing = {'district': 'Babelsberg', 'rooms': 2.0, 'area_m2': 64.0, 'total_rent_eur': 700.0}
        filt = {'districts': 'Babelsberg', 'min_total_rent_eur': 800.0}

        self.assertFalse(propotsdam_matching.matches_filter(listing, filt))

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


class BothRentBoundsTests(unittest.TestCase):
    """У ProPotsdam дві ціни: повна зі списку і холодна з картки оголошення."""

    def _listing(self, price_eur=326.48, total_rent_eur=485.52):
        return {
            "district": "Babelsberg", "rooms": 1, "area_m2": 37.44,
            "total_rent_eur": total_rent_eur, "price_eur": price_eur,
        }

    def test_the_cold_bound_is_measured_against_the_cold_rent(self):
        self.assertTrue(propotsdam_matching.matches_filter(
            self._listing(), {"max_price_eur": 400}))
        self.assertFalse(propotsdam_matching.matches_filter(
            self._listing(), {"max_price_eur": 300}))

    def test_the_full_bound_still_works_as_before(self):
        self.assertTrue(propotsdam_matching.matches_filter(
            self._listing(), {"max_total_rent_eur": 500}))
        self.assertFalse(propotsdam_matching.matches_filter(
            self._listing(), {"max_total_rent_eur": 450}))

    def test_a_flat_whose_card_was_never_opened_is_not_dropped_by_a_cold_bound(self):
        """Холодна ціна відома лише після відкриття картки; доки її немає,
        квартиру не можна відкидати за те, чого список не друкує."""
        self.assertTrue(propotsdam_matching.matches_filter(
            self._listing(price_eur=None), {"max_price_eur": 300}))

    def test_both_bounds_apply_together(self):
        self.assertTrue(propotsdam_matching.matches_filter(
            self._listing(), {"max_price_eur": 400, "max_total_rent_eur": 500}))
        self.assertFalse(propotsdam_matching.matches_filter(
            self._listing(), {"max_price_eur": 400, "max_total_rent_eur": 450}))
