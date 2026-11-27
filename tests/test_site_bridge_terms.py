"""Мост сайта, область terms (пакет H3): подписки на ДП «Документ» и посольство через /int/terms/*.
Живой HTTP-сервер бота на случайном порту, настоящие функции equeue_monitor/econsul_monitor и временная база контейнера (config/bot.db).
Проверяется: чужой user_id (подписки не пересекаются), плохие параметры, права (econsul без ECONSUL_PUBLIC), «Я записався» (поздравление
уходит человеку и админу, второй раз отказ), история находок, три языка, user_id только из тела."""

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

import site_bridge
from database import (
    DBSession,
    EconsulDay,
    EconsulService,
    EconsulState,
    EqueueAvailableSighting,
    EqueueStatus,
    EqueueSubscription,
    User,
)
from user_handlers import econsul_monitor as ec
from user_handlers import equeue_monitor as eq
from user_handlers import housing_payment, housing_web

KEY = "t" * 64
A, B = 900300001, 900300002
ADMIN = 900300999


def _clean():
    session = DBSession()
    try:
        session.query(EqueueSubscription).filter(EqueueSubscription.user_id.in_((A, B))).delete(synchronize_session=False)
        session.query(EqueueStatus).delete()
        session.query(EqueueAvailableSighting).delete()
        session.query(EconsulService).delete()
        session.query(EconsulState).delete()
        session.query(EconsulDay).delete()
        session.query(User).filter(User.id.in_((A, B))).delete(synchronize_session=False)
        session.commit()
    finally:
        session.close()


def _services():
    session = DBSession()
    try:
        now = datetime.utcnow()
        session.add(EconsulService(code="pass", name="Паспорт", minutes=20, served=True, free_count=3, nearest="2026-12-10 09:20", updated_at=now))
        session.add(EconsulService(code="visa", name="Віза & <b>х</b>", minutes=15, served=True, free_count=0, nearest=None, updated_at=now))
        session.add(EconsulService(code="old", name="Не обслуговується", minutes=10, served=False, free_count=1, nearest="2026-12-11 10:00", updated_at=now))
        session.add(EconsulState(source=ec.SOURCE, institution_name="Посольство УКР", last_checked_at=now, last_ok_at=now, last_status="ok", baseline_done=True))
        session.commit()
    finally:
        session.close()


def _subs(user_id, prefix=None):
    session = DBSession()
    try:
        q = session.query(EqueueSubscription).filter(EqueueSubscription.user_id == user_id)
        if prefix:
            q = q.filter(EqueueSubscription.service.like(prefix + "%"))
        return {row.service: bool(row.active) for row in q.all()}
    finally:
        session.close()


class TermsBridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        site_bridge.load()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), housing_web.Handler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        _clean()
        self.addCleanup(_clean)
        fd, self.keyfile = tempfile.mkstemp()
        os.write(fd, (KEY + "\n").encode())
        os.close(fd)
        self.addCleanup(os.remove, self.keyfile)
        for patcher in (
            mock.patch.object(site_bridge, "KEY_FILE", self.keyfile),
            mock.patch.object(ec, "PUBLIC", True),
            mock.patch.object(housing_payment, "ADMIN_ID", ADMIN),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.tg = mock.Mock()
        bot_patch = mock.patch.object(site_bridge, "bot", return_value=self.tg)
        bot_patch.start()
        self.addCleanup(bot_patch.stop)
        _services()

    def call(self, name, user=A, params=None, lang="uk", body=None, key=KEY):
        payload = body if body is not None else {"user_id": user, "lang": lang, "params": params or {}}
        req = urllib.request.Request("http://127.0.0.1:%d/int/terms/%s" % (self.port, name), data=json.dumps(payload).encode(), method="POST")
        req.add_header("Content-Type", "application/json")
        if key:
            req.add_header("X-Bridge-Key", key)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read())

    def ok(self, *a, **k):
        status, body = self.call(*a, **k)
        self.assertEqual(status, 200, body)
        return body["data"]

    def refused(self, code, *a, **k):
        status, body = self.call(*a, **k)
        self.assertEqual((status, body.get("error")), (422, code), body)
        self.assertTrue(body.get("message"))
        return body

    # ---------- каркас ----------
    def test_all_operations_registered(self):
        names = set(site_bridge.registered())
        for name in ("overview", "equeue_state", "equeue_sub", "equeue_booked", "econsul_state", "econsul_sub", "econsul_unsub", "econsul_booked"):
            self.assertIn("terms." + name, names)

    def test_no_key_and_no_user(self):
        self.assertEqual(self.call("equeue_state", key="x" * 64)[0], 403)
        self.assertEqual(self.call("equeue_state", body={"user_id": 0, "lang": "uk", "params": {}})[0], 400)
        self.assertEqual(self.call("equeue_state", body={"lang": "uk"})[0], 400)

    def test_params_user_id_is_ignored(self):
        self.ok("equeue_sub", user=A, params={"on": True, "user_id": B})
        self.assertEqual(_subs(A, eq.SERVICE_KEY), {eq.SERVICE_KEY: True})
        self.assertEqual(_subs(B), {})

    # ---------- ДП «Документ» ----------
    def test_equeue_empty_state(self):
        data = self.ok("equeue_state")
        self.assertFalse(data["subscribed"])
        self.assertEqual(data["status"]["code"], "never")
        self.assertEqual(data["sightings"], [])
        self.assertTrue(data["open_url"].startswith("https://"))
        self.assertTrue(data["intro"])
        self.assertNotIn("<b>", json.dumps(data, ensure_ascii=False))

    def test_equeue_subscribe_unsubscribe_and_isolation(self):
        data = self.ok("equeue_sub", params={"on": True})
        self.assertTrue(data["subscribed"])
        self.assertTrue(data["message"])
        self.assertEqual(_subs(A), {eq.SERVICE_KEY: True})
        self.assertFalse(self.ok("equeue_state", user=B)["subscribed"])
        self.assertEqual(_subs(B), {})
        # чужой user_id не выключает чужую подписку
        self.ok("equeue_sub", user=B, params={"on": False})
        self.assertEqual(_subs(A), {eq.SERVICE_KEY: True})
        data = self.ok("equeue_sub", params={"on": False})
        self.assertFalse(data["subscribed"])
        self.assertEqual(_subs(A), {eq.SERVICE_KEY: False})
        self.assertTrue(self.ok("equeue_sub", params={"on": True})["subscribed"])
        self.assertEqual(len(_subs(A)), 1)       # повторное включение обновляет ту же строку

    def test_equeue_bad_params(self):
        for params in ({}, {"on": "yes"}, {"on": 1}, {"on": None}, {"on": [True]}):
            self.refused("bad_request", "equeue_sub", params=params)
        self.assertEqual(_subs(A), {})

    def test_equeue_name_from_known_user(self):
        session = DBSession()
        session.add(User(id=A, fullname="Іван Тест", username="ivan_t"))
        session.commit()
        session.close()
        self.ok("equeue_sub", params={"on": True})
        session = DBSession()
        try:
            row = session.query(EqueueSubscription).filter(EqueueSubscription.user_id == A).one()
            self.assertEqual((row.username, row.display_name), ("ivan_t", "Іван Тест"))
        finally:
            session.close()

    def test_equeue_status_and_history(self):
        eq._record_service_status("available", "x")
        now = datetime.utcnow()
        session = DBSession()
        session.add(EqueueAvailableSighting(service=eq.SERVICE_KEY, found_at=eq.SIGHTINGS_RELIABLE_SINCE - timedelta(days=1), reason=""))
        for hours in (50, 30, 5):
            session.add(EqueueAvailableSighting(service=eq.SERVICE_KEY, found_at=now - timedelta(hours=hours), reason=""))
        session.add(EqueueAvailableSighting(service=ec.SOURCE, found_at=now, reason="чужой сервис"))
        session.commit()
        session.close()
        data = self.ok("equeue_state")
        self.assertEqual(data["status"]["code"], "available")
        self.assertTrue(data["status"]["checked"])
        self.assertEqual(len(data["sightings"]), 3)             # старые (до исправления) и посольство не показываются
        self.assertIn("5", data["sightings_ago"])
        eq._record_service_status("blocked", "cf")
        self.assertEqual(self.ok("equeue_state")["status"]["code"], "blocked")
        eq._record_service_status("weird", "?")
        self.assertEqual(self.ok("equeue_state")["status"]["code"], "other")

    def test_equeue_history_limit(self):
        now = datetime.utcnow()
        session = DBSession()
        for n in range(15):
            session.add(EqueueAvailableSighting(service=eq.SERVICE_KEY, found_at=now - timedelta(hours=n + 1), reason=""))
        session.commit()
        session.close()
        self.assertEqual(len(self.ok("equeue_state")["sightings"]), 10)

    def test_equeue_booked(self):
        self.refused("not_subscribed", "equeue_booked")
        self.tg.send_message.assert_not_called()
        self.ok("equeue_sub", params={"on": True})
        data = self.ok("equeue_booked")
        self.assertTrue(data["booked"] and data["tg_sent"] and not data["subscribed"])
        self.assertEqual(_subs(A), {eq.SERVICE_KEY: False})
        targets = sorted(call.kwargs.get("chat_id") for call in self.tg.send_message.call_args_list)
        self.assertEqual(targets, sorted([A, ADMIN]))           # поздравление человеку и строка админу, как в боте
        self.refused("not_subscribed", "equeue_booked")
        self.assertEqual(self.tg.send_message.call_count, 2)
        # чужой человек ничего не отписывает
        self.ok("equeue_sub", params={"on": True})
        self.refused("not_subscribed", "equeue_booked", user=B)
        self.assertEqual(_subs(A), {eq.SERVICE_KEY: True})

    def test_equeue_booked_survives_telegram_failure(self):
        self.ok("equeue_sub", params={"on": True})
        self.tg.send_message.side_effect = RuntimeError("telegram down")
        data = self.ok("equeue_booked")
        self.assertFalse(data["subscribed"])

    # ---------- посольство ----------
    def test_econsul_not_allowed(self):
        with mock.patch.object(ec, "PUBLIC", False):
            for name, params in (("econsul_state", {}), ("econsul_sub", {"all": True}), ("econsul_unsub", {"all": True}), ("econsul_booked", {"all": True})):
                self.refused("not_allowed", name, params=params)
            self.assertEqual(_subs(A), {})
            data = self.ok("overview")
            self.assertFalse(data["econsul"]["allowed"])
            self.assertTrue(data["equeue"]["allowed"])

    def test_econsul_state(self):
        data = self.ok("econsul_state")
        self.assertEqual(data["institution"], "Посольство УКР")
        self.assertEqual(data["status"]["code"], "ok")
        self.assertEqual([s["code"] for s in data["services"]], ["pass", "visa"] if data["services"][0]["code"] == "pass" else ["visa", "pass"])
        by = {s["code"]: s for s in data["services"]}
        self.assertNotIn("old", by)                              # необслуживаемые не показываются (как в боте)
        self.assertEqual(by["pass"]["nearest"], "10.12.2026 09:20")
        self.assertEqual(by["pass"]["free"], 3)
        self.assertIsNone(by["visa"]["nearest"])
        self.assertEqual(by["visa"]["name"], "Віза & <b>х</b>")  # имя отдаём как есть, экранирует страница
        self.assertTrue(data["can_add"] and not data["all"] and data["subscriptions"] == [])
        self.assertTrue(data["intro"] and "<b>" not in data["intro"])
        self.assertTrue(data["book_url"].startswith("https://"))

    def test_econsul_subscribe_codes(self):
        data = self.ok("econsul_sub", params={"codes": ["pass"]})
        self.assertEqual(_subs(A, ec.SUB_PREFIX), {"econsul_berlin:pass": True})
        self.assertEqual([s["code"] for s in data["subscriptions"]], ["pass"])
        self.assertEqual(data["subscriptions"][0]["nearest"], "10.12.2026 09:20")
        self.assertTrue(data["message"] and "<" not in data["message"])
        by = {s["code"]: s for s in data["services"]}
        self.assertTrue(by["pass"]["subscribed"] and not by["visa"]["subscribed"])
        self.assertEqual(self.ok("econsul_state", user=B)["subscriptions"], [])
        # повторно ту же и неизвестную/необслуживаемую нельзя
        self.refused("not_found", "econsul_sub", params={"codes": ["pass"]})
        self.refused("not_found", "econsul_sub", params={"codes": ["visa", "nope"]})
        self.refused("not_found", "econsul_sub", params={"codes": ["old"]})
        self.assertEqual(_subs(A, ec.SUB_PREFIX), {"econsul_berlin:pass": True})
        data = self.ok("econsul_sub", params={"codes": ["visa"]})
        self.assertEqual(sorted(s["code"] for s in data["subscriptions"]), ["pass", "visa"])
        self.refused("nothing_left", "econsul_sub", params={"codes": ["pass"]})
        self.assertFalse(self.ok("econsul_state")["can_add"])

    def test_econsul_subscribe_all(self):
        self.ok("econsul_sub", params={"codes": ["pass"]})
        data = self.ok("econsul_sub", params={"all": True})
        self.assertEqual(_subs(A, ec.SUB_PREFIX), {"econsul_berlin:pass": False, "econsul_berlin:*": True})
        self.assertTrue(data["all"] and data["subscriptions"] == [] and not data["can_add"])
        self.assertTrue(all(s["subscribed"] for s in data["services"]))
        self.refused("already_all", "econsul_sub", params={"all": True})
        self.refused("already_all", "econsul_sub", params={"codes": ["pass"]})

    def test_econsul_subscribe_bad_params(self):
        bad = ({}, {"codes": []}, {"codes": "pass"}, {"codes": [1]}, {"codes": [""]}, {"codes": ["*"]}, {"codes": [None]},
               {"codes": ["x"] * 41}, {"codes": ["a" * 64]}, {"codes": ["a\nb"]}, {"all": "yes"}, {"all": 1},
               {"all": True, "codes": ["pass"]}, {"all": False}, {"codes": {"pass": 1}})
        for params in bad:
            self.refused("bad_request", "econsul_sub", params=params)
        self.assertEqual(_subs(A), {})

    def test_econsul_unsubscribe(self):
        self.ok("econsul_sub", params={"codes": ["pass", "visa"]})
        self.ok("econsul_sub", user=B, params={"codes": ["pass"]})
        data = self.ok("econsul_unsub", params={"code": "pass"})
        self.assertEqual([s["code"] for s in data["subscriptions"]], ["visa"])
        self.assertEqual(_subs(B, ec.SUB_PREFIX), {"econsul_berlin:pass": True})        # у другого человека не тронуто
        self.refused("not_found", "econsul_unsub", params={"code": "pass"})
        self.refused("not_found", "econsul_unsub", params={"code": "nope"})
        self.refused("not_found", "econsul_unsub", user=B, params={"code": "visa"})     # чужая подписка недоступна
        self.assertEqual(_subs(A, ec.SUB_PREFIX)["econsul_berlin:visa"], True)
        data = self.ok("econsul_unsub", params={"code": "visa"})
        self.assertEqual(data["subscriptions"], [])
        self.assertTrue(data["message"])
        self.refused("not_found", "econsul_unsub", params={"all": True})                # отписываться не от чего

    def test_econsul_unsubscribe_all_and_star(self):
        self.ok("econsul_sub", params={"codes": ["pass", "visa"]})
        data = self.ok("econsul_unsub", params={"all": True})
        self.assertEqual(data["subscriptions"], [])
        self.assertEqual(set(_subs(A, ec.SUB_PREFIX).values()), {False})
        self.ok("econsul_sub", params={"all": True})
        data = self.ok("econsul_unsub", params={"code": "*"})
        self.assertFalse(data["all"])
        self.assertTrue(data["can_add"])

    def test_econsul_unsubscribe_bad_params(self):
        self.ok("econsul_sub", params={"codes": ["pass"]})
        for params in ({}, {"code": 5}, {"code": ["pass"]}, {"code": ""}, {"all": "x"}, {"all": True, "code": "pass"}, {"code": "a" * 100}):
            self.refused("bad_request", "econsul_unsub", params=params)
        self.assertEqual(_subs(A, ec.SUB_PREFIX), {"econsul_berlin:pass": True})

    def test_econsul_booked_only_and_all(self):
        self.refused("not_subscribed", "econsul_booked", params={"all": True})
        self.ok("econsul_sub", params={"codes": ["pass", "visa"]})
        self.refused("not_found", "econsul_booked", params={"codes": ["nope"]})
        self.tg.send_message.assert_not_called()
        data = self.ok("econsul_booked", params={"codes": ["pass"]})
        self.assertTrue(data["booked"] and data["tg_sent"])
        self.assertEqual(_subs(A, ec.SUB_PREFIX), {"econsul_berlin:pass": False, "econsul_berlin:visa": True})
        self.assertEqual(sorted(c.kwargs["chat_id"] for c in self.tg.send_message.call_args_list), sorted([A, ADMIN]))
        data = self.ok("econsul_booked", params={"all": True})
        self.assertEqual(set(_subs(A, ec.SUB_PREFIX).values()), {False})
        self.assertEqual(data["subscriptions"], [])
        self.refused("not_subscribed", "econsul_booked", params={"all": True})
        self.assertEqual(self.tg.send_message.call_count, 4)

    def test_econsul_booked_only_with_star_keeps_the_rest(self):
        self.ok("econsul_sub", params={"all": True})
        data = self.ok("econsul_booked", params={"codes": ["pass"]})
        self.assertFalse(data["all"])
        self.assertEqual(_subs(A, ec.SUB_PREFIX), {"econsul_berlin:*": False, "econsul_berlin:visa": True})
        self.assertEqual([s["code"] for s in data["subscriptions"]], ["visa"])

    def test_econsul_booked_bad_params(self):
        self.ok("econsul_sub", params={"codes": ["pass"]})
        for params in ({}, {"all": True, "codes": ["pass"]}, {"codes": []}, {"all": "x"}, {"codes": ["*"]}):
            self.refused("bad_request", "econsul_booked", params=params)
        self.assertEqual(_subs(A, ec.SUB_PREFIX), {"econsul_berlin:pass": True})
        self.tg.send_message.assert_not_called()

    def test_econsul_history_and_lifetime(self):
        now = datetime.utcnow()
        session = DBSession()
        for n in range(12):
            session.add(EqueueAvailableSighting(service=ec.SOURCE, found_at=now - timedelta(hours=n), reason="Паспорт: 10.12.2026"))
        session.add(EqueueAvailableSighting(service=eq.SERVICE_KEY, found_at=now, reason="чужой сервис"))
        session.commit()
        session.close()
        data = self.ok("econsul_state")
        self.assertEqual(len(data["sightings"]), 10)
        self.assertEqual(data["sightings"][0]["what"], "Паспорт: 10.12.2026")
        self.assertIsNone(data["lifetime"])

    def test_econsul_status_codes(self):
        session = DBSession()
        state = session.query(EconsulState).one()
        state.last_ok_at = datetime.utcnow() - timedelta(days=5)
        session.commit()
        session.close()
        self.assertEqual(self.ok("econsul_state")["status"]["code"], "stale")
        session = DBSession()
        state = session.query(EconsulState).one()
        state.last_status = "auth_required"
        session.commit()
        session.close()
        self.assertEqual(self.ok("econsul_state")["status"]["code"], "broken")
        session = DBSession()
        session.query(EconsulState).delete()
        session.commit()
        session.close()
        self.assertEqual(self.ok("econsul_state")["status"]["code"], "never")

    # ---------- языки и обзор ----------
    def test_languages(self):
        texts = {}
        for lang in ("uk", "ru", "de"):
            data = self.ok("equeue_sub", params={"on": False}, lang=lang)
            texts[lang] = data["message"]
            self.assertNotIn("[[", json.dumps(data, ensure_ascii=False))
            self.assertNotIn("[[", json.dumps(self.ok("econsul_state", lang=lang), ensure_ascii=False))
            self.assertNotIn("[[", self.refused("bad_request", "equeue_sub", lang=lang, params={})["message"])
        self.assertEqual(len(set(texts.values())), 3)

    def test_overview(self):
        data = self.ok("overview")
        self.assertEqual(data["equeue"], {"allowed": True, "subscribed": False, "status": "never"})
        self.assertEqual(data["econsul"], {"allowed": True, "all": False, "count": 0, "status": "ok"})
        self.ok("equeue_sub", params={"on": True})
        self.ok("econsul_sub", params={"codes": ["pass", "visa"]})
        data = self.ok("overview")
        self.assertTrue(data["equeue"]["subscribed"])
        self.assertEqual(data["econsul"]["count"], 2)
        other = self.ok("overview", user=B)
        self.assertFalse(other["equeue"]["subscribed"])
        self.assertEqual(other["econsul"]["count"], 0)

    def test_read_operations_do_not_write(self):
        before = (_subs(A), )
        for name in ("overview", "equeue_state", "econsul_state"):
            self.ok(name)
        self.assertEqual((_subs(A), ), before)
        self.tg.send_message.assert_not_called()


if __name__ == "__main__":
    unittest.main()
