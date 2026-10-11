"""Сторож зависшего обхода в колекторі ProPotsdam (tools/propotsdam_receiver.py).

26.09.2026 обхід повис усередині Playwright: HTTP-сервер далі відповідав 202,
а нових результатів не було два тижні.
"""

import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

try:
    from tools import propotsdam_receiver
except ModuleNotFoundError as exc:  # playwright живе на хості, не в контейнері бота
    propotsdam_receiver = None
    _IMPORT_ERROR = exc
else:
    _IMPORT_ERROR = None


@unittest.skipIf(
    propotsdam_receiver is None,
    f"колектор ProPotsdam доступний лише на хості: {_IMPORT_ERROR}",
)
class HungScanGuardTests(unittest.TestCase):
    def _state(self, running, started_minutes_ago):
        started = None
        if started_minutes_ago is not None:
            started = (datetime.now(timezone.utc) - timedelta(minutes=started_minutes_ago)).isoformat()
        return mock.patch.multiple(propotsdam_receiver, _scan_running=running, _scan_started_at=started)

    def test_a_normal_scan_is_left_alone(self):
        with self._state(True, 3):
            self.assertEqual(propotsdam_receiver._scan_overdue(), 0)

    def test_idle_receiver_is_never_overdue(self):
        with self._state(False, 60):
            self.assertEqual(propotsdam_receiver._scan_overdue(), 0)

    def test_a_scan_past_the_hard_limit_is_overdue(self):
        minutes = propotsdam_receiver.SCAN_HARD_LIMIT_SECONDS // 60 + 5
        with self._state(True, minutes):
            self.assertGreater(propotsdam_receiver._scan_overdue(), 0)

    def test_guard_runs_with_the_server(self):
        import inspect

        self.assertIn("_hung_scan_guard", inspect.getsource(propotsdam_receiver.main))


if __name__ == "__main__":
    unittest.main()
