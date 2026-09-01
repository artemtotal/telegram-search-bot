import unittest

from user_jobs import regiomakler_parser


def _immoteam_card(status, title, href, subtitle, objekt_id, rooms, area, price_row):
    status_html = (
        f'<div class="property-status-bar"><div class="property-status property-status-{status}">{status}</div></div>'
        if status else ""
    )
    return f"""
<div class="property" role="listitem"><div class="property-container" id="id-{objekt_id}">
<div class="property-thumbnail col-sm-12 vertical"><a href="{href}" class="thumbnail">
<img src="x.jpg" /></a>{status_html}</div>
<div class="property-details col-sm-12 vertical"><h3 class="property-title"> <a href="{href}">{title}</a></h3>
<div class="property-subtitle"> {subtitle}</div>
<div class="property-data" role="list">
<div class="row data-objektnr_extern" role="listitem"><div class="dt col-sm-5">Objekt-ID:</div><div class="dd col-sm-7">{objekt_id}</div></div>
<div class="row data-anzahl_zimmer" role="listitem"><div class="dt col-sm-5">Zimmer:</div><div class="dd col-sm-7">{rooms}</div></div>
<div class="row data-wohnflaeche" role="listitem"><div class="dt col-sm-5">Wohnfläche&nbsp;ca.:</div><div class="dd col-sm-7">{area}&#8239;m²</div></div>
{price_row}
</div></div></div></div>
"""


def _price_row(kind, value):
    return f'<div class="row price data-{kind}" role="listitem"><div class="dt col-sm-5">Label:</div><div class="dd col-sm-7">{value}&#8239;EUR</div></div>'


def _alpha_card(title, href, subtitle, objekt_id, rooms, area, price_row):
    # alpha's theme wraps cards differently, but the plugin-owned inner markup is identical.
    return f"""
<div class="alpha-grid-item"><div class="property-thumbnail"><a href="{href}"><img src="x.jpg" /></a></div>
<div class="property-details col-sm-7">
<h3 class="property-title">
	<a href="{href}">{title}</a></h3>
	<div class="property-subtitle">
		{subtitle}	</div>
<div class="property-data" role="list">
<div class="row data-objektnr_extern" role="listitem"><div class="dt col-sm-5">Objekt-ID:</div><div class="dd col-sm-7">{objekt_id}</div></div>
<div class="row data-anzahl_zimmer" role="listitem"><div class="dt col-sm-5">Zimmer:</div><div class="dd col-sm-7">{rooms}</div></div>
<div class="row data-wohnflaeche" role="listitem"><div class="dt col-sm-5">Wohnfläche&nbsp;ca.:</div><div class="dd col-sm-7">{area}&#8239;m²</div></div>
{price_row}
</div></div></div>
"""


VACANT_RENTAL = _immoteam_card(
    "", "Coming Soon – moderne Neubau-Maisonettewohnung", "https://immoteam-potsdam.de/x-12863-4/",
    "14482 Potsdam-Babelsberg, Maisonettewohnung", "12863_4", "3", "73,12", _price_row("kaltmiete", "1.754,88"),
)
RENTED_OUT = _immoteam_card(
    "vermietet", "Moderne Wohnung mit 2 Balkonen", "https://immoteam-potsdam.de/x-12766/",
    "14469 Potsdam, Etagenwohnung", "12766", "3", "87,32", _price_row("kaltmiete", "1.680,00"),
)
FOR_SALE = _immoteam_card(
    "", "Modernisierter Bungalow", "https://immoteam-potsdam.de/x-12868/",
    "14532 Kleinmachnow, Einfamilienhaus", "12868", "3", "98", _price_row("kaufpreis", "770.000"),
)
IMMOTEAM_PAGE = "<html><body>" + VACANT_RENTAL + RENTED_OUT + FOR_SALE + "</body></html>"

ALPHA_DUPLICATE = _alpha_card(
    "Coming Soon – moderne Neubau-Maisonettewohnung", "https://potsdam-immobilien.de/x-12863-4/",
    "14482 Potsdam-Babelsberg, Maisonettewohnung", "12863_4", "3", "73,12", _price_row("kaltmiete", "1.754,88"),
)
ALPHA_ONLY = _alpha_card(
    "Großzügige Neubau-Maisonettewohnung", "https://potsdam-immobilien.de/x-12863-2/",
    "14482 Potsdam-Babelsberg, Maisonettewohnung", "12863_2", "5", "129,99", _price_row("kaltmiete", "3.119,76"),
)
ALPHA_PAGE = "<html><body>" + ALPHA_DUPLICATE + ALPHA_ONLY + "</body></html>"


class RegiomaklerParserTests(unittest.TestCase):
    def test_parses_every_card_regardless_of_theme_wrapper(self):
        listings = regiomakler_parser.parse_listings(IMMOTEAM_PAGE, "immoteam")
        self.assertEqual(len(listings), 3)
        listings2 = regiomakler_parser.parse_listings(ALPHA_PAGE, "alpha")
        self.assertEqual(len(listings2), 2)

    def test_vacant_rental_card_is_extracted_correctly(self):
        listings = regiomakler_parser.parse_listings(IMMOTEAM_PAGE, "immoteam")
        vacant = next(item for item in listings if item["listing_key"] == "12863_4")

        self.assertTrue(vacant["is_rental"])
        self.assertTrue(vacant["is_vacant"])
        self.assertEqual(vacant["rooms"], 3.0)
        self.assertEqual(vacant["area_m2"], 73.12)
        self.assertEqual(vacant["price_eur"], 1754.88)
        self.assertEqual(vacant["city"], "Potsdam-Babelsberg")

    def test_rented_out_card_is_flagged_not_vacant(self):
        listings = regiomakler_parser.parse_listings(IMMOTEAM_PAGE, "immoteam")
        rented = next(item for item in listings if item["listing_key"] == "12766")
        self.assertEqual(rented["status"], "vermietet")
        self.assertFalse(rented["is_vacant"])

    def test_sale_card_is_flagged_not_rental(self):
        listings = regiomakler_parser.parse_listings(IMMOTEAM_PAGE, "immoteam")
        sale = next(item for item in listings if item["listing_key"] == "12868")
        self.assertFalse(sale["is_rental"])
        self.assertIsNone(sale["price_eur"])

    def test_same_objekt_id_parses_identically_from_both_sites(self):
        """Confirms the two feeds really do republish the same listing under the same ID."""
        immoteam_listing = next(
            item for item in regiomakler_parser.parse_listings(IMMOTEAM_PAGE, "immoteam")
            if item["listing_key"] == "12863_4"
        )
        alpha_listing = next(
            item for item in regiomakler_parser.parse_listings(ALPHA_PAGE, "alpha")
            if item["listing_key"] == "12863_4"
        )
        self.assertEqual(immoteam_listing["rooms"], alpha_listing["rooms"])
        self.assertEqual(immoteam_listing["area_m2"], alpha_listing["area_m2"])
        self.assertEqual(immoteam_listing["price_eur"], alpha_listing["price_eur"])

    def test_empty_page_returns_no_listings(self):
        self.assertEqual(regiomakler_parser.parse_listings("<html><body>Nothing</body></html>", "immoteam"), [])

    def test_parse_gallery_urls_extracts_full_size_photos_deduped(self):
        detail_html = """
        <img src="https://immoteam-potsdam.de/wp-content/uploads/immomakler/attachments/abc123/photo1-360x270.jpg" srcset="https://immoteam-potsdam.de/wp-content/uploads/immomakler/attachments/abc123/photo1.jpg 1280w, https://immoteam-potsdam.de/wp-content/uploads/immomakler/attachments/abc123/photo1-360x270.jpg 360w">
        <img src="https://immoteam-potsdam.de/wp-content/uploads/immomakler/attachments/abc123/photo2.jpg">
        """

        urls = regiomakler_parser.parse_gallery_urls(detail_html)

        self.assertEqual(urls, [
            "https://immoteam-potsdam.de/wp-content/uploads/immomakler/attachments/abc123/photo1.jpg",
            "https://immoteam-potsdam.de/wp-content/uploads/immomakler/attachments/abc123/photo2.jpg",
        ])

    def test_parse_gallery_urls_ignores_prev_next_navigation_thumbnails(self):
        """Сторінка оголошення показує ще один-два кадри з навігації
        "попереднє/наступне оголошення" — це вже тека ІНШОГО оголошення, і
        вона завжди рідше згадується, ніж справжня галерея."""
        this_listing = "".join(
            f'<img src="https://potsdam-immobilien.de/wp-content/uploads/immomakler/attachments/dominant/p{i}.jpg">'
            for i in range(5)
        )
        nav_prev = '<img src="https://potsdam-immobilien.de/wp-content/uploads/immomakler/attachments/otherprev/x.jpg">'
        nav_next = '<img src="https://potsdam-immobilien.de/wp-content/uploads/immomakler/attachments/othernext/y.jpg">'
        detail_html = nav_prev + this_listing + nav_next

        urls = regiomakler_parser.parse_gallery_urls(detail_html)

        self.assertEqual(len(urls), 5)
        self.assertTrue(all("/dominant/" in url for url in urls))

    def test_parse_gallery_urls_on_a_page_without_a_gallery_yields_nothing(self):
        self.assertEqual(regiomakler_parser.parse_gallery_urls("<html><body>Nothing</body></html>"), [])

    def test_format_listing_message_includes_the_key_fields(self):
        text = regiomakler_parser.format_listing_message({
            "title": "Maisonettewohnung", "address": "14482 Potsdam-Babelsberg, Maisonettewohnung",
            "rooms": 3.0, "area_m2": 73.12, "price_eur": 1754.88,
            "detail_url": "https://immoteam-potsdam.de/x-12863-4/",
        })
        self.assertIn("ImmoTeam/alpha", text)
        self.assertIn("Maisonettewohnung", text)
        self.assertIn("1754.88", text)


if __name__ == "__main__":
    unittest.main()


class RegiomaklerWarmRentTests(unittest.TestCase):
    """Тепла оренда стоїть у тій самій картці — другого запиту не треба."""

    def _card_with(self, price_rows):
        return _immoteam_card(
            "", "Wohnung mit Warmmiete", "https://immoteam-potsdam.de/x-1/",
            "14469 Potsdam, Etagenwohnung", "77_1", "3", "67,00", price_rows,
        )

    def test_both_rents_are_taken_from_one_card(self):
        html = self._card_with(
            _price_row("kaltmiete", "1.070,00") + _price_row("warmmiete", "1.250,00")
        )

        listing = regiomakler_parser.parse_listings(html, source="immoteam")[0]

        self.assertEqual(listing["price_eur"], 1070.0)
        self.assertEqual(listing["price_warm_eur"], 1250.0)

    def test_a_card_without_a_warm_rent_keeps_the_cold_one(self):
        """Приблизно кожне дев'яте оголошення теплої ціни не називає."""
        html = self._card_with(_price_row("kaltmiete", "1.140,00"))

        listing = regiomakler_parser.parse_listings(html, source="immoteam")[0]

        self.assertEqual(listing["price_eur"], 1140.0)
        self.assertIsNone(listing["price_warm_eur"])
