import os
import unittest
from datetime import datetime, timedelta
from unittest import mock


class EqueueMonitorTest(unittest.TestCase):
    def test_parse_negative_text(self):
        from user_handlers.equeue_monitor import parse_availability

        available, reason = parse_availability("Наразі вільних місць немає")
        self.assertFalse(available)
        self.assertIn("немає", reason)

    def test_parse_positive_text(self):
        from user_handlers.equeue_monitor import parse_availability

        available, _reason = parse_availability("Є вільні терміни на завтра")
        self.assertTrue(available)

    def test_allowed_admin_and_extra_ids(self):
        with mock.patch.dict(
            os.environ,
            {
                "ADMIN_ID": "312029534",
                "PASSPORT_EQUEUE_ALLOWED_USER_IDS": "111, 222",
            },
            clear=False,
        ):
            import importlib
            import user_handlers.equeue_monitor as monitor

            importlib.reload(monitor)
            self.assertTrue(monitor.is_allowed(312029534))
            self.assertTrue(monitor.is_allowed(111))
            # «Open DP Document checks to all users» зняв білий список, тож чергу
            # бачить будь-який Telegram ID; закритим лишається тільки порожній.
            self.assertTrue(monitor.is_allowed(333))
            self.assertFalse(monitor.is_allowed(None))
            self.assertEqual(len(list(monitor.private_home_rows(111))), 1)

    def test_cloudflare_challenge_detection(self):
        from user_handlers.equeue_monitor import _looks_like_cloudflare_challenge

        self.assertTrue(_looks_like_cloudflare_challenge("Just a moment... cf_chl", 403))
        self.assertFalse(_looks_like_cloudflare_challenge("Just a moment... cf_chl", 200))

    def test_latest_status_uses_newest_browser_check_globally(self):
        """`_latest_status_text` prefers the live `EqueueStatus` singleton row
        (kept fresh by this same container's real scheduled browser check) and
        only falls back to `EqueueSubscription` rows when that singleton has
        no timestamp yet. The real row is neutralized for the duration of the
        test (not deleted) and always restored, since it reflects genuine
        production monitoring state that other code and users rely on.
        """
        import user_handlers.equeue_monitor as monitor

        session = monitor.DBSession()
        user_ids = [900000001, 900000002]
        status_row = session.query(monitor.EqueueStatus).filter(
            monitor.EqueueStatus.service == monitor.SERVICE_KEY
        ).first()
        original_status = None
        if status_row is not None:
            original_status = {
                "last_checked_at": status_row.last_checked_at,
                "last_status": status_row.last_status,
                "last_reason": status_row.last_reason,
            }
            status_row.last_checked_at = None
            session.commit()
        try:
            session.query(monitor.EqueueSubscription).filter(
                monitor.EqueueSubscription.user_id.in_(user_ids)
            ).delete(synchronize_session=False)
            old_row = monitor.EqueueSubscription(
                user_id=user_ids[0],
                username="old",
                display_name="Old",
                service=monitor.SERVICE_KEY,
                active=True,
                last_status="none",
                last_checked_at=datetime(2099, 1, 1, 8, 0, 0),
                created_at=datetime(2099, 1, 1, 8, 0, 0),
                updated_at=datetime(2099, 1, 1, 8, 0, 0),
            )
            newest_row = monitor.EqueueSubscription(
                user_id=user_ids[1],
                username="new",
                display_name="New",
                service=monitor.SERVICE_KEY,
                active=True,
                last_status="available",
                last_checked_at=datetime(2099, 1, 1, 10, 0, 0),
                created_at=datetime(2099, 1, 1, 10, 0, 0),
                updated_at=datetime(2099, 1, 1, 10, 0, 0),
            )
            session.add_all([old_row, newest_row])
            session.commit()

            text = monitor._latest_status_text(user_ids[0])
            self.assertIn("11:00", text)
            self.assertIn("є ознаки", text)
        finally:
            session.query(monitor.EqueueSubscription).filter(
                monitor.EqueueSubscription.user_id.in_(user_ids)
            ).delete(synchronize_session=False)
            if status_row is not None:
                status_row.last_checked_at = original_status["last_checked_at"]
                status_row.last_status = original_status["last_status"]
                status_row.last_reason = original_status["last_reason"]
            session.commit()
            session.close()


class EqueueAdminAlertCooldownTests(unittest.TestCase):
    """Regression: the cooldown used to compare against subscribers'
    `last_checked_at`, which is refreshed on every single check regardless
    of status - so once a status like "blocked" persisted for more than one
    check cycle, that timestamp was always "recent" and the admin never got
    a second alert, ever, no matter how long the outage lasted. It must
    instead track the moment the admin was actually last notified.
    """

    def setUp(self):
        import user_handlers.equeue_monitor as monitor

        self.monitor = monitor
        self.admin_id_patch = mock.patch.object(monitor, "ADMIN_ID", 312029534)
        self.admin_id_patch.start()
        session = monitor.DBSession()
        self.row = session.query(monitor.EqueueStatus).filter(
            monitor.EqueueStatus.service == monitor.SERVICE_KEY
        ).first()
        self.original_alert_at = self.row.last_admin_alert_at if self.row else None
        session.close()

    def tearDown(self):
        self.admin_id_patch.stop()
        session = self.monitor.DBSession()
        row = session.query(self.monitor.EqueueStatus).filter(
            self.monitor.EqueueStatus.service == self.monitor.SERVICE_KEY
        ).first()
        if row is not None:
            row.last_admin_alert_at = self.original_alert_at
            session.commit()
        session.close()

    def _set_last_alert_at(self, value):
        session = self.monitor.DBSession()
        row = session.query(self.monitor.EqueueStatus).filter(
            self.monitor.EqueueStatus.service == self.monitor.SERVICE_KEY
        ).first()
        if row is None:
            row = self.monitor.EqueueStatus(service=self.monitor.SERVICE_KEY)
            session.add(row)
        row.last_admin_alert_at = value
        session.commit()
        session.close()

    def test_blocked_status_alerts_immediately_the_first_time(self):
        self._set_last_alert_at(None)
        bot = mock.Mock()

        self.monitor._notify_admin_error(bot, {"status": "blocked", "reason": "сторінка показала Cloudflare"})

        bot.send_message.assert_called_once()
        text = bot.send_message.call_args.args[1]
        self.assertIn("вручну", text)
        self.assertIn("Cloudflare", text)

    def test_repeated_blocked_checks_do_not_spam_within_the_cooldown(self):
        self._set_last_alert_at(self.monitor.utc_now() - timedelta(hours=1))
        bot = mock.Mock()

        # Simulates many 15-minute checks in a row all still reporting
        # "blocked" - this used to keep finding a "recent" subscriber
        # timestamp forever and never re-alert.
        for _ in range(5):
            self.monitor._notify_admin_error(bot, {"status": "blocked", "reason": "still blocked"})

        bot.send_message.assert_not_called()

    def test_admin_gets_reminded_again_once_the_cooldown_passes(self):
        self._set_last_alert_at(self.monitor.utc_now() - self.monitor.ADMIN_ERROR_COOLDOWN - timedelta(minutes=1))
        bot = mock.Mock()

        self.monitor._notify_admin_error(bot, {"status": "blocked", "reason": "still blocked"})

        bot.send_message.assert_called_once()

    def test_non_blocked_errors_get_a_generic_message_not_the_captcha_hint(self):
        self._set_last_alert_at(None)
        bot = mock.Mock()

        self.monitor._notify_admin_error(bot, {"status": "error", "reason": "timeout"})

        text = bot.send_message.call_args.args[1]
        self.assertNotIn("вручну", text)
        self.assertIn("не виконана", text)


class EqueueSightingsTests(unittest.TestCase):
    """"Свободных мест не подтверждено" само по себе не отличает "мест сейчас
    нет" от "проверка молча сломалась неделю назад" - историю находок видно
    в меню, и по ней же считается тревога о слишком долгой тишине."""

    def setUp(self):
        import user_handlers.equeue_monitor as monitor

        self.monitor = monitor
        session = monitor.DBSession()
        # Тестовый сервис-ключ, чтобы не трогать реальные production-строки.
        self.original_service = monitor.SERVICE_KEY
        self.service_patch = mock.patch.object(monitor, "SERVICE_KEY", "test_equeue_sightings")
        self.service_patch.start()
        session.query(monitor.EqueueAvailableSighting).filter(
            monitor.EqueueAvailableSighting.service == "test_equeue_sightings"
        ).delete(synchronize_session=False)
        session.query(monitor.EqueueStatus).filter(
            monitor.EqueueStatus.service == "test_equeue_sightings"
        ).delete(synchronize_session=False)
        session.commit()
        session.close()

    def tearDown(self):
        session = self.monitor.DBSession()
        session.query(self.monitor.EqueueAvailableSighting).filter(
            self.monitor.EqueueAvailableSighting.service == "test_equeue_sightings"
        ).delete(synchronize_session=False)
        session.query(self.monitor.EqueueStatus).filter(
            self.monitor.EqueueStatus.service == "test_equeue_sightings"
        ).delete(synchronize_session=False)
        session.commit()
        session.close()
        self.service_patch.stop()

    def _add_sighting(self, found_at):
        session = self.monitor.DBSession()
        session.add(self.monitor.EqueueAvailableSighting(
            service="test_equeue_sightings", found_at=found_at, reason="test",
        ))
        session.commit()
        session.close()

    def test_menu_lists_the_most_recent_finds_newest_first(self):
        now = self.monitor.utc_now()
        for days in (5, 1, 3):
            self._add_sighting(now - timedelta(days=days))

        found = self.monitor.recent_sightings()

        self.assertEqual(len(found), 3)
        self.assertEqual(found, sorted(found, reverse=True))

    def test_only_the_configured_number_of_finds_is_kept_in_the_menu(self):
        now = self.monitor.utc_now()
        for days in range(1, 8):
            self._add_sighting(now - timedelta(days=days))

        self.assertEqual(
            len(self.monitor.recent_sightings()), self.monitor.SIGHTINGS_SHOWN,
        )

    def test_text_says_plainly_when_nothing_was_ever_found(self):
        text = self.monitor._sightings_text("ru")

        self.assertIn("ни разу", text)

    def test_a_long_quiet_stretch_alerts_the_admin(self):
        self._add_sighting(self.monitor.utc_now() - timedelta(hours=30))
        bot = mock.Mock()

        with mock.patch.object(self.monitor, "ADMIN_ID", 312029534):
            self.monitor._notify_admin_stale(bot)

        bot.send_message.assert_called_once()
        self.assertIn("давно не було", bot.send_message.call_args.args[1])

    def test_a_recent_find_keeps_the_admin_undisturbed(self):
        self._add_sighting(self.monitor.utc_now() - timedelta(hours=2))
        bot = mock.Mock()

        with mock.patch.object(self.monitor, "ADMIN_ID", 312029534):
            self.monitor._notify_admin_stale(bot)

        bot.send_message.assert_not_called()

    def test_the_stale_alert_does_not_repeat_on_every_check(self):
        self._add_sighting(self.monitor.utc_now() - timedelta(hours=30))
        bot = mock.Mock()

        # Проверки идут каждые 15 минут - без кулдауна это был бы поток
        # одинаковых сообщений весь день.
        with mock.patch.object(self.monitor, "ADMIN_ID", 312029534):
            for _ in range(4):
                self.monitor._notify_admin_stale(bot)

        bot.send_message.assert_called_once()

    def test_an_available_result_is_recorded_even_with_no_subscribers(self):
        bot = mock.Mock()
        payload = {
            "source": "test_equeue_sightings",
            "status": "available",
            "available": True,
            "reason": "є вільні терміни",
        }

        with mock.patch.object(self.monitor, "ADMIN_ID", 0), \
             mock.patch.object(self.monitor, "_active_subscribers", return_value=[]), \
             mock.patch.object(self.monitor, "_record_service_status"), \
             mock.patch.object(self.monitor, "_update_status_for_active"):
            self.monitor.handle_browser_result(bot, payload)

        self.assertEqual(len(self.monitor.recent_sightings()), 1)


class EqueueTranslationSmokeTests(unittest.TestCase):
    """Not a full duplicate of every uk-language assertion - just enough to
    prove lang actually reaches the rendered text for this module."""

    def test_menu_keyboard_in_russian_and_german(self):
        from user_handlers.equeue_monitor import _menu_keyboard

        ru_labels = [b.text for row in _menu_keyboard(False, lang='ru').inline_keyboard for b in row]
        de_labels = [b.text for row in _menu_keyboard(False, lang='de').inline_keyboard for b in row]

        self.assertIn('🔔 Подписаться на проверку', ru_labels)
        self.assertIn('🔔 Prüfung abonnieren', de_labels)

    def test_render_menu_in_russian_and_german(self):
        from user_handlers.equeue_monitor import _render_menu

        ru = _render_menu(True, lang='ru')
        de = _render_menu(True, lang='de')

        self.assertIn('включена', ru)
        self.assertIn('aktiviert', de)


if __name__ == "__main__":
    unittest.main()
