"""Мост сайта, область misc (пакет H5b): me, news и анонимный вопрос (anon_*) через /int/misc/*.
Живой HTTP-сервер бота на случайном порту, настоящие функции user_handlers/anonymous_posts и временная база контейнера; Telegram подменён
(отправка в чат и админу записывается, в сеть ничего не уходит). Проверяется: тот же путь, что у бота (правила текста, дубль за 30 дней,
капча и блокировка, кулдаун, окно удаления), чужой user_id, плохие параметры, отсутствие записи без нужды (info/me/check ничего не публикуют)."""

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from unittest import mock

import site_bridge
from database import AnonymousPost, AnonymousTopic, AnonymousUser, DBSession, UserSettings
from site_bridge import misc
from user_handlers import anonymous_posts as ap
from user_handlers import housing_web

KEY = "t" * 64
A, B, C = 900500001, 900500002, 900500003
USERS = (A, B, C)
CHAT = -1001900500
ADMIN = 900500999
GOOD = "Підкажіть, будь ласка, де в Потсдамі можна знайти хорошого сімейного лікаря?"


def _clean():
    session = DBSession()
    try:
        session.query(AnonymousPost).filter(AnonymousPost.user_id.in_(USERS)).delete(synchronize_session=False)
        session.query(AnonymousUser).filter(AnonymousUser.user_id.in_(USERS)).delete(synchronize_session=False)
        session.query(UserSettings).filter(UserSettings.user_id.in_(USERS)).delete(synchronize_session=False)
        session.query(AnonymousTopic).filter(AnonymousTopic.chat_id == CHAT).delete(synchronize_session=False)
        session.commit()
    finally:
        session.close()


def _row(model, key):
    session = DBSession()
    try:
        row = session.query(model).get(key)
        if row is not None:
            session.expunge(row)
        return row
    finally:
        session.close()


def _posts(user):
    session = DBSession()
    try:
        return [(p.id, p.status, p.text) for p in session.query(AnonymousPost).filter(AnonymousPost.user_id == user).order_by(AnonymousPost.id)]
    finally:
        session.close()


class MiscBridgeTests(unittest.TestCase):
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
        misc._FLOW.clear()
        fd, self.keyfile = tempfile.mkstemp()
        os.write(fd, (KEY + "\n").encode())
        os.close(fd)
        self.addCleanup(os.remove, self.keyfile)
        self.sent = []

        def send_message(chat_id=None, text=None, **kwargs):
            self.sent.append((chat_id, text, kwargs))
            if chat_id == CHAT:
                return SimpleNamespace(message_id=700 + len(self.sent), chat=SimpleNamespace(username="PotsdamChat"), chat_id=CHAT,
                                       message_thread_id=kwargs.get("message_thread_id", 0), date=datetime.utcnow(),
                                       from_user=SimpleNamespace(id=424242))
            return SimpleNamespace(message_id=1)

        self.tg = mock.Mock()
        self.tg.send_message.side_effect = send_message
        self.tg.get_chat.return_value = SimpleNamespace(username="tester", full_name="Test Er")
        for patcher in (
            mock.patch.object(site_bridge, "KEY_FILE", self.keyfile),
            mock.patch.object(site_bridge, "bot", return_value=self.tg),
            mock.patch.object(ap, "TARGET_CHAT_ID", CHAT),
            mock.patch.object(ap, "TOPIC_SOURCE_DB", ""),
            mock.patch.object(ap, "ADMIN_ID", ADMIN),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        ap._upsert_topic(CHAT, 5, "Здоров’я")
        ap._upsert_topic(CHAT, 9, "Побут")
        self.topic = next(t for t in misc._topics() if t["name"] == "Здоров’я")["id"]

    def call(self, name, user=A, params=None, lang="uk", body=None, key=KEY):
        payload = body if body is not None else {"user_id": user, "lang": lang, "params": params or {}}
        req = urllib.request.Request("http://127.0.0.1:%d/int/misc/%s" % (self.port, name), data=json.dumps(payload).encode(), method="POST")
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

    def solve(self, cap, right=True):
        a, b = [int(x) for x in cap["question"].split("+")]
        want = a + b
        if right:
            return want
        return next(v for v in cap["choices"] if v != want)

    def start_and_pass(self, user=A):
        cap = self.ok("anon_start", user)["captcha"]
        res = self.ok("anon_captcha", user, {"token": cap["token"], "answer": self.solve(cap)})
        self.assertTrue(res["passed"])
        return res["ticket"]

    def chat_posts(self):
        return [s for s in self.sent if s[0] == CHAT]

    # ---------- каркас ----------
    def test_registered_and_auth(self):
        names = set(site_bridge.registered())
        for name in ("ping", "me", "news", "anon_info", "anon_start", "anon_captcha", "anon_check", "anon_send", "anon_delete"):
            self.assertIn("misc." + name, names)
        self.assertEqual(self.call("anon_info", key="x" * 64)[0], 403)
        self.assertEqual(self.call("anon_info", body={"user_id": 0, "lang": "uk", "params": {}})[0], 400)

    # ---------- me / news ----------
    def test_me_does_not_create_settings(self):
        d = self.ok("me", A)
        self.assertEqual(d, {"known": False, "lang": None, "news": True})
        self.assertIsNone(_row(UserSettings, A))

    def test_me_reads_bot_settings(self):
        from user_jobs import user_settings_store as st

        st.set_language(A, "de")
        st.set_news_subscribed(A, False)
        self.assertEqual(self.ok("me", A), {"known": True, "lang": "de", "news": False})
        self.assertEqual(self.ok("me", B)["known"], False)

    def test_news_toggle_and_bad_params(self):
        from user_jobs import user_settings_store as st

        self.assertEqual(self.ok("news", A, {"on": False}), {"news": False})
        self.assertFalse(st.get_news_subscribed(A))
        self.assertTrue(st.get_news_subscribed(B))                 # чужой не затронут
        self.assertEqual(self.ok("news", A, {"on": True}), {"news": True})
        for bad in ({}, {"on": 1}, {"on": "yes"}, {"on": None}):
            self.refused("bad_request", "news", A, bad)
        self.assertEqual(self.ok("news", A, {"on": False, "user_id": B})["news"], False)
        self.assertTrue(st.get_news_subscribed(B))                 # user_id из params не читается

    # ---------- anon_info ----------
    def test_info_is_read_only(self):
        d = self.ok("anon_info", A)
        self.assertEqual((d["state"], d["topics"], d["posts"]), ("ok", 2, []))
        self.assertEqual((d["min"], d["max"]), (ap.MIN_LENGTH, ap.MAX_LENGTH))
        self.assertIn(str(ap.MIN_LENGTH), d["rules"])
        self.assertEqual(len(d["tips"]), 3)
        self.assertIsNone(_row(AnonymousUser, A))
        self.assertIsNone(_row(UserSettings, A))
        self.assertEqual(self.sent, [])

    def test_info_languages(self):
        for lang in ("uk", "ru", "de"):
            d = self.ok("anon_info", A, lang=lang)
            self.assertNotIn("[[", d["rules"] + "".join(d["tips"]) + d["intro"])
        self.assertNotEqual(self.ok("anon_info", A, lang="uk")["rules"], self.ok("anon_info", A, lang="de")["rules"])

    def test_info_without_topics(self):
        _clean()
        d = self.ok("anon_info", A)
        self.assertEqual(d["topics"], 0)
        self.assertTrue(d["no_topics"])
        status, body = self.call("anon_start", A)
        self.assertEqual((status, body["error"]), (422, "no_topics"))

    # ---------- полный путь ----------
    def test_full_flow_publishes_like_the_bot(self):
        ticket = self.start_and_pass()
        chk = self.ok("anon_check", A, {"ticket": ticket, "topic_id": self.topic, "text": "  " + GOOD + "  "})
        self.assertEqual((chk["topic"], chk["text"]), ("Здоров’я", GOOD))
        self.assertEqual(self.chat_posts(), [])                    # предпросмотр ничего не публикует
        res = self.ok("anon_send", A, {"ticket": ticket})
        posts = self.chat_posts()
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0][0], CHAT)
        self.assertEqual(posts[0][2].get("message_thread_id"), 5)
        self.assertIn(GOOD, posts[0][1])
        self.assertIn("Анонімне запитання", posts[0][1])
        self.assertTrue(res["post"]["link"].startswith("https://t.me/PotsdamChat/5/"))
        self.assertEqual([p[1] for p in _posts(A)], ["published"])
        admin = [s for s in self.sent if s[0] == ADMIN]
        self.assertEqual(len(admin), 1)                            # админу уведомление, как в боте
        self.assertIn(str(A), admin[0][1])
        self.assertTrue(_row(AnonymousUser, A).last_submission_at)
        # мои вопросы
        info = self.ok("anon_info", A)
        self.assertEqual(info["state"], "cooldown")
        self.assertEqual(len(info["posts"]), 1)
        p = info["posts"][0]
        self.assertEqual((p["status"], p["can_delete"], p["text"]), ("published", True, GOOD))
        self.assertTrue(p["link"])
        # повтор того же билета и новый вопрос
        self.refused("stale", "anon_send", A, {"ticket": ticket})
        self.refused("cooldown", "anon_start", A)
        self.assertEqual(len(self.chat_posts()), 1)

    def test_wrong_captcha_then_right(self):
        cap = self.ok("anon_start", A)["captcha"]
        res = self.ok("anon_captcha", A, {"token": cap["token"], "answer": self.solve(cap, right=False)})
        self.assertFalse(res["passed"])
        cap2 = res["captcha"]
        self.assertNotEqual(cap2["token"], cap["token"])
        self.refused("stale", "anon_captcha", A, {"token": cap["token"], "answer": 1})      # старая капча недействительна
        res = self.ok("anon_captcha", A, {"token": cap2["token"], "answer": self.solve(cap2)})
        self.assertTrue(res["passed"])
        self.assertEqual({t["name"] for t in res["topics"]}, {"Здоров’я", "Побут"})

    def test_three_wrong_answers_lock(self):
        cap = self.ok("anon_start", A)["captcha"]
        for i in range(3):
            if i < 2:
                res = self.ok("anon_captcha", A, {"token": cap["token"], "answer": self.solve(cap, right=False)})
                cap = res["captcha"]
            else:
                self.refused("locked", "anon_captcha", A, {"token": cap["token"], "answer": self.solve(cap, right=False)})
        self.refused("locked", "anon_start", A)
        self.assertEqual(self.ok("anon_info", A)["state"], "locked")

    def test_blocked_user(self):
        self.ok("anon_start", A)
        session = DBSession()
        try:
            session.query(AnonymousUser).get(A).is_blocked = True
            session.commit()
        finally:
            session.close()
        self.refused("blocked", "anon_start", A)
        self.assertEqual(self.ok("anon_info", A)["state"], "blocked")
        ticket = misc._FLOW[A]["ctx"].user_data["anonymous"]["submit_token"]
        self.refused("stale", "anon_send", A, {"ticket": ticket})

    def test_check_rules_and_duplicates(self):
        ticket = self.start_and_pass()
        for text, word in (("Коротко", None), ("Напишіть мені на https://example.com будь ласка прямо зараз", None),
                           ("a" * (ap.MAX_LENGTH + 1), None), ("Мій номер +49 151 23456789, зателефонуйте мені", None),
                           ("Допоможіть @some_user з одним питанням щодо оренди", None)):
            self.refused("invalid", "anon_check", A, {"ticket": ticket, "topic_id": self.topic, "text": text})
        self.assertEqual(self.chat_posts(), [])
        self.ok("anon_check", A, {"ticket": ticket, "topic_id": self.topic, "text": GOOD})
        self.ok("anon_send", A, {"ticket": ticket})
        ticket_b = self.start_and_pass(B)
        self.refused("duplicate", "anon_check", B, {"ticket": ticket_b, "topic_id": self.topic, "text": GOOD.upper()})
        self.assertEqual(len(self.chat_posts()), 1)

    def test_bad_params_and_stale_flow(self):
        self.refused("stale", "anon_check", A, {"ticket": "x", "topic_id": self.topic, "text": GOOD})   # без начатого вопроса
        self.refused("stale", "anon_send", A, {"ticket": "x"})
        ticket = self.start_and_pass()
        for bad in ({"ticket": ticket, "topic_id": "5", "text": GOOD}, {"ticket": ticket, "topic_id": 0, "text": GOOD},
                    {"ticket": ticket, "topic_id": True, "text": GOOD}, {"ticket": ticket, "topic_id": self.topic, "text": 5},
                    {"ticket": ticket, "topic_id": self.topic}):
            self.refused("bad_request", "anon_check", A, bad)
        self.refused("stale", "anon_check", A, {"ticket": "чужой", "topic_id": self.topic, "text": GOOD})
        self.refused("topic_gone", "anon_check", A, {"ticket": ticket, "topic_id": 999999, "text": GOOD})
        self.refused("stale", "anon_send", A, {"ticket": ticket})        # предпросмотр ещё не пройден
        for bad in ({"token": 5, "answer": 1}, {"token": "zz", "answer": 1}, {"token": "abcdef", "answer": "1"}, {"token": "abcdef", "answer": True}):
            self.refused("bad_request", "anon_captcha", A, bad)
        self.refused("bad_request", "anon_delete", A, {"id": "1"})
        self.refused("bad_request", "anon_delete", A, {})
        self.assertEqual(self.chat_posts(), [])

    def test_flow_is_per_user(self):
        ticket_a = self.start_and_pass(A)
        self.start_and_pass(B)
        self.refused("stale", "anon_check", B, {"ticket": ticket_a, "topic_id": self.topic, "text": GOOD})
        self.ok("anon_check", A, {"ticket": ticket_a, "topic_id": self.topic, "text": GOOD})
        self.refused("stale", "anon_send", C, {"ticket": ticket_a})

    def test_failed_send_keeps_the_limit_free(self):
        ticket = self.start_and_pass()
        self.ok("anon_check", A, {"ticket": ticket, "topic_id": self.topic, "text": GOOD})
        self.tg.send_message.side_effect = RuntimeError("telegram down")
        self.refused("failed", "anon_send", A, {"ticket": ticket})
        self.assertEqual([p[1] for p in _posts(A)], ["failed"])
        self.assertIsNone(_row(AnonymousUser, A).last_submission_at)
        self.assertEqual(self.ok("anon_info", A)["posts"], [])
        self.assertEqual(self.ok("anon_info", A)["state"], "ok")

    # ---------- удаление ----------
    def publish(self, user=A, text=GOOD):
        ticket = self.start_and_pass(user)
        self.ok("anon_check", user, {"ticket": ticket, "topic_id": self.topic, "text": text})
        return self.ok("anon_send", user, {"ticket": ticket})["post"]["id"]

    def test_delete_own_post(self):
        post_id = self.publish()
        self.tg.delete_message.reset_mock()
        d = self.ok("anon_delete", A, {"id": post_id})
        self.assertTrue(d["deleted"])
        self.tg.delete_message.assert_called_once()
        self.assertEqual([p[1] for p in _posts(A)], ["deleted"])
        info = self.ok("anon_info", A)
        self.assertEqual(info["posts"][0]["status"], "deleted")
        self.assertFalse(info["posts"][0]["can_delete"])
        self.refused("refused", "anon_delete", A, {"id": post_id})          # второй раз
        self.assertEqual(info["state"], "cooldown")                          # удаление не возвращает лимит (как в боте)

    def test_cannot_delete_foreign_or_missing_post(self):
        post_id = self.publish(A)
        self.tg.delete_message.reset_mock()
        self.refused("refused", "anon_delete", B, {"id": post_id})
        self.refused("refused", "anon_delete", B, {"id": 987654})
        self.tg.delete_message.assert_not_called()
        self.assertEqual([p[1] for p in _posts(A)], ["published"])
        self.assertEqual(self.ok("anon_info", B)["posts"], [])               # чужой вопрос в «моих» не виден

    def test_delete_window_expired(self):
        post_id = self.publish()
        session = DBSession()
        try:
            session.query(AnonymousPost).get(post_id).can_delete_until = datetime.utcnow() - timedelta(minutes=1)
            session.commit()
        finally:
            session.close()
        self.tg.delete_message.reset_mock()
        self.assertFalse(self.ok("anon_info", A)["posts"][0]["can_delete"])
        self.refused("refused", "anon_delete", A, {"id": post_id})
        self.tg.delete_message.assert_not_called()
        self.assertEqual([p[1] for p in _posts(A)], ["published"])


if __name__ == "__main__":
    unittest.main()
