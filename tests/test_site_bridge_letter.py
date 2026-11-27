"""Мост сайта, область letter (пакет H4): письмо арендодателю через /int/letter/*.
Живой HTTP-сервер бота на случайном порту, настоящие функции housing_letter / housing_letter_store, временная база контейнера.
Модель НЕ вызывается: housing_letter._call_ai подменён заглушкой. Проверяется: чужой user_id, не подписчик (отказ locked), плохие параметры,
анкета (обязательные поля, длина), запуск и опрос статуса, суточный лимит бота, один незаконченный запуск на человека, чужой job,
история (только свои), три языка, user_id только из тела."""

import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime
from http.server import ThreadingHTTPServer
from unittest import mock

import site_bridge
from database import (
    DBSession,
    HousingAccessUser,
    HousingLetterLog,
    HousingLetterProfile,
    HousingLetterRef,
    ImmoscoutListing,
)
from site_bridge import letter as bridge_letter
from user_handlers import housing_letter, housing_web
from user_jobs import housing_letter_store as store
from user_jobs import housing_tier

KEY = "t" * 64
SUB, SUB2, FREE = 900400001, 900400002, 900400003
ADMIN = 900400999
LK = "h4test-flat-1"
FULL = {"full_name": "Іван Петренко", "household": "2 дорослих і дитина", "income": "Jobcenter платить оренду", "move_in": "з 01.01", "contact": "", "about": ""}


def _clean():
    session = DBSession()
    try:
        ids = (SUB, SUB2, FREE)
        session.query(HousingAccessUser).filter(HousingAccessUser.user_id.in_(ids)).delete(synchronize_session=False)
        session.query(HousingLetterProfile).filter(HousingLetterProfile.user_id.in_(ids)).delete(synchronize_session=False)
        session.query(HousingLetterLog).filter(HousingLetterLog.user_id.in_(ids)).delete(synchronize_session=False)
        session.query(HousingLetterRef).filter(HousingLetterRef.listing_key.like("h4test-%")).delete(synchronize_session=False)
        session.query(ImmoscoutListing).filter(ImmoscoutListing.listing_key.like("h4test-%")).delete(synchronize_session=False)
        session.commit()
    finally:
        session.close()
    bridge_letter._jobs.clear()
    housing_letter._daily.clear()


def _flat():
    session = DBSession()
    try:
        now = datetime.utcnow()
        session.add(ImmoscoutListing(
            listing_key=LK, title="2-Zimmer <b>Wohnung</b> & Co", address="Zeppelinstr. 1, Potsdam", rooms=2, area_m2=55.5, price_eur=900,
            detail_url="https://www.immobilienscout24.de/expose/1", first_seen_at=now, last_seen_at=now, is_active=True))
        session.commit()
    finally:
        session.close()
    return store.ref_for("immoscout", LK)


def _grant(user_id):
    session = DBSession()
    try:
        session.add(HousingAccessUser(user_id=user_id, display_name="t", active=True, created_at=datetime.utcnow(), updated_at=datetime.utcnow()))
        session.commit()
    finally:
        session.close()


class LetterBridgeTests(unittest.TestCase):
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
        self.calls = []
        self.reply = "Sehr geehrte Damen und Herren,\n\nich interessiere mich für die Wohnung.\n\nMit freundlichen Grüßen\nIvan"

        def fake_ai(prompt):
            self.calls.append(prompt)
            if isinstance(self.reply, Exception):
                raise self.reply
            return self.reply

        for patcher in (
            mock.patch.object(site_bridge, "KEY_FILE", self.keyfile),
            mock.patch.object(housing_tier, "ADMIN_ID", ADMIN),
            mock.patch.object(housing_letter, "_call_ai", side_effect=fake_ai),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        _grant(SUB)
        _grant(SUB2)
        self.ref = _flat()

    def call(self, name, user=SUB, params=None, lang="uk", body=None, key=KEY):
        payload = body if body is not None else {"user_id": user, "lang": lang, "params": params or {}}
        req = urllib.request.Request("http://127.0.0.1:%d/int/letter/%s" % (self.port, name), data=json.dumps(payload).encode(), method="POST")
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
        return body

    def finish(self, job, user=SUB):
        for _ in range(100):
            data = self.ok("status", user=user, params={"job": job})
            if data["state"] != "running":
                return data
            time.sleep(0.05)
        self.fail("job did not finish")

    # ---------- каркас ----------
    def test_all_operations_registered(self):
        names = set(site_bridge.registered())
        for name in ("overview", "profile_save", "listing", "start", "status", "history"):
            self.assertIn("letter." + name, names)

    def test_no_key_and_no_user(self):
        self.assertEqual(self.call("overview", key="x" * 64)[0], 403)
        self.assertEqual(self.call("overview", body={"user_id": 0, "lang": "uk", "params": {}})[0], 400)
        self.assertEqual(self.call("overview", body={"lang": "uk"})[0], 400)

    # ---------- не подписчик ----------
    def test_free_user_is_locked_everywhere_and_nothing_written(self):
        data = self.ok("overview", user=FREE)
        self.assertFalse(data["premium"])
        self.assertIn("Лист", data["locked"])
        self.assertNotIn("<b>", data["locked"])
        self.assertNotIn("profile", data)
        for name, params in (("profile_save", FULL), ("listing", {"ref": self.ref}), ("start", {"ref": self.ref}), ("history", {})):
            body = self.refused("locked", name, user=FREE, params=params)
            self.assertTrue(body["message"])
        self.assertIsNone(store.get_profile(FREE))
        self.assertEqual(self.calls, [])
        self.assertEqual(housing_letter._daily, {})

    def test_admin_counts_as_subscriber(self):
        self.assertTrue(self.ok("overview", user=ADMIN)["premium"])

    # ---------- обзор и анкета ----------
    def test_overview_subscriber(self):
        data = self.ok("overview")
        self.assertTrue(data["premium"])
        self.assertIsNone(data["profile"])
        self.assertEqual([f["key"] for f in data["fields"]], list(store.PROFILE_FIELDS))
        self.assertEqual([f["key"] for f in data["fields"] if f["optional"]], list(store.OPTIONAL_FIELDS))
        self.assertEqual((data["used"], data["limit"], data["left"], data["count"]), (0, housing_letter.DAILY_LIMIT, housing_letter.DAILY_LIMIT, 0))

    def test_profile_save_and_isolation(self):
        data = self.ok("profile_save", params=dict(FULL))
        self.assertEqual(data["profile"]["full_name"], "Іван Петренко")
        self.assertTrue(data["message"])
        self.assertEqual(store.get_profile(SUB)["income"], "Jobcenter платить оренду")
        self.assertIsNone(store.get_profile(SUB2))
        self.assertIsNone(self.ok("overview", user=SUB2)["profile"])
        self.assertEqual(self.ok("overview")["profile"]["move_in"], "з 01.01")

    def test_profile_user_id_in_params_ignored(self):
        self.ok("profile_save", user=SUB, params=dict(FULL, user_id=SUB2))
        self.assertIsNotNone(store.get_profile(SUB))
        self.assertIsNone(store.get_profile(SUB2))

    def test_profile_required_and_limits(self):
        self.refused("required", "profile_save", params=dict(FULL, full_name="  "))
        self.refused("required", "profile_save", params={k: v for k, v in FULL.items() if k != "income"})
        self.refused("too_long", "profile_save", params=dict(FULL, about="я" * 501))
        self.refused("bad_request", "profile_save", params=dict(FULL, about=["x"]))
        self.refused("bad_request", "profile_save", params=dict(FULL, about=12))
        self.refused("bad_request", "profile_save", params=dict(FULL, about="я" * 5000))
        self.assertIsNone(store.get_profile(SUB))
        self.ok("profile_save", params=dict(FULL, contact="a@b.de", about="не палимо"))      # необязательные можно непустыми и пустыми
        self.assertEqual(store.get_profile(SUB)["about"], "не палимо")

    def test_profile_control_chars_removed(self):
        self.ok("profile_save", params=dict(FULL, full_name="Іван\x00\x07 П"))
        self.assertEqual(store.get_profile(SUB)["full_name"], "Іван П")

    # ---------- квартира ----------
    def test_listing_info(self):
        self.ok("profile_save", params=dict(FULL))
        data = self.ok("listing", params={"ref": self.ref})
        self.assertEqual(data["flat"]["title"], "2-Zimmer <b>Wohnung</b> & Co")      # сырой текст: экранирует страница
        self.assertEqual(data["flat"]["url"], "https://www.immobilienscout24.de/expose/1")
        self.assertEqual((data["flat"]["rooms"], data["flat"]["area"], data["flat"]["price"]), ("2", "55.5", "900"))
        self.assertTrue(data["flat"]["subject"].startswith("Anfrage"))
        self.assertIsNone(data["last"])
        self.assertEqual(data["profile"]["full_name"], "Іван Петренко")

    def test_listing_bad_params(self):
        for ref in (0, -1, "abc", None, True, 10 ** 12, [1], {"a": 1}, 999999):
            status, body = self.call("listing", params={"ref": ref})
            self.assertEqual(status, 422, (ref, body))
            self.assertIn(body["error"], ("bad_request", "not_found"))
        self.assertEqual(self.call("listing", params={})[1]["error"], "bad_request")

    def test_listing_gone(self):
        session = DBSession()
        try:
            session.query(ImmoscoutListing).filter(ImmoscoutListing.listing_key == LK).delete()
            session.commit()
        finally:
            session.close()
        body = self.refused("not_found", "listing", params={"ref": self.ref})
        self.assertTrue(body["message"])

    def test_listing_http_url_not_offered(self):
        session = DBSession()
        try:
            session.query(ImmoscoutListing).filter(ImmoscoutListing.listing_key == LK).update({"detail_url": "javascript:alert(1)"})
            session.commit()
        finally:
            session.close()
        self.assertEqual(self.ok("listing", params={"ref": self.ref})["flat"]["url"], "")

    # ---------- генерация ----------
    def test_start_needs_profile(self):
        self.refused("no_profile", "start", params={"ref": self.ref})
        self.assertEqual(self.calls, [])
        self.assertEqual(housing_letter._daily, {})      # отказ без анкеты лимит не тратит

    def test_start_status_done_and_history(self):
        self.ok("profile_save", params=dict(FULL))
        started = self.ok("start", params={"ref": self.ref})
        self.assertEqual(started["state"], "running")
        data = self.finish(started["job"])
        self.assertEqual(data["state"], "done")
        self.assertIn("Sehr geehrte", data["letter"])
        self.assertEqual(data["ref"], self.ref)
        self.assertEqual(data["flat"]["label"].split(" · ")[0], "2-Zimmer <b>Wohnung</b> & Co")
        self.assertEqual((data["used"], data["left"]), (1, housing_letter.DAILY_LIMIT - 1))
        self.assertEqual(len(self.calls), 1)
        self.assertIn("Іван Петренко", self.calls[0])      # анкета ушла в запрос тем же _prompt, что в боте
        self.assertIn("55.5", self.calls[0])
        hist = self.ok("history")["items"]
        self.assertEqual(len(hist), 1)
        self.assertEqual(hist[0]["ref"], self.ref)
        self.assertIn("Sehr geehrte", hist[0]["text"])
        self.assertTrue(hist[0]["ts"].endswith("Z"))
        self.assertEqual(self.ok("history", user=SUB2)["items"], [])
        self.assertEqual(self.ok("listing", params={"ref": self.ref})["last"]["text"], hist[0]["text"])
        self.assertEqual(self.ok("overview")["count"], 1)

    def test_variant_changes_prompt(self):
        self.ok("profile_save", params=dict(FULL))
        self.finish(self.ok("start", params={"ref": self.ref})["job"])
        self.finish(self.ok("start", params={"ref": self.ref, "variant": True})["job"])
        self.assertNotIn("deutlich anders", self.calls[0])
        self.assertIn("deutlich anders", self.calls[1])
        self.refused("bad_request", "start", params={"ref": self.ref, "variant": "yes"})

    def test_failed_generation(self):
        self.ok("profile_save", params=dict(FULL))
        for bad in ("", "RATE_LIMIT", RuntimeError("boom")):
            self.reply = bad
            data = self.finish(self.ok("start", params={"ref": self.ref})["job"])
            self.assertEqual(data["state"], "failed")
            self.assertNotIn("letter", data)
            self.assertTrue(data["message"])
        self.assertEqual(self.ok("history")["items"], [])      # неудачные в историю не попадают

    def test_one_running_job_per_user(self):
        self.ok("profile_save", params=dict(FULL))
        gate = threading.Event()
        self.reply = None

        def slow(prompt):
            self.calls.append(prompt)
            gate.wait(5)
            return "Sehr geehrte Damen und Herren"

        with mock.patch.object(housing_letter, "_call_ai", side_effect=slow):
            first = self.ok("start", params={"ref": self.ref})
            again = self.ok("start", params={"ref": self.ref, "variant": True})
            self.assertEqual(again["job"], first["job"])
            self.assertTrue(again["again"])
            self.assertEqual(self.ok("status", params={"job": first["job"]})["state"], "running")
            gate.set()
            self.assertEqual(self.finish(first["job"])["state"], "done")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(housing_letter._daily[(SUB, __import__("datetime").date.today().isoformat())], 1)

    def test_other_user_cannot_read_job(self):
        self.ok("profile_save", params=dict(FULL))
        job = self.ok("start", params={"ref": self.ref})["job"]
        self.finish(job)
        self.refused("not_found", "status", user=SUB2, params={"job": job})
        self.refused("not_found", "status", user=FREE, params={"job": job})

    def test_status_bad_params(self):
        for job in (None, 5, "", "zz", "../x", "A" * 20, ["a"], "0" * 21):
            self.refused("bad_request", "status", params={"job": job})
        self.refused("not_found", "status", params={"job": "0" * 20})

    def test_daily_limit_is_the_bots(self):
        self.ok("profile_save", params=dict(FULL))
        with mock.patch.object(housing_letter, "DAILY_LIMIT", 2):
            for _ in range(2):
                self.finish(self.ok("start", params={"ref": self.ref})["job"])
            body = self.refused("limit", "start", params={"ref": self.ref})
            self.assertTrue(body["message"])
            self.assertEqual(self.ok("overview")["left"], 0)
            self.refused("limit", "start", user=SUB, params={"ref": self.ref, "variant": True})
            self.assertEqual(len(self.calls), 2)
            # у другого человека свой счётчик
            self.ok("profile_save", user=SUB2, params=dict(FULL))
            self.assertEqual(self.finish(self.ok("start", user=SUB2, params={"ref": self.ref})["job"], user=SUB2)["state"], "done")

    def test_history_only_own(self):
        store.log_letter(SUB2, "immoscout", LK, "чужое", "https://x.de", "чужое письмо")
        store.log_letter(SUB, "immoscout", "h4test-other", "моё", "http://plain", "моё письмо")
        items = self.ok("history")["items"]
        self.assertEqual([i["title"] for i in items], ["моё"])
        self.assertEqual(items[0]["url"], "")        # не https: ссылку не отдаём
        self.assertIsNone(items[0]["ref"])           # ссылки на ту квартиру нет: новых не создаём
        self.assertEqual(self.ok("history", user=SUB2)["items"][0]["ref"], self.ref)

    # ---------- языки ----------
    def test_three_languages(self):
        for lang in ("uk", "ru", "de"):
            body = self.refused("required", "profile_save", params=dict(FULL, income=""), lang=lang)
            self.assertTrue(body["message"])
            self.assertEqual(len({self.refused("required", "profile_save", params=dict(FULL, income=""), lang=l)["message"] for l in ("uk", "ru", "de")}), 3)
            self.assertTrue(self.refused("locked", "history", user=FREE, lang=lang)["message"])
        self.assertTrue(self.refused("no_profile", "start", params={"ref": self.ref}, lang="de")["message"].startswith("Bitte"))


if __name__ == "__main__":
    unittest.main()
