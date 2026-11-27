"""Мост сайта «Потсдам», область housing (пакет H1, 28.11.2026): поиски, заявки, лента, статистика, уведомления.

Те же поддельные хранилища и приёмник Immowelt, что у карточки фильтра (tests.test_housing_card). Проверяем: операции зовут функции бота, чужой user_id
ничего не видит и не меняет, лимиты бесплатного как в боте, плохие значения отклоняются, лёгкие операции не ходят к приёмнику Immowelt."""

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer
from unittest import mock

import database
import site_bridge
from site_bridge import Refuse, housing
from tests import test_housing_card as card_tests
from user_handlers import housing_card, housing_found, housing_letter, housing_monitor as hm, housing_tracker, housing_web
from user_jobs import (
    housing_access_store, housing_dateparse, housing_letter_store, housing_links, housing_pay_store, housing_stats_store, housing_tier,
    user_settings_store,
)

USER, OTHER, ADMIN = card_tests.USER, 777, card_tests.ADMIN
PREMIUM = {USER}


def call(op, user_id, params=None, lang="uk"):
    return getattr(housing, "apply_" if op == "apply" else op)(user_id, lang, params or {})


class Base(card_tests.HousingCardTest):
    def setUp(self):
        super().setUp()
        session = hm.DBSession
        patches = [mock.patch.object(database, "DBSession", session)]
        for module in (housing_tracker, housing_letter, housing_web, housing_letter_store, housing_access_store, housing_links, housing_pay_store, housing_stats_store):
            if hasattr(module, "DBSession"):
                patches.append(mock.patch.object(module, "DBSession", session))
        patches += [
            mock.patch.object(housing_tier, "is_premium", side_effect=lambda uid: int(uid) in PREMIUM),
            mock.patch.object(hm, "OPEN_TO_ALL", True),
            mock.patch.object(housing_links, "public_url", return_value="https://pots.example"),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        # свой набор подписчиков на каждый тест
        PREMIUM.clear()
        PREMIUM.add(USER)
        self.addCleanup(lambda: (PREMIUM.clear(), PREMIUM.add(USER)))

    def new_search(self, user=USER, **extra):
        params = {"place": {"quick": "potsdam", "radius": 0}, "sources": ["immoscout", "kleinanzeigen"], "c": {"max_price_eur": 900, "min_rooms": 2}}
        params.update(extra)
        return call("search_save", user, params)

    def add_flat(self, key="is1", source="immoscout", user=USER, when=None, delayed=False):
        session = hm.DBSession()
        try:
            if not session.query(hm._LISTING_MODELS[source]).filter_by(listing_key=key).first():
                model = hm._LISTING_MODELS[source]
                fields = dict(
                    listing_key=key, title="2 Zimmer " + key, address="Hauptstr. 1", district="Mitte", rooms=2, area_m2=55, price_eur=800, price_warm_eur=950,
                    detail_url="https://example.org/" + key, cover_image_url="https://img.example/" + key + ".jpg",
                    first_seen_at=datetime.utcnow(), last_seen_at=datetime.utcnow(), is_active=True)
                session.add(model(**{k: v for k, v in fields.items() if hasattr(model, k)}))
            session.add(database.HousingSendLog(user_id=user, source=source, listing_key=key, delayed=delayed, sent_at=when or datetime.utcnow(),
                                                snapshot=json.dumps({"rooms": 2, "area_m2": 55, "price_eur": 800, "price_warm_eur": 950, "district": "Mitte"})))
            session.commit()
        finally:
            session.close()
        return housing_letter_store.ref_for(source, key)


# Унаследованные тесты карточки уже идут в HousingCardTest; здесь только свои.
for _name in [name for name in vars(card_tests.HousingCardTest) if name.startswith("test_")]:
    setattr(Base, _name, None)


class SearchTests(Base):
    def test_create_edit_pause_resume_delete(self):
        made = self.new_search()
        self.assertTrue(made["created"] and made["activated"])
        s = made["search"]
        self.assertTrue(s["on"])
        self.assertEqual(s["c"]["max_price_eur"], 900)
        self.assertEqual(sorted(s["sources"]), ["immoscout", "kleinanzeigen"])
        sid = s["id"]
        # изменить: Берлин +10, WBS, площадки, комнаты
        saved = call("search_save", USER, {"id": sid, "place": {"quick": "berlin", "radius": 10}, "wbs": "none", "sources": ["immoscout"],
                                           "c": {"max_price_eur": "1 000,5", "min_rooms": 3, "max_rooms": None}, "berlin_coops": True})
        s = saved["search"]
        self.assertFalse(saved["created"])
        self.assertEqual((s["area"], s["radius"], s["wbs"], s["sources"]), ("Berlin + 10 км", 10, "none", ["immoscout"]))
        self.assertEqual((s["c"]["max_price_eur"], s["c"]["min_rooms"], s["c"]["max_rooms"]), (1000.5, 3, None))
        # то же место, другой радиус («keep»), варианты формы считаются для этого же поиска
        kept = call("search_save", USER, {"id": sid, "place": {"keep": True, "radius": 20}})["search"]
        self.assertEqual((kept["area"], kept["radius"], kept["sources"]), ("Berlin + 20 км", 20, ["immoscout"]))
        self.assertEqual(call("search_options", USER, {"id": sid, "place": {"keep": True, "radius": 0}})["kind"], "berlin")
        # пауза и возврат
        self.assertFalse(call("search_pause", USER, {"id": sid, "paused": True})["search"]["on"])
        self.assertTrue(call("search_pause", USER, {"id": sid, "paused": False})["search"]["on"])
        # список
        items = call("searches", USER)["items"]
        self.assertEqual([i["id"] for i in items], [sid])
        # правка записана как зависимая «правка фильтра» (самопроверка/сводка)
        session = hm.DBSession()
        try:
            self.assertEqual(session.query(hm.HousingFilterEvent).filter_by(user_id=USER, edited=True).count(), 2)
        finally:
            session.close()
        self.assertTrue(call("search_delete", USER, {"id": sid})["deleted"])
        self.assertEqual(call("searches", USER)["items"], [])

    def test_districts_and_bezirke(self):
        sid = self.new_search(sources=["immoscout", "propotsdam"])["search"]["id"]
        saved = call("search_save", USER, {"id": sid, "districts": ["Babelsberg", "Bornim"]})["search"]
        self.assertEqual(saved["districts"], ["Babelsberg", "Bornim"])
        self.assertEqual(call("search_save", USER, {"id": sid, "districts": []})["search"]["districts"], [])
        # весь список = «все районы» (пустой список, как в боте)
        every = list(hm._canonical_districts(["immoscout", "propotsdam"]))
        self.assertEqual(call("search_save", USER, {"id": sid, "districts": every})["search"]["districts"], [])
        with self.assertRaises(Refuse):
            call("search_save", USER, {"id": sid, "districts": ["Atlantis"]})
        # округа Берлина только у «Berlin, лишь город»
        with self.assertRaises(Refuse):
            call("search_save", USER, {"id": sid, "bezirke": ["Mitte"]})
        call("search_save", USER, {"id": sid, "place": {"quick": "berlin", "radius": 0}})
        got = call("search_save", USER, {"id": sid, "bezirke": ["Mitte", "Pankow"]})["search"]
        self.assertEqual(got["bezirke"], ["Mitte", "Pankow"])
        # тот же Берлин при повторном сохранении округа не сбрасывает
        again = call("search_save", USER, {"id": sid, "place": {"quick": "berlin", "radius": 0}, "wbs": "100"})["search"]
        self.assertEqual(again["bezirke"], ["Mitte", "Pankow"])
        self.assertEqual(call("search_save", USER, {"id": sid, "bezirke": []})["search"]["bezirke"], [])
        with self.assertRaises(Refuse):
            call("search_save", USER, {"id": sid, "bezirke": ["Atlantis"]})

    def test_options_follow_the_place(self):
        potsdam = call("search_options", USER, {})
        self.assertEqual(potsdam["kind"], "potsdam")
        self.assertIn("Babelsberg", potsdam["districts_welt"])
        self.assertTrue(potsdam["coops"])
        self.assertEqual(potsdam["jobcenter"]["3"]["cold"], hm.jobcenter_limits(3)[1])
        self.assertEqual(potsdam["jobcenter"]["3"]["warm"], hm.jobcenter_limits(3)[2])
        berlin = call("search_options", USER, {"place": {"quick": "berlin", "radius": 0}})
        self.assertEqual(berlin["kind"], "berlin")
        self.assertIn("Mitte", berlin["bezirke"])
        self.assertIn("inberlin", [s["key"] for s in berlin["sources"]])
        self.assertIsNotNone(berlin["berlin_coops"])
        self.assertNotIn("inberlin", [s["key"] for s in potsdam["sources"]])
        self.assertEqual([w["v"] for w in potsdam["wbs"]], ["", "none", "100", "140", "160", "180", "220"])
        found = call("place_find", USER, {"text": "Werder"})
        self.assertTrue(found["options"])
        far = call("search_options", USER, {"place": {"text": "Werder", "pick": 0, "radius": 10}})
        self.assertEqual(far["kind"], "other")
        self.assertEqual(far["bezirke"], [])

    def test_household_is_saved_like_the_bot(self):
        call("search_save", USER, {"place": {"quick": "potsdam", "radius": 0}, "sources": ["immoscout"], "persons": 3,
                                   "c": {"max_price_eur": hm.jobcenter_limits(3)[1]}})
        self.assertEqual(call("search_options", USER, {})["household"], 3)
        self.assertEqual(self._search()["criteria"]["max_price_eur"], hm.jobcenter_limits(3)[1])

    def test_invalid_values_are_refused_and_nothing_stays_half_made(self):
        sid = self.new_search()["search"]["id"]
        bad = [
            {"c": {"max_price_eur": -5}}, {"c": {"max_price_eur": "abc"}}, {"c": {"min_rooms": 21}}, {"c": {"max_area_m2": 1001}},
            {"c": {"max_price_eur": True}}, {"c": {"min_rooms": 5, "max_rooms": 2}}, {"c": {"min_area_m2": 90, "max_area_m2": 40}},
            {"sources": ["nonsense"]}, {"sources": []}, {"sources": "immoscout"}, {"wbs": "999"}, {"coops": ["x"], "sources": ["immoscout"]},
            {"place": {"text": "Hamburg", "pick": 0, "radius": 0}}, {"place": {"quick": "potsdam", "radius": 7}}, {"place": {"quick": "mars", "radius": 0}},
            {"place": {"text": "Werder", "pick": 99, "radius": 0}}, {"place": "Berlin"}, {"c": "x"}, {"ptypes": ["castle"], "sources": ["immowelt"]},
        ]
        for params in bad:
            before = self._search()["criteria"]
            with self.assertRaises(Refuse, msg=str(params)):
                call("search_save", USER, dict(params, id=sid))
            self.assertEqual(before["max_price_eur"], self._search()["criteria"]["max_price_eur"], msg=str(params))
        for bad_id in (0, -1, "x", True, None, 10 ** 12):
            with self.assertRaises(Refuse):
                call("search_pause", USER, {"id": bad_id, "paused": True})
        with self.assertRaises(Refuse):
            call("search_pause", USER, {"id": sid, "paused": "yes"})
        # новый поиск с плохим значением не остаётся
        before = len(self._all())
        with self.assertRaises(Refuse):
            self.new_search(c={"max_price_eur": "-1"}, id=None)
        self.assertEqual(len(self._all()), before)

    def _all(self):
        return hm.user_searches(USER)

    def test_refusal_text_comes_from_the_bot_in_the_chosen_language(self):
        try:
            call("search_save", USER, {"place": {"text": "Hamburg", "pick": 0, "radius": 0}}, lang="de")
        except Refuse as refusal:
            self.assertEqual(refusal.code, "place_not_found")
            self.assertIn("Hamburg", refusal.message)
        sid = self.new_search()["search"]["id"]
        with self.assertRaises(Refuse) as ctx:
            call("search_save", USER, {"id": sid, "c": {"min_rooms": 5, "max_rooms": 1}}, lang="ru")
        self.assertEqual(ctx.exception.message, housing_web._t("min_over_max", "ru"))

    def test_free_user_one_search_and_one_active(self):
        PREMIUM.discard(USER)
        first = self.new_search()
        self.assertTrue(first["activated"])
        with self.assertRaises(Refuse) as ctx:
            self.new_search()
        self.assertEqual(ctx.exception.code, "second_paid")
        self.assertFalse(call("searches", USER)["can_add"])
        self.assertEqual(call("searches", USER)["paid_note"], housing_web._t("second_paid", "uk"))
        self.assertEqual(len(self._all()), 1)
        # два поиска, оставшиеся с подписки: включить второй при включённом первом нельзя
        PREMIUM.add(USER)
        second = self.new_search()["search"]["id"]
        PREMIUM.discard(USER)
        with self.assertRaises(Refuse) as ctx:
            call("search_pause", USER, {"id": second, "paused": False})
        self.assertEqual(ctx.exception.code, "only_one_active")
        call("search_pause", USER, {"id": first["id"], "paused": True})
        self.assertTrue(call("search_pause", USER, {"id": second, "paused": False})["search"]["on"])

    def test_new_search_for_free_user_is_paused_when_another_runs(self):
        PREMIUM.add(USER)
        self.new_search()
        PREMIUM.discard(USER)
        # у бесплатного уже есть поиск: второй создать нельзя, а из подписки включённые лишние на паузу не уходят сами
        with self.assertRaises(Refuse):
            self.new_search()

    def test_monitoring_off_and_on_respects_the_free_limit(self):
        a = self.new_search()["search"]["id"]
        b = self.new_search()["search"]["id"]
        self.assertTrue(call("status", USER)["monitoring"])
        self.assertFalse(call("monitoring_set", USER, {"on": False})["monitoring"])
        self.assertTrue(all(not s["on"] for s in call("searches", USER)["items"]))
        PREMIUM.discard(USER)
        again = call("monitoring_set", USER, {"on": True})
        self.assertTrue(again["monitoring"])
        self.assertEqual(sum(1 for s in call("searches", USER)["items"] if s["on"]), 1)       # бесплатному один, как в боте
        PREMIUM.add(USER)
        call("monitoring_set", USER, {"on": True})
        self.assertEqual(sum(1 for s in call("searches", USER)["items"] if s["on"]), 2)
        with self.assertRaises(Refuse):
            call("monitoring_set", USER, {"on": 1})
        self.assertIsNotNone((a, b))

    def test_found_pauses_and_returns(self):
        self.new_search()
        with mock.patch.object(housing_found, "congrats") as congrats, mock.patch.object(site_bridge, "bot", return_value=object()):
            found = call("found_set", USER, {"found": True})
        self.assertTrue(found["found"] and not found["monitoring"])
        congrats.assert_called_once()
        back = call("found_set", USER, {"found": False})
        self.assertFalse(back["found"])
        self.assertTrue(back["monitoring"])

    # --- чужое ---
    def test_someone_else_cannot_see_or_change_my_search(self):
        sid = self.new_search()["search"]["id"]
        self.assertEqual(call("searches", OTHER)["items"], [])
        for op, params in (("search_get", {"id": sid}), ("search_pause", {"id": sid, "paused": True}), ("search_delete", {"id": sid}),
                           ("search_save", {"id": sid, "c": {"max_price_eur": 1}})):
            with self.assertRaises(Refuse, msg=op) as ctx:
                call(op, OTHER, params)
            self.assertEqual(ctx.exception.code, "not_found")
        # user_id в params ничего не меняет: берётся только первый аргумент
        with self.assertRaises(Refuse):
            call("search_delete", OTHER, {"id": sid, "user_id": USER, "uid": USER})
        self.assertEqual(self._search()["criteria"]["max_price_eur"], 900)
        self.assertEqual(len(self._all()), 1)
        # админ без подмены: через мост видит только свои
        self.assertEqual(call("searches", ADMIN)["items"], [])

    def test_no_access_when_the_bot_closes_monitoring(self):
        with mock.patch.object(hm, "OPEN_TO_ALL", False), mock.patch.object(hm, "_has_grandfathered_filter", return_value=False):
            for op in ("searches", "feed", "matches", "apps", "stats", "notify_get"):
                with self.assertRaises(Refuse, msg=op) as ctx:
                    call(op, 123456)
                self.assertEqual(ctx.exception.code, "no_access")
            self.assertFalse(call("status", 123456)["allowed"])

    def test_light_operations_do_not_ask_the_immowelt_receiver(self):
        self.new_search(sources=["immowelt", "immoscout"])
        self.add_flat()
        hm._all_immowelt_filters.reset_mock()
        hm._request.reset_mock()
        call("status", USER)
        call("feed", USER)
        call("listing", USER, {"ref": housing_letter_store.ref_for("immoscout", "is1")})
        self.assertEqual(hm._all_immowelt_filters.call_count, 0)
        self.assertEqual(hm._request.call_count, 0)


class FeedAndAppsTests(Base):
    def test_feed_pages_and_fields(self):
        base = datetime.utcnow() - timedelta(hours=5)
        for i in range(25):
            self.add_flat("is%d" % i, when=base + timedelta(minutes=i))
        first = call("feed", USER, {"n": 20})
        self.assertEqual(len(first["items"]), 20)
        self.assertTrue(first["next"])
        top = first["items"][0]
        self.assertEqual((top["t"], top["srcl"], top["p"], top["pw"], top["r"], top["m2"]), ("2 Zimmer is24", "ImmoScout24", 800, 950, 2, 55))
        self.assertTrue(top["ref"] and top["url"] == "https://example.org/is24" and top["img"].startswith("https://img.example/"))
        rest = call("feed", USER, {"n": 20, "before": first["next"]})
        self.assertEqual(len(rest["items"]), 5)
        self.assertIsNone(rest["next"])
        self.assertEqual(call("feed", OTHER)["items"], [])                  # чужая лента пуста
        self.assertEqual(len(call("feed", USER, {"n": 5000})["items"]), 25)  # не больше FEED_MAX, но всё влезло
        with self.assertRaises(Refuse):
            call("feed", USER, {"n": "x"})

    def test_listing_card_only_for_what_the_person_received(self):
        ref = self.add_flat("is7")
        got = call("listing", USER, {"ref": ref})
        self.assertEqual((got["t"], got["srcl"], got["url"]), ("2 Zimmer is7", "ImmoScout24", "https://example.org/is7"))
        self.assertIsNone(got["ap"])
        # другой человек (бесплатный) не получал это: «не найдено», а не данные (задержка бесплатного не обходится перебором номеров)
        with self.assertRaises(Refuse) as ctx:
            call("listing", OTHER, {"ref": ref})
        self.assertEqual(ctx.exception.code, "not_found")
        # подписчик видит и не полученное
        PREMIUM.add(OTHER)
        self.assertEqual(call("listing", OTHER, {"ref": ref})["t"], "2 Zimmer is7")
        for bad in (0, -3, "x", None, True, 10 ** 12, 999999):
            with self.assertRaises(Refuse, msg=str(bad)):
                call("listing", USER, {"ref": bad})

    def test_apply_list_status_viewing_found_remove(self):
        ref = self.add_flat("is9")
        made = call("apply", USER, {"ref": ref})
        self.assertTrue(made["created"])
        again = call("apply", USER, {"ref": ref})
        self.assertFalse(again["created"])
        self.assertEqual(again["id"], made["id"])
        listing = call("listing", USER, {"ref": ref})
        self.assertEqual(listing["ap"]["status"], "applied")
        lst = call("apps", USER)
        self.assertEqual((lst["total"], lst["items"][0]["t"], lst["items"][0]["status"]), (1, "2 Zimmer is9", "applied"))
        self.assertEqual(lst["items"][0]["ref"], ref)
        aid = made["id"]
        self.assertEqual(call("app_status", USER, {"id": aid, "status": "replied"})["app"]["status"], "replied")
        with self.assertRaises(Refuse):
            call("app_status", USER, {"id": aid, "status": "found"})                # только те, что в боте кнопками
        with self.assertRaises(Refuse) as ctx:
            call("app_viewing", USER, {"id": aid, "text": "когда-нибудь"})
        self.assertEqual(ctx.exception.code, "date_bad")
        moment = datetime.utcnow() + timedelta(days=2)
        with mock.patch.object(housing_dateparse, "parse_viewing", return_value=moment):
            viewing = call("app_viewing", USER, {"id": aid, "text": "послезавтра 15:00"})
        self.assertEqual(viewing["app"]["status"], "viewing")
        self.assertIsNotNone(viewing["app"]["viewing"])
        with mock.patch.object(housing_tracker, "_call_mark_found", return_value=True) as marked, mock.patch.object(site_bridge, "bot", return_value=object()):
            done = call("app_found", USER, {"id": aid})
        self.assertEqual(done["app"]["status"], "found")
        marked.assert_called_once()
        self.assertTrue(call("app_remove", USER, {"id": aid})["removed"])
        self.assertEqual(call("apps", USER)["items"], [])
        self.assertEqual(call("status", USER)["applied"], 1)                         # «подались» остаётся честным числом, как в боте
        # после «Прибрати» повторное «Подався» возвращает заявку
        self.assertTrue(call("apply", USER, {"ref": ref})["id"] == aid)

    def test_someone_else_cannot_touch_my_application(self):
        ref = self.add_flat("is10")
        aid = call("apply", USER, {"ref": ref})["id"]
        for op, params in (("app_status", {"id": aid, "status": "rejected"}), ("app_viewing", {"id": aid, "text": "28.12 10:30"}),
                           ("app_found", {"id": aid}), ("app_remove", {"id": aid})):
            with self.assertRaises(Refuse, msg=op) as ctx:
                call(op, OTHER, params)
            self.assertEqual(ctx.exception.code, "not_found")
        self.assertEqual(call("apps", OTHER)["items"], [])
        with self.assertRaises(Refuse):
            call("apply", OTHER, {"ref": ref})                                          # не получал эту квартиру
        self.assertEqual(call("apps", USER)["items"][0]["status"], "applied")

    def test_free_applications_are_limited_like_in_the_bot(self):
        PREMIUM.discard(USER)
        for i in range(housing_tracker.FREE_LIMIT + 2):
            call("apply", USER, {"ref": self.add_flat("f%d" % i)})
        got = call("apps", USER)
        self.assertEqual((len(got["items"]), got["hidden_count"], got["full"]), (housing_tracker.FREE_LIMIT, 2, False))

    def test_tracker_off_is_a_refusal(self):
        ref = self.add_flat("is11")
        with mock.patch.object(housing_tracker, "enabled", return_value=False):
            with self.assertRaises(Refuse):
                call("apply", USER, {"ref": ref})
            self.assertFalse(call("status", USER)["tracker"])

    def test_letters_only_for_subscribers_and_only_own(self):
        housing_letter_store.log_letter(USER, "immoscout", "1", "Titel", "https://x/1", "Sehr geehrte …")
        self.assertEqual(call("letters", USER)["items"][0]["text"], "Sehr geehrte …")
        self.assertEqual(call("letters", USER)["items"][0]["ref"], housing_letter_store.ref_for("immoscout", "1"))
        self.assertEqual(call("letters", OTHER)["items"], [])
        PREMIUM.add(OTHER)
        self.assertEqual(call("letters", OTHER)["items"], [])


class StatsAndNotifyTests(Base):
    def test_stats_by_day_and_source_and_free_window(self):
        now = datetime.utcnow()
        for i, source in enumerate(("immoscout", "immoscout", "kleinanzeigen")):
            self.add_flat("s%d" % i, source=source, when=now - timedelta(hours=i))
        self.add_flat("old", when=now - timedelta(days=20))
        full = call("stats", USER, {"days": 30})
        self.assertEqual((full["days"], full["total"], len(full["series"])), (30, 4, 30))
        self.assertEqual(full["sources"][0], {"src": "immoscout", "label": "ImmoScout24", "n": 3})
        self.assertEqual(sum(d["n"] for d in full["series"]), 4)
        PREMIUM.discard(USER)
        free = call("stats", USER, {"days": 30})
        self.assertEqual((free["days"], free["total"], free["full"]), (7, 3, False))
        with self.assertRaises(Refuse):
            call("stats", USER, {"days": 3})
        self.assertEqual(call("stats", OTHER, {"days": 7})["total"], 0)

    def test_market_counts(self):
        self.add_flat("m1")
        data = call("market", USER, {"period": "week"})
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["rooms"]["counts"][1], 1)
        self.assertEqual(sum(data["price"]["counts"]), 1)
        with self.assertRaises(Refuse):
            call("market", USER, {"period": "year"})

    def test_notify_quiet_digest_news(self):
        store = {"digest_mode": "instant"}
        with mock.patch.object(hm, "_notification_prefs", side_effect=lambda uid: dict(store)), \
                mock.patch.object(hm, "_set_notification_prefs", side_effect=lambda uid, **kw: store.update(kw)):
            got = call("notify_get", USER)
            self.assertEqual((got["premium"], got["quiet"], got["digest"], got["news"]), (True, True, "instant", True))
            self.assertFalse(call("notify_set", USER, {"quiet": False})["quiet"])
            self.assertEqual(call("notify_set", USER, {"digest": "daily"})["digest"], "daily")
            self.assertFalse(call("notify_set", USER, {"news": False})["news"])
            with self.assertRaises(Refuse):
                call("notify_set", USER, {"digest": "hourly"})
            with self.assertRaises(Refuse):
                call("notify_set", USER, {"news": "no"})
            # бесплатному тихая ночь недоступна
            free = call("notify_get", OTHER)
            self.assertFalse(free["premium"] or free["quiet"])
            with self.assertRaises(Refuse) as ctx:
                call("notify_set", OTHER, {"quiet": True})
            self.assertEqual(ctx.exception.code, "quiet_locked")
            # настройки чужого не меняются
            self.assertTrue(user_settings_store.get_news_subscribed(OTHER))
            self.assertFalse(user_settings_store.get_news_subscribed(USER))

    def test_notify_digest_failure_is_a_refusal(self):
        with mock.patch.object(hm, "_set_notification_prefs", side_effect=RuntimeError("down")):
            with self.assertRaises(Refuse) as ctx:
                call("notify_set", USER, {"digest": "daily"})
        self.assertEqual(ctx.exception.code, "save_failed")


class MatchesTests(Base):
    def test_matches_list_for_subscriber_count_for_free(self):
        self.new_search(sources=["immoscout"], c={"max_price_eur": 1000})
        session = hm.DBSession()
        try:
            session.add(hm._LISTING_MODELS["immoscout"](
                listing_key="mm1", title="Passende", address="x", district="Mitte", rooms=2, area_m2=50, price_eur=700, price_warm_eur=800,
                detail_url="https://example.org/mm1", first_seen_at=datetime.utcnow(), last_seen_at=datetime.utcnow(), is_active=True, plz="14467"))
            session.commit()
        finally:
            session.close()
        paid = call("matches", USER)
        self.assertTrue(paid["full"])
        self.assertEqual((paid["total"], paid["items"][0]["t"], paid["items"][0]["url"].startswith("https://")), (1, "Passende", True))
        PREMIUM.discard(USER)
        free = call("matches", USER)
        self.assertEqual((free["full"], free["items"]), (False, []))
        self.assertIn("1", free["message"])
        self.assertEqual(free["total"], paid["total"])
        self.assertEqual(call("matches", OTHER)["filters"], 0)


class LiveBridgeTests(unittest.TestCase):
    """Через настоящий HTTP-сервер бота: секрет, чужой user_id, коды ответов (каркас как в tests/test_site_bridge.py)."""

    KEY = "k" * 64

    def setUp(self):
        fd, self.keyfile = tempfile.mkstemp()
        os.write(fd, self.KEY.encode())
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(self.keyfile) and os.unlink(self.keyfile))
        for patch in (mock.patch.object(site_bridge, "KEY_FILE", self.keyfile), mock.patch.object(hm, "_all_immowelt_filters", return_value=[])):
            patch.start()
            self.addCleanup(patch.stop)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), housing_web.Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def post(self, op, body, key=KEY):
        req = urllib.request.Request("http://127.0.0.1:%d/int/housing/%s" % (self.port, op), json.dumps(body).encode(),
                                     {"X-Bridge-Key": key, "Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as err:
            data = json.loads(err.read())
            err.close()
            return err.code, data

    def test_all_operations_are_registered(self):
        names = set(site_bridge.registered())
        for op in ("me", "status", "feed", "listing", "searches", "place_find", "search_get", "search_options", "search_save", "search_pause",
                   "search_delete", "monitoring_set", "found_set", "matches", "apps", "apply", "app_status", "app_viewing", "app_found",
                   "app_remove", "stats", "market", "notify_get", "notify_set", "letters"):
            self.assertIn("housing." + op, names)

    def test_secret_and_user_are_required(self):
        self.assertEqual(self.post("status", {"user_id": 5}, key="x" * 64)[0], 403)
        self.assertEqual(self.post("status", {"user_id": 0})[0], 400)
        self.assertEqual(self.post("status", {})[0], 400)

    def test_refusal_is_422_with_text_and_unknown_user_is_empty(self):
        status, body = self.post("listing", {"user_id": 987654321, "lang": "de", "params": {"ref": 5}})
        self.assertEqual((status, body["error"]), (422, "not_found"))
        self.assertTrue(body["message"])
        status, body = self.post("searches", {"user_id": 987654321, "lang": "uk", "params": {}})
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["items"], [])


if __name__ == "__main__":
    unittest.main()
