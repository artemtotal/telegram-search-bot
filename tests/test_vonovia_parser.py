import unittest

from user_jobs import vonovia_parser

# Скорочена, але справжня за формою відповідь пошуку Vonovia (знята з
# /api/real-estate/list 2026-09-02): квартира, гараж і рекламні картинки в
# галереї — усе так, як портал віддає насправді.
LIST_PAYLOAD = {
    "paging": {"info": {"count": 92, "limit": 15}},
    "results": [
        {
            "wrk_id": "1439890008",
            "titel": "Das Glück hat ein Zuhause: individuelle 2,5-Zimmer-Wohnung",
            "strasse": "Weitmarer Str. 145 a",
            "plz": "44795",
            "ort": "Bochum OT Weitmar",
            "preis": 841.89,
            "groesse": 63.3,
            "anzahl_zimmer": 2,
            "preview_img_url": "https://cdn.expose.vonovia.de/VNA-aadff53f.jpg?width=324&crop=4:3",
            "imageUrls": [
                "https://cdn.expose.vonovia.de/VNA-aadff53f.jpg?width=324&crop=4:3",
                "https://cdn.expose.vonovia.de/VNA-5b15f2bc.jpg?width=324&crop=4:3",
                "https://cdn.expose.vonovia.de/CAMP-Gruenstrom_v5.jpg?width=324&crop=4:3",
                "https://cdn.expose.vonovia.de/CAMP-APP_v1.jpg?width=324&crop=4:3",
            ],
            "slug": "das-glueck-hat-ein-zuhause-84-1439890008",
        },
        {
            "wrk_id": "1716010037",
            "titel": "Nie wieder Parkplatzsuche!",
            "strasse": "Lotte-Laserstein-Straße bei 6-16,13-37",
            "plz": "14482",
            "ort": "Potsdam OT Babelsberg",
            "preis": 80,
            "groesse": 0,
            "anzahl_zimmer": 0,
            "preview_img_url": "https://cdn.expose.vonovia.de/CAMP-Garagen_v1.jpg?width=324&crop=4:3",
            "imageUrls": [],
            "slug": "nie-wieder-parkplatzsuche-84-1716010037",
        },
    ],
}

# Сторінка оголошення несе всі свої дані одним JSON в атрибуті, і сервер
# екранує його як HTML — саме в такому вигляді, як тут.
DETAIL_HTML = (
    '<div class="estate-detail-page" '
    'data-vonovia-data="&#x7B;&quot;objectId&quot;&#x3A;&quot;1439890008&quot;,'
    '&quot;heading&quot;&#x3A;&quot;individuelle&#x20;2,5-Zimmer-Wohnung&quot;,'
    '&quot;space&quot;&#x3A;&quot;63,30&#x20;m&#x5C;u00b2&quot;,'
    '&quot;numberOfRooms&quot;&#x3A;&quot;2&quot;,'
    '&quot;rent&quot;&#x3A;841.88999999999999,&quot;warmRent&quot;&#x3A;1111.8900000000001,'
    '&quot;operatingCosts&quot;&#x3A;176,&quot;heatingCosts&quot;&#x3A;94,'
    '&quot;securityDeposit&quot;&#x3A;2525.67,'
    '&quot;images&quot;&#x3A;&#x5B;&#x7B;&quot;url&quot;&#x3A;&quot;https&#x3A;&#x5C;&#x2F;&#x5C;&#x2F;'
    'cdn.expose.vonovia.de&#x5C;&#x2F;VNA-aadff53f.jpg&#x3F;width&#x3D;538&quot;,&quot;caption&quot;&#x3A;&quot;&quot;&#x7D;,'
    '&#x7B;&quot;url&quot;&#x3A;&quot;https&#x3A;&#x5C;&#x2F;&#x5C;&#x2F;'
    'cdn.expose.vonovia.de&#x5C;&#x2F;CAMP-APP_v1.jpg&#x3F;width&#x3D;538&quot;,&quot;caption&quot;&#x3A;&quot;&quot;&#x7D;&#x5D;,'
    '&quot;streetAndHouseNumber&quot;&#x3A;&quot;Weitmarer&#x20;Str.&#x20;145&#x20;a&quot;&#x7D;">'
    "</div>"
)


class VonoviaListParsingTests(unittest.TestCase):
    def test_the_apartment_is_parsed_with_address_price_and_link(self):
        listings = vonovia_parser.parse_listings(LIST_PAYLOAD)

        self.assertEqual(len(listings), 1)
        listing = listings[0]
        self.assertEqual(listing["listing_key"], "1439890008")
        self.assertEqual(listing["address"], "Weitmarer Str. 145 a, 44795 Bochum OT Weitmar")
        self.assertEqual(listing["price_eur"], 841.89)
        self.assertEqual(listing["area_m2"], 63.3)
        self.assertEqual(
            listing["detail_url"],
            "https://www.vonovia.de/zuhause-finden/immobilien/das-glueck-hat-ein-zuhause-84-1439890008",
        )

    def test_a_parking_space_is_not_offered_as_a_flat(self):
        """Гаражів у видачі Vonovia по Потсдаму більше, ніж квартир.

        Вони приходять з нулями в площі й кімнатах, і без цієї перевірки
        людині прилетіла б «квартира» за 80 € без жодної кімнати.
        """
        keys = [item["listing_key"] for item in vonovia_parser.parse_listings(LIST_PAYLOAD)]

        self.assertNotIn("1716010037", keys)

    def test_half_rooms_come_from_the_title_not_from_the_rounded_api_field(self):
        """`anzahl_zimmer` округлює вниз: 2,5-кімнатна приходить як 2."""
        listing = vonovia_parser.parse_listings(LIST_PAYLOAD)[0]

        self.assertEqual(listing["rooms"], 2.5)

    def test_marketing_pictures_are_kept_out_of_the_gallery(self):
        """У галереї поруч із фото квартири лежить реклама застосунку."""
        listing = vonovia_parser.parse_listings(LIST_PAYLOAD)[0]

        self.assertEqual(len(listing["gallery_urls"]), 2)
        self.assertTrue(all("CAMP-" not in url for url in listing["gallery_urls"]))

    def test_photos_are_requested_at_full_size_instead_of_the_324px_preview(self):
        listing = vonovia_parser.parse_listings(LIST_PAYLOAD)[0]

        self.assertTrue(all(url.endswith("?width=1200") for url in listing["gallery_urls"]))
        self.assertTrue(listing["cover_image_url"].endswith("?width=1200"))

    def test_total_count_drives_paging(self):
        self.assertEqual(vonovia_parser.total_count(LIST_PAYLOAD), 92)
        self.assertEqual(vonovia_parser.total_count({}), 0)


class VonoviaDetailParsingTests(unittest.TestCase):
    def test_the_full_rent_is_read_from_the_listing_page(self):
        prices = vonovia_parser.parse_detail_prices(DETAIL_HTML)

        self.assertEqual(prices["price_eur"], 841.89)
        self.assertEqual(prices["price_warm_eur"], 1111.89)
        self.assertEqual(prices["nebenkosten_eur"], 176)
        self.assertEqual(prices["heizkosten_eur"], 94)

    def test_the_full_rent_falls_back_to_the_sum_when_the_page_omits_it(self):
        html = DETAIL_HTML.replace("&quot;warmRent&quot;&#x3A;1111.8900000000001,", "")

        self.assertEqual(vonovia_parser.parse_detail_prices(html)["price_warm_eur"], 1111.89)

    def test_a_page_without_the_data_block_yields_no_prices_instead_of_raising(self):
        prices = vonovia_parser.parse_detail_prices("<html><body>Wartung</body></html>")

        self.assertIsNone(prices["price_eur"])
        self.assertIsNone(prices["price_warm_eur"])

    def test_the_listing_page_gallery_also_drops_the_marketing_pictures(self):
        urls = vonovia_parser.parse_gallery_urls(DETAIL_HTML)

        self.assertEqual(urls, ["https://cdn.expose.vonovia.de/VNA-aadff53f.jpg?width=1200"])


class VonoviaMessageTests(unittest.TestCase):
    def test_both_rents_are_shown_when_known(self):
        listing = dict(vonovia_parser.parse_listings(LIST_PAYLOAD)[0], price_warm_eur=1111.89)

        text = vonovia_parser.format_listing_message(listing)

        self.assertIn("Kaltmiete EUR: 841.89", text)
        self.assertIn("Warmmiete EUR: 1111.89", text)
        self.assertIn("Weitmarer Str. 145 a", text)

    def test_a_listing_without_the_full_rent_simply_omits_that_line(self):
        text = vonovia_parser.format_listing_message(vonovia_parser.parse_listings(LIST_PAYLOAD)[0])

        self.assertNotIn("Warmmiete", text)
