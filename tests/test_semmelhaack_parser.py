import unittest

from user_jobs import semmelhaack_parser


def _card(title, address, area_label, area_value, rooms_label, rooms_value, price, href):
    return f"""
    <div class="objekt-single">
        <img src="data:..." data-src="https://api.semmelhaack.de/bilder/objekte/{href.rstrip('/').rsplit('/', 1)[-1]}.jpg" alt="{title}" />
        <div class="objekt-single-data">
            <h3>{title}</h3>
            <div class="table">
                <div class="row">
                    <span class="label">Adresse:</span>
                    <span class="gap"></span>
                    <span class="value">{address}</span>
                </div>
                <div class="row">
                    <span class="label">
                        {area_label}:
                    </span>
                    <span class="gap"></span>
                    <span class="value">{area_value}</span>
                </div>
                <div class="row">
                    <span class="label">
                        {rooms_label}:
                    </span>
                    <span class="gap"></span>
                    <span class="value">{rooms_value}</span>
                </div>
                <div class="row">
                    <span class="label">
                        Kaltmiete:
                    </span>
                    <span class="gap"></span>
                    <span class="value">{price}</span>
                </div>
            </div>
            <div class="objekt-single-data-last">
                <a href="{href}" target="_blank" class="poi__container-content-anchor zur-objektbeschreibung">
                    Zur Objektbeschreibung
                </a>
            </div>
        </div>
    </div>
    """


PAGE_HTML = "<html><body>" + _card(
    "4-Zimmer-DHH mit Terrasse", "Gärtner-Schmidt-Str. 10, 14476 Potsdam",
    "Nutzfläche", "94,94 m²", "Räume", "4", "1.850,00 €",
    "/vermietung/wohnobjekte/details-wohnobjekt/63860/",
) + _card(
    "Kompakte 1-Zi. Wohnung", "Hermannstraße 22, 38114 Braunschweig",
    "Wohnfläche", "38,49 m²", "Zimmer", "1", "450,00 €",
    "/vermietung/wohnobjekte/details-wohnobjekt/24361/",
) + "</body></html>"


class SemmelhaackParserTests(unittest.TestCase):
    def test_parses_every_card_on_the_page(self):
        listings = semmelhaack_parser.parse_listings(PAGE_HTML)
        self.assertEqual(len(listings), 2)

    def test_extracts_rooms_area_price_and_address_for_a_real_potsdam_card(self):
        listings = semmelhaack_parser.parse_listings(PAGE_HTML)
        potsdam = next(item for item in listings if item["city"] == "Potsdam")

        self.assertEqual(potsdam["listing_key"], "63860")
        self.assertEqual(potsdam["title"], "4-Zimmer-DHH mit Terrasse")
        self.assertEqual(potsdam["street"], "Gärtner-Schmidt-Str. 10")
        self.assertEqual(potsdam["plz"], "14476")
        self.assertEqual(potsdam["rooms"], 4.0)
        self.assertEqual(potsdam["area_m2"], 94.94)
        self.assertEqual(potsdam["price_eur"], 1850.0)
        self.assertEqual(potsdam["detail_url"], "https://semmelhaack.de/vermietung/wohnobjekte/details-wohnobjekt/63860/")

    def test_accepts_both_zimmer_and_raeume_room_labels(self):
        listings = semmelhaack_parser.parse_listings(PAGE_HTML)
        braunschweig = next(item for item in listings if item["city"] == "Braunschweig")
        self.assertEqual(braunschweig["rooms"], 1.0)
        self.assertEqual(braunschweig["area_m2"], 38.49)

    def test_parse_decimal_handles_german_thousands_and_dashes(self):
        self.assertEqual(semmelhaack_parser.parse_decimal("1.850,00 €"), 1850.0)
        self.assertEqual(semmelhaack_parser.parse_decimal("94,94 m²"), 94.94)
        self.assertIsNone(semmelhaack_parser.parse_decimal("-"))
        self.assertIsNone(semmelhaack_parser.parse_decimal(""))

    def test_empty_page_returns_no_listings(self):
        self.assertEqual(semmelhaack_parser.parse_listings("<html><body>Nothing here</body></html>"), [])

    def test_parse_gallery_urls_extracts_every_photo_from_a_detail_page(self):
        html_page = """
        <html><body>
        <div class="swiper-container details-wohnobjekt-slider">
          <div class="swiper-slide"><img data-src="https://api.semmelhaack.de/bilder/objekte/1001.jpg"/></div>
          <div class="swiper-slide"><img data-src="https://api.semmelhaack.de/bilder/objekte/1002.jpg"/></div>
          <div class="swiper-slide"><img data-src="https://api.semmelhaack.de/bilder/objekte/1003.jpeg" alt="Grundriss"/></div>
        </div>
        <img data-src="https://semmelhaack.de/wp-content/uploads/2024/03/Semmelhaack.png" alt="Logo"/>
        </body></html>
        """

        urls = semmelhaack_parser.parse_gallery_urls(html_page)

        self.assertEqual(urls, [
            "https://api.semmelhaack.de/bilder/objekte/1001.jpg",
            "https://api.semmelhaack.de/bilder/objekte/1002.jpg",
            "https://api.semmelhaack.de/bilder/objekte/1003.jpeg",
        ])

    def test_parse_gallery_urls_drops_duplicates_from_the_thumbnail_strip(self):
        """Слайдер і смуга мініатюр посилаються на ті самі URL — рахуємо кожне фото один раз."""
        html_page = """
        <div><img data-src="https://api.semmelhaack.de/bilder/objekte/1001.jpg"/></div>
        <div><img data-src="https://api.semmelhaack.de/bilder/objekte/1001.jpg"/></div>
        """

        self.assertEqual(
            semmelhaack_parser.parse_gallery_urls(html_page),
            ["https://api.semmelhaack.de/bilder/objekte/1001.jpg"],
        )

    def test_parse_gallery_urls_on_a_page_without_a_gallery_yields_nothing(self):
        self.assertEqual(semmelhaack_parser.parse_gallery_urls("<html><body>Nothing here</body></html>"), [])

    def test_format_listing_message_includes_the_key_fields(self):
        text = semmelhaack_parser.format_listing_message({
            "title": "4-Zimmer-DHH", "address": "Gärtner-Schmidt-Str. 10, 14476 Potsdam",
            "rooms": 4.0, "area_m2": 94.94, "price_eur": 1850.0,
            "detail_url": "https://semmelhaack.de/vermietung/wohnobjekte/details-wohnobjekt/63860/",
        })
        self.assertIn("SEMMELHAACK", text)
        self.assertIn("4-Zimmer-DHH", text)
        self.assertIn("1850.0", text)
        self.assertIn("63860", text)


if __name__ == "__main__":
    unittest.main()
