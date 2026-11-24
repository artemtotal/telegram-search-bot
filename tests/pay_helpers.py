"""Общая обвязка тестов функций подписки (25.11.2026): база в памяти и
подмена DBSession во всех модулях, которые ходят в неё напрямую."""

import importlib
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database import Base

ADMIN = 999
USER = 777
FRIEND = 778

MODULE_NAMES = (
    "user_handlers.housing_payment", "user_handlers.housing_pay_report", "user_handlers.housing_winback",
    "user_handlers.housing_referral", "user_handlers.housing_found", "user_handlers.housing_leave",
    "user_handlers.housing_pay_survey", "user_jobs.housing_access_store", "user_jobs.housing_tier",
    "user_jobs.user_settings_store", "user_jobs.housing_pay_store", "user_jobs.housing_missed",
    "user_jobs.housing_journey_store", "user_jobs.housing_search_store",
)


def tg_user(user_id=USER, name="Olena"):
    return SimpleNamespace(id=user_id, first_name=name, last_name="", username=name.lower(), full_name=name)


def callback(data, user_id=USER):
    query = mock.MagicMock()
    query.data = data
    return SimpleNamespace(callback_query=query, effective_user=tg_user(user_id), effective_message=query.message)


def photo(user_id=USER, caption=None):
    message = mock.MagicMock()
    message.chat_id = user_id
    message.message_id = 55
    message.caption = caption
    return SimpleNamespace(callback_query=None, effective_user=tg_user(user_id), effective_message=message)


def text_update(text, user_id=USER):
    message = mock.MagicMock()
    message.text = text
    message.chat.type = "private"
    return SimpleNamespace(callback_query=None, message=message, effective_user=tg_user(user_id), effective_message=message)


class PayDbCase(unittest.TestCase):
    """База в памяти; Telegram-бот и тяжёлые вызовы подменены."""

    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.factory = sessionmaker(bind=self.engine)
        self._saved = []
        for name in MODULE_NAMES:
            module = importlib.import_module(name)
            if hasattr(module, "DBSession"):
                self._saved.append((module, module.DBSession))
                module.DBSession = self.factory
        from user_handlers import housing_monitor, housing_payment

        self.housing_monitor = housing_monitor
        self.housing_payment = housing_payment
        self.patches = [
            mock.patch.object(housing_payment, "PAY_CARD", "4111111111111111"),
            mock.patch.object(housing_payment, "PAY_CARD_HOLDER", "Тарасюк"),
            mock.patch.object(housing_payment, "ADMIN_ID", ADMIN),
            mock.patch.object(housing_payment, "eur_uah_rate", return_value=48.3),
            mock.patch.object(housing_monitor, "_set_all_filters_active_for_user", return_value=1),
        ]
        for patch in self.patches:
            patch.start()
        self.bot = mock.MagicMock()
        self.context = SimpleNamespace(bot=self.bot, bot_data={}, user_data={}, args=[])

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        for module, session in self._saved:
            module.DBSession = session
        self.engine.dispose()

    def session(self):
        return self.factory()

    def sent_texts(self, chat_id=None):
        result = []
        for call in self.bot.send_message.call_args_list:
            if chat_id is None or call.kwargs.get("chat_id") == chat_id:
                result.append(call.kwargs.get("text", ""))
        return result

    def sent_calls(self, chat_id):
        return [c for c in self.bot.send_message.call_args_list if c.kwargs.get("chat_id") == chat_id]

    @staticmethod
    def buttons(markup):
        return [b for row in markup.inline_keyboard for b in row]

    @staticmethod
    def now():
        return datetime.utcnow()
