"""Перевірка нових учасників (join_gate): заявка → правила → питання → схвалення / відмова. Telegram підмінено."""
import importlib
import json
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class FakeBot:
    username = "PotsdamTestBot"

    def __init__(self):
        self.sent, self.approved, self.declined = [], [], []

    def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text, kw))
        return types.SimpleNamespace(message_id=len(self.sent))

    def approve_chat_join_request(self, chat_id, user_id):
        self.approved.append((chat_id, user_id))

    def decline_chat_join_request(self, chat_id, user_id):
        self.declined.append((chat_id, user_id))

    def edit_message_text(self, **kw):
        pass


class FakeQuery:
    def __init__(self, bot, data, user_id):
        self.bot, self.data = bot, data
        self.from_user = types.SimpleNamespace(id=user_id)
        self.texts, self.kb = [], None

    def answer(self, *a, **kw):
        pass

    def edit_message_text(self, text, **kw):
        self.texts.append(text)
        self.kb = kw.get("reply_markup")

    def edit_message_reply_markup(self, **kw):
        pass


class JoinGateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["JOIN_GATE_CONFIG"] = os.path.join(self.tmp, "cfg.json")
        os.environ["JOIN_GATE_STATE"] = os.path.join(self.tmp, "state.json")
        self.settings = mock.patch("user_jobs.user_settings_store.get_language", return_value="uk")
        self.settings.start()
        mock.patch("user_jobs.user_settings_store.set_language").start()
        import user_handlers.join_gate as jg
        self.jg = importlib.reload(jg)
        self.bot = FakeBot()
        self.ctx = types.SimpleNamespace(bot=self.bot, args=[])

    def tearDown(self):
        mock.patch.stopall()

    def request(self, user_id=7, lang="ru"):
        user = types.SimpleNamespace(id=user_id, first_name="Іван", last_name="", username="", language_code=lang)
        req = types.SimpleNamespace(chat=types.SimpleNamespace(id=-100500, title="Потсдам"), from_user=user)
        self.jg.on_join_request(types.SimpleNamespace(chat_join_request=req), self.ctx)

    def press(self, data, user_id=7):
        q = FakeQuery(self.bot, data, user_id)
        self.jg.on_callback(types.SimpleNamespace(callback_query=q), self.ctx)
        return q

    def answer_buttons(self, q):
        return [(b.text, b.callback_data) for row in q.kb.inline_keyboard for b in row]

    def test_rules_question_approve_after_one_mistake(self):
        self.request()
        self.assertEqual(self.bot.sent[0][0], 7)
        self.assertIn("Правила", self.bot.sent[0][1])            # русский по language_code
        cfg = json.load(open(os.environ["JOIN_GATE_CONFIG"], encoding="utf-8"))   # чернетка створилась
        q = self.press("jg:ok:-100500")
        btns = self.answer_buttons(q)
        qi = int(btns[0][1].split(":")[3])
        good = int(cfg["questions"][qi]["correct"])
        wrong = next(d for _, d in btns if int(d.split(":")[4]) != good)
        q2 = self.press(wrong)
        self.assertTrue(q2.texts[-1].startswith("❌"))
        btns2 = self.answer_buttons(q2)
        qi2 = int(btns2[0][1].split(":")[3])
        right = next(d for _, d in btns2 if int(d.split(":")[4]) == int(cfg["questions"][qi2]["correct"]))
        q3 = self.press(right)
        self.assertEqual(self.bot.approved, [(-100500, 7)])
        self.assertIn("t.me/PotsdamTestBot?start=hello", self.answer_buttons(q3)[0][1] or q3.kb.inline_keyboard[0][0].url)
        self.assertEqual(json.load(open(os.environ["JOIN_GATE_STATE"], encoding="utf-8"))["pending"], {})

    def test_two_mistakes_decline(self):
        self.request(user_id=8, lang="uk")
        cfg = json.load(open(os.environ["JOIN_GATE_CONFIG"], encoding="utf-8"))
        q = self.press("jg:ok:-100500", user_id=8)
        for _ in range(2):
            btns = self.answer_buttons(q)
            qi = int(btns[0][1].split(":")[3])
            wrong = next(d for _, d in btns if int(d.split(":")[4]) != int(cfg["questions"][qi]["correct"]))
            q = self.press(wrong, user_id=8)
        self.assertEqual(self.bot.declined, [(-100500, 8)])
        self.assertEqual(self.bot.approved, [])

    def test_other_users_button_ignored(self):
        self.request(user_id=9)
        q = self.press("jg:ok:-100500", user_id=10)   # чужа кнопка — заявки немає
        self.assertEqual(q.texts, [])

    def test_disabled_does_nothing(self):
        json.dump({"enabled": False}, open(os.environ["JOIN_GATE_CONFIG"], "w", encoding="utf-8"))
        self.request(user_id=11)
        self.assertEqual(self.bot.sent, [])


if __name__ == "__main__":
    unittest.main()
