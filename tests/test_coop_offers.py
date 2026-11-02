"""Кооперативи Берліна (етап 9, 02.11.2026): нові посилання на сторінках пропозицій."""

import json
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database import Base
from user_jobs import coop_offers, housing_area, housing_search_store, immoscout_store

BERLIN = housing_area.make(housing_area.find("Berlin")[0], 0)
PAGE = """
<a href="/">Start</a><a href="/kontakt">Kontakt</a>
<a href="/wohnungsangebote/">Wohnungsangebote</a>
<a href="/wohnungsangebote/2-zimmer-wohnung-mueggelheim/?session=abc">2-Zimmer-Wohnung in Müggelheim</a>
<a href="https://www.example.org/objekt.php?id=17&amp;utm_source=x">Objekt 17</a>
<a href="/files/plan.pdf">Grundriss</a>
"""


class OfferLinkTests(unittest.TestCase):
    def test_offer_like_links_without_floating_params(self):
        links = coop_offers.offer_links(PAGE, "https://coop.example/wohnungsangebote/")
        self.assertEqual(set(links), {
            "https://coop.example/wohnungsangebote/2-zimmer-wohnung-mueggelheim",
            "https://www.example.org/objekt.php?id=17",
        })
        self.assertEqual(links["https://www.example.org/objekt.php?id=17"], "Objekt 17")

    def test_the_list_has_unique_keys_and_urls(self):
        keys = [key for key, _label, _url in coop_offers.BERLIN_COOPS]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(coop_offers.count(), 19)


class _Db(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(engine)
        session = sessionmaker(bind=engine)
        for module in (coop_offers, housing_search_store, immoscout_store):
            patch = mock.patch.object(module, "DBSession", session)
            patch.start()
            self.addCleanup(patch.stop)


class CoopStateTests(_Db):
    def test_first_scan_is_a_baseline_and_a_new_link_counts_on_the_second_sighting(self):
        now = datetime.utcnow()
        self.assertEqual(coop_offers.update_links("x", {"https://a/1": "alt"}, now), [])
        self.assertEqual(coop_offers.update_links("x", {"https://a/1": "alt", "https://a/2": "neu"}, now), [])
        confirmed = coop_offers.update_links("x", {"https://a/1": "alt", "https://a/2": "neu"}, now + timedelta(minutes=30))
        self.assertEqual(confirmed, [("https://a/2", "neu")])
        # Більше не повторюється.
        self.assertEqual(coop_offers.update_links("x", {"https://a/2": "neu"}, now + timedelta(hours=1)), [])

    def test_a_link_seen_only_once_is_noise(self):
        now = datetime.utcnow()
        coop_offers.update_links("x", {"https://a/1": "alt"}, now)
        coop_offers.update_links("x", {"https://a/1": "alt", "https://a/session-123": "?"}, now)
        coop_offers.update_links("x", {"https://a/1": "alt"}, now)
        self.assertEqual(coop_offers.update_links("x", {"https://a/1": "alt", "https://a/session-123": "?"}, now), [])

    def test_recipients_are_active_searches_that_touch_berlin(self):
        berlin_scout = immoscout_store.create_filter(user_id=8, title="t", area=BERLIN)
        paused_scout = immoscout_store.create_filter(user_id=9, title="t", area=BERLIN)
        immoscout_store.set_filter_active(paused_scout, False)
        off_scout = immoscout_store.create_filter(user_id=10, title="t", area=BERLIN)
        potsdam_scout = immoscout_store.create_filter(user_id=7, title="t")
        housing_search_store.create(8, {"area": BERLIN}, [("immoscout", berlin_scout)])
        housing_search_store.create(9, {"area": BERLIN}, [("immoscout", paused_scout)])
        housing_search_store.create(10, {"area": BERLIN, "berlin_coops": False}, [("immoscout", off_scout)])
        housing_search_store.create(7, {}, [("immoscout", potsdam_scout)])
        self.assertEqual(coop_offers.recipients(), {8})

    def test_the_job_sends_each_new_offer_to_each_recipient(self):
        bot = mock.Mock()
        pages = {"html": PAGE}
        with mock.patch.object(coop_offers, "BERLIN_COOPS", [("x", "WG X eG", "https://coop.example/wohnungsangebote/")]), \
             mock.patch.object(coop_offers, "PAUSE_SECONDS", 0), \
             mock.patch.object(coop_offers, "_fetch", side_effect=lambda url: (pages["html"], url)), \
             mock.patch.object(coop_offers, "recipients", return_value={8}):
            coop_offers.check_job(SimpleNamespace(bot=bot))
            pages["html"] = PAGE + '<a href="/wohnungsangebote/3-zimmer-neu/">3-Zimmer neu</a>'
            coop_offers.check_job(SimpleNamespace(bot=bot))
            bot.send_message.assert_not_called()
            coop_offers.check_job(SimpleNamespace(bot=bot))
        bot.send_message.assert_called_once()
        text = bot.send_message.call_args.kwargs["text"]
        self.assertIn("WG X eG", text)
        self.assertIn("3-zimmer-neu", text)


if __name__ == "__main__":
    unittest.main()
