"""H5a (28.11.2026): «забыть про бота». Стартовое меню = одна кнопка сайта; под уведомлениями кнопки на сайт «Потсдам» внутри Telegram;
кнопка вместо голой ссылки в ответе справочника; старый кабинет выключен. Главное, что проверяем: ТЕКСТ уведомлений не меняется,
клавиатура корректна и укладывается в ограничения Telegram, а без ссылки Mini App или с выключателем всё уходит как раньше."""

import re
import unittest
from types import SimpleNamespace
from unittest import mock

import telegram
from telegram import InlineKeyboardMarkup

import i18n
from user_handlers import anonymous_posts, kb_search, masters_hint as mh
from user_jobs import housing_site_buttons, locals_monitor

URL = "https://example.test/"
APP = "https://t.me/Potsdambot/potsdam"


def _on(**extra):
    return mock.patch.multiple(mh, MASTERS_URL=URL, WEBAPP=True, APP_LINK=APP, START_MODE="site", SITE_BUTTONS=True, ASK_SITE=False, **extra)


def _walk(markup):
    return [button for row in markup.inline_keyboard for button in row]


def _check_limits(testcase, markup):
    """Ограничения Bot API: до 8 кнопок в ряду, до 100 всего; у URL-кнопки адрес до 2048 знаков и только http(s)/tg; текст кнопки не пустой."""
    buttons = _walk(markup)
    testcase.assertLessEqual(len(buttons), 100)
    for row in markup.inline_keyboard:
        testcase.assertLessEqual(len(row), 8)
    for button in buttons:
        testcase.assertTrue(button.text.strip())
        if button.url:
            testcase.assertLessEqual(len(button.url), 2048)
            testcase.assertRegex(button.url, r"^https?://")
        if button.callback_data:
            testcase.assertLessEqual(len(button.callback_data.encode("utf-8")), 64)


class StartMenuTests(unittest.TestCase):
    def _rows(self, **kwargs):
        with _on(), mock.patch.object(anonymous_posts, "MASTERS_URL", URL), mock.patch.object(i18n, "get_lang", return_value=kwargs.get("lang", "uk")):
            return anonymous_posts._home_keyboard(5)

    def test_one_big_web_app_button_then_anon_and_language_only(self):
        markup = self._rows()
        rows = markup.inline_keyboard
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0][0].text, "🌐 Відкрити Потсдамбот")
        self.assertEqual(rows[0][0].web_app.url, URL + "?s=bot")
        self.assertEqual(rows[1][0].callback_data, "anon:menu")
        self.assertEqual(rows[2][0].callback_data, "anon:lang:menu")
        self.assertEqual(rows[2][0].text, "🌐 Мова / Язык / Sprache")
        _check_limits(self, markup)

    def test_button_in_three_languages(self):
        texts = {lang: self._rows(lang=lang).inline_keyboard[0][0].text for lang in ("uk", "ru", "de")}
        self.assertEqual(texts, {"uk": "🌐 Відкрити Потсдамбот", "ru": "🌐 Открыть Потсдамбот", "de": "🌐 Potsdambot öffnen"})

    def test_old_buttons_are_gone_from_the_start_menu(self):
        callbacks = [b.callback_data for b in _walk(self._rows())]
        for old in ("equeue:menu", "econsul:menu", "housing:menu", "anon:feedback", "support:start"):
            self.assertNotIn(old, callbacks)
        self.assertFalse([b for b in _walk(self._rows()) if b.url])      # ни рекламы, ни каталога

    def test_classic_switch_returns_the_old_keyboard(self):
        with _on(), mock.patch.object(mh, "START_MODE", "classic"), mock.patch.object(anonymous_posts, "MASTERS_URL", URL):
            callbacks = [b.callback_data for b in _walk(anonymous_posts._home_keyboard(None))]
        self.assertIn("anon:feedback", callbacks)

    def test_web_app_off_gives_the_old_keyboard(self):
        with mock.patch.multiple(mh, MASTERS_URL=URL, WEBAPP=False), mock.patch.object(anonymous_posts, "MASTERS_URL", URL):
            callbacks = [b.callback_data for b in _walk(anonymous_posts._home_keyboard(None))]
        self.assertIn("anon:feedback", callbacks)

    def test_anon_goes_to_the_site_when_the_flag_is_on(self):
        with _on(), mock.patch.object(mh, "ASK_SITE", True), mock.patch.object(anonymous_posts, "MASTERS_URL", URL):
            button = anonymous_posts._home_keyboard(None).inline_keyboard[1][0]
        self.assertEqual(button.web_app.url, URL + "?s=bot&go=ask")

    def test_start_text_and_single_message(self):
        sent = []
        message = SimpleNamespace(reply_text=lambda text, **kw: sent.append((text, kw)))
        update = SimpleNamespace(effective_message=message, callback_query=None, effective_user=SimpleNamespace(id=5))
        with _on(), mock.patch.object(anonymous_posts, "MASTERS_URL", URL), mock.patch.object(i18n, "get_lang", return_value="ru"):
            anonymous_posts.show_home(update, SimpleNamespace(user_data={}))
        self.assertEqual(len(sent), 1)                       # одно сообщение, без нижней клавиатуры
        text, kw = sent[0]
        self.assertIn("Теперь всё в одном приложении", text)
        self.assertIsInstance(kw["reply_markup"], InlineKeyboardMarkup)
        self.assertEqual(kw["parse_mode"], "HTML")


class GoInfoTests(unittest.TestCase):
    def test_go_url_and_validation(self):
        with _on():
            self.assertEqual(mh.go_info("terms_dp").url, URL + "?s=bot&go=terms_dp")
            self.assertEqual(mh.go_info("").url, URL + "?s=bot")
            self.assertIsNone(mh.go_info("a b"))
            self.assertIsNone(mh.go_info("x" * 65))
        with mock.patch.multiple(mh, MASTERS_URL=URL, WEBAPP=False):
            self.assertIsNone(mh.go_info("housing"))

    def test_app_url_rules(self):
        with _on():
            self.assertEqual(mh.app_url("go_terms_dp"), APP + "?startapp=go_terms_dp")
            self.assertEqual(mh.app_url("go_housing_listing_123456789"), APP + "?startapp=go_housing_listing_123456789")
            self.assertEqual(mh.app_url("bad param"), "")
            self.assertEqual(mh.app_url("a" * 65), "")
        with mock.patch.object(mh, "APP_LINK", ""):
            self.assertEqual(mh.app_url("go_terms_dp"), "")


class SiteRowOnOldScreensTests(unittest.TestCase):
    def test_row_is_on_top_of_every_old_screen(self):
        from user_handlers import econsul_monitor, equeue_monitor, housing_monitor, support
        with _on():
            cases = [(housing_monitor._locked_keyboard("ru", 5), "go=housing"),
                     (equeue_monitor._menu_keyboard(False, "ru"), "go=terms_dp"),
                     (econsul_monitor._home_keyboard(set(), "ru"), "go=terms_consul"),
                     (support._amounts_keyboard("ru"), "go=support")]
            for markup, go in cases:
                first = markup.inline_keyboard[0][0]
                self.assertEqual(first.text, "📱 Удобнее на сайте: открыть")
                self.assertTrue(first.web_app.url.endswith(go), first.web_app.url)
                _check_limits(self, markup)

    def test_housing_menu_has_the_row_and_keeps_everything_else(self):
        from user_handlers import housing_monitor
        plain = housing_monitor._locked_keyboard("uk", 5)
        with _on():
            marked = housing_monitor._locked_keyboard("uk", 5)
        self.assertEqual([[b.callback_data for b in row] for row in marked.inline_keyboard[1:]],
                         [[b.callback_data for b in row] for row in plain.inline_keyboard])

    def test_no_row_when_mini_app_is_off(self):
        from user_handlers import housing_monitor
        with mock.patch.multiple(mh, MASTERS_URL=URL, WEBAPP=False):
            markup = housing_monitor._locked_keyboard("uk", 5)
        self.assertFalse([b for b in _walk(markup) if b.web_app])


class ListingKeyboardTests(unittest.TestCase):
    def setUp(self):
        self.patches = [
            mock.patch("user_jobs.housing_letter_store.ref_for", return_value=4711),
            mock.patch("user_jobs.housing_links.link_for", side_effect=lambda uid, src, key, url: ("https://bot.test/o/tok" if url else url)),
            mock.patch.object(i18n, "get_lang", return_value="uk"),
        ]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_two_url_buttons(self):
        with _on():
            markup = housing_site_buttons.listing_keyboard(5, "locals", "k1", "https://portal.test/x")
        buttons = _walk(markup)
        self.assertEqual([b.text for b in buttons], ["🏠 Відкрити в Потсдамботі", "🔗 Оголошення на сайті"])
        self.assertEqual(buttons[0].url, APP + "?startapp=go_housing_listing_4711")
        self.assertEqual(buttons[1].url, "https://bot.test/o/tok")
        self.assertRegex(buttons[0].url.split("startapp=")[1], r"^[A-Za-z0-9_-]{1,64}$")
        self.assertTrue(all(b.callback_data is None and b.web_app is None for b in buttons))      # URL-кнопки, в личке допустимы
        _check_limits(self, markup)

    def test_three_languages(self):
        names = {}
        for lang in ("ru", "de"):
            with _on(), mock.patch.object(i18n, "get_lang", return_value=lang):
                names[lang] = [b.text for b in _walk(housing_site_buttons.listing_keyboard(5, "locals", "k1", "https://p.test/x"))]
        self.assertEqual(names["ru"], ["🏠 Открыть в Потсдамботе", "🔗 Объявление на сайте"])
        self.assertEqual(names["de"], ["🏠 In Potsdambot öffnen", "🔗 Anzeige auf der Website"])

    def test_no_source_button_without_an_address(self):
        with _on():
            markup = housing_site_buttons.listing_keyboard(5, "immowelt", "k1", "")
        self.assertEqual(len(_walk(markup)), 1)

    def test_none_without_app_link_or_with_switch_or_on_error(self):
        with _on(), mock.patch.object(mh, "APP_LINK", ""):
            self.assertIsNone(housing_site_buttons.listing_keyboard(5, "locals", "k1", "https://p.test/x"))
        with _on(), mock.patch.object(mh, "SITE_BUTTONS", False):
            self.assertIsNone(housing_site_buttons.listing_keyboard(5, "locals", "k1", "https://p.test/x"))
        with _on(), mock.patch("user_jobs.housing_letter_store.ref_for", side_effect=RuntimeError("db")):
            self.assertIsNone(housing_site_buttons.listing_keyboard(5, "locals", "k1", "https://p.test/x"))
        with _on(), mock.patch("user_jobs.housing_letter_store.ref_for", return_value=None):
            self.assertIsNone(housing_site_buttons.listing_keyboard(5, "locals", "k1", "https://p.test/x"))

    def test_long_ref_still_fits_startapp(self):
        with _on(), mock.patch("user_jobs.housing_letter_store.ref_for", return_value=10 ** 15):
            markup = housing_site_buttons.listing_keyboard(5, "locals", "k1", "")
        self.assertLessEqual(len(_walk(markup)[0].url.split("startapp=")[1]), 64)

    def test_terms_keyboard(self):
        with _on():
            dp = housing_site_buttons.terms_keyboard(5, "dp")
            consul = housing_site_buttons.terms_keyboard(5, "consul")
            self.assertIsNone(housing_site_buttons.terms_keyboard(5, "other"))
        self.assertEqual(_walk(dp)[0].url, APP + "?startapp=go_terms_dp")
        self.assertEqual(_walk(consul)[0].url, APP + "?startapp=go_terms_consul")
        self.assertEqual(_walk(dp)[0].text, "🏠 Відкрити в Потсдамботі")
        with mock.patch.object(mh, "APP_LINK", ""):
            self.assertIsNone(housing_site_buttons.terms_keyboard(5, "dp"))


class _Bot:
    def __init__(self):
        self.messages, self.photos, self.albums = [], [], []

    def send_message(self, chat_id, text=None, **kwargs):
        self.messages.append({"chat_id": chat_id, "text": text, **kwargs})

    def send_photo(self, **kwargs):
        self.photos.append(kwargs)

    def send_media_group(self, **kwargs):
        self.albums.append(kwargs)


class SendListingTests(unittest.TestCase):
    """Тот же _send_listing, что и у остальных мониторов: подпись и порядок отправки прежние, клавиатура только добавляется."""

    def setUp(self):
        patcher = mock.patch.object(i18n, "get_lang", return_value="uk")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.markup = InlineKeyboardMarkup([[telegram.InlineKeyboardButton("x", url="https://t.me/Potsdambot/potsdam?startapp=go_housing_listing_1")]])
        self.listing = {"listing_key": "k1"}

    def _send(self, photos, text="T" * 100, markup=None):
        bot = _Bot()
        with mock.patch.object(locals_monitor, "_fetch_gallery", return_value=photos):
            kwargs = {"reply_markup": markup} if markup is not None else {}
            result = locals_monitor._send_listing(bot, 5, self.listing, text, **kwargs)
        return bot, result

    def test_single_photo_keeps_the_caption_and_gets_the_keyboard(self):
        bot, result = self._send(["https://p.test/1.jpg"], markup=self.markup)
        self.assertTrue(result)
        self.assertEqual(bot.photos[0]["caption"], "T" * 100)
        self.assertIs(bot.photos[0]["reply_markup"], self.markup)
        self.assertEqual(bot.messages, [])

    def test_without_keyboard_the_calls_are_exactly_as_before(self):
        bot, _ = self._send(["https://p.test/1.jpg"])
        self.assertNotIn("reply_markup", bot.photos[0])
        bot, _ = self._send(["https://p.test/1.jpg", "https://p.test/2.jpg"])
        self.assertEqual(bot.messages, [])

    def test_album_has_no_keyboard_so_a_short_message_follows(self):
        bot, result = self._send(["https://p.test/1.jpg", "https://p.test/2.jpg"], markup=self.markup)
        self.assertTrue(result)
        self.assertEqual(len(bot.albums), 1)
        self.assertEqual(bot.albums[0]["media"][0].caption, "T" * 100)
        self.assertEqual(len(bot.messages), 1)
        self.assertIs(bot.messages[0]["reply_markup"], self.markup)
        self.assertTrue(bot.messages[0]["disable_notification"])
        self.assertEqual(bot.messages[0]["text"], "⬆️ Кнопки до цієї квартири")

    def test_album_follow_up_failure_does_not_lose_the_flat(self):
        bot = _Bot()
        bot.send_message = mock.Mock(side_effect=RuntimeError("blocked"))
        with mock.patch.object(locals_monitor, "_fetch_gallery", return_value=["https://p.test/1.jpg", "https://p.test/2.jpg"]):
            self.assertTrue(locals_monitor._send_listing(bot, 5, self.listing, "T" * 100, reply_markup=self.markup))

    def test_long_text_goes_to_the_caller_which_adds_the_keyboard(self):
        bot, result = self._send(["https://p.test/1.jpg"], text="T" * 5000, markup=self.markup)
        self.assertFalse(result)                                # подпись не влезла: текст уйдёт отдельным сообщением с клавиатурой
        self.assertNotIn("reply_markup", bot.photos[0])        # у фото без подписи кнопок нет


class CheckJobEndToEndTests(unittest.TestCase):
    """check_job целиком (как в test_locals_monitor): текст уведомления одинаков с кнопками и без них."""

    def _run(self, app_link):
        from tests.test_locals_monitor import FakeBot, FakeResponse, _listing
        listing = _listing("l-1")
        filt = {"filter_id": 9, "user_id": 544675510, "max_price_eur": 2000.0}
        bot = FakeBot()
        with mock.patch.object(locals_monitor, "CHECK_ENABLED", True), \
                mock.patch.multiple(mh, APP_LINK=app_link, SITE_BUTTONS=True), \
                mock.patch("user_jobs.housing_letter_store.ref_for", return_value=4711), \
                mock.patch("user_jobs.housing_tier.free_footer", return_value="\n\nFREE"), \
                mock.patch("user_jobs.housing_links.free_social_footer", return_value=""), \
                mock.patch("requests.get", return_value=FakeResponse("x")), \
                mock.patch("user_jobs.locals_monitor.locals_parser.parse_listings", return_value=[listing]), \
                mock.patch("user_jobs.locals_monitor.locals_store.record_status"), \
                mock.patch("user_jobs.locals_monitor.locals_store.upsert_listings", return_value=1), \
                mock.patch("user_jobs.locals_monitor.locals_store.list_active_listings", return_value=[listing]), \
                mock.patch("user_jobs.locals_monitor.locals_store.list_filters", return_value=[filt]), \
                mock.patch("user_jobs.locals_monitor.locals_store.delivered_pairs", return_value=set()), \
                mock.patch("user_jobs.locals_monitor.locals_store.mark_delivered"):
            result = locals_monitor.check_job(SimpleNamespace(bot=bot))
        return bot, result

    def test_text_is_identical_and_buttons_are_added(self):
        plain, r1 = self._run("")
        marked, r2 = self._run(APP)
        self.assertEqual((r1["sent"], r2["sent"]), (1, 1))
        self.assertEqual(plain.sent[0][1], marked.sent[0][1])                     # ни знака в тексте не изменилось
        self.assertNotIn("reply_markup", plain.sent[0][2])
        markup = marked.sent[0][2]["reply_markup"]
        self.assertEqual(_walk(markup)[0].url, APP + "?startapp=go_housing_listing_4711")
        self.assertEqual(_walk(markup)[1].url, "https://locals.de/immobilien/l-1")
        _check_limits(self, markup)


class TermsNotificationTests(unittest.TestCase):
    def test_equeue_notification_text_and_old_buttons_unchanged_site_button_last(self):
        from user_handlers import equeue_monitor
        for app, expect_extra in ((APP, True), ("", False)):
            bot = mock.Mock()
            with mock.patch.multiple(mh, APP_LINK=app, SITE_BUTTONS=True, MASTERS_URL=URL), mock.patch.object(i18n, "get_lang", return_value="uk"):
                equeue_monitor._notify_available(bot, [(5, "none", None)], {})
            args, kwargs = bot.send_message.call_args
            rows = kwargs["reply_markup"].inline_keyboard
            self.assertEqual([b.callback_data for row in rows[:3] for b in row], [None, "equeue:booked", "equeue:unsubscribe"])
            self.assertEqual(len(rows), 4 if expect_extra else 3)
            if expect_extra:
                self.assertEqual(rows[3][0].url, APP + "?startapp=go_terms_dp")
            self.assertEqual(args[1], i18n.t("equeue.notify.available", "uk", title=equeue_monitor.html.escape(equeue_monitor.SERVICE_TITLE)))
            _check_limits(self, kwargs["reply_markup"])

    def test_econsul_has_the_consul_button(self):
        from user_handlers import econsul_monitor
        bot = mock.Mock()
        fresh = {"svc": [{"date": "2026-12-01"}]}
        with mock.patch.multiple(mh, APP_LINK=APP, SITE_BUTTONS=True), mock.patch.object(i18n, "get_lang", return_value="uk"), \
                mock.patch.object(econsul_monitor, "_subscribers_for", return_value={5: {"svc"}}), \
                mock.patch.object(econsul_monitor, "_notify_text", return_value="TEXT"):
            econsul_monitor._notify_subscribers(bot, fresh, {"svc": "Service"}, "Inst")
        args, kwargs = bot.send_message.call_args
        self.assertEqual(args[1], "TEXT")
        rows = kwargs["reply_markup"].inline_keyboard
        self.assertEqual(rows[-1][0].url, APP + "?startapp=go_terms_consul")
        self.assertEqual(rows[2][0].callback_data, "econsul:menu")
        _check_limits(self, kwargs["reply_markup"])


class LetterLinkTests(unittest.TestCase):
    def test_letter_link_goes_to_the_site_section_when_app_link_is_set(self):
        from user_jobs import housing_letter_store as store
        with mock.patch.multiple(mh, APP_LINK=APP, SITE_BUTTONS=True):
            self.assertEqual(store.letter_href("Potsdambot", 55), APP + "?startapp=go_letter_55")
        with mock.patch.multiple(mh, APP_LINK="", SITE_BUTTONS=True):
            self.assertEqual(store.letter_href("Potsdambot", 55), "https://t.me/Potsdambot?start=lt55")
        with mock.patch.multiple(mh, APP_LINK=APP, SITE_BUTTONS=False):
            self.assertEqual(store.letter_href("Potsdambot", 55), "https://t.me/Potsdambot?start=lt55")


class ProactiveAnswerTests(unittest.TestCase):
    def test_no_bare_catalog_link_in_the_text(self):
        with mock.patch.multiple(mh, MASTERS_URL=URL, MODE="on", APP_LINK=APP):
            text = kb_search._proactive_text("1. Варіант", "Підкажіть, будь ласка, хорошого стоматолога в Потсдамі?", "Potsdambot")
        self.assertNotIn("http", text)
        self.assertNotIn("Увесь каталог", text)

    def test_catalog_button_under_the_message(self):
        question = "Підкажіть, будь ласка, хорошого стоматолога в Потсдамі?"
        with mock.patch.multiple(mh, MASTERS_URL=URL, MODE="on", APP_LINK=APP):
            markup = kb_search._proactive_markup(question)
        button = _walk(markup)[0]
        self.assertEqual(button.text, "🧰 Відкрити каталог")
        self.assertEqual(button.url, APP + "?startapp=cat_health")
        with mock.patch.multiple(mh, MASTERS_URL=URL, MODE="on", APP_LINK=APP):
            self.assertIsNone(kb_search._proactive_markup("Яка сьогодні погода у Потсдамі?"))
        with mock.patch.multiple(mh, MASTERS_URL=URL, MODE="off", APP_LINK=APP):
            self.assertIsNone(kb_search._proactive_markup(question))


class OldCabinetTests(unittest.TestCase):
    def test_switch(self):
        from user_handlers import housing_web
        with mock.patch.dict("os.environ", {"HOUSING_WEB_CABINET": ""}), mock.patch.object(mh, "_file_cfg", return_value={}):
            self.assertFalse(housing_web.cabinet_enabled())
        with mock.patch.dict("os.environ", {"HOUSING_WEB_CABINET": ""}), mock.patch.object(mh, "_file_cfg", return_value={"cabinet": "on"}):
            self.assertTrue(housing_web.cabinet_enabled())
        with mock.patch.dict("os.environ", {"HOUSING_WEB_CABINET": "on"}):
            self.assertTrue(housing_web.cabinet_enabled())

    def test_moved_page_links_to_the_mini_app(self):
        from user_handlers import housing_web
        with mock.patch.object(mh, "APP_LINK", APP):
            page = housing_web.moved_page()
        self.assertIn("Кабінет переїхав у Telegram", page)
        self.assertIn("Кабинет переехал в Telegram", page)
        self.assertIn("Das Konto ist in Telegram umgezogen", page)
        self.assertIn(f'href="{APP}"', page)

    def test_login_button_gives_the_mini_app_instead_of_a_cabinet_link(self):
        from user_handlers import housing_web
        query = mock.Mock()
        update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=5))
        context = SimpleNamespace(bot=mock.Mock())
        with mock.patch.dict("os.environ", {"HOUSING_WEB_CABINET": ""}), mock.patch.object(mh, "_file_cfg", return_value={}), \
                mock.patch("user_handlers.housing_site.send_link") as send_link, \
                mock.patch.object(housing_web, "create_login_link") as create:
            housing_web.send_login_link(update, context)
        send_link.assert_called_once()
        create.assert_not_called()


class TranslationsTests(unittest.TestCase):
    def test_site_keys_exist_in_all_languages(self):
        for key in ("site.btn.open", "site.btn.better", "site.btn.app", "site.btn.source", "site.btn.album_note", "site.start.text",
                    "site.cabinet.moved", "site.cabinet.text", "site.cabinet.open"):
            values = {lang: i18n.t(key, lang) for lang in ("uk", "ru", "de")}
            for value in values.values():
                self.assertNotIn("[[", value)
            self.assertEqual(len(set(values.values())), 3, key)

    def test_menu_button_is_called_potsdam(self):
        for lang in ("uk", "ru", "de"):
            self.assertIn(i18n.t("masters.menu.btn", lang), ("Потсдам", "Potsdam"))


if __name__ == "__main__":
    unittest.main()
