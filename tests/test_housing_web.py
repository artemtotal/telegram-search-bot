"""Сайт підписника (етап 10, 02.11.2026): вхід посиланням із бота, пошуки,
статистика, історія. Сервер — справжній ThreadingHTTPServer на вільному порту."""

import http.client
import os
import re
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer
from unittest import mock
from urllib.parse import urlencode

from tests import test_housing_card as card_tests
from user_handlers import housing_monitor as hm
from user_handlers import housing_web
from user_jobs import housing_access_store, housing_letter_store, housing_tier


USER, ADMIN = card_tests.USER, card_tests.ADMIN


class HousingWebTest(card_tests.HousingCardTest):
    def setUp(self):
        super().setUp()
        engine_session = hm.DBSession
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        self.addCleanup(lambda: os.path.exists(tmp.name) and os.unlink(tmp.name))
        for patch in (
            mock.patch.object(housing_web, "DBSession", engine_session),
            mock.patch.object(housing_letter_store, "DBSession", engine_session),
            mock.patch.object(housing_access_store, "DBSession", engine_session),
            mock.patch.object(housing_web, "PUBLIC_URL", "https://example.ts.net"),
            mock.patch.object(housing_web, "SECRET_FILE", tmp.name),
            mock.patch.object(housing_web, "_secret_cache", None),
            mock.patch.object(housing_tier, "is_premium", side_effect=lambda uid: int(uid) == USER),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        os.unlink(tmp.name)  # секрет створиться сам
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), housing_web.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.cookie = ""

    def _req(self, method, path, form=None, cookie=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=20)
        headers = {}
        if cookie if cookie is not None else self.cookie:
            headers["Cookie"] = cookie if cookie is not None else self.cookie
        body = None
        if form is not None:
            body = urlencode(form, doseq=True)
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        data = response.read().decode("utf-8")
        result = (response.status, dict(response.getheaders()), data)
        conn.close()
        return result

    def _login(self, user_id=USER):
        link = housing_web.create_login_link(user_id)
        status, headers, _body = self._req("GET", link.replace("https://example.ts.net", ""), cookie="")
        self.assertEqual(status, 303)
        self.cookie = headers["Set-Cookie"].split(";")[0]
        return link

    def test_login_link_works_once_and_only_for_subscribers(self):
        self.assertEqual(self._req("GET", "/", cookie="")[0], 401)
        link = self._login()
        self.assertIn("HttpOnly", self._req("GET", "/")[1].get("Set-Cookie", "HttpOnly"))
        self.assertEqual(self._req("GET", "/")[0], 200)
        # Те саме посилання вдруге — ні.
        self.assertEqual(self._req("GET", link.replace("https://example.ts.net", ""), cookie="")[0], 403)
        # Хто не може користуватись моніторингом узагалі — ні.
        other = housing_web.create_login_link(777)
        with mock.patch.object(hm, "is_allowed", return_value=False):
            self.assertEqual(self._req("GET", other.replace("https://example.ts.net", ""), cookie="")[0], 403)

    def test_a_free_user_sees_part_of_the_site(self):
        free = 777
        housing_letter_store.log_letter(free, "immoscout", "1", "2-Zimmer in Mitte", "https://x/1", "Sehr geehrte …")
        with mock.patch.object(hm, "is_allowed", return_value=True):
            self._login(free)
            csrf = housing_web.csrf_token(free)
            status, headers, _ = self._req("POST", "/search/new", {"csrf": csrf})
            self.assertEqual(status, 303)
            home = self._req("GET", "/")[2]
            self.assertIn("promo", home)
            self.assertEqual(len(hm.user_searches(free)), 1)
            history = self._req("GET", "/history")[2]
            self.assertNotIn("Sehr geehrte", history)
            self.assertIn(housing_web._t("letters_paid", "uk"), history)
            self.assertEqual(self._req("GET", "/stats")[0], 200)
            # Другий пошук — лише з підпискою.
            self.assertEqual(self._req("POST", "/search/new", {"csrf": csrf})[0], 303)
            self.assertEqual(len(hm.user_searches(free)), 1)
            self.assertNotIn('action="/search/new"', self._req("GET", "/")[2])
        # Підписник банера не бачить.
        self._login(USER)
        self.assertNotIn('class="card promo"', self._req("GET", "/")[2])

    def test_a_forged_or_expired_cookie_is_refused(self):
        self.assertIsNone(housing_web.session_user("544675510.9999999999.deadbeef"))
        value = housing_web.session_cookie(USER)
        self.assertEqual(housing_web.session_user(value), USER)
        user, expires, sig = value.split(".")
        self.assertIsNone(housing_web.session_user(f"{ADMIN}.{expires}.{sig}"))
        with mock.patch.object(housing_web.time, "time", return_value=10 ** 11):
            self.assertIsNone(housing_web.session_user(value))

    def test_an_expired_link_is_refused(self):
        with mock.patch.object(housing_web, "utc_now", return_value=datetime.utcnow() - timedelta(hours=1)):
            link = housing_web.create_login_link(USER)
        self.assertEqual(self._req("GET", link.replace("https://example.ts.net", ""), cookie="")[0], 403)

    def test_searches_from_the_site_are_the_bot_s_searches(self):
        self._login()
        csrf = housing_web.csrf_token(USER)
        self.assertEqual(self._req("POST", "/search/new", {"csrf": "wrong"})[0], 403)
        status, headers, _ = self._req("POST", "/search/new", {"csrf": csrf})
        self.assertEqual(status, 303)
        search_id = int(re.search(r"/search/(\d+)", headers["Location"]).group(1))
        self.assertEqual(self._req("GET", f"/search/{search_id}")[0], 200)
        form = {"csrf": csrf, "place": "Berlin", "radius": "10", "max_price_eur": "900", "min_rooms": "2",
                "wbs": "none", "src": ["immoscout", "kleinanzeigen"], "berlin_coops": "1"}
        status, _headers, body = self._req("POST", f"/search/{search_id}", form)
        self.assertEqual(status, 303, body[:300])
        search = self._search()
        self.assertEqual(search["criteria"]["area"]["name"], "Berlin")
        self.assertEqual(search["criteria"]["area"]["radius_km"], 10)
        self.assertEqual(search["criteria"]["max_price_eur"], 900)
        self.assertEqual(search["criteria"]["wbs"], "none")
        self.assertEqual(sorted(hm._search_sources(search)), ["immoscout", "kleinanzeigen"])
        # 09.11.2026: правка на сайті записується як зміна фільтра (самоперевірка, зведення).
        session = hm.DBSession()
        try:
            edits = session.query(hm.HousingFilterEvent).filter_by(user_id=USER, edited=True).count()
        finally:
            session.close()
        self.assertEqual(edits, 1)
        # Невідоме місто — помилка, а пошук не змінюється.
        status, _headers, body = self._req("POST", f"/search/{search_id}", dict(form, place="Hamburg"))
        self.assertEqual(status, 400)
        self.assertIn("Hamburg", body)
        # Пауза й видалення.
        self._req("POST", f"/search/{search_id}/resume", {"csrf": csrf})
        self.assertFalse(hm._search_paused(self._search()))
        self._req("POST", f"/search/{search_id}/pause", {"csrf": csrf})
        self.assertTrue(hm._search_paused(self._search()))
        self.assertIn("Berlin + 10", self._req("GET", "/")[2])
        self.assertEqual(self._req("GET", "/stats")[0], 200)
        self.assertEqual(self._req("GET", "/history")[0], 200)
        self._req("POST", f"/search/{search_id}/delete", {"csrf": csrf})
        self.assertEqual(hm.user_searches(USER), [])

    def test_someone_else_s_search_is_not_found(self):
        self._login()
        csrf = housing_web.csrf_token(USER)
        self._req("POST", "/search/new", {"csrf": csrf})
        search_id = self._search()["search_id"]
        with mock.patch.object(housing_tier, "is_premium", return_value=True):
            other = housing_web.create_login_link(777)
            status, headers, _ = self._req("GET", other.replace("https://example.ts.net", ""), cookie="")
            cookie = headers["Set-Cookie"].split(";")[0]
            self.assertEqual(self._req("GET", f"/search/{search_id}", cookie=cookie)[0], 404)

    def test_letters_show_up_in_the_history(self):
        housing_letter_store.log_letter(USER, "immoscout", "1", "2-Zimmer in Mitte", "https://x/1", "Sehr geehrte …")
        self._login()
        body = self._req("GET", "/history")[2]
        self.assertIn("2-Zimmer in Mitte", body)
        self.assertIn("Sehr geehrte", body)


# Успадковані тести картки вже йдуть у HousingCardTest — тут лише свої.
for _name in [name for name in vars(card_tests.HousingCardTest) if name.startswith("test_")]:
    setattr(HousingWebTest, _name, None)


if __name__ == "__main__":
    unittest.main()
