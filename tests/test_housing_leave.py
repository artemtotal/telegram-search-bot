"""Зведення 21:00: хто заблокував бота / вимкнув моніторинг житла (07.11.2026)."""

import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database import Base
from user_handlers import housing_leave, housing_monitor


def _member_update(chat_id, old, new, chat_type="private"):
    m = SimpleNamespace(chat=SimpleNamespace(id=chat_id, type=chat_type),
                        old_chat_member=SimpleNamespace(status=old), new_chat_member=SimpleNamespace(status=new))
    return SimpleNamespace(my_chat_member=m)


class HousingLeaveTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self._saved = [(m, m.DBSession) for m in (housing_leave, housing_monitor)]
        for m, _ in self._saved:
            m.DBSession = sessionmaker(bind=self.engine)
        self.filters = {111: [{"filter_id": 1}], 222: [], 333: [{"filter_id": 7}]}
        self.p = mock.patch.object(housing_monitor, "user_filters", side_effect=lambda uid: self.filters.get(int(uid), []))
        self.p.start()
        self.bot = mock.MagicMock()
        self.bot.get_chat.side_effect = lambda uid: SimpleNamespace(first_name=f"U{uid}", last_name="", username=None)

    def tearDown(self):
        self.p.stop()
        for m, s in self._saved:
            m.DBSession = s
        self.engine.dispose()

    def window(self):
        now = datetime.utcnow()
        return now - timedelta(hours=1), now + timedelta(minutes=1)

    def test_block_unblock_and_stop(self):
        ctx = SimpleNamespace(bot=self.bot)
        housing_leave.on_my_chat_member(_member_update(111, "member", "kicked"), ctx)   # заблокував, мав фільтри
        housing_leave.on_my_chat_member(_member_update(222, "member", "kicked"), ctx)   # заблокував, без фільтрів
        housing_leave.on_my_chat_member(_member_update(333, "member", "kicked"), ctx)
        housing_leave.on_my_chat_member(_member_update(333, "kicked", "member"), ctx)   # передумав — не рахуємо
        housing_leave.on_my_chat_member(_member_update(-100, "member", "kicked", "supergroup"), ctx)   # не особистий
        self.filters[333] = []
        housing_leave.after_filter_deleted(333)   # видалив останній фільтр
        housing_leave.after_filter_deleted(111)   # ще має фільтри — не рахуємо
        text = "\n".join(housing_leave.summary_lines(self.bot, *self.window(), housing_monitor._display_name))
        self.assertIn("🚫 Заблокували бота: <b>2</b> (з фільтрами житла: 1)", text)
        self.assertIn("<code>111</code> (мав фільтри)", text)
        self.assertIn("🔕 Вимкнули моніторинг (видалили всі фільтри): <b>1</b>", text)
        self.assertIn("↩️ Повернулись (розблокували): <b>1</b>", text)
        self.assertNotIn("-100", text)

    def test_summary_without_filter_events(self):
        housing_leave.record(222, "blocked", had_filters=False)
        text = housing_monitor.filter_summary_text(self.bot, *self.window())
        self.assertIn("Фільтри житла за добу", text)
        self.assertIn("Заблокували бота: <b>1</b>", text)

    def test_quiet_day(self):
        self.assertIsNone(housing_monitor.filter_summary_text(self.bot, *self.window()))


if __name__ == "__main__":
    unittest.main()
