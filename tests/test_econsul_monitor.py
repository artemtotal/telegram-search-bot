import json
import os
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import mock

import user_handlers.econsul_monitor as monitor
from database import (
    DBSession,
    EconsulDay,
    EconsulService,
    EconsulSnapshot,
    EconsulState,
    EqueueAvailableSighting,
    EqueueSubscription,
)


ADMIN = 900100001
USER_ALL = 900100002
USER_PASSPORT = 900100003
USER_OTHER = 900100004


def _payload(days_by_code, status="ok"):
    return {
        "source": "econsul_berlin",
        "status": status,
        "institution": {"code": "u1", "name": "Посольство України у ФРН"},
        "services": [
            {"code": code, "name": name, "minutes": 20, "served": True, "days": days}
            for code, (name, days) in days_by_code.items()
        ],
    }


def _day(date, count, first="09:00"):
    return {"date": date, "count": count, "first": first}


class EconsulMonitorTest(unittest.TestCase):
    def setUp(self):
        self._clean()
        self.bot = mock.Mock()
        patcher = mock.patch.object(monitor, "ADMIN_ID", ADMIN)
        patcher.start()
        self.addCleanup(patcher.stop)
        lang = mock.patch.object(monitor.i18n, "get_lang", return_value="uk")
        lang.start()
        self.addCleanup(lang.stop)
        self.addCleanup(self._clean)

    def _clean(self):
        session = DBSession()
        try:
            session.query(EconsulState).delete()
            session.query(EconsulService).delete()
            session.query(EconsulDay).delete()
            session.query(EconsulSnapshot).delete()
            session.query(EqueueAvailableSighting).filter(
                EqueueAvailableSighting.service == monitor.SOURCE
            ).delete(synchronize_session=False)
            session.query(EqueueSubscription).filter(
                EqueueSubscription.service.like(monitor.SUB_PREFIX + "%")
            ).delete(synchronize_session=False)
            session.commit()
        finally:
            session.close()

    def _subscribe(self, user_id, action):
        session = DBSession()
        try:
            monitor._subscribe(session, SimpleNamespace(id=user_id, username="", full_name=""), {"*"} if action == "all" else {action})
            session.commit()
        finally:
            session.close()

    def _sent_to(self):
        return [call.args[0] for call in self.bot.send_message.call_args_list]

    def test_first_result_is_a_baseline_without_notifications(self):
        self._subscribe(USER_ALL, "all")

        result = monitor.handle_browser_result(
            self.bot, _payload({"7": ("Паспорт", [_day("2026-10-14", 5)])})
        )

        self.assertTrue(result["baseline"])
        self.bot.send_message.assert_not_called()

    def test_new_date_notifies_only_matching_subscribers(self):
        monitor.handle_browser_result(self.bot, _payload({
            "7": ("Паспорт", [_day("2026-10-14", 5)]),
            "8": ("Облік", []),
        }))
        self._subscribe(USER_ALL, "all")
        self._subscribe(USER_PASSPORT, "7")
        self._subscribe(USER_OTHER, "8")

        result = monitor.handle_browser_result(self.bot, _payload({
            "7": ("Паспорт", [_day("2026-10-14", 5), _day("2026-10-21", 12, "10:40")]),
            "8": ("Облік", []),
        }))

        self.assertEqual(result["new_days"], 1)
        self.assertCountEqual(self._sent_to(), [USER_ALL, USER_PASSPORT])
        text = self.bot.send_message.call_args_list[0].args[1]
        self.assertIn("21.10.2026", text)
        self.assertIn("10:40", text)
        self.assertNotIn("14.10.2026", text)

    def test_more_free_times_on_a_known_date_notify_but_flicker_does_not(self):
        monitor.handle_browser_result(self.bot, _payload({"7": ("Паспорт", [_day("2026-10-14", 5)])}))
        self._subscribe(USER_ALL, "all")

        monitor.handle_browser_result(self.bot, _payload({"7": ("Паспорт", [_day("2026-10-14", 4)])}))
        monitor.handle_browser_result(self.bot, _payload({"7": ("Паспорт", [_day("2026-10-14", 5)])}))
        self.bot.send_message.assert_not_called()

        monitor.handle_browser_result(self.bot, _payload({"7": ("Паспорт", [_day("2026-10-14", 7)])}))
        self.assertEqual(self._sent_to(), [USER_ALL])

    def test_a_date_that_was_taken_and_freed_again_is_news(self):
        monitor.handle_browser_result(self.bot, _payload({"7": ("Паспорт", [_day("2026-10-14", 1)])}))
        self._subscribe(USER_ALL, "all")

        monitor.handle_browser_result(self.bot, _payload({"7": ("Паспорт", [])}))
        monitor.handle_browser_result(self.bot, _payload({"7": ("Паспорт", [_day("2026-10-14", 1)])}))

        self.assertEqual(self._sent_to(), [USER_ALL])

    def test_auth_problem_alerts_admin_once_then_reports_recovery(self):
        monitor.handle_browser_result(self.bot, _payload({"7": ("Паспорт", [])}))

        broken = {"source": "econsul_berlin", "status": "auth_required", "reason": "токена нет"}
        monitor.handle_browser_result(self.bot, broken)
        monitor.handle_browser_result(self.bot, broken)
        self.assertEqual(self._sent_to(), [ADMIN])
        self.assertIn("потрібен вхід", self.bot.send_message.call_args.args[1])

        monitor.handle_browser_result(self.bot, _payload({"7": ("Паспорт", [])}))
        self.assertEqual(self._sent_to(), [ADMIN, ADMIN])
        self.assertIn("знову працює", self.bot.send_message.call_args.args[1])

    def test_a_new_breakage_is_reported_even_within_the_cooldown(self):
        ok = _payload({"7": ("Паспорт", [])})
        broken = {"source": "econsul_berlin", "status": "auth_required", "reason": "401"}
        monitor.handle_browser_result(self.bot, ok)
        monitor.handle_browser_result(self.bot, broken)
        monitor.handle_browser_result(self.bot, ok)
        monitor.handle_browser_result(self.bot, broken)

        # тривога, «знову працює», і знову тривога - без мовчання через кулдаун
        self.assertEqual(self._sent_to(), [ADMIN, ADMIN, ADMIN])
        self.assertIn("потрібен вхід", self.bot.send_message.call_args.args[1])

    def test_unsubscribing_one_service_keeps_the_rest(self):
        monitor.handle_browser_result(self.bot, _payload({
            "7": ("Паспорт", []),
            "8": ("Облік", []),
            "9": ("Довіреність", []),
        }))
        self._subscribe(USER_PASSPORT, "7")
        self._subscribe(USER_PASSPORT, "8")

        session = DBSession()
        try:
            monitor._unsubscribe(session, SimpleNamespace(id=USER_PASSPORT, username="", full_name=""), "8")
            session.commit()
            codes = monitor._active_codes(monitor._user_subscriptions(session, USER_PASSPORT))
        finally:
            session.close()
        self.assertEqual(codes, {"7"})

        text, keyboard = monitor.render_manage(USER_PASSPORT, "uk")
        callbacks = [row[0].callback_data for row in keyboard.inline_keyboard]
        self.assertEqual(callbacks, ["econsul:u:7", "econsul:menu"])
        self.assertIn("❌", keyboard.inline_keyboard[0][0].text)

    def test_subscribing_to_all_replaces_single_services(self):
        self._subscribe(USER_ALL, "7")
        self._subscribe(USER_ALL, "all")

        session = DBSession()
        try:
            codes = monitor._active_codes(monitor._user_subscriptions(session, USER_ALL))
        finally:
            session.close()
        self.assertEqual(codes, {"*"})

    def test_typical_lifetime_ignores_dates_that_simply_arrived(self):
        now = datetime(2026, 10, 1, 12, 0)
        session = DBSession()
        try:
            for hours in (1, 2, 3):
                session.add(EconsulDay(
                    service_code="7", date="2026-10-20", count=0,
                    first_seen_at=now - timedelta(hours=hours + 1), gone_at=now - timedelta(hours=1),
                ))
            # Дата настала - її не розбирали, просто перевірка вже рахує від завтра.
            session.add(EconsulDay(
                service_code="7", date="2026-10-01", count=0,
                first_seen_at=now - timedelta(days=5), gone_at=now - timedelta(hours=1),
            ))
            session.commit()

            lifetime = monitor.typical_lifetime(session, now=now)
        finally:
            session.close()

        self.assertEqual(lifetime, timedelta(hours=2))

    def test_menu_is_admin_only_until_made_public(self):
        with mock.patch.object(monitor, "PUBLIC", False), \
                mock.patch.dict(os.environ, {"ECONSUL_ALLOWED_USER_IDS": str(USER_PASSPORT)}):
            self.assertTrue(monitor.is_allowed(ADMIN))
            self.assertTrue(monitor.is_allowed(USER_PASSPORT))
            self.assertFalse(monitor.is_allowed(USER_OTHER))
        with mock.patch.object(monitor, "PUBLIC", True):
            self.assertTrue(monitor.is_allowed(USER_OTHER))
            self.assertFalse(monitor.is_allowed(None))

    def test_main_menu_lists_only_own_subscriptions(self):
        monitor.handle_browser_result(self.bot, _payload({
            "7": ("Паспорт", [_day("2026-10-21", 3, "11:20"), _day("2026-10-14", 5)]),
            "8": ("Облік", []),
        }))
        self._subscribe(USER_PASSPORT, "7")

        text, keyboard = monitor.render_menu(USER_PASSPORT, "uk")

        self.assertIn("<b>Паспорт</b> — найближчий вільний 14.10.2026 09:00", text)
        self.assertNotIn("Облік", text)
        self.assertNotIn("Вхід e-Consul", text)
        callbacks = [row[0].callback_data for row in keyboard.inline_keyboard]
        self.assertEqual(callbacks[:3], ["econsul:add", "econsul:manage", "econsul:free"])

    def test_menu_without_subscriptions_invites_to_subscribe(self):
        text, keyboard = monitor.render_menu(USER_OTHER, "uk")

        self.assertIn("ще немає підписок", text)
        callbacks = [row[0].callback_data for row in keyboard.inline_keyboard]
        self.assertNotIn("econsul:manage", callbacks)

    def test_picker_hides_taken_services_and_shows_subscribe_only_after_a_pick(self):
        monitor.handle_browser_result(self.bot, _payload({
            "7": ("Паспорт", []),
            "8": ("Облік", []),
            "9": ("Довіреність", []),
        }))
        self._subscribe(USER_PASSPORT, "7")

        _text, keyboard = monitor.render_picker(USER_PASSPORT, "uk", set())
        callbacks = [row[0].callback_data for row in keyboard.inline_keyboard]
        self.assertEqual(callbacks, ["econsul:add_all", "econsul:p:9", "econsul:p:8", "econsul:menu"])

        _text, keyboard = monitor.render_picker(USER_PASSPORT, "uk", {"8"})
        labels = [row[0].text for row in keyboard.inline_keyboard]
        self.assertIn("✅ Облік", labels)
        self.assertIn("✅ Підписатися (1)", labels)

    def test_login_link_is_one_message_that_gets_updated_and_closed(self):
        self.bot.send_message.return_value = SimpleNamespace(message_id=77)
        link = {"source": "econsul_berlin", "status": "login_link", "login_id": "a1",
                "link": "https://mbnk.app/nbi123", "wait_minutes": 30}

        monitor.handle_browser_result(self.bot, link)
        monitor.handle_browser_result(self.bot, dict(link, link="https://mbnk.app/nbi456"))
        monitor.handle_browser_result(self.bot, {"source": "econsul_berlin", "status": "login_done", "login_id": "a1"})

        [sent] = self.bot.send_message.call_args_list
        self.assertEqual(sent.args[0], ADMIN)
        button = sent.kwargs["reply_markup"].inline_keyboard[0][0]
        self.assertEqual(button.url, "https://mbnk.app/nbi123")
        edits = self.bot.edit_message_text.call_args_list
        self.assertEqual(edits[0].kwargs["reply_markup"].inline_keyboard[0][0].url, "https://mbnk.app/nbi456")
        self.assertIn("підтверджено", edits[1].args[0])
        self.assertTrue(all(call.kwargs["message_id"] == 77 for call in edits))

    def test_login_messages_do_not_touch_the_check_status(self):
        monitor.handle_browser_result(self.bot, _payload({"7": ("Паспорт", [])}))
        self.bot.send_message.return_value = SimpleNamespace(message_id=5)
        monitor.handle_browser_result(self.bot, {"source": "econsul_berlin", "status": "login_link",
                                                 "login_id": "b", "link": "monobank://x"})

        session = DBSession()
        try:
            state = session.query(EconsulState).first()
            self.assertEqual(state.last_status, "ok")
        finally:
            session.close()
        # не-http посилання Telegram не прийме кнопкою - воно йде текстом
        self.assertIn("monobank://x", self.bot.send_message.call_args.args[1])

    def test_every_successful_check_is_kept_for_statistics(self):
        monitor.handle_browser_result(self.bot, _payload({"7": ("Паспорт", [_day("2026-10-14", 5)])}))
        monitor.handle_browser_result(self.bot, _payload({"7": ("Паспорт", [])}))

        session = DBSession()
        try:
            rows = session.query(EconsulSnapshot).order_by(EconsulSnapshot.id).all()
            data = [json.loads(row.services) for row in rows]
        finally:
            session.close()
        self.assertEqual(data[-2:], [[{"code": "7", "days": [["2026-10-14", 5, "09:00"]]}],
                                     [{"code": "7", "days": []}]])

if __name__ == "__main__":
    unittest.main()
