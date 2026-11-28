"""Замки и «продающие» подсказки моста (28.11.2026, docs/paywall-contract.md): housing.me -> paywall, early_h в ленте и карточке, «Підходящі зараз»
для бесплатного (одна полная квартира + картинки без утечек), единый ответ замка, пробное письмо (хранилище)."""

import json
import unittest
from datetime import datetime, timedelta
from unittest import mock

import database
from site_bridge import Paywall, Refuse, housing
from tests import test_site_bridge_housing as bh
from user_handlers import housing_pay_report
from user_jobs import housing_letter_store, housing_paywall, housing_pay_store, housing_tier

USER, OTHER, STRANGER = bh.USER, bh.OTHER, 424242
PREMIUM = bh.PREMIUM
call = bh.call
FEATURES = ("instant", "matches", "letter", "quiet_night", "more_searches", "stats_full", "feed_full", "mappe", "apps_full")


class PaywallBase(bh.Base):
    def setUp(self):
        super().setUp()
        session = bh.hm.DBSession
        for module in (housing_paywall, housing_pay_report):
            if hasattr(module, "DBSession"):
                patch = mock.patch.object(module, "DBSession", session)
                patch.start()
                self.addCleanup(patch.stop)


for _name in [name for name in vars(bh.Base) if name.startswith("test_")]:
    setattr(PaywallBase, _name, None)


class MeTests(PaywallBase):
    def test_free_gets_every_field_of_the_contract(self):
        pw = call("me", OTHER)["paywall"]
        self.assertEqual(set(pw), {"premium", "price_from", "delay_h", "features", "loss"})
        self.assertFalse(pw["premium"])
        self.assertEqual(pw["price_from"], "від 5 €")
        self.assertEqual(pw["delay_h"], 2)
        self.assertIsInstance(pw["delay_h"], int)
        self.assertEqual(set(pw["features"]), set(FEATURES))
        for name in FEATURES:
            self.assertEqual(pw["features"][name]["locked"], name != "feed_full", name)      # feed_full в боте не ограничен
        self.assertEqual(pw["features"]["letter"]["free_left"], 1)
        self.assertIsNone(pw["loss"])            # ничего не опоздало: нечего показывать

    def test_text_comes_in_three_languages(self):
        self.assertEqual(call("me", OTHER, lang="ru")["paywall"]["price_from"], "от 5 €")
        self.assertEqual(call("me", OTHER, lang="de")["paywall"]["price_from"], "ab 5 €")

    def test_subscriber_everything_open_and_no_loss(self):
        self.add_flat("d1", user=USER, delayed=True)          # даже если когда-то была задержка: подписчику потерь не показываем
        pw = call("me", USER)["paywall"]
        self.assertTrue(pw["premium"])
        self.assertTrue(all(pw["features"][name]["locked"] is False for name in FEATURES))
        self.assertEqual(pw["features"]["letter"]["free_left"], 0)
        self.assertIsNone(pw["loss"])

    def test_delay_follows_the_setting(self):
        with mock.patch.object(housing_tier, "FREE_DELAY", timedelta(hours=1.5)):
            self.assertEqual(call("me", OTHER)["paywall"]["delay_h"], 1.5)

    def test_letter_free_left_drops_to_zero_when_spent(self):
        mode = housing_letter_store.begin_letter(OTHER)
        self.assertEqual(call("me", OTHER)["paywall"]["features"]["letter"]["free_left"], 1)      # идущая генерация пробу ещё не тратит
        housing_letter_store.finish_letter(OTHER, mode, True)
        self.assertEqual(call("me", OTHER)["paywall"]["features"]["letter"]["free_left"], 0)

    def test_a_foreign_user_sees_only_his_own_numbers(self):
        self.add_flat("d1", user=OTHER, delayed=True)
        self.assertEqual(call("me", OTHER)["paywall"]["loss"]["delayed"], 1)
        self.assertIsNone(call("me", STRANGER)["paywall"]["loss"])

    def test_loss_counts_delayed_and_taken_down(self):
        now = datetime.utcnow()
        for i in range(3):
            self.add_flat("d%d" % i, user=OTHER, delayed=True, when=now - timedelta(days=1, hours=i))
        self.add_flat("old", user=OTHER, delayed=True, when=now - timedelta(days=10))      # за пределами 7 дней
        self.add_flat("fast", user=OTHER, delayed=False, when=now - timedelta(hours=1))    # пришла сразу: не задержка
        housing_pay_store.log_missed(OTHER, "immoscout", "m1")
        housing_pay_store.log_missed(OTHER, "immoscout", "m2")
        housing_pay_store.log_missed(OTHER, "immoscout", "m2")                               # одна квартира дважды = одна
        housing_pay_store.log_missed(STRANGER, "immoscout", "m9")
        loss = call("me", OTHER)["paywall"]["loss"]
        self.assertEqual(loss, {"days": 7, "delayed": 5, "gone": 2})

    def test_loss_gone_ignores_delivered_but_already_gone_guess(self):       # решение 28.11: считаем только точные «вже зняли»
        now = datetime.utcnow()
        self.add_flat("v1", user=OTHER, delayed=True, when=now - timedelta(hours=3))
        session = bh.hm.DBSession()
        try:
            model = bh.hm._LISTING_MODELS["immoscout"]
            row = session.query(model).filter_by(listing_key="v1").one()
            row.is_active, row.first_seen_at, row.last_seen_at = False, now - timedelta(hours=6), now - timedelta(hours=5)
            session.commit()
        finally:
            session.close()
        self.add_flat("alive", user=STRANGER, delayed=False)       # обход площадки шёл дальше
        loss = call("me", OTHER)["paywall"]["loss"]
        self.assertEqual(loss, {"days": 7, "delayed": 1, "gone": 0})

    def test_loss_failure_is_null_not_an_error(self):
        with mock.patch.object(housing_paywall, "delivered_and_gone", side_effect=RuntimeError("x")):
            self.assertIsNone(call("me", OTHER)["paywall"]["loss"])
        with mock.patch.object(housing_paywall, "block", side_effect=RuntimeError("x")):
            self.assertIsNone(call("me", OTHER)["paywall"])
            self.assertIn("filters", call("me", OTHER))          # остальное в ответе на месте


class EarlyTests(PaywallBase):
    def test_early_h_from_real_timestamps(self):
        t0 = datetime.utcnow() - timedelta(hours=5)
        self.add_flat("e1", user=USER, delayed=False, when=t0)                             # подписчик получил
        self.add_flat("e1", user=OTHER, delayed=True, when=t0 + timedelta(hours=2, minutes=6))
        self.add_flat("e2", user=OTHER, delayed=True, when=t0 + timedelta(hours=2))        # подписчикам эту не слали
        self.add_flat("e3", user=OTHER, delayed=False, when=t0)                            # не с задержкой
        self.add_flat("e4", user=USER, delayed=False, when=t0 + timedelta(hours=3))        # подписчик позже, чем бесплатный: честной разницы нет
        self.add_flat("e4", user=OTHER, delayed=True, when=t0 + timedelta(hours=2))
        items = {i["t"].split()[-1]: i for i in call("feed", OTHER)["items"]}
        self.assertEqual(items["e1"]["early_h"], 2.1)
        self.assertIsNone(items["e2"]["early_h"])
        self.assertIsNone(items["e3"]["early_h"])
        self.assertIsNone(items["e4"]["early_h"])
        for item in call("feed", USER)["items"]:
            self.assertIsNone(item["early_h"])                                             # подписчику «раньше» не бывает
        ref = items["e1"]["ref"]
        self.assertEqual(call("listing", OTHER, {"ref": ref})["early_h"], 2.1)
        self.assertIsNone(call("listing", USER, {"ref": ref})["early_h"])

    def test_absurd_differences_are_not_shown(self):
        t0 = datetime.utcnow() - timedelta(days=5)
        self.add_flat("long", user=USER, delayed=False, when=t0)
        self.add_flat("long", user=OTHER, delayed=True, when=t0 + timedelta(hours=40))      # не волна «сразу / через задержку»
        self.add_flat("tiny", user=USER, delayed=False, when=t0)
        self.add_flat("tiny", user=OTHER, delayed=True, when=t0 + timedelta(minutes=2))
        self.add_flat("edge", user=USER, delayed=False, when=t0)
        self.add_flat("edge", user=OTHER, delayed=True, when=t0 + timedelta(hours=23, minutes=30))
        items = {i["t"].split()[-1]: i for i in call("feed", OTHER)["items"]}
        self.assertIsNone(items["long"]["early_h"])
        self.assertIsNone(items["tiny"]["early_h"])
        self.assertEqual(items["edge"]["early_h"], 23.5)

    def test_early_h_is_none_when_nothing_to_compare(self):
        self.add_flat("only", user=OTHER, delayed=True)
        self.assertIsNone(call("feed", OTHER)["items"][0]["early_h"])
        self.assertEqual(housing_paywall.early_hours(OTHER, []), {})

    def test_early_h_failure_does_not_break_the_feed(self):
        self.add_flat("f1", user=OTHER, delayed=True)
        with mock.patch.object(housing_paywall, "early_hours", side_effect=RuntimeError("x")):
            self.assertIsNone(call("feed", OTHER)["items"][0]["early_h"])


class MatchesPaywallTests(PaywallBase):
    def _flats(self, n, with_image=True):
        session = bh.hm.DBSession()
        now = datetime.utcnow()
        try:
            for i in range(n):
                session.add(bh.hm._LISTING_MODELS["immoscout"](
                    listing_key="mt%d" % i, title="Geheim-Titel %d" % i, address="Geheimstr. %d" % i, district="Mitte", rooms=2, area_m2=50 + i,
                    price_eur=700 + i, price_warm_eur=800, detail_url="https://example.org/mt%d" % i,
                    cover_image_url=("https://img.example/mt%d.jpg" % i) if with_image else None,
                    first_seen_at=now - timedelta(hours=10 - i), last_seen_at=now, is_active=True, plz="14467"))
            session.commit()
        finally:
            session.close()

    def test_free_gets_total_one_full_flat_and_picture_only_teasers(self):
        self.new_search(sources=["immoscout"], c={"max_price_eur": 2000})
        self._flats(9)
        PREMIUM.discard(USER)
        free = call("matches", USER)
        self.assertTrue(free["locked"] and free["full"] is False)
        self.assertEqual(free["total"], 9)
        self.assertEqual(len(free["items"]), 1)
        self.assertEqual(free["items"][0]["t"], "Geheim-Titel 8")                  # самая свежая
        self.assertTrue(free["items"][0]["url"].startswith("https://") and free["items"][0]["ref"])
        self.assertEqual(len(free["teasers"]), 5)
        for teaser in free["teasers"]:
            self.assertEqual(set(teaser), {"img"})
            self.assertTrue(teaser["img"].startswith("https://img.example/"))
        self.assertNotIn("img.example/mt8", json.dumps(free["teasers"]))             # картинка полной квартиры в подсказки не дублируется
        leaked = json.dumps(free["teasers"], ensure_ascii=False)
        for word in ("Geheim", "example.org", "14467", "Mitte", "immoscout", "title", "ref", "url"):
            self.assertNotIn(word, leaked, word)

    def test_teasers_skip_flats_without_a_picture_and_are_empty_without_any(self):
        self.new_search(sources=["immoscout"], c={"max_price_eur": 2000})
        self._flats(4, with_image=False)
        PREMIUM.discard(USER)
        free = call("matches", USER)
        self.assertEqual((free["total"], len(free["items"]), free["teasers"], free["locked"]), (4, 1, [], True))

    def test_free_with_nothing_matching(self):
        PREMIUM.discard(USER)
        free = call("matches", USER)
        self.assertEqual((free["total"], free["items"], free["locked"]), (0, [], True))
        self.assertEqual(free["teasers"], [])

    def test_subscriber_as_before_without_teasers(self):
        self.new_search(sources=["immoscout"], c={"max_price_eur": 2000})
        self._flats(3)
        paid = call("matches", USER)
        self.assertEqual((paid["locked"], paid["full"], paid["total"], len(paid["items"])), (False, True, 3, 3))
        self.assertNotIn("teasers", paid)

    def test_a_foreign_user_sees_nothing_of_anothers_filters(self):
        self.new_search(sources=["immoscout"], c={"max_price_eur": 2000})
        self._flats(3)
        other = call("matches", STRANGER)
        self.assertEqual((other["filters"], other["total"], other["items"]), (0, 0, []))


class PaywallErrorTests(PaywallBase):
    def test_second_search_pause_and_quiet_and_stats_all_speak_paywall(self):
        first = self.new_search()["search"]["id"]
        second = self.new_search(sources=["immoscout"])["search"]["id"]         # подписчик завёл два поиска
        PREMIUM.discard(USER)
        call("search_pause", USER, {"id": first, "paused": True})
        call("search_pause", USER, {"id": second, "paused": False})
        with self.assertRaises(Paywall) as ctx:                                  # бесплатному включить второй при активном первом нельзя
            call("search_pause", USER, {"id": first, "paused": False})
        self.assertEqual((ctx.exception.code, ctx.exception.extra), ("paywall", {"feature": "more_searches"}))
        for fn, feature in ((lambda: call("notify_set", USER, {"quiet": True}), "quiet_night"), (lambda: call("stats", USER, {"days": 30}), "stats_full")):
            with self.assertRaises(Paywall) as ctx:
                fn()
            self.assertEqual(ctx.exception.extra, {"feature": feature})

    def test_paywall_is_a_refuse_with_the_feature_field_in_the_reply(self):
        self.assertTrue(issubclass(Paywall, Refuse))
        self.assertEqual(Paywall("letter").extra, {"feature": "letter"})
        self.assertEqual(Paywall("letter").code, "paywall")

    def test_my_letters_visible_to_free_without_a_lock(self):
        housing_letter_store.log_letter(OTHER, "immoscout", "k", "t", "https://x.example", "Sehr geehrte")
        got = call("letters", OTHER)
        self.assertEqual((len(got["items"]), got["full"]), (1, False))
        self.assertEqual(call("letters", STRANGER)["items"], [])


class TrialStoreTests(PaywallBase):
    def test_spent_once_forever(self):
        self.assertEqual(housing_letter_store.begin_letter(OTHER), "trial")
        housing_letter_store.finish_letter(OTHER, "trial", True)
        self.assertIsNone(housing_letter_store.begin_letter(OTHER))
        self.assertEqual(housing_letter_store.free_left(OTHER), 0)
        self.assertFalse(housing_letter_store.can_write(OTHER))

    def test_failure_releases_and_retry_works(self):
        mode = housing_letter_store.begin_letter(OTHER)
        housing_letter_store.finish_letter(OTHER, mode, False)
        self.assertEqual(housing_letter_store.free_left(OTHER), 1)
        self.assertEqual(housing_letter_store.begin_letter(OTHER), "trial")

    def test_second_start_during_generation_is_busy_and_cannot_double_spend(self):
        self.assertEqual(housing_letter_store.begin_letter(OTHER), "trial")
        self.assertEqual(housing_letter_store.begin_letter(OTHER), "busy")
        housing_letter_store.finish_letter(OTHER, "busy", True)          # «занято» ничего не меняет
        self.assertEqual(housing_letter_store.free_left(OTHER), 1)
        housing_letter_store.finish_letter(OTHER, "trial", True)
        self.assertEqual(housing_letter_store.free_left(OTHER), 0)

    def test_stale_reservation_expires(self):
        housing_letter_store.begin_letter(OTHER)
        session = bh.hm.DBSession()
        try:
            session.query(housing_letter_store.HousingLetterTrial).filter_by(user_id=OTHER).one().updated_at = (
                datetime.utcnow() - housing_letter_store.PENDING_TTL - timedelta(minutes=1))
            session.commit()
        finally:
            session.close()
        self.assertEqual(housing_letter_store.begin_letter(OTHER), "trial")

    def test_subscriber_never_touches_the_trial(self):
        self.assertEqual(housing_letter_store.begin_letter(USER), "sub")
        housing_letter_store.finish_letter(USER, "sub", True)
        session = bh.hm.DBSession()
        try:
            self.assertEqual(session.query(housing_letter_store.HousingLetterTrial).filter_by(user_id=USER).count(), 0)
        finally:
            session.close()

    def test_former_subscriber_with_old_letters_gets_the_trial(self):
        housing_letter_store.log_letter(OTHER, "immoscout", "k", "t", "https://x.example", "Sehr geehrte")
        self.assertEqual(housing_letter_store.free_left(OTHER), 1)
        self.assertEqual(housing_letter_store.begin_letter(OTHER), "trial")

    def test_trial_is_per_user(self):
        housing_letter_store.finish_letter(OTHER, housing_letter_store.begin_letter(OTHER), True)
        self.assertEqual(housing_letter_store.free_left(STRANGER), 1)


if __name__ == "__main__":
    unittest.main()
