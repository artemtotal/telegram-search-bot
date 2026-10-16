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

    def test_portal_aggregate_districts_match_wizard_names(self):
        # The portal labels flats "Am Stern", "Nördl. VS/IS", "Potsdam Nord";
        # the filter wizard offers "Stern", "Innenstadt", "Bornstedt".
        cases = [
            ('Am Stern', 'Stern'),
            ('Nördl. VS/IS', 'Innenstadt'),
            ('Nördl. VS/IS', 'Jägervorstadt'),
            ('Südl. VS/IS', 'Teltower Vorstadt'),
            ('Potsdam Nord', 'Bornstedt'),
            ('Babelsberg', 'Babelsberg Nord'),
        ]
        for district, chosen in cases:
            with self.subTest(district=district, chosen=chosen):
                self.assertTrue(propotsdam_matching.matches_filter({'district': district}, {'districts': chosen}))

    def test_portal_aggregate_district_does_not_leak_elsewhere(self):
        for district, chosen in [('Am Stern', 'Drewitz'), ('Potsdam Nord', 'Innenstadt'), ('Nördl. VS/IS', 'Babelsberg')]:
            with self.subTest(district=district, chosen=chosen):
                self.assertFalse(propotsdam_matching.matches_filter({'district': district}, {'districts': chosen}))

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
        квартиру не можна відкидати за те, чого список не друкує. Межу оцінює
        повна оренда з запасом на комунальні (rent_bounds): 485,52 € повної
        при 37,44 м² вкладаються в «до 340 € холодна» (340 + 4 × 37,44 ≈ 490)."""
        self.assertTrue(propotsdam_matching.matches_filter(
            self._listing(price_eur=None), {"max_price_eur": 340}))

    def test_a_flat_far_above_a_cold_only_bound_is_dropped_without_its_card(self):
        """Аудит 13.10.2026: без цього межа «лише холодна» на ProPotsdam не
        діяла зовсім."""
        self.assertFalse(propotsdam_matching.matches_filter(
            self._listing(price_eur=None, total_rent_eur=1100), {"max_price_eur": 562.1}))

    def test_both_bounds_apply_together(self):
        self.assertTrue(propotsdam_matching.matches_filter(
            self._listing(), {"max_price_eur": 400, "max_total_rent_eur": 500}))
        self.assertFalse(propotsdam_matching.matches_filter(
            self._listing(), {"max_price_eur": 400, "max_total_rent_eur": 450}))
