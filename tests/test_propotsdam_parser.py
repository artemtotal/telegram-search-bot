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

    def test_parse_boxlist_xml_extracts_reobj_heads(self):
        xml = '''<?xml version="1.0" encoding="utf-8"?>
        <boxlist xmlns="http://www.openpromos.com/OPPC/XMLForms">
          <section title="Immobilien">
            <box boxid="ESQ_VM_REOBJ_ALL" title="Immobilien">
              <head>
                <id>ABC</id>
                <originalId>ORIG</originalId>
                <address city="Potsdam" postcode="14480" street="Wolfgang-Staudte-Str. 3"/>
                <title>Wohnen in der Gartenstadt Drewitz</title>
                <details>
                  <row title="Stadtteil">Drewitz</row>
                  <row title="Zimmer">3</row>
                  <row title="Wohnfläche">61 m²</row>
                  <row title="Gesamtmiete">705,67 EUR</row>
                  <row title="Verfügbarkeit">ab sofort</row>
                </details>
                <image resourceId="IMG1"/>
              </head>
            </box>
          </section>
        </boxlist>'''

        listings = propotsdam_parser.parse_boxlist_xml(xml)

        self.assertEqual(len(listings), 1)
        # Not 'ABC' (the <id>) - see the _stable_key docstring-comment for
        # why: <id> rotates on every poll for the same real listing, <originalId>
        # doesn't, so the dedup key has to be built from the latter.
        self.assertEqual(listings[0]['listing_key'], 'ORIG')
        self.assertEqual(listings[0]['district'], 'Drewitz')
        self.assertEqual(listings[0]['rooms'], 3.0)
        self.assertEqual(listings[0]['area_m2'], 61.0)
        self.assertEqual(listings[0]['total_rent_eur'], 705.67)
        self.assertIn('IMG1', listings[0]['image_url'])
        self.assertEqual(listings[0]['extra']['originalId'], 'ORIG')
        self.assertTrue(listings[0]['listing_key'])

    def test_the_same_listing_keeps_one_key_even_when_id_rotates(self):
        """Reproduces the real duplicate-notification bug: ProPotsdam sends
        a different <id> for the identical listing on every poll, while
        <originalId>/everything else stays put."""

        def xml_with_id(rotating_id: str) -> str:
            return f'''<?xml version="1.0" encoding="utf-8"?>
            <boxlist xmlns="http://www.openpromos.com/OPPC/XMLForms">
              <section title="Immobilien">
                <box boxid="ESQ_VM_REOBJ_ALL" title="Immobilien">
                  <head>
                    <id>{rotating_id}</id>
                    <originalId>872F068F-FE11-BB52-C157-C3145E5825C8</originalId>
                    <address city="Potsdam" postcode="14478" street="Saarmunder Str. 45"/>
                    <title>Helle 3-Raum-Wohnung!</title>
                    <details>
                      <row title="Stadtteil">Waldstadt 2</row>
                      <row title="Zimmer">3</row>
                      <row title="Wohnfläche">54 m²</row>
                      <row title="Gesamtmiete">650,40 EUR</row>
                      <row title="Verfügbarkeit">ab sofort</row>
                    </details>
                    <image resourceId="A252DE53"/>
                  </head>
                </box>
              </section>
            </boxlist>'''

        keys = {
            propotsdam_parser.parse_boxlist_xml(xml_with_id(rotating_id))[0]['listing_key']
            for rotating_id in (
                '1D903B03-CA4F-9D7D-2F6B-4DD721ED0F16',
                '0CA15BD0-02A5-D0A6-1685-72745683CD2E',
                '1450418F-660A-B5A7-6AAC-9582BA8E26DB',
                'B4563664-482D-223C-2845-24069DAA5C57',
            )
        }
        self.assertEqual(keys, {'872F068F-FE11-BB52-C157-C3145E5825C8'})

    def test_every_image_is_kept_not_only_the_cover(self):
        """Оголошення з трьома фото має віддавати три, а не саму обкладинку."""
        xml = '''<?xml version="1.0" encoding="utf-8"?>
        <boxlist xmlns="http://www.openpromos.com/OPPC/XMLForms">
          <section title="Immobilien">
            <box boxid="ESQ_VM_REOBJ_ALL" title="Immobilien">
              <head>
                <id>ABC</id>
                <originalId>ORIG</originalId>
                <address city="Potsdam" postcode="14480" street="Wolfgang-Staudte-Str. 3"/>
                <title>Wohnen in der Gartenstadt Drewitz</title>
                <image resourceId="707C13F6-743D-744E-F05B-26541CFC470D"/>
                <image resourceId="790A6B78-172E-460C-BCEA-EE355B49537C"/>
                <image resourceId="3A2FDA42-680E-5A45-74B3-73D6408B6DAE"/>
              </head>
            </box>
          </section>
        </boxlist>'''

        listing = propotsdam_parser.parse_boxlist_xml(xml)[0]

        self.assertEqual(propotsdam_parser.image_resource_ids(listing), [
            '707C13F6-743D-744E-F05B-26541CFC470D',
            '790A6B78-172E-460C-BCEA-EE355B49537C',
            '3A2FDA42-680E-5A45-74B3-73D6408B6DAE',
        ])
        urls = propotsdam_parser.image_urls(listing)
        self.assertEqual(len(urls), 3)
        # Обкладинка лишається першою: підпис і прев'ю мають не з'їхати.
        self.assertEqual(urls[0], listing['image_url'])

    def test_image_ids_survive_a_round_trip_through_storage(self):
        """Сховище тримає extra в raw_json, тож старі оголошення теж дають усі фото."""
        stored = propotsdam_parser.normalize_listing({
            'title': 'Helle 3-Raum-Wohnung',
            'image_url': propotsdam_parser.IMAGE_URL_TEMPLATE.format(resource_id='F39EA718-C883-DBAE-33EE-A602DB15D3CA'),
            'extra': {'image_resource_ids': 'F39EA718-C883-DBAE-33EE-A602DB15D3CA,54561F8A-A8D6-7D5B-5FF3-E238A0AC478E'},
        })

        self.assertEqual(len(propotsdam_parser.image_resource_ids(stored)), 2)

    def test_a_listing_without_resource_ids_falls_back_to_the_cover(self):
        """DOM-розбір resourceId не бачить — лишається одна обкладинка."""
        listing = propotsdam_parser.normalize_listing({
            'title': 'Wohnung',
            'image_url': 'https://portal.example/img/1.jpg',
        })

        self.assertEqual(propotsdam_parser.image_resource_ids(listing), [])
        self.assertEqual(propotsdam_parser.image_urls(listing), ['https://portal.example/img/1.jpg'])

    def test_a_listing_without_any_image_yields_nothing(self):
        listing = propotsdam_parser.normalize_listing({'title': 'Wohnung'})

        self.assertEqual(propotsdam_parser.image_urls(listing), [])

    def test_unusable_resource_ids_are_dropped(self):
        """Id доходить до імені файла й до HTTP-шляху, тож сміття туди не пускаємо."""
        listing = propotsdam_parser.normalize_listing({
            'title': 'Wohnung',
            'extra': {'image_resource_ids': '../../etc/passwd,,short,GOOD-RESOURCE-ID-0001'},
        })

        self.assertEqual(propotsdam_parser.image_resource_ids(listing), ['GOOD-RESOURCE-ID-0001'])

    def test_a_repeated_resource_id_is_listed_once(self):
        listing = propotsdam_parser.normalize_listing({
            'title': 'Wohnung',
            'extra': {'image_resource_ids': 'GOOD-RESOURCE-ID-0001,GOOD-RESOURCE-ID-0001'},
        })

        self.assertEqual(propotsdam_parser.image_resource_ids(listing), ['GOOD-RESOURCE-ID-0001'])

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


class CardPriceTests(unittest.TestCase):
    """Розбивка ціни зі сторінки оголошення.

    Список порталу друкує саму лише Gesamtmiete, і довго вважалось, що
    холодної оренди ProPotsdam не публікує взагалі. Вона є — усередині
    картки, у блоці «Kosten».
    """

    CARD_TEXT = """DetailKarte
1-Zimmer-Wohnung mit separater Küche
Wohnfläche
37,44 m²
Gesamtmiete
485,52 EUR
Kosten
Kaltmiete
326,48 EUR
Betriebskosten
81,24 EUR
Heizkosten
77,80 EUR
Gesamtmiete
485,52 EUR
Kaution
3 Nettokaltmieten
"""

    def test_every_part_of_the_rent_is_read(self):
        prices = propotsdam_parser.parse_card_prices(self.CARD_TEXT)

        self.assertEqual(prices["price_eur"], 326.48)
        self.assertEqual(prices["nebenkosten_eur"], 81.24)
        self.assertEqual(prices["heizkosten_eur"], 77.80)
        self.assertEqual(prices["total_rent_eur"], 485.52)

    def test_the_parts_add_up_to_the_total(self):
        prices = propotsdam_parser.parse_card_prices(self.CARD_TEXT)
        total = prices["price_eur"] + prices["nebenkosten_eur"] + prices["heizkosten_eur"]

        self.assertEqual(round(total, 2), prices["total_rent_eur"])

    def test_a_card_without_a_cost_block_reports_nothing(self):
        prices = propotsdam_parser.parse_card_prices("Wohnfläche\n50 m²\nZimmer\n2\n")

        self.assertIsNone(prices["price_eur"])
        self.assertIsNone(prices["total_rent_eur"])

    def test_the_word_kaution_is_not_mistaken_for_a_price(self):
        """«Kaution: 3 Nettokaltmieten» — не сума, і в ціну потрапити не має."""
        prices = propotsdam_parser.parse_card_prices(self.CARD_TEXT)

        self.assertEqual(prices["price_eur"], 326.48)
