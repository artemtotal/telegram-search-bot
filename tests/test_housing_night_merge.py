"""25.11.2026, злиття потоків A/B/C (pb-night): стики, які жоден потік не перевіряв сам.

- одне очікування тексту за раз (housing_text_wait) і вихід кнопкою нижнього меню;
- меню житла: усі нові пункти, по два в ряд, не більше 7 рядів;
- «⏳ Цю квартиру вже зняли» + справжній рядок «відкрили / подались»;
- пошук, що пролежав на паузі довше доби, при вмиканні не віддає накопичене (housing_resume_guard);
- ціни в новині беруться з тих самих змінних, що й кнопки оплати.
"""

import importlib.util
import os
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database import Base, KleinanzeigenListing
from user_handlers import anonymous_posts, housing_monitor as hm
from user_jobs import housing_resume_guard, housing_text_wait, housing_tier, kleinanzeigen_store

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("news25", os.path.join(_ROOT, "tools", "news_2026_11_25_features.py"))
news = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(news)

USER = 6262


class TextWaitTests(unittest.TestCase):
    def _all_waits(self):
        return {
            housing_text_wait.SUPPORT: datetime.utcnow(), housing_text_wait.LETTER: {"step": 0},
            housing_text_wait.MAPPE: {"field": "name"}, housing_text_wait.TRACKER: 5,
            housing_text_wait.SURVEY: {"at": datetime.utcnow()}, housing_text_wait.CARD: {"sid": 3, "input": "min_rooms"},
        }

    def test_a_new_wait_drops_every_other_one(self):
        for key in housing_text_wait.KEYS + (housing_text_wait.CARD,):
            data = self._all_waits()
            housing_text_wait.claim(data, key)
            left = {k for k in housing_text_wait.KEYS if k in data}
            if key == housing_text_wait.CARD:
                self.assertEqual(left, set())
                self.assertEqual(data[housing_text_wait.CARD].get("input"), "min_rooms")
            else:
                self.assertEqual(left, {key})
                # Картка пошуку лишається (кнопки працюють), зникає лише очікування числа.
                self.assertEqual(data[housing_text_wait.CARD], {"sid": 3})

    def test_clear_all_and_none(self):
        data = self._all_waits()
        housing_text_wait.clear_all(data)
        self.assertEqual({k for k in housing_text_wait.KEYS if k in data}, set())
        self.assertEqual(data[housing_text_wait.CARD], {"sid": 3})
        housing_text_wait.clear_all(None)  # без падіння

    def test_bottom_menu_button_leaves_any_wait(self):
        data = self._all_waits()
        data.pop(housing_text_wait.SUPPORT)  # своя сума має власне вікно; тут не про неї
        data.pop(housing_text_wait.SURVEY)
        message = SimpleNamespace(text=anonymous_posts.i18n.t("anon.kb.housing", "uk"), chat=SimpleNamespace(type="private"),
                                  reply_text=mock.Mock())
        update = SimpleNamespace(message=message, effective_user=SimpleNamespace(id=USER),
                                 effective_message=message)
        context = SimpleNamespace(user_data=data, bot=mock.Mock())
        with mock.patch.object(anonymous_posts.i18n, "get_lang", return_value="uk"), \
                mock.patch.object(anonymous_posts.housing_monitor, "show_menu") as show_menu:
            anonymous_posts.handle_private_text(update, context)
        show_menu.assert_called_once()
        self.assertEqual({k for k in housing_text_wait.KEYS if k in data}, set())
        self.assertNotIn("input", data[housing_text_wait.CARD])

    def test_tracker_date_wait_does_not_eat_the_mappe_answer(self):
        from user_handlers import housing_mappe, housing_tracker

        data = {housing_tracker.STATE_KEY: 7}
        context = SimpleNamespace(user_data=data, bot=mock.Mock())
        with mock.patch.object(housing_mappe.store, "get_answers", return_value={}), \
                mock.patch.object(housing_mappe, "_next"):
            housing_mappe._start_questions(context.bot, context, USER, None, False, False)
        self.assertNotIn(housing_tracker.STATE_KEY, data)
        self.assertIn(housing_mappe.STATE_KEY, data)


class MenuTests(unittest.TestCase):
    def _menu(self, premium, found=False):
        env = {"HOUSING_APPLY_TRACKER": "1", "HOUSING_REFERRAL_ENABLED": "1", "HOUSING_FOUND_ENABLED": "1"}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(hm, "ADMIN_ID", 1), mock.patch.object(hm, "is_allowed", return_value=True), \
                mock.patch.object(hm.housing_tier, "is_premium", return_value=premium), \
                mock.patch.object(hm.housing_mappe, "ENABLED", True), \
                mock.patch.object(hm.housing_found, "is_found", return_value=found):
            return hm._menu_keyboard(USER, "uk").inline_keyboard

    def test_every_new_item_fits_in_seven_rows(self):
        for premium in (False, True):
            for found in (False, True):
                rows = self._menu(premium, found)
                data = [b.callback_data for row in rows for b in row]
                self.assertLessEqual(len(rows), 7, (premium, found))
                self.assertTrue(all(len(row) <= 3 for row in rows), (premium, found))
                for callback in ("housing:self_manage", "housing:self_add", "housing:current_matches", "hap:list:0",
                                 "housing:letter:menu", "housing:mappe:menu", "housing:web", "housing:faq",
                                 "housing:lang:menu", "anon:home"):
                    self.assertIn(callback, data, (premium, found, callback))
                self.assertEqual([b.callback_data for b in rows[-1]][-1], "anon:home")
                if premium:
                    self.assertIn("housing:found:resume" if found else "housing:found", data)
                    self.assertIn("housing:invite", data)
                else:
                    self.assertIn("housing:access_request", data)
                    self.assertNotIn("housing:found", data)

    def test_daily_items_come_first(self):
        rows = self._menu(True)
        self.assertEqual([b.callback_data for b in rows[0]], ["housing:self_manage", "housing:self_add"])
        self.assertEqual([b.callback_data for b in rows[1]], ["housing:current_matches", "hap:list:0"])


class MissedSocialLineTests(unittest.TestCase):
    def test_the_real_line_is_added_only_when_there_is_one(self):
        from user_jobs import housing_links

        with mock.patch.object(housing_tier.i18n, "get_lang", return_value="uk"), \
                mock.patch.object(housing_links, "listing_social_line", return_value="⚡ Підписники отримали о 14:05 · відкрили: 3") as line:
            text, _ = housing_tier.missed_notice(USER, "Kleinanzeigen", "2 Zi · 700 €", listing_key="k1", source="kleinanzeigen")
        line.assert_called_once_with("k1", "uk", "kleinanzeigen")
        self.assertIn("відкрили: 3", text)
        with mock.patch.object(housing_tier.i18n, "get_lang", return_value="uk"), \
                mock.patch.object(housing_links, "listing_social_line", return_value=""):
            text, _ = housing_tier.missed_notice(USER, "Kleinanzeigen", "2 Zi · 700 €", listing_key="k1", source="kleinanzeigen")
        self.assertNotIn("відкрили", text)
        self.assertNotIn("Підписники отримали", text)

    def test_a_broken_social_line_never_stops_the_notice(self):
        from user_jobs import housing_links

        with mock.patch.object(housing_tier.i18n, "get_lang", return_value="uk"), \
                mock.patch.object(housing_links, "listing_social_line", side_effect=RuntimeError("db")):
            text, keyboard = housing_tier.missed_notice(USER, "Kleinanzeigen", "x", listing_key="k1", source="kleinanzeigen")
        self.assertTrue(text)
        self.assertIsNotNone(keyboard)


class ResumeGuardTests(unittest.TestCase):
    MODULES = (kleinanzeigen_store, housing_resume_guard)

    def setUp(self):
        self.engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        factory = sessionmaker(bind=self.engine)
        self._saved = [(m, m.DBSession) for m in self.MODULES]
        for module in self.MODULES:
            module.DBSession = factory
        self.session = factory()
        self.env = mock.patch.dict(os.environ, {"HOUSING_RESUME_REBASE": "1", "HOUSING_RESUME_REBASE_HOURS": "24"})
        self.env.start()
        self._listing("old1")
        self.filter_id = kleinanzeigen_store.create_filter(USER, "Пошук", max_price_eur=1000)

    def tearDown(self):
        self.env.stop()
        self.session.close()
        for module, session in self._saved:
            module.DBSession = session
        self.engine.dispose()

    def _listing(self, key):
        self.session.add(KleinanzeigenListing(
            listing_key=key, title='Wohnung', address='Potsdam', rooms=2, area_m2=50, price_eur=700,
            detail_url=f'https://x/{key}', first_seen_at=datetime.utcnow(), last_seen_at=datetime.utcnow(),
            is_active=True))
        self.session.commit()

    def _unsent(self):
        return [listing["listing_key"] for _f, listing in kleinanzeigen_store.select_unsent_matches(
            kleinanzeigen_store.list_active_listings(), kleinanzeigen_store.list_filters(active_only=True),
            kleinanzeigen_store.delivered_pairs())]

    def _pause(self, ago):
        hm._set_one_filter_active("kleinanzeigen", self.filter_id, False, USER)
        row = self.session.query(housing_resume_guard.HousingFilterPause).get(("kleinanzeigen", self.filter_id))
        self.assertIsNotNone(row)
        row.paused_at = datetime.utcnow() - ago
        self.session.commit()

    def test_a_long_pause_does_not_flood_on_resume(self):
        self._pause(timedelta(days=14))
        for key in ("w1", "w2", "w3"):
            self._listing(key)          # накопичилось за паузу
        hm._set_one_filter_active("kleinanzeigen", self.filter_id, True, USER)
        self.assertEqual(self._unsent(), [])
        self._listing("new")            # нове після вмикання - приходить
        self.assertEqual(self._unsent(), ["new"])
        self.assertIsNone(self.session.query(housing_resume_guard.HousingFilterPause).get(("kleinanzeigen", self.filter_id)))

    def test_a_short_pause_keeps_what_was_missed(self):
        self._pause(timedelta(hours=2))
        self._listing("w1")
        hm._set_one_filter_active("kleinanzeigen", self.filter_id, True, USER)
        self.assertEqual(self._unsent(), ["w1"])

    def test_a_pause_from_before_the_guard_counts_as_long(self):
        kleinanzeigen_store.set_filter_active(self.filter_id, False, user_id=USER)  # без запису паузи
        self._listing("w1")
        housing_resume_guard.before_activate("kleinanzeigen", self.filter_id, USER)
        kleinanzeigen_store.set_filter_active(self.filter_id, True, user_id=USER)
        self.assertEqual(self._unsent(), [])

    def test_resume_of_all_filters_after_found_uses_the_guard(self):
        self._pause(timedelta(days=3))
        self._listing("w1")
        with mock.patch.object(hm, "_all_immowelt_filters", return_value=[]), \
                mock.patch.object(hm, "_sync_propot_filters"):
            hm._set_all_filters_active_for_user(USER, True)
        self.assertEqual(self._unsent(), [])

    def test_switched_off_guard_behaves_as_before(self):
        self._pause(timedelta(days=14))
        self._listing("w1")
        with mock.patch.dict(os.environ, {"HOUSING_RESUME_REBASE": "0"}):
            hm._set_one_filter_active("kleinanzeigen", self.filter_id, True, USER)
        self.assertEqual(self._unsent(), ["w1"])

    def test_already_active_filter_is_not_rebased(self):
        self._listing("w1")
        self.assertEqual(housing_resume_guard.before_activate("kleinanzeigen", self.filter_id, USER), 0)
        self.assertEqual(self._unsent(), ["w1"])


class NewsPriceTests(unittest.TestCase):
    def test_prices_follow_the_package_variables(self):
        from user_jobs import housing_packages

        with mock.patch.dict(housing_packages.PRICES_EUR, {housing_packages.M1: 6, housing_packages.M3: 14,
                                                           housing_packages.UNTIL_FOUND: 17}):
            text = news.compose("uk", ["price"], False)
        self.assertIn("1 місяць — 6 €", text)
        self.assertIn("3 місяці — 14 €", text)
        self.assertIn("17 € разово", text)
        self.assertIn("5 €", news.compose("uk", ["price"], False))


if __name__ == "__main__":
    unittest.main()
