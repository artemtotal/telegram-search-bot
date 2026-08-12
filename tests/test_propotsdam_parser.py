import unittest

from user_jobs import propotsdam_parser


class ProPotsdamParserTests(unittest.TestCase):
    def test_parse_german_numbers(self):
        self.assertEqual(propotsdam_parser.parse_decimal('963,79 EUR'), 963.79)
        self.assertEqual(propotsdam_parser.parse_decimal('1.234,50 €'), 1234.50)
        self.assertEqual(propotsdam_parser.parse_decimal('-'), None)

    def test_parse_rooms_area_and_money_from_labelled_payload(self):
        listing = propotsdam_parser.normalize_listing({
            'title': 'renovierte Altbauwohnung',
            'address': 'Großbeerenstr. 19, 14482 Potsdam',
            'district': 'Babelsberg',
            'rooms': '2',
            'area': '64 m²',
            'total_rent': '963,79 EUR',
            'available_from': 'ab sofort',
            'detail_url': 'https://example.test/#/expose/42',
            'extra': {'Etage': '2. OG'},
        })

        self.assertEqual(listing['title'], 'renovierte Altbauwohnung')
        self.assertEqual(listing['district'], 'Babelsberg')
        self.assertEqual(listing['rooms'], 2.0)
        self.assertEqual(listing['area_m2'], 64.0)
        self.assertEqual(listing['total_rent_eur'], 963.79)
        self.assertEqual(listing['available_from'], 'ab sofort')
        self.assertEqual(listing['extra']['Etage'], '2. OG')
        self.assertTrue(listing['listing_key'])

    def test_format_all_listing_data_keeps_unknown_extra_fields(self):
        listing = propotsdam_parser.normalize_listing({
            'title': 'Wohnung mit Balkon',
            'address': 'Beispielstr. 1, 14482 Potsdam',
            'district': 'Waldstadt 2',
            'rooms': '1,5',
            'area': '48,5 m²',
            'total_rent': '700 EUR',
            'available_from': '01.11.2026',
            'detail_url': '',
            'extra': {'Kaution': '1.400 EUR', 'Etage': '3'},
        })

        text = propotsdam_parser.format_listing_message(listing, portal_url='https://portal.example/')

        self.assertIn('Wohnung mit Balkon', text)
        self.assertIn('Waldstadt 2', text)
        self.assertIn('1.5', text)
        self.assertIn('48.5', text)
        self.assertIn('700', text)
        self.assertIn('Kaution: 1.400 EUR', text)
        self.assertIn('Etage: 3', text)
        self.assertIn('https://portal.example/', text)


if __name__ == '__main__':
    unittest.main()
