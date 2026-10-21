"""ImmoScout24 через API мобільного застосунку (сайт закритий капчею AWS WAF).

Фікстури — справжні відповіді API від 12.10.2026, урізані до кількох оголошень.
"""

import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database import Base
from user_jobs import (
    housing_search_store,
    housing_tier,
    immoscout_matching,
    immoscout_monitor,
    immoscout_parser,
    immoscout_store,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class ImmoscoutParserTests(unittest.TestCase):
    def setUp(self):
        self.listings = {item["listing_key"]: item for item in immoscout_parser.parse_listings(_fixture("immoscout_list_page.json"))}

    def test_list_values_are_kaltmiete_area_and_rooms(self):
        flat = self.listings["171646044"]

        self.assertEqual(flat["price_eur"], 1070.0)
        self.assertEqual(flat["area_m2"], 66.0)
        self.assertEqual(flat["rooms"], 2.0)
        self.assertEqual(flat["district"], "Südliche Innenstadt")
        self.assertEqual(flat["detail_url"], "https://www.immobilienscout24.de/expose/171646044")

    def test_decimal_area_and_thousands_in_price(self):
        self.assertEqual(self.listings["171650646"]["area_m2"], 27.1)
        self.assertEqual(self.listings["171651924"]["price_eur"], 1850.0)

    def test_incomplete_address_still_names_the_district(self):
        flat = self.listings["171651924"]

        self.assertEqual(flat["district"], "Babelsberg Süd")
        self.assertNotIn("unvollständige", flat["address"])

    def test_a_postcode_is_not_a_district(self):
        self.assertIsNone(immoscout_parser.district_from_address("Am Neuen Garten 5, 14469 Potsdam, 14469 Potsdam"))
        self.assertIsNone(immoscout_parser.district_from_address("14469 Potsdam"))

    def test_plus_exclusive_listing_remembers_until_when(self):
        self.assertTrue(self.listings["125780388"]["plus_until"].startswith("2026-10-12T19:45"))
        self.assertEqual(self.listings["171646044"]["plus_until"], "")

    def test_pictures_come_as_jpeg_of_a_real_size(self):
        cover = self.listings["171646044"]["cover_image_url"]

        self.assertIn("/format/jpg", cover)
        self.assertNotIn("webp", cover)
        self.assertNotIn("%WIDTH%", "".join(self.listings["171646044"]["gallery_urls"]))

    def test_listing_page_gives_warm_rent_district_and_gallery(self):
        details = immoscout_parser.parse_expose(_fixture("immoscout_expose.json"))

        self.assertEqual(details["price_eur"], 1069.39)
        self.assertEqual(details["price_warm_eur"], 1345.39)
        self.assertEqual(details["district"], "Südliche Innenstadt")
        self.assertEqual(len(details["gallery_urls"]), 3)
        self.assertTrue(all("/format/jpg" in url and "1200x900" in url for url in details["gallery_urls"]))

    @staticmethod
    def _expose(*attrs, total_rent=None):
        sections = [{"type": "ATTRIBUTE_LIST", "title": "Kosten",
                     "attributes": [{"label": label, "text": text} for label, text in attrs]}]
        if total_rent is not None:
            sections.append({"type": "COST_CHECK", "totalRent": total_rent})
        return {"sections": sections}

    def test_gesamtmiete_plus_side_costs_is_not_the_warm_rent(self):
        # 16.10.2026, expose 171820799: Warmmiete «keine Angabe», Gesamtmiete «zzgl. Heiz- und
        # Nebenkosten», totalRent — та сама холодна.
        details = immoscout_parser.parse_expose(self._expose(
            ("Kaltmiete (zzgl. Nebenkosten):", "2.903,56 €"), ("Warmmiete", "keine Angabe"),
            ("Gesamtmiete:", "2.903,56 € zzgl. Heiz- und Nebenkosten"), total_rent=2903.56))

        self.assertEqual(details["price_eur"], 2903.56)
        self.assertIsNone(details["price_warm_eur"])

    def test_warm_below_cold_is_rebuilt_from_side_costs(self):
        # 16.10.2026, expose 171728016: Kalt 1.440, NK 329 (Heizung drin), Warm 1.410.
        details = immoscout_parser.parse_expose(self._expose(
            ("Kaltmiete (zzgl. Nebenkosten):", "1.440 €"), ("Nebenkosten:", "329 €"),
            ("Heizkosten in Nebenkosten enthalten:", "Ja"), ("Gesamtmiete:", "1.410 €"), total_rent=1410))
        self.assertEqual(details["price_warm_eur"], 1769.0)

        no_side = immoscout_parser.parse_expose(self._expose(
            ("Kaltmiete (zzgl. Nebenkosten):", "1.440 €"), ("Gesamtmiete:", "1.410 €")))
        self.assertIsNone(no_side["price_warm_eur"])

    def test_all_inclusive_rent_stays_equal(self):
        details = immoscout_parser.parse_expose(self._expose(
            ("Kaltmiete (zzgl. Nebenkosten):", "1.728,55 €"), ("Gesamtmiete:", "1.728,55 €"), total_rent=1728.55))
        self.assertEqual((details["price_eur"], details["price_warm_eur"]), (1728.55, 1728.55))

    def test_message_mentions_both_rents_and_the_plus_window(self):
        listing = dict(self.listings["171646044"], price_warm_eur=1345.39)
        future = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()

        text = immoscout_parser.format_listing_message(dict(listing, plus_until=future), "uk")

        self.assertIn("ImmoScout24", text)
        self.assertIn("Kaltmiete EUR: 1070", text)
        self.assertIn("Warmmiete EUR: 1345.39", text)
        self.assertIn("ImmoScout24 Plus", text)
        self.assertNotIn("Plus", immoscout_parser.format_listing_message(dict(listing, plus_until="2020-01-01T00:00:00+02:00"), "uk"))


class ImmoscoutMatchingTests(unittest.TestCase):
    def test_a_broad_district_covers_its_parts(self):
        self.assertTrue(immoscout_matching.district_matches("Babelsberg Nord", ["Babelsberg"]))
        self.assertTrue(immoscout_matching.district_matches("Südliche Innenstadt", ["Innenstadt"]))

    def test_waldstadt_one_is_not_waldstadt_two(self):
        self.assertFalse(immoscout_matching.district_matches("Waldstadt II", ["Waldstadt I"]))
        self.assertTrue(immoscout_matching.district_matches("Waldstadt II", ["Waldstadt 2"]))
        self.assertTrue(immoscout_matching.district_matches("Waldstadt I", ["Waldstadt 1"]))

    def test_no_districts_chosen_means_all_and_unknown_district_never_matches_a_choice(self):
        self.assertTrue(immoscout_matching.district_matches(None, []))
        self.assertFalse(immoscout_matching.district_matches(None, ["Golm"]))

    def test_bounds(self):
        listing = {"district": "Golm", "rooms": 2.0, "area_m2": 60.0, "price_eur": 800.0, "price_warm_eur": 1000.0}

        self.assertTrue(immoscout_matching.matches_filter(listing, {"districts": "Golm", "min_rooms": 2, "max_price_warm_eur": 1000}))
        self.assertFalse(immoscout_matching.matches_filter(listing, {"max_price_warm_eur": 950}))
        self.assertFalse(immoscout_matching.matches_filter(listing, {"districts": "Drewitz"}))


class _DbTest(unittest.TestCase):
    def setUp(self):
        engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
        Base.metadata.create_all(engine)
        session = sessionmaker(bind=engine)
        for patch in (
            mock.patch.object(immoscout_store, "DBSession", session),
            mock.patch.object(housing_search_store, "DBSession", session),
            mock.patch("user_jobs.housing_tier.DBSession", session),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    @staticmethod
    def _listing(key, detail=True, **extra):
        return {
            "listing_key": key, "title": f"Wohnung {key}", "address": "Golm", "district": "Golm",
            "rooms": 2.0, "area_m2": 60.0, "price_eur": 800.0, "detail_url": f"https://x/{key}",
            "gallery_urls": [], "detail_fetched": detail, **extra,
        }


class ImmoscoutStoreTests(_DbTest):
    def test_a_new_filter_does_not_resend_flats_already_online(self):
        immoscout_store.upsert_listings([self._listing("1")])

        filter_id = immoscout_store.create_filter(user_id=5, title="t", districts="Golm", min_rooms=2)

        self.assertIn((filter_id, "1"), immoscout_store.delivered_pairs())
        self.assertEqual(immoscout_store.list_filters(user_id=5)[0]["districts"], "Golm")

    def test_the_list_does_not_overwrite_what_the_listing_page_said(self):
        immoscout_store.upsert_listings([self._listing("1", district="Babelsberg Nord", address="Str. 1, Babelsberg Nord")])
        immoscout_store.upsert_listings([self._listing("1", detail=False, district="Babelsberg", address="Babelsberg")])

        stored = immoscout_store.list_active_listings()[0]
        self.assertEqual(stored["district"], "Babelsberg Nord")
        self.assertTrue(stored["detail_fetched"])
        self.assertEqual(immoscout_store.keys_with_details(), {"1"})

    def test_the_rounded_list_price_keeps_the_exact_one_from_the_listing_page(self):
        # 16.10.2026: видача 2.904 € затирала 2.903,56 € з картки → «повна менша за холодну».
        immoscout_store.upsert_listings([self._listing("1", price_eur=2903.56, price_warm_eur=3300.0)])
        immoscout_store.upsert_listings([self._listing("1", detail=False, price_eur=2904.0)])
        stored = immoscout_store.list_active_listings()[0]
        self.assertEqual((stored["price_eur"], stored["price_warm_eur"]), (2903.56, 3300.0))

        immoscout_store.upsert_listings([self._listing("1", detail=False, price_eur=2800.0)])  # real change
        self.assertEqual(immoscout_store.list_active_listings()[0]["price_eur"], 2800.0)

        immoscout_store.upsert_listings([self._listing("1", price_eur=2800.0, price_warm_eur=None)])
        self.assertIsNone(immoscout_store.list_active_listings()[0]["price_warm_eur"])


class FakeBot:
    def __init__(self):
        self.messages = []

    def send_message(self, chat_id, text, **kwargs):
        self.messages.append((chat_id, text))

    def send_photo(self, chat_id, photo, caption=None, **kwargs):
        self.messages.append((chat_id, caption))

    def send_media_group(self, chat_id, media, **kwargs):
        self.messages.append((chat_id, media[0].caption))


class ImmoscoutMonitorTests(_DbTest):
    def _response(self, payload):
        return SimpleNamespace(json=lambda: payload, raise_for_status=lambda: None)

    def test_every_result_page_is_read(self):
        page = {"numberOfPages": 2, "resultListItems": []}
        with mock.patch("requests.post", return_value=self._response(page)) as post:
            immoscout_monitor._fetch_listings()

        self.assertEqual([call.kwargs["params"]["pagenumber"] for call in post.call_args_list], [1, 2])
        self.assertEqual(post.call_args.kwargs["headers"]["User-Agent"], immoscout_parser.USER_AGENT)

    def test_listing_pages_are_read_once_and_only_a_limited_number_per_scan(self):
        listings = [self._listing(str(i), detail=False) for i in range(5)]
        expose = _fixture("immoscout_expose.json")
        with mock.patch.object(immoscout_monitor, "DETAILS_PER_SCAN", 3), \
             mock.patch.object(immoscout_monitor, "DETAIL_PAUSE_SECONDS", 0), \
             mock.patch("requests.get", return_value=self._response(expose)) as get:
            filled = immoscout_monitor._add_details(listings)

        self.assertEqual(filled, 3)
        self.assertEqual(get.call_count, 3)
        self.assertEqual(listings[0]["price_warm_eur"], 1345.39)
        self.assertFalse(listings[4].get("detail_fetched"))

    def test_first_scan_adds_immoscout_to_existing_filters_without_flooding(self):
        housing_search_store.create(7, {"districts": ["Golm"], "min_rooms": 2.0}, [("semmelhaack", 3)], [])
        bot = FakeBot()
        context = SimpleNamespace(bot=bot)
        with mock.patch.object(immoscout_monitor, "_fetch_listings", return_value=[self._listing("old")]), \
             mock.patch.object(immoscout_monitor, "_add_details", return_value=0), \
             mock.patch("user_jobs.housing_tier.premium_user_ids", return_value={7}):
            immoscout_monitor.check_job(context)

        search = housing_search_store.list_for_user(7)[0]
        scout = [filter_id for source, filter_id in search["members"] if source == "immoscout"]
        self.assertEqual(len(scout), 1)
        self.assertEqual(immoscout_store.list_filters(user_id=7)[0]["districts"], "Golm")
        self.assertEqual(bot.messages, [])  # квартира, що вже була онлайн, не розсилається

        # Нова квартира наступного обходу — приходить; повторного додавання немає.
        with mock.patch.object(immoscout_monitor, "_fetch_listings", return_value=[self._listing("old"), self._listing("new")]), \
             mock.patch.object(immoscout_monitor, "_add_details", return_value=0), \
             mock.patch("user_jobs.housing_tier.premium_user_ids", return_value={7}):
            immoscout_monitor.check_job(context)

        self.assertEqual(len(bot.messages), 1)
        self.assertIn("Wohnung new", bot.messages[0][1])
        self.assertEqual(len(immoscout_store.list_filters(user_id=7)), 1)

    def test_a_karl_marx_only_filter_gets_no_immoscout(self):
        housing_search_store.create(7, {"min_rooms": 2.0}, [("karlmarx", 2)], [])
        with mock.patch.object(immoscout_monitor, "_fetch_listings", return_value=[self._listing("old")]),              mock.patch.object(immoscout_monitor, "_add_details", return_value=0),              mock.patch("user_jobs.housing_tier.premium_user_ids", return_value={7}):
            immoscout_monitor.check_job(SimpleNamespace(bot=FakeBot()))

        self.assertEqual(housing_search_store.list_for_user(7)[0]["members"], [("karlmarx", 2)])
        self.assertEqual(immoscout_store.list_filters(user_id=7), [])

    def test_a_flat_swap_is_not_sent(self):
        housing_search_store.create(7, {"min_rooms": 2.0}, [("semmelhaack", 3)], [])
        context = SimpleNamespace(bot=FakeBot())
        with mock.patch.object(immoscout_monitor, "_fetch_listings", return_value=[]),              mock.patch.object(immoscout_monitor, "_add_details", return_value=0),              mock.patch("user_jobs.housing_tier.premium_user_ids", return_value={7}):
            immoscout_monitor.check_job(context)
        fresh = [self._listing("swap", title="Tauschwohnung: 2-Zimmer-Wohnung in Potsdams Innenstadt"),
                 self._listing("flat")]
        with mock.patch.object(immoscout_monitor, "_fetch_listings", return_value=fresh),              mock.patch.object(immoscout_monitor, "_add_details", return_value=0),              mock.patch("user_jobs.housing_tier.premium_user_ids", return_value={7}):
            immoscout_monitor.check_job(context)

        self.assertEqual(len(context.bot.messages), 1)
        self.assertIn("Wohnung flat", context.bot.messages[0][1])

    def test_remove_member(self):
        search_id = housing_search_store.create(7, {}, [("karlmarx", 2), ("immoscout", 5)], [])

        housing_search_store.remove_member(search_id, "immoscout", 5)

        self.assertEqual(housing_search_store.list_for_user(7)[0]["members"], [("karlmarx", 2)])

    def test_a_person_who_blocked_the_bot_does_not_stop_everyone_after_them(self):
        from telegram.error import Unauthorized

        class BlockedByFirst(FakeBot):
            def send_message(self, chat_id, text, **kwargs):
                if chat_id == 7:
                    raise Unauthorized("Forbidden: bot was blocked by the user")
                super().send_message(chat_id, text, **kwargs)

        blocked = immoscout_store.create_filter(user_id=7, title="t")
        immoscout_store.create_filter(user_id=8, title="t")
        bot = BlockedByFirst()
        with mock.patch.object(immoscout_monitor, "_fetch_listings", return_value=[self._listing("x")]),              mock.patch.object(immoscout_monitor, "_add_details", return_value=0),              mock.patch.object(immoscout_monitor, "_backfill_existing_searches"),              mock.patch.object(immoscout_monitor, "_send_listing", return_value=False),              mock.patch("user_jobs.housing_tier.premium_user_ids", return_value={7, 8}):
            immoscout_monitor.check_job(SimpleNamespace(bot=bot))

        self.assertEqual([chat for chat, _ in bot.messages], [8])
        # Недосяжній людині не пробуємо слати те саме знову й знову.
        self.assertIn((blocked, "x"), immoscout_store.delivered_pairs())

    def test_only_a_gone_recipient_counts_as_gone(self):
        from telegram.error import BadRequest, NetworkError, Unauthorized

        self.assertTrue(housing_tier.recipient_gone(Unauthorized("Forbidden: bot was blocked by the user")))
        self.assertTrue(housing_tier.recipient_gone(BadRequest('Failed to send message #1 with the error message "user_is_blocked"')))
        self.assertTrue(housing_tier.recipient_gone(BadRequest("Chat not found")))
        self.assertFalse(housing_tier.recipient_gone(BadRequest("Message is too long")))
        self.assertFalse(housing_tier.recipient_gone(NetworkError("timed out")))

    def test_a_listing_whose_page_was_not_read_waits(self):
        immoscout_store.create_filter(user_id=7, title="t")
        bot = FakeBot()
        with mock.patch.object(immoscout_monitor, "_fetch_listings", return_value=[self._listing("x", detail=False)]), \
             mock.patch.object(immoscout_monitor, "_add_details", return_value=0), \
             mock.patch.object(immoscout_monitor, "_backfill_existing_searches"), \
             mock.patch("user_jobs.housing_tier.premium_user_ids", return_value={7}):
            immoscout_monitor.check_job(SimpleNamespace(bot=bot))

        self.assertEqual(bot.messages, [])


if __name__ == "__main__":
    unittest.main()
