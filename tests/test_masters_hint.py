import importlib
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock


def load(**env):
    """Свежий модуль с нужными настройками (они читаются при импорте)."""
    base = {"MASTERS_URL": "https://example.org/m/", "MASTERS_HINT": "on"}
    base.update(env)
    with mock.patch.dict(os.environ, base, clear=False):
        import user_handlers.masters_hint as mh
        return importlib.reload(mh)


class FakeMessage:
    def __init__(self, text, chat_id=-100, message_id=1, reply=None, is_topic=False, thread_id=None):
        self.text = text
        self.chat_id = chat_id
        self.message_id = message_id
        self.reply_to_message = reply
        self.is_topic_message = is_topic
        self.message_thread_id = thread_id
        self.sent = []

    def reply_text(self, text, **kwargs):
        self.sent.append((text, kwargs))


class MatchTest(unittest.TestCase):
    def setUp(self):
        self.mh = load()

    def test_requests_for_masters(self):
        cases = {
            "Добрый день. Посоветуйте русскоговорящего гинеколога.": "health",
            "Здравствуйте,подскажите пожалуйста нотариуса": "docs",
            "Привіт. Порадьте, будь ласка, перекладача, щоб піти на прийом до окуліста.": "docs",
            "Хто робить перевезення по Потсдаму?": "movers",
            "Добрый день. Подскажите пожалуйста перевозчика на Украину (перевозка посылок)": "ukraine",
            "шукаю хорошого перукаря з чоловічих стрижок, якщо у когось є рекомендації, напишіть будь ласка": "beauty",
            "Добрый день\nКто может подсказать контакты СТО?": "auto",
            "Нужен электрик с документами, кто проверит проводку и составит схему.": "home",
            "Э хтось хто робить торти? Треба весільний торт": "food",
            "Доброго дня! Підкажіть, будь ласка, хороші ремонтні сервіси для Макбуків в Берліні чи поблизу": "tech",
            "Ищу мастера, который разбирается в принтерах Epson, писать в личку": "tech",
            "Доброго дня. Підкажіть будь ласка контакти майстра з татуювання у Потсдамі": "",
        }
        for text, cat in cases.items():
            self.assertEqual(self.mh.match(text), cat, text)

    def test_not_a_request(self):
        for text in [
            "Потсдам сегодня красивый, правда?",                                    # «сдам» внутри слова, нет услуги
            "Пропоную манікюр і педикюр у Потсдамі, запис відкрито",                 # предложение, не просьба
            "Шукаю роботу перукарем, є досвід",                                      # ищет работу
            "Шукаю професійного жіночого перукаря в салон. Якщо ви зацікавлені, напишіть",   # наём
            "Шукаю 2-х малярів і 2-х сантехніків в Берліні +49 176 61461546",        # объявление с телефоном
            "Якщо просто психолог потрібен, то і в Потсдамі є. В Альбатрос не зверталися?",   # совет в ответ
            "Друзі, де можна знайти недороге приміщення для кабінету майстра манікюру?",      # ищут помещение
            "Кто знает, где можно в Потсдаме купить коробку для торта?",             # покупка, не мастер
            "Підкажіть, чи потрібен WBS, якщо платить джобцентр?",                   # вопрос не про услугу
            "Шукаю квартиру в Потсдамі, 2 кімнати, порадьте маклера чи сайт?",       # жильё
            "ок",
        ]:
            self.assertIsNone(self.mh.match(text), text)

    def test_text_is_short_ukrainian_with_link(self):
        self.assertEqual(self.mh.text_for(""), "🧰 Увесь каталог майстрів і послуг нашого чату тут: https://example.org/m/?s=hint")
        self.assertIn("https://example.org/m/?s=hint#cat=beauty", self.mh.text_for("beauty"))
        self.assertEqual(self.mh.link("bot"), "https://example.org/m/?s=bot")
        self.assertIn("«Краса»", self.mh.text_for("beauty"))
        self.assertLess(len(self.mh.text_for("docs")), 160)


class ReplyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = os.path.join(self.tmp.name, "hint.json")
        self.user = SimpleNamespace(id=7, is_bot=False)

    def tearDown(self):
        self.tmp.cleanup()

    def test_on_sends_once_per_user_and_respects_gap(self):
        mh = load(MASTERS_HINT_STATE=self.state)
        ask = "Порадьте, будь ласка, стоматолога в Потсдамі"
        first = FakeMessage(ask)
        self.assertTrue(mh.maybe_reply(first, self.user, ask))
        self.assertEqual(len(first.sent), 1)
        self.assertIn("#cat=health", first.sent[0][0])
        again = FakeMessage(ask, message_id=2)
        self.assertFalse(mh.maybe_reply(again, self.user, ask))          # тому же человеку не повторяем
        other = FakeMessage(ask, message_id=3)
        self.assertFalse(mh.maybe_reply(other, SimpleNamespace(id=8, is_bot=False), ask))   # пауза в чате
        self.assertEqual(again.sent + other.sent, [])

    def test_gap_and_daily_limit(self):
        mh = load(MASTERS_HINT_STATE=self.state, MASTERS_HINT_GAP_MIN="20", MASTERS_HINT_MAX_PER_DAY="2")
        t0 = 1_000_000.0
        self.assertTrue(mh._reserve(-100, 1, now=t0))
        self.assertFalse(mh._reserve(-100, 2, now=t0 + 60))               # 1 минута: рано
        self.assertTrue(mh._reserve(-100, 2, now=t0 + 21 * 60))           # через 21 минуту можно
        self.assertFalse(mh._reserve(-100, 3, now=t0 + 3 * 3600))         # третья за сутки: нельзя
        self.assertTrue(mh._reserve(-100, 3, now=t0 + 25 * 3600))         # на следующий день можно
        self.assertFalse(mh._reserve(-100, 1, now=t0 + 6 * 86400))        # тому же человеку раньше недели нельзя
        self.assertTrue(mh._reserve(-100, 1, now=t0 + 8 * 86400))

    def test_log_mode_and_off_do_not_write_to_chat(self):
        ask = "Порадьте, будь ласка, стоматолога в Потсдамі"
        for mode in ("log", "off"):
            mh = load(MASTERS_HINT=mode, MASTERS_HINT_STATE=os.path.join(self.tmp.name, mode + ".json"))
            msg = FakeMessage(ask)
            self.assertFalse(mh.maybe_reply(msg, self.user, ask))
            self.assertEqual(msg.sent, [])
        mh = load(MASTERS_URL="", MASTERS_HINT_STATE=self.state)           # без ссылки выключено
        self.assertFalse(mh.enabled())

    def test_settings_from_config_file_and_env_wins(self):
        import json
        cfg = os.path.join(self.tmp.name, "masters.json")
        with open(cfg, "w", encoding="utf-8") as fh:
            json.dump({"url": "https://file.example/", "hint": "on", "gap_min": 5, "max_per_day": 3}, fh)
        clean = {k: "" for k in ("MASTERS_URL", "MASTERS_HINT", "MASTERS_HINT_GAP_MIN", "MASTERS_HINT_MAX_PER_DAY")}
        mh = load(MASTERS_CONFIG=cfg, MASTERS_HINT_STATE=self.state, **clean)
        self.assertEqual((mh.MASTERS_URL, mh.MODE, mh.GAP_MIN, mh.MAX_PER_DAY), ("https://file.example/", "on", 5.0, 3))
        self.assertTrue(mh.enabled())
        mh = load(MASTERS_CONFIG=cfg, MASTERS_HINT_STATE=self.state, MASTERS_URL="https://env.example/", MASTERS_HINT="off")
        self.assertEqual((mh.MASTERS_URL, mh.MODE), ("https://env.example/", "off"))
        mh = load(MASTERS_CONFIG=os.path.join(self.tmp.name, "nope.json"), MASTERS_HINT_STATE=self.state, **clean)
        self.assertEqual((mh.MASTERS_URL, mh.MODE), ("", "off"))

    def test_reply_failure_is_swallowed(self):
        mh = load(MASTERS_HINT_STATE=self.state)
        msg = FakeMessage("Порадьте стоматолога в Потсдамі, будь ласка?")
        msg.reply_text = mock.Mock(side_effect=RuntimeError("boom"))
        self.assertFalse(mh.maybe_reply(msg, self.user, msg.text))


class GroupHandlerTest(unittest.TestCase):
    """kb_search.on_group_message: подсказка уходит, когда ответа из справочника в чат не было."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.mh = load(MASTERS_HINT_STATE=os.path.join(self.tmp.name, "hint.json"))
        import user_handlers.kb_search as kb_search
        self.kb = importlib.reload(kb_search)
        self.user = SimpleNamespace(id=42, is_bot=False)

    def tearDown(self):
        self.tmp.cleanup()

    def _update(self, message):
        message.from_user = self.user
        return SimpleNamespace(effective_message=message)

    def test_hint_when_kb_not_ready(self):
        msg = FakeMessage("Добрый день. Посоветуйте русскоговорящего гинеколога.")
        with mock.patch.object(self.kb, "ready", return_value=False):
            self.kb.on_group_message(self._update(msg), SimpleNamespace(bot=SimpleNamespace(username="Potsdambot")))
        self.assertEqual(len(msg.sent), 1)
        self.assertIn("каталог", msg.sent[0][0])

    def test_topic_message_is_not_a_reply_but_real_reply_is_skipped(self):
        topic_root = SimpleNamespace(message_id=500, forum_topic_created=SimpleNamespace(name="Послуги"))
        in_topic = FakeMessage("Порадьте, будь ласка, перукаря в Потсдамі", reply=topic_root, is_topic=True, thread_id=500)
        with mock.patch.object(self.kb, "ready", return_value=False):
            self.kb.on_group_message(self._update(in_topic), SimpleNamespace(bot=SimpleNamespace(username="Potsdambot")))
        self.assertEqual(len(in_topic.sent), 1)
        other = SimpleNamespace(message_id=777, forum_topic_created=None)
        answer = FakeMessage("Порадьте, будь ласка, перукаря в Потсдамі", message_id=9, reply=other, is_topic=True, thread_id=500)
        self.user = SimpleNamespace(id=43, is_bot=False)
        with mock.patch.object(self.kb, "ready", return_value=False):
            self.kb.on_group_message(self._update(answer), SimpleNamespace(bot=SimpleNamespace(username="Potsdambot")))
        self.assertEqual(answer.sent, [])

    def test_bot_and_potsdambot_queries_are_ignored(self):
        msg = FakeMessage("потсдамбот порадь стоматолога?")
        with mock.patch.object(self.kb, "ready", return_value=False):
            self.kb.on_group_message(self._update(msg), SimpleNamespace(bot=SimpleNamespace(username="Potsdambot")))
        self.assertEqual(msg.sent, [])


if __name__ == "__main__":
    unittest.main()
