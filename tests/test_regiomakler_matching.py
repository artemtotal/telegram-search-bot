"""Дві межі ціни у фільтрі ImmoTeam/alpha.

Портали називають різні величини: одні холодну оренду, інші повну. Фільтр
тримає обидві межі, і кожна звіряється зі своєю ціною — інакше квартира
відкидалась би за число, якого їй ніхто не обіцяв.
"""

import unittest

from user_jobs import regiomakler_matching


class WarmRentBoundTests(unittest.TestCase):
    """Дві межі ціни: холодна і тепла. Кожна звіряється зі своєю величиною."""

    def _listing(self, price_eur=1000.0, price_warm_eur=1300.0):
        return {"rooms": 3, "area_m2": 70, "price_eur": price_eur, "price_warm_eur": price_warm_eur}

    def test_a_warm_bound_is_measured_against_the_warm_rent(self):
        self.assertTrue(regiomakler_matching.matches_filter(
            self._listing(), {"max_price_warm_eur": 1400}))
        self.assertFalse(regiomakler_matching.matches_filter(
            self._listing(), {"max_price_warm_eur": 1200}))

    def test_a_cold_bound_ignores_the_warm_rent(self):
        """Холодна межа 1100 пропускає квартиру з теплою 1300 — це різні величини."""
        self.assertTrue(regiomakler_matching.matches_filter(
            self._listing(), {"max_price_warm_eur": None, "max_price_eur": 1100}))

    def test_both_bounds_apply_together(self):
        self.assertTrue(regiomakler_matching.matches_filter(
            self._listing(), {"max_price_eur": 1100, "max_price_warm_eur": 1400}))
        self.assertFalse(regiomakler_matching.matches_filter(
            self._listing(), {"max_price_eur": 1100, "max_price_warm_eur": 1250}))

    def test_a_listing_without_a_warm_rent_is_not_dropped_by_a_warm_bound(self):
        """Портал теплої ціни не назвав — умова просто не застосовується, як і
        для будь-якого іншого невідомого показника."""
        self.assertTrue(regiomakler_matching.matches_filter(
            self._listing(price_warm_eur=None), {"max_price_warm_eur": 900}))
