import unittest

from user_jobs import karlmarx_parser


def _card(data_type, href, title, area_label, area_value, price_label, price_value, rooms_value, street, city):
    return f"""
	<div class="immo-object card"
                        data-type="{data_type}"><a class="card-link" href="{href}"><div class="card-content"><div class="card-image"><img alt="{title}" src="x.png" width="1000" height="496" /></div><div class="card-body"><h3 class="card-title">{title}</h3><div class="card-details"><div class="space"><div class="number">
                            {area_value} m&sup2;
                        </div><div class="title">
                            {area_label}
                        </div></div><div class="price"><div class="number">
                            {price_value} &euro;
                        </div><div class="title">
                            {price_label}
                        </div></div><div class="rooms"><div class="number">
                            {rooms_value}
                        </div><div class="title">
                            Zimmer
                        </div></div></div><div class="location"><div class="street">
                        {street}
                    </div><div class="city">
                        {city}
                    </div></div></div></div></a></div>"""


RESIDENTIAL_CARD = _card(
    "Wohnung Miete", "/fuer-wohnungssucher/expose/potsdamer-mitte-gewerbe-bueroflaechen",
    "Potsdamer Mitte - Gewerbe, Bürofläche zu vermieten",
    "Wohnfläche", "97", "Warmmiete", "2861,50", "2",
    "Alter Markt 5a", "14467 Potsdam",
)
COMMERCIAL_CARD = _card(
    "Büro/Praxis", "/fuer-wohnungssucher/expose/bueroraeume-ahornstrasse",
    "Büroräume Ahornstraße zu vermieten",
    "Hauptfläche", "234", "Miete pro Monat", "4212", "2026",
    "Ahornstraße 20", "14482 Potsdam",
)
PAGE_HTML = "<html><body>" + COMMERCIAL_CARD + RESIDENTIAL_CARD + "</body></html>"


class KarlmarxParserTests(unittest.TestCase):
    def test_commercial_cards_are_filtered_out(self):
        listings = karlmarx_parser.parse_listings(PAGE_HTML)
        self.assertEqual(len(listings), 1)

    def test_residential_card_extracts_rooms_area_price_address_and_link(self):
        listings = karlmarx_parser.parse_listings(PAGE_HTML)
        listing = listings[0]

        self.assertEqual(listing["listing_key"], "potsdamer-mitte-gewerbe-bueroflaechen")
        self.assertEqual(listing["title"], "Potsdamer Mitte - Gewerbe, Bürofläche zu vermieten")
        self.assertEqual(listing["rooms"], 2.0)
        self.assertEqual(listing["area_m2"], 97.0)
        self.assertEqual(listing["price_eur"], 2861.5)
        self.assertEqual(listing["city"], "Potsdam")
        self.assertEqual(
            listing["detail_url"],
            "https://wgkarlmarx.de/fuer-wohnungssucher/expose/potsdamer-mitte-gewerbe-bueroflaechen",
        )

    def test_count_all_cards_counts_both_types(self):
        self.assertEqual(karlmarx_parser.count_all_cards(PAGE_HTML), 2)

    def test_empty_page_returns_no_listings_and_zero_cards(self):
        self.assertEqual(karlmarx_parser.parse_listings("<html><body>Nothing here</body></html>"), [])
        self.assertEqual(karlmarx_parser.count_all_cards("<html><body>Nothing here</body></html>"), 0)

    def test_page_with_only_commercial_cards_returns_no_residential_listings(self):
        """A market day with 0 residential Karl Marx listings is normal — the
        page itself is never empty, just filtered down to nothing relevant."""
        html = "<html><body>" + COMMERCIAL_CARD + "</body></html>"
        self.assertEqual(karlmarx_parser.parse_listings(html), [])
        self.assertEqual(karlmarx_parser.count_all_cards(html), 1)

    def test_parse_decimal_handles_german_thousands_and_comma_decimals(self):
        self.assertEqual(karlmarx_parser.parse_decimal("2.861,50 €"), 2861.5)
        self.assertEqual(karlmarx_parser.parse_decimal("97 m²"), 97.0)
        self.assertIsNone(karlmarx_parser.parse_decimal(""))

    def test_format_listing_message_includes_the_key_fields(self):
        text = karlmarx_parser.format_listing_message({
            "title": "Potsdamer Mitte - Gewerbe, Bürofläche zu vermieten",
            "address": "Alter Markt 5a, 14467 Potsdam",
            "rooms": 2.0, "area_m2": 97.0, "price_eur": 2861.5,
            "detail_url": "https://wgkarlmarx.de/fuer-wohnungssucher/expose/x",
        })
        self.assertIn("Karl Marx", text)
        self.assertIn("Potsdamer Mitte", text)
        self.assertIn("2861.5", text)


if __name__ == "__main__":
    unittest.main()
