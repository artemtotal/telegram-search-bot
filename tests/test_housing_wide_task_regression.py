"""Панель житла більше не читає завдання браузера як фільтри користувачів.

Після переходу приймача на широкий обхід `/api/housing/tasks` віддає одне
зведене завдання без `filter_id`, `user_id` і `last_checked_at`. Панель усе ще
читала його як список фільтрів, і це давало три симптоми одразу: «перевірка ще
не запускалась», «немає активних фільтрів» у всіх акаунтів і падіння адмінки на
`int(None)`.
"""

import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest import mock

from user_handlers import housing_monitor


# Те, що приймач справді віддає на GET /api/housing/tasks при активному фільтрі.
WIDE_BROWSER_TASK = {
    "source": "immowelt",
    "mode": "wide",
    "url": "https://www.immowelt.de/classified-search?distributionTypes=Rent",
    "known_listing_keys": [],
    "minimum_known": 15,
}

HOUSING_FILTER = {
    "filter_id": 3,
    "user_id": 312029534,
    "title": "Пошук Артема",
    "source": "immowelt",
    "url": "https://www.immowelt.de/classified-search?x=1",
    "active": True,
    "initialized": True,
    "last_checked_at": "2026-08-15T08:30:00+00:00",
    "seen_count": 8,
}

SECOND_ACCOUNT_FILTER = {**HOUSING_FILTER, "filter_id": 4, "user_id": 5115109366, "title": "Другий акаунт"}


class WideTaskRegressionTests(unittest.TestCase):
    def test_admin_panel_opens_when_an_immowelt_filter_is_active(self):
        """`int(None)` на зведеному завданні валив колбек, і адмінка не відкривалась."""

        with mock.patch.object(housing_monitor, '_tasks', return_value=[WIDE_BROWSER_TASK]), \
                mock.patch.object(housing_monitor, '_all_immowelt_filters', return_value=[HOUSING_FILTER]), \
                mock.patch.object(housing_monitor.propotsdam_store, 'list_filters', return_value=[]):
            text = housing_monitor._render_admin()

        self.assertIn("#3", text)
        self.assertIn("312029534", text)
        self.assertIn("Пошук Артема", text)

    def test_admin_panel_survives_a_filter_without_an_id(self):
        with mock.patch.object(housing_monitor, '_all_immowelt_filters', return_value=[{"user_id": 1}]), \
                mock.patch.object(housing_monitor.propotsdam_store, 'list_filters', return_value=[]):
            text = housing_monitor._render_admin()

        self.assertIn("#?", text)

    def test_admin_panel_marks_a_disabled_filter(self):
        disabled = {**HOUSING_FILTER, "active": False}
        with mock.patch.object(housing_monitor, '_all_immowelt_filters', return_value=[disabled]), \
                mock.patch.object(housing_monitor.propotsdam_store, 'list_filters', return_value=[]):
            text = housing_monitor._render_admin()

        self.assertIn("вимкнено", text)

    def test_every_account_sees_its_own_filter(self):
        # The bot-native sources beyond ProPotsdam don't matter to this test,
        # but this admin ID is a real production account with real filters
        # on them — leaving these unmocked would leak that live state in.
        with mock.patch.object(housing_monitor, '_tasks', return_value=[WIDE_BROWSER_TASK]), \
                mock.patch.object(
                    housing_monitor, '_all_immowelt_filters',
                    return_value=[HOUSING_FILTER, SECOND_ACCOUNT_FILTER],
                ), \
                mock.patch.object(housing_monitor.propotsdam_store, 'list_filters', return_value=[]), \
                mock.patch.object(housing_monitor.semmelhaack_store, 'list_filters', return_value=[]), \
                mock.patch.object(housing_monitor.schoba_store, 'list_filters', return_value=[]), \
                mock.patch.object(housing_monitor.regiomakler_store, 'list_filters', return_value=[]), \
                mock.patch.object(housing_monitor.kleinanzeigen_store, 'list_filters', return_value=[]), \
                mock.patch.object(housing_monitor.locals_store, 'list_filters', return_value=[]), \
                mock.patch.object(housing_monitor.karlmarx_store, 'list_filters', return_value=[]):
            first = housing_monitor.user_filters(312029534)
            second = housing_monitor.user_filters(5115109366)
            stranger = housing_monitor.user_filters(999)

        self.assertEqual([item["filter_id"] for item in first], [3])
        self.assertEqual([item["filter_id"] for item in second], [4])
        self.assertEqual(stranger, [])

    def test_disabled_filter_is_not_offered_as_active(self):
        disabled = {**HOUSING_FILTER, "active": False}
        with mock.patch.object(housing_monitor, '_all_immowelt_filters', return_value=[disabled]), \
                mock.patch.object(housing_monitor.propotsdam_store, 'list_filters', return_value=[]), \
                mock.patch.object(housing_monitor.semmelhaack_store, 'list_filters', return_value=[]), \
                mock.patch.object(housing_monitor.schoba_store, 'list_filters', return_value=[]), \
                mock.patch.object(housing_monitor.regiomakler_store, 'list_filters', return_value=[]), \
                mock.patch.object(housing_monitor.kleinanzeigen_store, 'list_filters', return_value=[]), \
                mock.patch.object(housing_monitor.locals_store, 'list_filters', return_value=[]), \
                mock.patch.object(housing_monitor.karlmarx_store, 'list_filters', return_value=[]):
            self.assertEqual(housing_monitor.user_filters(312029534), [])

    def test_status_uses_the_receiver_scan_time(self):
        now = datetime(2026, 8, 15, 12, 0, 0, tzinfo=housing_monitor.BERLIN_TZ)
        status = {
            "ok": True,
            "immowelt_last_check_at": now.isoformat(),
            "immowelt_last_error": "",
            "immowelt_last_skip_reason": "",
        }
        with mock.patch.object(housing_monitor, '_all_immowelt_filters', return_value=[HOUSING_FILTER]), \
                mock.patch.object(housing_monitor, '_receiver_status', return_value=status), \
                mock.patch.object(housing_monitor, '_now_berlin', return_value=now):
            lines = housing_monitor._immowelt_status_lines()

        self.assertIn("🟢 Immowelt: перевірка щойно", lines[0])
        self.assertNotIn("ще не запускалась", " ".join(lines))

    def test_status_shows_why_the_check_stopped_arriving(self):
        now = datetime(2026, 8, 15, 12, 0, 0, tzinfo=housing_monitor.BERLIN_TZ)
        status = {
            "ok": True,
            "immowelt_last_check_at": "2026-08-15T06:00:00+00:00",
            "immowelt_last_error": "Вкладка не відповіла на запит до сторінки",
        }
        with mock.patch.object(housing_monitor, '_all_immowelt_filters', return_value=[HOUSING_FILTER]), \
                mock.patch.object(housing_monitor, '_receiver_status', return_value=status), \
                mock.patch.object(housing_monitor, '_now_berlin', return_value=now):
            lines = housing_monitor._immowelt_status_lines()

        joined = " ".join(lines)
        self.assertIn("🔴", joined)
        self.assertIn("Вкладка не відповіла", joined)

    def test_status_falls_back_to_filter_time_on_an_old_receiver(self):
        """Приймач без нових полів не повинен ховати перевірку зовсім."""
        # HOUSING_FILTER.last_checked_at = 2026-08-15T08:30:00+00:00 UTC = 10:30 Берлін (CEST).
        now = datetime(2026, 8, 15, 10, 40, 0, tzinfo=housing_monitor.BERLIN_TZ)

        with mock.patch.object(housing_monitor, '_all_immowelt_filters', return_value=[HOUSING_FILTER]), \
                mock.patch.object(housing_monitor, '_receiver_status', return_value={"ok": True}), \
                mock.patch.object(housing_monitor, '_now_berlin', return_value=now):
            lines = housing_monitor._immowelt_status_lines()

        joined = " ".join(lines)
        self.assertNotIn("ще не запускалась", joined)
        self.assertIn("хв тому", joined)

    def test_status_says_plainly_when_no_filter_is_active(self):
        with mock.patch.object(housing_monitor, '_all_immowelt_filters', return_value=[]), \
                mock.patch.object(housing_monitor, '_receiver_status', return_value={"ok": True}):
            self.assertEqual(housing_monitor._immowelt_status_lines(), ["⚪ Immowelt: активних фільтрів немає."])


if __name__ == '__main__':
    unittest.main()
