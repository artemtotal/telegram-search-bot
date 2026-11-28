"""Мост сайта, пакет H6 (сверка функций): housing.mappe_*, housing.invite, housing.help, флаги меню, misc.lang_set, misc.feedback.
Живой HTTP-сервер бота на случайном порту, настоящие функции бота и временная база контейнера; Telegram подменён (в сеть и людям ничего не уходит).
Проверяется: чужой user_id, права (папка только подписчику), плохие параметры, что info/status/invite/help ничего не пишут, что сборка папки зовёт ту же
функцию бота и не запускается без анкеты/в лимит/повторно, что сообщение админу уходит той же функцией бота и не чаще лимита."""

import base64
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
from types import SimpleNamespace
from unittest import mock

import i18n
import site_bridge
from database import DBSession, HousingMappeLog, HousingMappeProfile, HousingLetterProfile, UserSettings
from site_bridge import housing_extra, misc
from user_handlers import anonymous_posts as ap
from user_handlers import housing_mappe, housing_monitor as hm, housing_referral, housing_web
from user_jobs import housing_letter_store, housing_mappe_store, housing_pay_store, housing_tier

KEY = "h" * 64
A, B, C = 900600001, 900600002, 900600003
USERS = (A, B, C)
ADMIN = 900600999
PREMIUM = {A, B}


def _clean():
    session = DBSession()
    try:
        for model in (HousingMappeProfile, HousingMappeLog, HousingLetterProfile, UserSettings):
            session.query(model).filter(model.user_id.in_(USERS)).delete(synchronize_session=False)
        session.commit()
    finally:
        session.close()


def _count(model, user):
    session = DBSession()
    try:
        return session.query(model).filter(model.user_id == user).count()
    finally:
        session.close()


class H6Tests(unittest.TestCase):
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
        misc._feedback_log.clear()
        housing_mappe._busy.discard(A)
        fd, self.keyfile = tempfile.mkstemp()
        os.write(fd, (KEY + "\n").encode())
        os.close(fd)
        self.addCleanup(os.remove, self.keyfile)
        self.sent = []

        def send_message(chat_id=None, text=None, **kwargs):
            self.sent.append((chat_id, text, kwargs))
            return SimpleNamespace(message_id=1)

        self.tg = mock.Mock()
        self.tg.send_message.side_effect = send_message
        self.tg.get_chat.return_value = SimpleNamespace(username="tester", full_name="Test Er")
        self.generated = []
        for patcher in (
            mock.patch.object(site_bridge, "KEY_FILE", self.keyfile),
            mock.patch.object(site_bridge, "bot", return_value=self.tg),
            mock.patch.object(hm, "OPEN_TO_ALL", True),
            mock.patch.object(housing_tier, "is_premium", side_effect=lambda uid: int(uid) in PREMIUM),
            mock.patch.object(ap, "ADMIN_ID", ADMIN),
            mock.patch.object(housing_mappe, "ENABLED", True),
            mock.patch.object(housing_mappe, "generate", side_effect=lambda bot, uid, ref=None: self.generated.append((uid, ref))),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    # ---------- вызов ----------
    def call(self, domain, name, user=A, params=None, lang="uk", key=KEY):
        payload = {"user_id": user, "lang": lang, "params": params or {}}
        req = urllib.request.Request("http://127.0.0.1:%d/int/%s/%s" % (self.port, domain, name), data=json.dumps(payload).encode(), method="POST")
        req.add_header("Content-Type", "application/json")
        if key:
            req.add_header("X-Bridge-Key", key)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read())

    def ok(self, domain, name, *a, **k):
        status, body = self.call(domain, name, *a, **k)
        self.assertEqual(status, 200, body)
        return body["data"]

    def refused(self, code, domain, name, *a, **k):
        status, body = self.call(domain, name, *a, **k)
        self.assertEqual((status, body.get("error")), (422, code), body)
        self.assertTrue(body.get("message"), body)
        return body

    def full_answers(self, **extra):
        answers = {"full_name": "Ivan Test", "birth_date": "01.02.1990", "address": "Teststr. 1, 14467 Potsdam", "move_in": "01.12", "contact": "+49 1",
                   "occupation": "jobcenter", "adults": "2", "children": "1", "pets": "no", "smoking": "no", "debts": "no", "insolvency": "no"}
        answers.update(extra)
        return answers

    # ---------- каркас ----------
    def test_registered_and_auth(self):
        names = site_bridge.registered()
        for op in ("mappe_info", "mappe_save", "mappe_make", "mappe_status", "mappe_pdf", "mappe_delete", "invite", "help"):
            self.assertIn("housing." + op, names)
        for op in ("lang_set", "feedback"):
            self.assertIn("misc." + op, names)
        self.assertEqual(self.call("housing", "mappe_info", key="")[0], 403)
        self.assertEqual(self.call("housing", "mappe_info", key="x" * 64)[0], 403)
        self.assertEqual(self.call("housing", "mappe_info", user=0)[0], 400)

    # ---------- папка: права ----------
    def test_mappe_needs_subscription(self):
        info = self.ok("housing", "mappe_info", C)
        self.assertTrue(info["enabled"] and not info["premium"])
        self.assertIn("message", info)
        self.assertNotIn("fields", info)
        self.refused("paywall", "housing", "mappe_save", C, {"answers": self.full_answers()})
        self.refused("paywall", "housing", "mappe_make", C)
        self.refused("paywall", "housing", "mappe_delete", C)
        self.assertEqual(_count(HousingMappeProfile, C), 0)
        self.assertEqual(self.generated, [])

    def test_mappe_switched_off(self):
        with mock.patch.object(housing_mappe, "ENABLED", False):
            info = self.ok("housing", "mappe_info", A)
            self.assertFalse(info["enabled"])
            self.refused("off", "housing", "mappe_save", A, {"answers": self.full_answers()})
            self.refused("off", "housing", "mappe_make", A)

    def test_mappe_info_reads_only(self):
        info = self.ok("housing", "mappe_info", A)
        self.assertTrue(info["premium"] and not info["saved"] and not info["ready"] and not info["busy"])
        self.assertEqual([f["key"] for f in info["fields"]], list(housing_mappe.FIELDS))
        kinds = {f["key"]: f["kind"] for f in info["fields"]}
        self.assertEqual((kinds["occupation"], kinds["adults"], kinds["pets"], kinds["full_name"]), ("choice", "number", "yesno", "text"))
        occ = next(f for f in info["fields"] if f["key"] == "occupation")
        self.assertEqual([c["v"] for c in occ["choices"]], ["works", "jobcenter", "other"])
        # правила: зависимые вопросы считает функция бота
        self.assertIn("employer", info["rules"]["works|"])
        self.assertIn("net_income", info["rules"]["works|"])
        self.assertNotIn("employer", info["rules"]["jobcenter|"])
        self.assertIn("pet_kind", info["rules"]["works|yes"])
        self.assertNotIn("pet_kind", info["rules"]["works|no"])
        self.assertEqual((info["limit"], info["used"], info["left"]), (housing_mappe.DAILY_LIMIT, 0, housing_mappe.DAILY_LIMIT))
        self.assertEqual(_count(HousingMappeProfile, A), 0)          # чтение ничего не создало
        for lang in ("uk", "ru", "de"):
            labels = self.ok("housing", "mappe_info", A, lang=lang)["fields"]
            self.assertTrue(all(f["label"] and "<" not in f["label"] for f in labels))

    def test_mappe_info_prefills_from_letter_profile(self):
        housing_letter_store.save_profile(A, {"full_name": "Letter Name", "household": "2", "income": "x", "move_in": "asap", "contact": "mail@x.de", "about": ""})
        info = self.ok("housing", "mappe_info", A)
        self.assertEqual((info["answers"]["full_name"], info["answers"]["move_in"], info["answers"]["contact"]), ("Letter Name", "asap", "mail@x.de"))
        self.assertFalse(info["saved"])

    # ---------- папка: сохранение ----------
    def test_mappe_save_validation(self):
        for bad in ({}, {"answers": []}, {"answers": {"nope": "x"}}, {"answers": {"occupation": "king"}}, {"answers": {"pets": "maybe"}},
                    {"answers": {"full_name": ["a"]}}, {"answers": {"full_name": True}}, {"answers": {"full_name": "x" * 401}}):
            status, body = self.call("housing", "mappe_save", A, bad)
            self.assertIn(body.get("error"), ("bad_request",), (bad, body))
        for number in ("abc", "0", "21", "-1", "1.5"):
            self.refused("bad_number", "housing", "mappe_save", A, {"answers": {"adults": number}})
        self.refused("bad_number", "housing", "mappe_save", A, {"answers": {"children": "99"}})
        self.assertEqual(_count(HousingMappeProfile, A), 0)

    def test_mappe_save_keeps_only_applicable_and_marks_skipped(self):
        res = self.ok("housing", "mappe_save", A, {"answers": self.full_answers(employer="Old GmbH", net_income="2000", pet_kind="cat")})
        stored = housing_mappe_store.get_answers(A)
        self.assertNotIn("employer", stored)               # Jobcenter: работодателя и дохода нет
        self.assertNotIn("net_income", stored)
        self.assertNotIn("pet_kind", stored)               # животных нет
        self.assertEqual(stored["occupation"], "jobcenter")
        self.assertEqual(stored["children"], "1")
        self.assertTrue(res["ready"])
        # работает и «другое»: тогда спрашивается, чем именно, но не доход
        self.ok("housing", "mappe_save", A, {"answers": self.full_answers(occupation="other", employer="студент", net_income="5")})
        stored = housing_mappe_store.get_answers(A)
        self.assertEqual(stored["employer"], "студент")
        self.assertNotIn("net_income", stored)
        # пропущенный вопрос хранится пустым (как «пропустить» в боте): не переспрашивается
        self.ok("housing", "mappe_save", A, {"answers": self.full_answers(birth_date="")})
        self.assertEqual(housing_mappe_store.get_answers(A)["birth_date"], "")
        self.assertTrue(self.ok("housing", "mappe_info", A)["ready"])

    def test_mappe_save_does_not_copy_letter_fields(self):
        housing_letter_store.save_profile(A, {"full_name": "Ivan Test", "household": "2", "income": "x", "move_in": "01.12", "contact": "+49 1", "about": ""})
        self.ok("housing", "mappe_save", A, {"answers": self.full_answers()})
        stored = housing_mappe_store.get_answers(A)
        for key in ("full_name", "move_in", "contact"):
            self.assertNotIn(key, stored)                   # совпадает с анкетой писем: берётся оттуда
        # другое значение сохраняется своё
        self.ok("housing", "mappe_save", A, {"answers": self.full_answers(contact="other@x.de")})
        self.assertEqual(housing_mappe_store.get_answers(A)["contact"], "other@x.de")

    def test_mappe_is_per_person(self):
        self.ok("housing", "mappe_save", A, {"answers": self.full_answers()})
        self.assertIsNone(housing_mappe_store.get_answers(B))
        self.assertFalse(self.ok("housing", "mappe_info", B)["saved"])

    # ---------- папка: сборка ----------
    def test_mappe_make_needs_questionnaire(self):
        self.refused("no_profile", "housing", "mappe_make", A)
        self.assertEqual(self.generated, [])

    def test_mappe_make_calls_the_bot_function(self):
        self.ok("housing", "mappe_save", A, {"answers": self.full_answers()})
        res = self.ok("housing", "mappe_make", A)
        self.assertTrue(res["started"])
        self.assertEqual((res["before"], self.generated), (0, [(A, None)]))
        self.assertEqual(self.sent, [])                      # мост сам ничего не пишет: сообщения шлёт generate (здесь подменён)

    def test_mappe_make_for_flat_checks_the_reference(self):
        self.ok("housing", "mappe_save", A, {"answers": self.full_answers()})
        for bad in ("x", -1, 2 ** 40, True, [1], "1; drop"):
            status, body = self.call("housing", "mappe_make", A, {"ref": bad})
            self.assertEqual((status, body.get("error")), (422, "bad_request"), (bad, body))
        self.refused("not_found", "housing", "mappe_make", A, {"ref": 987654})
        self.assertEqual(self.generated, [])
        with mock.patch.object(housing_letter_store, "resolve_ref", return_value={"source": "immoscout", "listing_key": "k1"}), \
                mock.patch("user_handlers.housing_letter._listing", return_value={"title": "Flat"}):
            self.ok("housing", "mappe_make", A, {"ref": 5})
        self.assertEqual(self.generated, [(A, 5)])

    def test_mappe_make_busy_and_limit(self):
        self.ok("housing", "mappe_save", A, {"answers": self.full_answers()})
        housing_mappe._busy.add(A)
        self.addCleanup(housing_mappe._busy.discard, A)
        self.refused("busy", "housing", "mappe_make", A)
        self.assertTrue(self.ok("housing", "mappe_info", A)["busy"])
        housing_mappe._busy.discard(A)
        with mock.patch.object(housing_mappe, "DAILY_LIMIT", 1):
            housing_mappe_store.log_generation(A)
            self.refused("limit", "housing", "mappe_make", A)
            self.assertEqual(self.ok("housing", "mappe_info", A)["left"], 0)
        self.assertEqual(self.generated, [])

    def test_mappe_status(self):
        self.assertEqual(self.ok("housing", "mappe_status", A, {"before": 0}), {"busy": False, "done": False, "today": 0})
        housing_mappe._busy.add(A)
        self.addCleanup(housing_mappe._busy.discard, A)
        self.assertTrue(self.ok("housing", "mappe_status", A, {"before": 0})["busy"])
        housing_mappe._busy.discard(A)
        housing_mappe_store.log_generation(A)
        self.assertTrue(self.ok("housing", "mappe_status", A, {"before": 0})["done"])
        self.assertFalse(self.ok("housing", "mappe_status", A, {"before": 1})["done"])
        self.assertFalse(self.ok("housing", "mappe_status", B, {"before": 0})["done"])        # чужую папку не видно
        for bad in ({"before": "1"}, {"before": -1}, {"before": True}):
            self.assertEqual(self.call("housing", "mappe_status", A, bad)[1].get("error"), "bad_request")

    def test_mappe_result_for_the_site(self):
        # результат сборки: список документов на экран и PDF только владельцу; ошибка бота показывается на сайте
        self.assertNotIn("pdf", self.ok("housing", "mappe_status", A, {"before": 0}))
        self.refused("not_found", "housing", "mappe_pdf", A)
        housing_mappe._results[A] = {"ok": True, "pdf": b"%PDF-1.4 secret", "occupation": "jobcenter", "ts": time.time()}
        self.addCleanup(housing_mappe._results.clear)
        s = self.ok("housing", "mappe_status", A, {"before": 0}, lang="ru")
        self.assertTrue(s["done"] and s["pdf"])
        titles = [x["h"] for x in s["docs"]["items"]]
        self.assertTrue(any("Mietübernahmebescheinigung" in x for x in titles) and any("SCHUFA" in x for x in titles), titles)
        self.assertFalse(any("Gehaltsabrechnung" in x for x in titles))
        self.assertTrue(s["docs"]["title"] and s["docs"]["footer"] and all(x["p"] for x in s["docs"]["items"]))
        self.assertNotIn("<", json.dumps(s["docs"]))
        s_de = self.ok("housing", "mappe_status", A, {"before": 0}, lang="de")
        self.assertNotEqual(s_de["docs"]["title"], s["docs"]["title"])
        pdf = self.ok("housing", "mappe_pdf", A)
        self.assertEqual((base64.b64decode(pdf["pdf_b64"]), pdf["name"]), (b"%PDF-1.4 secret", "Bewerbungsmappe.pdf"))
        self.assertNotIn("pdf", self.ok("housing", "mappe_status", B, {"before": 0}))          # чужая папка не видна
        self.refused("not_found", "housing", "mappe_pdf", B)
        self.refused("paywall", "housing", "mappe_pdf", C)
        housing_mappe._results[A] = {"ok": False, "error": "Не вдалося зібрати папку", "ts": time.time()}
        s = self.ok("housing", "mappe_status", A, {"before": 0})
        self.assertEqual((s["failed"], s["done"], s["error"]), (True, False, "Не вдалося зібрати папку"))
        self.refused("not_found", "housing", "mappe_pdf", A)
        housing_mappe._results[A] = {"ok": True, "pdf": b"x", "occupation": "", "ts": time.time() - housing_mappe.RESULT_TTL - 5}
        self.refused("not_found", "housing", "mappe_pdf", A)                                   # просрочено

    def test_mappe_delete_only_own(self):
        for user in (A, B):
            housing_letter_store.save_profile(user, {"full_name": "n", "household": "h", "income": "i", "move_in": "m", "contact": "", "about": ""})
            self.ok("housing", "mappe_save", user, {"answers": self.full_answers()})
        res = self.ok("housing", "mappe_delete", A)
        self.assertTrue(res["deleted"])
        self.assertEqual((_count(HousingMappeProfile, A), _count(HousingLetterProfile, A)), (0, 0))
        self.assertEqual((_count(HousingMappeProfile, B), _count(HousingLetterProfile, B)), (1, 1))

    # ---------- «Запроси друга» ----------
    def test_invite(self):
        with mock.patch.object(housing_referral, "enabled", return_value=False):
            self.assertEqual(self.ok("housing", "invite", A), {"enabled": False})
        with mock.patch.object(housing_letter_store, "_bot_username", None):
            res = self.ok("housing", "invite", A)
            self.assertTrue(res["enabled"] and res["link"] is None and res["intro"])
        with mock.patch.object(housing_letter_store, "_bot_username", "Testbot"):
            res = self.ok("housing", "invite", A)
            self.assertTrue(res["link"].startswith("https://t.me/Testbot?start=rf"), res)
            self.assertEqual((res["invited"], res["paid"]), (0, 0))
            self.assertIn("t.me%2FTestbot", res["share"])
            self.assertTrue(res["intro"] and "{LINK}" not in res["intro"] + res["outro"])
            other = self.ok("housing", "invite", B)
            self.assertNotEqual(other["link"], res["link"])                # у каждого своя ссылка
            de = self.ok("housing", "invite", A, lang="de")
            self.assertNotEqual(de["intro"], res["intro"])

    # ---------- справка ----------
    def test_help(self):
        with mock.patch.object(hm, "_status_lines", return_value=["🟢 <b>ProPotsdam</b> проверено", "🔴 Vonovia"]):
            res = self.ok("housing", "help", A)
        self.assertGreaterEqual(len(res["sections"]), 3)
        self.assertTrue(res["sections"][0]["h"])
        self.assertTrue(all("<" not in s["h"] + s["p"] for s in res["sections"]))
        self.assertEqual(res["status"], ["🟢 ProPotsdam проверено", "🔴 Vonovia"])
        self.assertTrue(res["status_title"])
        with mock.patch.object(hm, "_status_lines", side_effect=RuntimeError("x")):
            self.assertEqual(self.ok("housing", "help", A)["status"], [])           # сбой статуса не ломает справку
        with mock.patch.object(hm, "_status_lines", return_value=[]):
            for lang in ("ru", "de"):
                self.assertTrue(self.ok("housing", "help", A, lang=lang)["sections"])

    # ---------- флаги меню ----------
    def test_menu_flags(self):
        from site_bridge import housing

        self.assertEqual(housing._extra_flags(), {"mappe": True, "invite": housing_referral.enabled()})
        with mock.patch.object(housing_mappe, "ENABLED", False), mock.patch.object(housing_referral, "enabled", return_value=False):
            self.assertEqual(housing._extra_flags(), {"mappe": False, "invite": False})

    # ---------- язык бота ----------
    def test_lang_set(self):
        for bad in ({}, {"lang": "fr"}, {"lang": 1}, {"lang": None}, {"lang": ["uk"]}):
            self.assertEqual(self.call("misc", "lang_set", A, bad)[1].get("error"), "bad_request")
        self.assertEqual(_count(UserSettings, A), 0)
        res = self.ok("misc", "lang_set", A, {"lang": "de"})
        self.assertEqual(res["lang"], "de")
        self.assertEqual(self.ok("misc", "me", A)["lang"], "de")
        self.ok("misc", "lang_set", A, {"lang": "uk"})
        self.assertEqual(self.ok("misc", "me", A)["lang"], "uk")
        self.assertFalse(self.ok("misc", "me", B)["known"])                # другого человека не затронуло

    # ---------- «Написати нам» ----------
    def test_feedback_goes_to_admin_by_bot_function(self):
        res = self.ok("misc", "feedback", A, {"text": "  Не працює кнопка «Статистика» у розділі.  "})
        self.assertTrue(res["sent"])
        admin = [s for s in self.sent if s[0] == ADMIN]
        self.assertEqual(len(admin), 1)
        self.assertIn("Зворотній звʼязок", admin[0][1])
        self.assertIn("ID %d" % A, admin[0][1])
        self.assertIn("Не працює кнопка", admin[0][1])
        self.assertEqual([s for s in self.sent if s[0] != ADMIN], [])      # больше никому ничего не ушло

    def test_feedback_rules(self):
        self.assertEqual(self.refused("too_short", "misc", "feedback", A, {"text": "ні"})["error"], "too_short")
        for bad in ({}, {"text": 5}, {"text": ["a"]}, {"text": "x" * 9000}):
            self.assertEqual(self.call("misc", "feedback", A, bad)[1].get("error"), "bad_request")
        self.assertEqual(self.sent, [])
        for n in range(misc.FEEDBACK_PER_HOUR):
            self.ok("misc", "feedback", A, {"text": "Повідомлення номер %d" % n})
        self.refused("too_many", "misc", "feedback", A, {"text": "Ще одне повідомлення"})
        self.ok("misc", "feedback", B, {"text": "Інша людина пише вперше"})      # лимит у каждого свой

    def test_feedback_failure_is_reported(self):
        self.tg.send_message.side_effect = RuntimeError("telegram down")
        self.refused("failed", "misc", "feedback", A, {"text": "Це повідомлення не дійде"})

    def test_feedback_page_language_is_independent_of_bot_language(self):
        self.ok("misc", "lang_set", A, {"lang": "de"})
        res = self.ok("misc", "feedback", A, {"text": "Das ist eine Nachricht"}, lang="ru")
        self.assertEqual(res["message"], i18n.t("anon.feedback.delivered", "ru"))

    def test_translations_complete(self):
        for key in ("h6.bad", "h6.mappe.noprofile", "h6.mappe.busy", "h6.mappe.started", "h6.feedback.rate"):
            texts = {lang: i18n.t(key, lang) for lang in ("uk", "ru", "de")}
            self.assertTrue(all(texts.values()) and len(set(texts.values())) == 3, texts)


if __name__ == "__main__":
    unittest.main()
