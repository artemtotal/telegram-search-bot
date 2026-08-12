import unittest
from datetime import datetime

from user_jobs import propotsdam_store


class ProPotsdamStoreTests(unittest.TestCase):
    def test_numeric_text_parsing_for_admin_flow(self):
        self.assertEqual(propotsdam_store.parse_optional_number('1,5'), 1.5)
        self.assertEqual(propotsdam_store.parse_optional_number('-'), None)
        self.assertEqual(propotsdam_store.parse_optional_number(''), None)

    def test_districts_are_normalized_and_deduplicated(self):
        self.assertEqual(
            propotsdam_store.normalize_districts('Babelsberg, babelsberg, Waldstadt 2'),
            'Babelsberg,Waldstadt 2',
        )
        self.assertEqual(propotsdam_store.normalize_districts('всі'), '')

    def test_select_unsent_matches_uses_delivery_keys(self):
        listing = {'listing_key': 'abc', 'district': 'Babelsberg', 'rooms': 2.0, 'area_m2': 64.0, 'total_rent_eur': 900.0}
        filt = {'filter_id': 7, 'user_id': 123, 'districts': 'Babelsberg', 'max_total_rent_eur': 1000.0}

        matches = propotsdam_store.select_unsent_matches([listing], [filt], delivered={(7, 'abc')})
        self.assertEqual(matches, [])

        matches = propotsdam_store.select_unsent_matches([listing], [filt], delivered=set())
        self.assertEqual(matches, [(filt, listing)])


if __name__ == '__main__':
    unittest.main()
