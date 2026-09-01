import unittest

from user_jobs import kleinanzeigen_parser


def _card(adid, plz_city, title, href, tags, price, image_url="https://img.kleinanzeigen.de/api/v1/prod-ads/images/x/y?rule=$_59.AUTO",
          description="Beschreibung..."):
    """Картка в тій розмітці, яку сайт віддає з 2026-09-01.

    Службові класи стали tailwind-подібними, а «aditem»/«aditem-main--*»
    зникли зовсім; впізнаваними лишились data-атрибути статті, JSON-LD з
    обкладинкою й самі текстові вузли. Опис усередині JSON-LD навмисно містить
    і площу, і суму в євро: саме там розбір раніше міг переплутати опис із
    характеристиками картки.
    """
    image_block = f"""
        <script type="application/ld+json">
            {{"title":"{title}","description":"{description}","contentUrl":"{image_url}"}}
        </script>
    """ if image_url else ""
    return f"""
    <article class="flex justify-between p-medium" data-adid="{adid}" data-href="{href}">
        <div class="relative z-raised h-[225px] basis-[337px]">
            {image_block}
            <a class="inline-flex items-center" href="{href}"><div data-image-container><img src="{image_url}"></div></a>
        </div>
        <div class="z-raised flex grow flex-col overflow-hidden">
            <div class="mb-xsmall flex items-start justify-between text-bodyRegular">
                <div class="flex items-center gap-xxsmall text-onSurfaceNonessential">
                    <svg viewBox="0 0 24 24" data-title="locationOutline"><path d="M15.37 8.89Z"/></svg>
                    <span>{plz_city}</span>
                </div>
            </div>
            <div class="flex flex-col">
                <h3 class="mb-xsmall line-clamp-2 text-title3 font-strong">
                    <a class="inline-flex items-center" href="{href}">{title}</a>
                </h3>
                <p class="mb-xsmall text-bodyRegular text-onSurfaceSubdued">{description}</p>
                <p class="font-strong text-onSurfaceSubdued">{tags}</p>
                <div class="flex"><p class="my-xsmall text-title3 font-strong text-secondary">{price}</p></div>
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

    def test_the_ads_own_description_does_not_pass_for_the_cards_specs(self):
        """Текст оголошення теж повний метрів і євро — брати треба саме вузол
        із характеристиками, інакше картка приїде з чужими числами."""
        listings = kleinanzeigen_parser.parse_listings(
            "<html><body>" + _card(
                "1", "14467 Potsdam", "Schöne Wohnung", "/s-anzeige/schoene-wohnung/1",
                "55 m² &#183; 2 Zi.", "800 &#8364;",
                description="Ruhige 120 m² Maisonette, Nebenkosten 250 € kommen extra dazu",
            ) + "</body></html>"
        )

        self.assertEqual(listings[0]["area_m2"], 55.0)
        self.assertEqual(listings[0]["rooms"], 2.0)
        self.assertEqual(listings[0]["price_eur"], 800.0)

    def test_a_swap_listing_without_a_price_still_parses(self):
        """У оголошень про обмін ціни немає зовсім — картка все одно має
        розібратись, відсіює її вже монітор за словом «Tausch»."""
        card = _card(
            "2", "14482 Potsdam", "TAUSCHWOHNUNG Wohnungstausch in Potsdam",
            "/s-anzeige/tauschwohnung/2", "52 m² &#183; 2 Zi.", "",
        )
        listings = kleinanzeigen_parser.parse_listings("<html><body>" + card + "</body></html>")

        self.assertEqual(len(listings), 1)
        self.assertIsNone(listings[0]["price_eur"])
        self.assertEqual(listings[0]["area_m2"], 52.0)

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


class KleinanzeigenDetailPriceTests(unittest.TestCase):
    """Площадка тримає ціни готовими числами в атрибутах оголошення."""

    DETAIL_HTML = '{"Nebenkosten":"150","Wohnflaeche":"80","Warmmiete":"1540","Preis":"1500","ExactPreis":"1390"}'

    def test_both_rents_are_read_from_the_ad_attributes(self):
        prices = kleinanzeigen_parser.parse_detail_prices(self.DETAIL_HTML)

        self.assertEqual(prices["price_eur"], 1390.0)
        self.assertEqual(prices["nebenkosten_eur"], 150.0)
        self.assertEqual(prices["price_warm_eur"], 1540.0)

    def test_the_full_rent_is_computed_when_the_page_omits_it(self):
        html = '{"Nebenkosten":"200","ExactPreis":"800"}'

        prices = kleinanzeigen_parser.parse_detail_prices(html)

        self.assertEqual(prices["price_warm_eur"], 1000.0)

    def test_a_page_without_prices_reports_nothing(self):
        prices = kleinanzeigen_parser.parse_detail_prices('{"Wohnflaeche":"80"}')

        self.assertIsNone(prices["price_eur"])
        self.assertIsNone(prices["price_warm_eur"])
