"""28.11.2026: сайт закрыл соединение, не дождавшись ответа моста (housing.search_save шёл 10,5 с при таймауте сайта 8 с):
без Traceback, одна строка warning."""
import logging
import unittest
from types import SimpleNamespace
from unittest import mock

import site_bridge


class ClientGone(unittest.TestCase):
    def _handler(self, exc):
        return SimpleNamespace(path="/int/housing/search_save", headers={}, command="POST", exc=exc)

    def test_broken_pipe_is_a_warning_not_a_traceback(self):
        for exc in (BrokenPipeError(32, "Broken pipe"), ConnectionResetError(), ConnectionAbortedError()):
            handler = self._handler(exc)
            with mock.patch.object(site_bridge, "_dispatch", side_effect=exc), \
                    mock.patch.object(site_bridge, "_reply") as reply, \
                    self.assertLogs(site_bridge.logger, "WARNING") as logs:
                self.assertTrue(site_bridge.handle(handler))
            reply.assert_not_called()
            self.assertEqual(len(logs.records), 1)
            self.assertIsNone(logs.records[0].exc_info)
            self.assertEqual(logs.records[0].levelno, logging.WARNING)

    def test_other_failures_still_answer_500(self):
        with mock.patch.object(site_bridge, "_dispatch", side_effect=RuntimeError("x")), \
                mock.patch.object(site_bridge, "_reply") as reply, \
                self.assertLogs(site_bridge.logger, "ERROR"):
            site_bridge.handle(self._handler(None))
        self.assertEqual(reply.call_args.args[1], 500)


