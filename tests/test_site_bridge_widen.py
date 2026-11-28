"""Мост сайта (fix8, 28.11.2026): housing.search_report и housing.search_relax, то есть «Розширити фільтр» без сообщения Telegram.

Те же поддельные хранилища, что у test_site_bridge_housing. Проверяется: отчёт и предложения совпадают с housing_relax (то, что получил бы человек в боте),
чужой user_id не видит и не применяет, плохие коды отклоняются, применение меняет поиск как правка (цены/комнаты/районы), отчёт читает, но не пишет."""

import json
import unittest
from datetime import datetime
from unittest import mock

import i18n
from site_bridge import Refuse, housing, housing_widen
from tests import test_site_bridge_housing as bh
from user_handlers import housing_monitor as hm, housing_relax

USER, OTHER = bh.USER, bh.OTHER


def call(op, user_id, params=None, lang="uk"):
    return getattr(housing_widen, op)(user_id, lang, params or {})


class WidenTests(bh.Base):
    def setUp(self):
        super().setUp()
        patch = mock.patch.object(housing_relax, "DBSession", hm.DBSession)   # Market читает базу через свой импорт DBSession
        patch.start()
        self.addCleanup(patch.stop)

    def flat(self, key, price, rooms=2, area=55, source="immoscout"):
        session = hm.DBSession()
        try:
            model = hm._LISTING_MODELS[source]
            fields = dict(listing_key=key, title="flat " + key, address="Hauptstr. 1", district="Mitte", rooms=rooms, area_m2=area, price_eur=price,
                          price_warm_eur=price + 150, detail_url="https://example.org/" + key, first_seen_at=datetime.utcnow(),
                          last_seen_at=datetime.utcnow(), is_active=True)
            session.add(model(**{k: v for k, v in fields.items() if hasattr(model, k)}))
            session.commit()
        finally:
            session.close()

    def search(self, **c):
        crit = {"max_price_eur": 900, "min_rooms": 2}
        crit.update(c)
        return housing.search_save(USER, "uk", {"place": {"quick": "potsdam", "radius": 0}, "sources": ["immoscout"], "c": crit})["search"]["id"]

    def test_report_matches_the_bot_numbers_and_offers(self):
        sid = self.search()
        self.flat("a1", 800)
        self.flat("a2", 950)          # +100 EUR
        self.flat("a3", 980)          # +100 EUR
        self.flat("a4", 1000, rooms=2)
        self.flat("a5", 850, rooms=1)  # «на одну комнату меньше»
        self.flat("a6", 880, rooms=1)
        r = call("search_report", USER, {"id": sid})
        stored, base, found = housing_relax.report_for_search(USER, sid)
        self.assertTrue(r["available"])
        self.assertEqual((r["total"], r["days"]), (base, housing_relax.RELAX_DAYS))
        self.assertEqual([(o["code"], o["count"], o["gain"]) for o in r["offers"]], [(c, n, n - base) for c, n in found])
        self.assertTrue(r["offers"], "ожидались предложения")
        self.assertEqual(r["offers"][0]["label"], housing._plain(housing_relax.offer_label(r["offers"][0]["code"], stored["criteria"], "uk")))
        self.assertIn("✅", r["offers"][0]["btn"])
        self.assertNotIn("<", r["summary"] + r["intro"] + r["tap"])
        for lang in ("ru", "de"):
            rl = call("search_report", USER, {"id": sid}, lang)
            self.assertNotEqual(rl["summary"], r["summary"])
            self.assertEqual([o["code"] for o in rl["offers"]], [o["code"] for o in r["offers"]])

    def test_report_does_not_write(self):
        sid = self.search()
        self.flat("w1", 800)
        before = json.dumps(housing.searches(USER, "uk", {}), sort_keys=True)
        with mock.patch.object(hm, "_record_filter_event") as rec:
            call("search_report", USER, {"id": sid})
        rec.assert_not_called()
        self.assertEqual(json.dumps(housing.searches(USER, "uk", {}), sort_keys=True), before)

    def test_thin_note_when_few_and_no_offers(self):
        sid = self.search(min_rooms=None)
        r = call("search_report", USER, {"id": sid})
        self.assertEqual((r["total"], r["offers"]), (0, []))
        self.assertTrue(r["thin"] and r["thin_note"])
        self.flat("t1", 500)
        self.flat("t2", 500)
        self.flat("t3", 500)
        self.flat("t4", 500)
        self.assertFalse(call("search_report", USER, {"id": sid})["thin"])

    def test_relax_price_applies_like_an_edit_and_the_next_report_grows(self):
        sid = self.search()
        for i, price in enumerate((800, 950, 960, 990)):
            self.flat("p%d" % i, price)
        r = call("search_report", USER, {"id": sid})
        offer = next(o for o in r["offers"] if o["code"] == "price")
        done = call("search_relax", USER, {"id": sid, "code": "price"})
        self.assertEqual(done["applied"], "price")
        self.assertEqual(done["search"]["c"]["max_price_eur"], 1000)
        self.assertEqual(done["search"]["id"], sid)
        again = call("search_report", USER, {"id": sid})
        self.assertEqual(again["total"], offer["count"])
        session = hm.DBSession()
        try:
            self.assertEqual(session.query(hm.HousingFilterEvent).filter_by(user_id=USER, edited=True).count(), 1)  # создание не правка; поступка записана как правка
        finally:
            session.close()

    def test_relax_rooms(self):
        sid = self.search(min_rooms=3)
        for i in range(3):
            self.flat("r%d" % i, 700, rooms=2)
        self.flat("r9", 700, rooms=3)
        call("search_relax", USER, {"id": sid, "code": "rooms"})
        self.assertEqual(housing.searches(USER, "uk", {})["items"][0]["c"]["min_rooms"], 2)

    def test_other_user_cannot_read_or_apply(self):
        sid = self.search()
        bh.PREMIUM.add(OTHER)
        with mock.patch.object(hm, "ALLOWED_USER_IDS", {USER, OTHER}):
            for op, params in (("search_report", {"id": sid}), ("search_relax", {"id": sid, "code": "price"})):
                with self.assertRaises(Refuse) as cm:
                    call(op, OTHER, params)
                self.assertEqual(cm.exception.code, "not_found")
        self.assertEqual(housing.searches(USER, "uk", {})["items"][0]["c"]["max_price_eur"], 900)

    def test_bad_params(self):
        sid = self.search()
        for params in ({}, {"id": "x"}, {"id": True}, {"id": 0}, {"id": -3}, {"id": 2 ** 40}):
            with self.assertRaises(Refuse):
                call("search_report", USER, params)
        for code in (None, 5, "", "nope", "PRICE", ["price"], "price; drop", "maxrooms"):   # maxrooms: у поиска нет верхней границы
            with self.assertRaises(Refuse) as cm:
                call("search_relax", USER, {"id": sid, "code": code})
            self.assertIn(cm.exception.code, ("bad_request", "not_offered"))
        self.assertEqual(housing.searches(USER, "uk", {})["items"][0]["c"]["max_price_eur"], 900)

    def test_not_allowed_user(self):
        with mock.patch.object(hm, "ALLOWED_USER_IDS", set()), mock.patch.object(hm, "OPEN_TO_ALL", False), mock.patch.object(hm, "_has_grandfathered_filter", return_value=False):
            for op, params in (("search_report", {"id": 1}), ("search_relax", {"id": 1, "code": "price"})):
                with self.assertRaises(Refuse) as cm:
                    call(op, 4242, params)
                self.assertEqual(cm.exception.code, "no_access")

    def test_unavailable_when_counting_fails(self):
        sid = self.search()
        with mock.patch.object(housing_relax, "report_for_search", return_value=None):
            r = call("search_report", USER, {"id": sid})
        self.assertFalse(r["available"])
        self.assertTrue(r["note"])

    def test_operations_are_registered(self):
        import site_bridge
        self.assertTrue({"housing.search_report", "housing.search_relax"} <= set(site_bridge.registered()))

    def test_texts_exist_in_three_languages(self):
        for key in ("hrelax.na", "hrelax.tap", "hrelax.thin", "hrelax.not_offered", "hrelax.done"):
            vals = {lang: i18n.t(key, lang) for lang in ("uk", "ru", "de")}
            self.assertEqual(len(set(vals.values())), 3, key)
            self.assertFalse(any(v.startswith("[") for v in vals.values()), key)


if __name__ == "__main__":
    unittest.main()
