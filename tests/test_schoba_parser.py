import unittest

from user_jobs import schoba_parser


def _card(status, kind, plz_city_district, headline, rows_html, href):
    return f"""
<div class="objektebilder">
    <a href="{href}" title="Details ansehen">
        <img src="bilder/x.jpg" title="Immobilien Potsdam" alt="Immobilien Potsdam - Objektfoto"></a>
</div>
<div class="objektetabelle">
    <table>
        <tr><th colspan="3" class="tabelletextleft-liste">
            <span class="objektart"><span class="farbe1">{status}</span>
                <br>{kind}
                <br></span>{plz_city_district}
                <br>{headline}</th></tr>
        {rows_html}
        <tr><td>Nettokaltmiete:</td><td class="objektetabellespalte2">PLACEHOLDER</td>
            <td><a href="{href}" title="Objektbeschreibung">zum Exposé</a></td></tr>
    </table>
</div>
"""


VACANT_ROWS = (
    '<tr><td>Zimmer:</td><td class="objektetabellespalte2" colspan="2">3</td></tr>'
    '<tr><td>Wohnfläche:</td><td class="objektetabellespalte2" colspan="2">ca. 61 m²</td></tr>'
    '<tr><td>Verfügbar ab:</td><td class="objektetabellespalte2 farbe1" colspan="2">01.08.2026</td></tr>'
)
RENTED_ROWS = (
    '<tr><td>Zimmer:</td><td class="objektetabellespalte2" colspan="2">4</td></tr>'
    '<tr><td>Wohnfläche:</td><td class="objektetabellespalte2" colspan="2">ca. 92 m²</td></tr>'
)

VACANT_CARD = _card(
    "Mietangebot", "Etagenwohnung (WE 65)", "14480 Potsdam (Babelsberg)",
    "Wohnen nahe Stern-Center Potsdam", VACANT_ROWS,
    "vm-gl-52-2.ogl-wohnen-nahe-stern-center-potsdam.htm",
).replace("PLACEHOLDER", "700,37 EUR")

RENTED_CARD = _card(
    "# vermietet", "Doppelhaushälfte", "14476 Potsdam (Fahrland)",
    "Modernes Haus in ruhiger Lage", RENTED_ROWS,
    "vm-mae-49-dhh-modernes-haus-in-ruhiger-lage.htm",
).replace("PLACEHOLDER", "0,00 EUR")

PAGE_HTML = "<html><body>" + RENTED_CARD + VACANT_CARD + "</body></html>"


class SchobaParserTests(unittest.TestCase):
    def test_parses_every_card_on_the_page(self):
        listings = schoba_parser.parse_listings(PAGE_HTML)
        self.assertEqual(len(listings), 2)

    def test_rented_card_is_flagged_not_vacant(self):
        listings = schoba_parser.parse_listings(PAGE_HTML)
        rented = next(item for item in listings if "Fahrland" in item["address"])
        self.assertFalse(rented["is_vacant"])
        self.assertEqual(rented["price_eur"], 0.0)

    def test_vacant_card_extracts_rooms_area_price_district_and_link(self):
        listings = schoba_parser.parse_listings(PAGE_HTML)
        vacant = next(item for item in listings if item["is_vacant"])

        self.assertEqual(vacant["listing_key"], "vm-gl-52-2.ogl-wohnen-nahe-stern-center-potsdam")
        self.assertEqual(vacant["title"], "Wohnen nahe Stern-Center Potsdam")
        self.assertEqual(vacant["rooms"], 3.0)
        self.assertEqual(vacant["area_m2"], 61.0)
        self.assertEqual(vacant["price_eur"], 700.37)
        self.assertEqual(vacant["district"], "Babelsberg")
        self.assertEqual(vacant["city"], "Potsdam")
        self.assertEqual(
            vacant["detail_url"],
            "https://www.schoba.de/immobilien/angebote/vm-gl-52-2.ogl-wohnen-nahe-stern-center-potsdam.htm",
        )

    def test_parse_decimal_handles_german_thousands_and_dashes(self):
        self.assertEqual(schoba_parser.parse_decimal("1.850,00 EUR"), 1850.0)
        self.assertEqual(schoba_parser.parse_decimal("ca. 61 m²"), 61.0)
        self.assertIsNone(schoba_parser.parse_decimal("-"))
        self.assertIsNone(schoba_parser.parse_decimal(""))

    def test_empty_page_returns_no_listings(self):
        self.assertEqual(schoba_parser.parse_listings("<html><body>Nothing here</body></html>"), [])

    def test_parse_gallery_urls_extracts_every_full_size_photo(self):
        detail_html = """
        <img src="bilder/objekt-id-bild-klein.jpg">
        <img src="bilder/objekt-id-foto-galerie-1gr.jpg">
        <img src="bilder/objekt-id-foto-galerie-1kl.jpg">
        <img src="bilder/objekt-id-foto-galerie-2gr.jpg">
        <img src="bilder/objekt-id-foto-galerie-2kl.jpg">
        """

        urls = schoba_parser.parse_gallery_urls(detail_html)

        self.assertEqual(urls, [
            "https://www.schoba.de/immobilien/angebote/bilder/objekt-id-foto-galerie-1gr.jpg",
            "https://www.schoba.de/immobilien/angebote/bilder/objekt-id-foto-galerie-2gr.jpg",
        ])

    def test_parse_gallery_urls_drops_the_repeated_thumbnail_strip(self):
        """Кожен кадр повторюється двічі на сторінці — вгорі й ще раз ближче до низу."""
        detail_html = (
            '<img src="bilder/objekt-id-foto-galerie-1gr.jpg">'
            '<img src="bilder/objekt-id-foto-galerie-1gr.jpg">'
        )

        self.assertEqual(
            schoba_parser.parse_gallery_urls(detail_html),
            ["https://www.schoba.de/immobilien/angebote/bilder/objekt-id-foto-galerie-1gr.jpg"],
        )

    def test_parse_gallery_urls_on_a_page_without_a_gallery_yields_nothing(self):
        self.assertEqual(schoba_parser.parse_gallery_urls("<html><body>Nothing here</body></html>"), [])

    def test_format_listing_message_includes_the_key_fields(self):
        text = schoba_parser.format_listing_message({
            "title": "Wohnen nahe Stern-Center Potsdam", "address": "14480 Potsdam, Babelsberg",
            "rooms": 3.0, "area_m2": 61.0, "price_eur": 700.37,
            "detail_url": "https://www.schoba.de/immobilien/angebote/vm-gl-52.htm",
        })
        self.assertIn("SCHOBA", text)
        self.assertIn("Wohnen nahe Stern-Center Potsdam", text)
        self.assertIn("700.37", text)


if __name__ == "__main__":
    unittest.main()


class SchobaDetailPriceTests(unittest.TestCase):
    """Повна ціна лежить на сторінці оголошення готовою — рахувати не треба."""

    DETAIL_HTML = """
    <table><tr><td>Nettokaltmiete:</td><td class="v">700,37 &euro;</td></tr>
    <tr><td>Nebenkosten:</td><td class="v">242,00 &euro;</td></tr>
    <tr><td>Gesamtmietpreis:</td><td class="v">942,37 EUR</td></tr></table>
    """

    def test_the_full_rent_is_read_from_the_detail_page(self):
        prices = schoba_parser.parse_detail_prices(self.DETAIL_HTML)

        self.assertEqual(prices["price_eur"], 700.37)
        self.assertEqual(prices["price_warm_eur"], 942.37)
        self.assertEqual(prices["nebenkosten_eur"], 242.0)

    def test_a_wg_room_priced_as_bruttowarmmiete_counts_as_the_full_rent(self):
        """Кімнати у WG сторінка показує вже теплою ціною, під іншою назвою."""
        html = '<table><tr><td>Bruttowarmmiete:</td><td>614,00 &euro;</td></tr></table>'

        prices = schoba_parser.parse_detail_prices(html)

        self.assertEqual(prices["price_warm_eur"], 614.0)

    def test_a_page_without_prices_returns_nothing_rather_than_guessing(self):
        prices = schoba_parser.parse_detail_prices("<html><body>Kein Preis</body></html>")

        self.assertIsNone(prices["price_warm_eur"])
        self.assertIsNone(prices["price_eur"])
