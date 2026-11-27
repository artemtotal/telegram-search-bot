"""Мост сайта «Потсдам» (site_bridge): секрет, прокси, маршруты, коды ответов, user_id только из тела. Живой сервер на случайном порту."""

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from unittest import mock

import site_bridge
from site_bridge import Refuse, op
from user_handlers import housing_web

KEY = "k" * 64


@op("test", "echo")
def _echo(user_id, lang, params):
    return {"user_id": user_id, "lang": lang, "params": params}


@op("test", "refuse")
def _refuse(user_id, lang, params):
    raise Refuse("limit", "забагато")


@op("test", "boom")
def _boom(user_id, lang, params):
    raise RuntimeError("secret internals")


@op("test", "small", max_body=50)
def _small(user_id, lang, params):
    return {}


class BridgeServerTests(unittest.TestCase):
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
        fd, self.keyfile = tempfile.mkstemp()
        os.write(fd, (KEY + "\n").encode())
        os.close(fd)
        self.patch = mock.patch.object(site_bridge, "KEY_FILE", self.keyfile)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        os.remove(self.keyfile)

    def call(self, path, body=None, key=KEY, headers=None, method="POST", raw=None):
        data = raw if raw is not None else json.dumps(body if body is not None else {}).encode()
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path), data=data if method == "POST" else None, method=method)
        req.add_header("Content-Type", "application/json")
        if key is not None:
            req.add_header("X-Bridge-Key", key)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as err:
            raw_body = err.read()
            try:
                return err.code, json.loads(raw_body)
            except ValueError:
                return err.code, raw_body

    # --- доступ ---
    def test_ping_ok_with_key(self):
        status, body = self.call("/int/misc/ping", {"user_id": 0, "lang": "ru"})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertTrue(body["data"]["pong"])
        self.assertEqual(body["data"]["lang"], "ru")

    def test_no_key_is_403(self):
        status, body = self.call("/int/misc/ping", key=None)
        self.assertEqual((status, body["error"]), (403, "forbidden"))

    def test_wrong_key_is_403(self):
        status, body = self.call("/int/misc/ping", key="x" * 64)
        self.assertEqual((status, body["error"]), (403, "forbidden"))
        status, _ = self.call("/int/misc/ping", key=KEY[:-1])
        self.assertEqual(status, 403)

    def test_proxy_headers_are_404_even_with_right_key(self):
        for header in ("X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Proto", "Forwarded", "X-Real-Ip"):
            status, body = self.call("/int/misc/ping", headers={header: "1.2.3.4"})
            self.assertEqual((header, status, body["error"]), (header, 404, "not_found"))

    def test_bridge_off_without_key_file(self):
        with mock.patch.object(site_bridge, "KEY_FILE", self.keyfile + ".missing"):
            status, body = self.call("/int/misc/ping")
        self.assertEqual((status, body["error"]), (404, "not_found"))

    def test_empty_key_file_is_off(self):
        open(self.keyfile, "w").close()
        status, _ = self.call("/int/misc/ping", key="")
        self.assertEqual(status, 404)

    def test_get_is_404(self):
        status, _ = self.call("/int/misc/ping", method="GET")
        self.assertEqual(status, 404)

    # --- маршруты ---
    def test_unknown_domain_and_op_are_404(self):
        for path in ("/int/nope/ping", "/int/misc/nope", "/int/misc", "/int/", "/int/misc/ping/extra", "/int/../misc/ping"):
            status, body = self.call(path)
            self.assertEqual((path, status), (path, 404))

    def test_registered_area_ops(self):
        names = site_bridge.registered()
        self.assertIn("misc.ping", names)
        self.assertIn("housing.me", names)

    # --- тело и коды ---
    def test_bad_json_and_types_are_400(self):
        self.assertEqual(self.call("/int/test/echo", raw=b"{not json")[0], 400)
        self.assertEqual(self.call("/int/test/echo", raw=b"[1,2]")[0], 400)
        self.assertEqual(self.call("/int/test/echo", {"user_id": "5"})[0], 400)
        self.assertEqual(self.call("/int/test/echo", {"user_id": True})[0], 400)
        self.assertEqual(self.call("/int/test/echo", {"user_id": -3})[0], 400)
        self.assertEqual(self.call("/int/test/echo", {"user_id": 5, "params": [1]})[0], 400)

    def test_user_required_unless_declared(self):
        self.assertEqual(self.call("/int/test/echo", {})[0], 400)
        self.assertEqual(self.call("/int/test/echo", {"user_id": 0})[0], 400)
        self.assertEqual(self.call("/int/misc/ping", {})[0], 200)

    def test_too_large_is_413(self):
        status, body = self.call("/int/test/small", {"user_id": 1, "params": {"x": "y" * 100}})
        self.assertEqual((status, body["error"]), (413, "too_large"))
        self.assertEqual(self.call("/int/test/small", {"user_id": 1})[0], 200)

    def test_refuse_is_422_with_message(self):
        status, body = self.call("/int/test/refuse", {"user_id": 1})
        self.assertEqual((status, body), (422, {"ok": False, "error": "limit", "message": "забагато"}))

    def test_exception_is_500_without_details(self):
        status, body = self.call("/int/test/boom", {"user_id": 1})
        self.assertEqual((status, body), (500, {"ok": False, "error": "internal"}))

    # --- кто пользователь ---
    def test_user_comes_only_from_the_body_not_from_params(self):
        status, body = self.call("/int/test/echo", {"user_id": 111, "lang": "de", "params": {"user_id": 999, "uid": 999}})
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["user_id"], 111)
        self.assertEqual(body["data"]["lang"], "de")
        self.assertEqual(body["data"]["params"]["user_id"], 999)       # параметры лишь данные, операция обязана брать user_id из первого аргумента

    def test_bad_lang_falls_back_to_bot_language(self):
        with mock.patch("i18n.get_lang", return_value="de"):
            status, body = self.call("/int/test/echo", {"user_id": 5, "lang": "xx"})
        self.assertEqual(body["data"]["lang"], "de")

    # --- старый кабинет не задет ---
    def test_old_cabinet_routes_still_work(self):
        with urllib.request.urlopen("http://127.0.0.1:%d/health" % self.port, timeout=5) as resp:
            self.assertEqual((resp.status, resp.read()), (200, b"ok"))
        req = urllib.request.Request("http://127.0.0.1:%d/search/1/delete" % self.port, data=b"x=1", method="POST")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(ctx.exception.code, 410)         # 28.11.2026: кабинет выключен (H5a), формы отвечают «переехал»; /health и мост на месте
        ctx.exception.close()


class HousingMeTests(unittest.TestCase):
    def test_me_uses_existing_bot_functions(self):
        from site_bridge import housing
        search = {"search_id": 7, "items": [{"active": True}]}
        with mock.patch("user_handlers.housing_monitor.user_searches", return_value=[search]) as us, \
                mock.patch("user_handlers.housing_monitor._search_paused", return_value=False), \
                mock.patch("user_handlers.housing_monitor._search_sources", return_value=["immoscout"]), \
                mock.patch("user_handlers.housing_monitor.is_allowed", return_value=True), \
                mock.patch("user_jobs.housing_tier.is_premium", return_value=False), \
                mock.patch("user_jobs.housing_tier.free_delay_hours", return_value="2"), \
                mock.patch("user_jobs.housing_access_store.list_users", return_value=[]), \
                mock.patch("user_handlers.housing_found.is_found", return_value=False):
            data = housing.me(42, "uk", {"user_id": 1})
        us.assert_called_once_with(42)
        self.assertEqual(data["tier"], "free")
        self.assertEqual(data["delay_h"], "2")
        self.assertTrue(data["monitoring"])
        self.assertEqual(data["filters"], [{"id": 7, "on": True, "sources": ["immoscout"]}])


class StartParamTests(unittest.TestCase):
    """Параметры для плиток сайта открывают те же экраны бота; прежние параметры не затронуты."""

    def _run(self, arg):
        from user_handlers import chat_start
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(type="private"),
            message=SimpleNamespace(from_user=SimpleNamespace(id=1)),
        )
        context = SimpleNamespace(args=[arg])
        chat_start.start(update, context)
        return update, context

    def test_new_params_open_existing_screens(self):
        for arg, target in (("equeue", "user_handlers.equeue_monitor.show_menu"), ("econsul", "user_handlers.econsul_monitor.show_menu"),
                            ("support", "user_handlers.support.show_amounts"), ("anon", "user_handlers.anonymous_posts.show_anon_submenu")):
            with mock.patch(target) as screen, mock.patch("user_handlers.anonymous_posts.show_home") as home:
                update, context = self._run(arg)
            screen.assert_called_once_with(update, context)
            home.assert_not_called()

    def test_broken_screen_falls_back_to_home(self):
        with mock.patch("user_handlers.equeue_monitor.show_menu", side_effect=RuntimeError), \
                mock.patch("user_handlers.anonymous_posts.show_home") as home:
            self._run("equeue")
        home.assert_called_once()

    def test_old_params_still_route(self):
        with mock.patch("user_handlers.housing_monitor.show_menu") as menu:
            update, context = self._run("housing")
        menu.assert_called_once_with(update, context)
        with mock.patch("user_handlers.masters_hint.masters_command") as masters:
            update, context = self._run("masters")
        masters.assert_called_once_with(update, context)


if __name__ == "__main__":
    unittest.main()
