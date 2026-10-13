"""Межа «лише холодна» чи «лише повна» на площадці, що публікує іншу оренду.

Аудит 13.10.2026: у 9 людей із межею лише холодної оренди ProPotsdam і
Karl Marx (там відома тільки повна) не обмежували ціну зовсім — людина з
«до 640 € холодна» отримала Neubau за 1723 € повної.
"""

import unittest

from user_jobs import (
    karlmarx_matching, kleinanzeigen_matching, propotsdam_matching, rent_bounds, vonovia_matching,
)


class RentBoundsTests(unittest.TestCase):
    def test_warm_price_against_a_cold_only_limit(self):
        # 640 + 4 €/м² × 80 м² = 960.
        self.assertTrue(rent_bounds.missing_price_within(None, 950, 80, 640, None))
        self.assertFalse(rent_bounds.missing_price_within(None, 1723.23, 80, 640, None))
        # Без площі — × 1,5.
        self.assertFalse(rent_bounds.missing_price_within(None, 1000, None, 640, None))

    def test_cold_price_against_a_warm_only_limit(self):
        self.assertFalse(rent_bounds.missing_price_within(900, None, 60, None, 800))
        self.assertTrue(rent_bounds.missing_price_within(700, None, 60, None, 800))

    def test_nothing_is_decided_when_both_prices_or_none_are_known(self):
        self.assertTrue(rent_bounds.missing_price_within(None, None, 60, 500, 600))
        self.assertTrue(rent_bounds.missing_price_within(2000, 2500, 60, 500, 600))
        self.assertTrue(rent_bounds.missing_price_within(None, 3000, 60, None, None))


class MatchersTests(unittest.TestCase):
    KAHLEBERG = {'rooms': 4, 'area_m2': 98.5, 'price_eur': None, 'price_warm_eur': 1723.23}

    def test_karl_marx_honours_a_cold_only_limit(self):
        self.assertFalse(karlmarx_matching.matches_filter(self.KAHLEBERG, {'max_price_eur': 640}))
        self.assertTrue(karlmarx_matching.matches_filter(self.KAHLEBERG, {'max_price_eur': 1400}))
        # Повна межа задана — рішення за нею, холодна оцінка не втручається.
        self.assertTrue(karlmarx_matching.matches_filter(self.KAHLEBERG, {'max_price_eur': 640, 'max_price_warm_eur': 1800}))

    def test_propotsdam_honours_a_cold_only_limit(self):
        flat = {'rooms': 2, 'area_m2': 60, 'total_rent_eur': 1100, 'price_eur': None}
        self.assertFalse(propotsdam_matching.matches_filter(flat, {'max_price_eur': 562.1}))
        self.assertTrue(propotsdam_matching.matches_filter(flat, {'max_price_eur': 900}))

    def test_a_cold_price_over_the_warm_limit_is_rejected_everywhere(self):
        flat = {'rooms': 2, 'area_m2': 60, 'price_eur': 900, 'price_warm_eur': None}
        for matching in (kleinanzeigen_matching, vonovia_matching):
            self.assertFalse(matching.matches_filter(flat, {'max_price_warm_eur': 811}), matching.__name__)


if __name__ == '__main__':
    unittest.main()
