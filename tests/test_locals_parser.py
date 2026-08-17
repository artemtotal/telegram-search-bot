import unittest

from user_jobs import locals_parser


def _card(href, aria_label, tagline, area, rooms, price):
    return f"""
	<div class="item--wrapper" >
		<div class="item item--rounded item--properties bg bg--items p0   item--link">
			<figure class="item__media">
				<a href="{href}" title="x" tabindex="-1" aria-hidden="true">
<img class="yn-image" src="https://live-files.ynfinite.de/x.jpg">
				</a>
			</figure>
			<div class="item__content">
				<p class="h6 fw500 tagline">{tagline}</p>
				<h3 class="h5"><a title="x" aria-label="{aria_label}" href="{href}" class="heading fw700">{aria_label}</a></h3>
				<div class="content">
					<div class="row text">
						<div class="col-xs-6">
							<p class="m0 small">Wohnfläche</p>
							<h3 class="h5 m0">ca. {area} m²</h3>
						</div>
						<div class="col-xs-6">
							<p class="m0 small">Zimmer</p>
							<h3 class="h5 m0">{rooms}</h3>
						</div>
					</div>
					<div class="price text mt-a">
						<span>
							<p class="m0 small">Kaltmiete</p>
							<h3 class="h4 fw700 m0">{price} €</h3>
						</span>
						<a href="{href}" class="m0 button button--solid no-mt-a">Mehr erfahren</a>
					</div>
				</div>
			</div>
		</div>
	</div>
"""


PENTHOUSE_CARD = _card(
    "/immobilien/penthouse-wohnung-in-potsdam-miete-loc14178",
    "Traumhaftes 4-Zimmer-Penthouse mit Balkon, Küche und TG-Stellplatz",
    "14469 Potsdam - Wohnung zu mieten",
    "107,30", "4", "2.180",
)
SOUTERRAIN_CARD = _card(
    "/immobilien/in-potsdam-miete_pacht-loc13866",
    "Zwei-Zimmer-Souterrain-Apartment, mit Küche in gefragter Bestlage von Potsdam-Babelsberg",
    "14482 Potsdam - Wohnung zu mieten",
    "75", "2", "1.350",
)
PAGE_HTML = "<html><body>" + PENTHOUSE_CARD + SOUTERRAIN_CARD + "</body></html>"


class LocalsParserTests(unittest.TestCase):
    def test_parses_every_card_on_the_page(self):
        listings = locals_parser.parse_listings(PAGE_HTML)
        self.assertEqual(len(listings), 2)

    def test_extracts_rooms_area_price_address_and_link(self):
        listings = locals_parser.parse_listings(PAGE_HTML)
        penthouse = next(item for item in listings if "loc14178" in item["listing_key"])

        self.assertEqual(penthouse["listing_key"], "penthouse-wohnung-in-potsdam-miete-loc14178")
        self.assertEqual(penthouse["title"], "Traumhaftes 4-Zimmer-Penthouse mit Balkon, Küche und TG-Stellplatz")
        self.assertEqual(penthouse["rooms"], 4.0)
        self.assertEqual(penthouse["area_m2"], 107.3)
        self.assertEqual(penthouse["price_eur"], 2180.0)
        self.assertEqual(penthouse["city"], "Potsdam")
        self.assertEqual(
            penthouse["detail_url"],
            "https://locals.de/immobilien/penthouse-wohnung-in-potsdam-miete-loc14178",
        )

    def test_second_card_with_underscore_slug_also_parses(self):
        listings = locals_parser.parse_listings(PAGE_HTML)
        souterrain = next(item for item in listings if "loc13866" in item["listing_key"])
        self.assertEqual(souterrain["rooms"], 2.0)
        self.assertEqual(souterrain["area_m2"], 75.0)
        self.assertEqual(souterrain["price_eur"], 1350.0)

    def test_parse_decimal_handles_bare_thousands_dot_with_no_decimal_comma(self):
        # Kaltmiete is shown as whole euros ("2.180 €") — a lone dot here is a
        # thousands separator, never a decimal point, unlike SCHOBA's "700,37".
        self.assertEqual(locals_parser.parse_decimal("2.180 €"), 2180.0)
        self.assertEqual(locals_parser.parse_decimal("ca. 107,30 m²"), 107.3)
        self.assertIsNone(locals_parser.parse_decimal(""))

    def test_empty_page_returns_no_listings(self):
        self.assertEqual(locals_parser.parse_listings("<html><body>Nothing here</body></html>"), [])

    def test_format_listing_message_includes_the_key_fields(self):
        text = locals_parser.format_listing_message({
            "title": "Traumhaftes 4-Zimmer-Penthouse", "address": "14469 Potsdam - Wohnung zu mieten",
            "rooms": 4.0, "area_m2": 107.3, "price_eur": 2180.0,
            "detail_url": "https://locals.de/immobilien/penthouse-wohnung-in-potsdam-miete-loc14178",
        })
        self.assertIn("locals®", text)
        self.assertIn("Traumhaftes 4-Zimmer-Penthouse", text)
        self.assertIn("2180", text)


if __name__ == "__main__":
    unittest.main()
