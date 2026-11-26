"""26.11.2026: builder pauses until the Gemini quota reset instead of retrying every 15 minutes."""
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from user_jobs import kb_builder, kb_common as kb  # noqa: E402


class KbPauseTests(unittest.TestCase):
    def test_next_reset_is_after_pacific_midnight(self):
        la = ZoneInfo("America/Los_Angeles")
        now = datetime(2026, 11, 25, 16, 52, tzinfo=ZoneInfo("Europe/Berlin"))
        reset = kb_builder.next_quota_reset(now)
        self.assertEqual(reset.astimezone(ZoneInfo("Europe/Berlin")).strftime("%d.%m %H:%M"), "26.11 09:05")
        self.assertEqual(reset.tzinfo.key, la.key)

    def test_failed_embedding_pauses_then_ticks_do_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "kb.db")
            with patch.object(kb, "KB_DB_PATH", db):
                store = kb.connect(db)
                self.assertFalse(kb_builder.paused(store))
                kb.set_meta(store, "embed_pause_until",
                            (datetime.now(ZoneInfo("UTC")) + timedelta(hours=1)).isoformat())
                self.assertTrue(kb_builder.paused(store))
                kb.set_meta(store, "embed_pause_until",
                            (datetime.now(ZoneInfo("UTC")) - timedelta(minutes=1)).isoformat())
                self.assertFalse(kb_builder.paused(store))
                store.close()


if __name__ == "__main__":
    unittest.main()
