import unittest

from user_jobs import kleinanzeigen_parser


def _card(adid, plz_city, title, href, tags, price, image_url="https://img.kleinanzeigen.de/api/v1/prod-ads/images/x/y?rule=$_59.AUTO"):
    image_block = f"""
                        <script type="application/ld+json">
                            {{"contentUrl":"{image_url}"}}
                        </script>
    """ if image_url else ""
    return f"""
    <article class="aditem" data-adid="{adid}"  data-href="{href}">
        <div class="aditem-image--badges"></div>
        <div class="aditem-image">{image_block}</div>
        <div class="aditem-main">
            <div class="aditem-main--top">
                <div class="aditem-main--top--left">
                    <i class="icon icon-small icon-pin-gray" aria-hidden="true"></i> {plz_city}
                </div>
                <div class="aditem-main--top--right">
                    <p class="aditem-main--middle--price-shipping--price">{price}</p>
                </div>
            </div>
            <div class="aditem-main--middle">
                <h2 class="text-module-begin">
                    <a class="ellipsis" href="{href}">{title}</a>
                </h2>
                <p class="aditem-main--middle--description">Beschreibung...</p>
                <p class="aditem-main--middle--tags">
                    {tags}
                </p>
            </div>
        </div>
    </article>
    """


POTSDAM_CARD = _card(
    "3368739590", "14467 Potsdam", "Komfort Wohnung in historischer Innenstadt von Potsdam",
    "/s-anzeige/komfort-wohnung-in-historischer-innenstadt-von-potsdam/3368739590-203-7962",
    "162 m² &#183; 4 Zi. &#183; Online-Besichtigung", "2.445 &#8364;",
)
NEARBY_CARD = _card(
    "3484113505", "14712 Rathenow", "kwr GmbH Potsdamer Straße 2",
    "/s-anzeige/kwr-gmbh-potsdamer-strasse/3484113505-203-8110",
    "76,20 m² &#183; 3 Zi.", "572 &#8364;",
)
SWAP_CARD = _card(
    "3460827321", "14469 Potsdam", "TAUSCHWOHNUNG 2-Zimmer-Wohnung Nähe Potsdam",
    "/s-anzeige/tauschwohnung-2-zimmer-wohnung/3460827321-203-3033",
    "53 m² &#183; 2 Zi.", "614 &#8364;",
)
PAGE_HTML = "<html><body>" + POTSDAM_CARD + NEARBY_CARD + SWAP_CARD + "</body></html>"


class KleinanzeigenParserTests(unittest.TestCase):
    def test_parses_every_card_on_the_page(self):
        listings = kleinanzeigen_parser.parse_listings(PAGE_HTML)
        self.assertEqual(len(listings), 3)

    def test_extracts_rooms_area_price_and_city_correctly(self):
        listings = kleinanzeigen_parser.parse_listings(PAGE_HTML)
        potsdam = next(item for item in listings if item["listing_key"] == "3368739590")

        self.assertEqual(potsdam["title"], "Komfort Wohnung in historischer Innenstadt von Potsdam")
        self.assertEqual(potsdam["city"], "Potsdam")
        self.assertEqual(potsdam["rooms"], 4.0)
        self.assertEqual(potsdam["area_m2"], 162.0)
        self.assertEqual(potsdam["price_eur"], 2445.0)
        self.assertEqual(
            potsdam["detail_url"],
            "https://www.kleinanzeigen.de/s-anzeige/komfort-wohnung-in-historischer-innenstadt-von-potsdam/3368739590-203-7962",
        )

    def test_nearby_town_is_parsed_but_distinguishable_by_city_field(self):
        listings = kleinanzeigen_parser.parse_listings(PAGE_HTML)
        nearby = next(item for item in listings if item["listing_key"] == "3484113505")
        self.assertEqual(nearby["city"], "Rathenow")

    def test_handles_decimal_room_counts(self):
        listings = kleinanzeigen_parser.parse_listings(
            "<html><body>" + _card("1", "14467 Potsdam", "X", "/s-anzeige/x/1", "40 m² &#183; 1,5 Zi.", "560 &#8364;") + "</body></html>"
        )
        self.assertEqual(listings[0]["rooms"], 1.5)

    def test_extracts_the_cover_image_from_the_search_card(self):
        listings = kleinanzeigen_parser.parse_listings(PAGE_HTML)
        potsdam = next(item for item in listings if item["listing_key"] == "3368739590")
        self.assertEqual(
            potsdam["cover_image_url"],
            "https://img.kleinanzeigen.de/api/v1/prod-ads/images/x/y?rule=$_59.AUTO",
        )

    def test_a_card_without_a_photo_gets_an_empty_cover(self):
        listings = kleinanzeigen_parser.parse_listings(
            "<html><body>" + _card("1", "14467 Potsdam", "X", "/s-anzeige/x/1", "40 m² &#183; 1 Zi.", "500 &#8364;", image_url="") + "</body></html>"
        )
        self.assertEqual(listings[0]["cover_image_url"], "")

    def test_empty_page_returns_no_listings(self):
        self.assertEqual(kleinanzeigen_parser.parse_listings("<html><body>Nothing</body></html>"), [])

    def test_format_listing_message_includes_the_key_fields(self):
        text = kleinanzeigen_parser.format_listing_message({
            "title": "Komfort Wohnung", "address": "14467 Potsdam",
            "rooms": 4.0, "area_m2": 162.0, "price_eur": 2445.0,
            "detail_url": "https://www.kleinanzeigen.de/s-anzeige/x/1",
        })
        self.assertIn("Kleinanzeigen", text)
        self.assertIn("Komfort Wohnung", text)
        self.assertIn("2445.0", text)


if __name__ == "__main__":
    unittest.main()
