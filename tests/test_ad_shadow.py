"""«Тіньовий» підрахунок реклами повз оплату і вимикачі з файлу (26.11.2026).

Тексти нижче вигадані. Справжню вибірку чату можна прогнати локально: AD_SHADOW_SAMPLE=<jsonl з полем "t">.
"""
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import mock

from user_handlers import ad_shadow, switches

ADMIN = 900
CLIENT = 901
CHAT_ADMIN = 902
PERSON = 903
CHAT = -1001

AD_TEXTS = [
    "Пропоную послуги манікюру та педикюру в Потсдамі. Запис за номером +49 176 1234567",
    "Ремонт квартир під ключ, Берлін і Бранденбург. Ціна від 20 € за кв.м. Звертайтесь, телефон 0176 1234 5678",
    "Українська студія краси в Берліні: https://www.instagram.com/studio.example",
    "Продовжуємо набір на онлайн-курси німецької мови. Вартість і графік на сайті https://t.me/example_courses_bot",
    "Перевезення речей Німеччина - Україна, щотижня. Вантажоперевезення, пишіть у вайбер +380 50 123 45 67",
    "Продаємо нові дитячі коляски, в наявності різні кольори, доставка по Німеччині. Ціна 150 євро, пишіть @shop_example",
]
NOT_AD_TEXTS = [
    "Підкажіть, будь ласка, хто робить манікюр у Потсдамі? Можна контакт, бажано недорого, до 30 євро",
    "Шукаю майстра для ремонту пральної машини, хто може порадити? Пишіть в особисті +49 176 1234567",
    "Продам велосипед дитячий 25 євро, майже новий, самовивіз з Бабельсберга",
    "Яка приблизна ціна зробити вії та манікюр у Берліні, не підкажете номер салону",
    "Доброго ранку всім! Сьогодні гарна погода, іду гуляти в парк Сансусі, хто зі мною?",
    "Потрібен електрик на 2 дні, оплата 150 євро, пишіть +49 176 1234567, Потсдам, термінова робота",
    "Віддам безкоштовно дитячий одяг, забирайте, телефон +49 176 1234567, Потсдам, Драйлінден",
    "Ціна 20 євро? Це дорого, чи не краще піти в інший салон, ніж платити стільки",
    "Коротке 25 € +49 176 1234567",
]


def make_message(text, user_id=PERSON, message_id=1, thread_id=None, topic_title=None, is_bot=False):
    reply = None
    if topic_title:
        reply = SimpleNamespace(forum_topic_created=SimpleNamespace(name=topic_title))
    return SimpleNamespace(
        text=text, from_user=SimpleNamespace(id=user_id, is_bot=is_bot), chat_id=CHAT, message_id=message_id,
        message_thread_id=thread_id, reply_to_message=reply, link="https://t.me/c/1/{}".format(message_id),
    )


class LooksLikeAdTest(unittest.TestCase):
    def test_ads(self):
        for text in AD_TEXTS:
            self.assertTrue(ad_shadow.looks_like_ad(text), text)

    def test_not_ads(self):
        for text in NOT_AD_TEXTS:
            self.assertFalse(ad_shadow.looks_like_ad(text), text)

    def test_word_boundary_does_not_match_inside_words(self):
        text = "Я сегодня заплатила 32,50, если бы выбрала услугу доставку, то было бы 5 €. Зачем я это доказываю"
        self.assertFalse(ad_shadow.looks_like_ad(text))

    def test_vacancies_are_not_counted(self):
        text = "Требуются электрики, монтаж, оплата 25 € в час, график работы 5/2, звоните +49 176 1234567"
        self.assertFalse(ad_shadow.looks_like_ad(text))


class ShadowBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.adbot = os.path.join(self.tmp.name, "adbot.db")
        con = sqlite3.connect(self.adbot)
        con.executescript(
            "CREATE TABLE ad_clients (user_id INTEGER PRIMARY KEY);"
            "CREATE TABLE published_posts (id INTEGER PRIMARY KEY, client_user_id INTEGER);"
            "CREATE TABLE topics (name TEXT, chat_id INTEGER, message_thread_id INTEGER);"
            "INSERT INTO ad_clients VALUES ({});"
            "INSERT INTO topics VALUES ('! Барахолка', {}, 94882);"
            "INSERT INTO topics VALUES ('! Б''юті', {}, 92893);".format(CLIENT, CHAT, CHAT)
        )
        con.commit()
        con.close()
        patches = [
            mock.patch.object(ad_shadow, "DB_PATH", os.path.join(self.tmp.name, "shadow.db")),
            mock.patch.object(ad_shadow, "ADBOT_DB", self.adbot),
            mock.patch.object(ad_shadow, "ADMIN_ID", ADMIN),
            mock.patch.dict(os.environ, {"AD_SHADOW_ENABLED": ""}),
            mock.patch.object(switches, "CONFIG_PATH", os.path.join(self.tmp.name, "none.json")),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        ad_shadow._clients_cache.update(at=0.0, ids=None)
        ad_shadow._topics_cache.update(at=0.0, names=None)
        ad_shadow._admins_cache.clear()
        self.bot = mock.MagicMock()
        self.bot.get_chat_administrators.return_value = [SimpleNamespace(user=SimpleNamespace(id=CHAT_ADMIN))]
        self.context = SimpleNamespace(bot=self.bot)

    def feed(self, message):
        return ad_shadow._handle(SimpleNamespace(effective_message=message), self.context)

    def rows(self):
        if not os.path.exists(ad_shadow.DB_PATH):
            return []
        con = sqlite3.connect(ad_shadow.DB_PATH)
        try:
            return con.execute("SELECT user_id, link FROM shadow_ad ORDER BY msg_id").fetchall()
        finally:
            con.close()


class HandlerTest(ShadowBase):
    AD = AD_TEXTS[0]

    def test_counts_a_person_and_never_writes_to_the_chat(self):
        message = make_message(self.AD)
        self.assertTrue(self.feed(message))
        self.assertEqual(self.rows(), [(PERSON, "https://t.me/c/1/1")])
        self.assertFalse(self.bot.send_message.called)
        self.assertFalse(self.bot.method_calls and any(c[0].startswith("send") or c[0].startswith("reply") for c in self.bot.method_calls))

    def test_same_message_twice_counts_once(self):
        self.feed(make_message(self.AD))
        self.feed(make_message(self.AD))
        self.assertEqual(len(self.rows()), 1)

    def test_skips_admin_chat_admin_client_bot(self):
        for user_id, kwargs in ((ADMIN, {}), (CHAT_ADMIN, {}), (CLIENT, {}), (PERSON, {"is_bot": True})):
            self.assertFalse(self.feed(make_message(self.AD, user_id=user_id, **kwargs)), user_id)
        self.assertEqual(self.rows(), [])

    def test_flea_topic_by_adbot_topics_and_by_topic_title(self):
        self.assertFalse(self.feed(make_message(self.AD, message_id=2, thread_id=94882)))
        self.assertFalse(self.feed(make_message(self.AD, message_id=3, thread_id=555, topic_title="Барахолка")))
        self.assertTrue(self.feed(make_message(self.AD, message_id=4, thread_id=92893)))
        self.assertEqual(len(self.rows()), 1)

    def test_nothing_counted_when_clients_unknown(self):
        with mock.patch.object(ad_shadow, "ADBOT_DB", os.path.join(self.tmp.name, "missing.db")):
            self.assertFalse(self.feed(make_message(self.AD)))
        self.assertFalse(os.path.exists(ad_shadow.DB_PATH))

    def test_nothing_counted_when_chat_admins_unknown(self):
        self.bot.get_chat_administrators.side_effect = RuntimeError("no rights")
        self.assertFalse(self.feed(make_message(self.AD)))

    def test_questions_are_not_counted(self):
        self.assertFalse(self.feed(make_message(NOT_AD_TEXTS[0])))
        self.assertFalse(os.path.exists(ad_shadow.DB_PATH))

    def test_switch_off_in_file(self):
        path = os.path.join(self.tmp.name, "sw.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"ad_shadow": False}, fh)
        with mock.patch.object(switches, "CONFIG_PATH", path):
            self.assertFalse(ad_shadow.enabled())
            self.assertFalse(self.feed(make_message(self.AD)))
            self.assertEqual(ad_shadow.summary_text(datetime(2000, 1, 1), datetime(2100, 1, 1)), "")

    def test_on_by_default_without_file(self):
        self.assertTrue(ad_shadow.enabled())

    def test_handler_failure_is_swallowed(self):
        with mock.patch.object(ad_shadow, "_handle", side_effect=RuntimeError("boom")):
            ad_shadow.on_group_message(SimpleNamespace(effective_message=None), self.context)


class SummaryTest(ShadowBase):
    def test_empty_when_nothing(self):
        now = datetime.utcnow()
        self.assertEqual(ad_shadow.summary_text(now - timedelta(days=1), now), "")

    def test_line_counts_messages_and_authors_and_limits_links(self):
        now = datetime.utcnow()
        for index in range(8):
            ad_shadow.record(CHAT, 10 + index, 1000 + index % 7, None, "https://t.me/c/1/{}".format(10 + index), now - timedelta(hours=2))
        text = ad_shadow.summary_text(now - timedelta(days=1), now)
        first = text.split("\n")[0]
        self.assertIn("повз оплату: 8, авторів: 7", first)
        self.assertEqual(text.count("<a href="), ad_shadow.MAX_LINKS)

    def test_old_rows_not_in_window_and_purged(self):
        now = datetime.utcnow()
        ad_shadow.record(CHAT, 1, 1, None, "https://t.me/c/1/1", now - timedelta(days=40))
        ad_shadow.record(CHAT, 2, 2, None, "https://t.me/c/1/2", now - timedelta(days=2))
        self.assertEqual(ad_shadow.summary_text(now - timedelta(days=1), now), "")
        self.assertEqual([r[0] for r in self.rows()], [2])

    def test_added_to_daily_summary_even_on_a_quiet_day(self):
        from user_handlers import housing_monitor

        now = datetime.utcnow()
        ad_shadow.record(CHAT, 5, 77, None, "https://t.me/c/1/5", now - timedelta(hours=3))
        bot = mock.MagicMock()
        with mock.patch.object(housing_monitor, "ADMIN_ID", ADMIN), \
                mock.patch.object(housing_monitor, "filter_summary_text", return_value=""):
            housing_monitor.daily_filter_summary_job(SimpleNamespace(bot=bot))
        self.assertEqual(bot.send_message.call_count, 1)
        sent = bot.send_message.call_args.kwargs
        self.assertEqual(sent["chat_id"], ADMIN)
        self.assertIn("повз оплату: 1, авторів: 1", sent["text"])

    def test_appended_after_existing_summary_and_nothing_when_both_empty(self):
        from user_handlers import housing_monitor

        now = datetime.utcnow()
        bot = mock.MagicMock()
        with mock.patch.object(housing_monitor, "ADMIN_ID", ADMIN), \
                mock.patch.object(housing_monitor, "filter_summary_text", return_value="Фільтри: тест"):
            housing_monitor.daily_filter_summary_job(SimpleNamespace(bot=bot))
        self.assertEqual(bot.send_message.call_args.kwargs["text"], "Фільтри: тест")
        bot.reset_mock()
        ad_shadow.record(CHAT, 6, 78, None, "https://t.me/c/1/6", now - timedelta(hours=1))
        with mock.patch.object(housing_monitor, "ADMIN_ID", ADMIN), \
                mock.patch.object(housing_monitor, "filter_summary_text", return_value="Фільтри: тест"):
            housing_monitor.daily_filter_summary_job(SimpleNamespace(bot=bot))
        text = bot.send_message.call_args.kwargs["text"]
        self.assertTrue(text.startswith("Фільтри: тест\n\n"))
        self.assertIn("повз оплату: 1", text)


class SwitchesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "bot_switches.json")
        patch = mock.patch.object(switches, "CONFIG_PATH", self.path)
        patch.start()
        self.addCleanup(patch.stop)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("X_FLAG", None)

    def write(self, data):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(data)
        self.tick = getattr(self, "tick", 1_700_000_000) + 10     # время изменения всегда разное, не зависит от точности часов
        os.utime(self.path, (self.tick, self.tick))

    def test_default_without_file(self):
        self.assertFalse(switches.flag("X_FLAG", "x", False))
        self.assertTrue(switches.flag("X_FLAG", "x", True))

    def test_file_value_and_env_wins(self):
        self.write('{"x": true}')
        self.assertTrue(switches.flag("X_FLAG", "x", False))
        os.environ["X_FLAG"] = "0"
        self.assertFalse(switches.flag("X_FLAG", "x", False))
        os.environ["X_FLAG"] = ""     # пуста змінна не заважає файлу
        self.assertTrue(switches.flag("X_FLAG", "x", False))

    def test_file_change_applies_and_broken_file_falls_back(self):
        self.write('{"x": true}')
        self.assertTrue(switches.flag("X_FLAG", "x", False))
        self.write('{"x": false}')
        self.assertFalse(switches.flag("X_FLAG", "x", True))
        self.write("not json")
        self.assertTrue(switches.flag("X_FLAG", "x", True))
        self.assertFalse(switches.flag("X_FLAG", "x", False))


@unittest.skipUnless(os.environ.get("AD_SHADOW_SAMPLE"), "справжня вибірка чату задається локально")
class RealSampleTest(unittest.TestCase):
    def test_rate_is_small(self):
        texts = set()
        with open(os.environ["AD_SHADOW_SAMPLE"], encoding="utf-8") as fh:
            for line in fh:
                texts.add(json.loads(line)["t"])
        hits = [t for t in texts if ad_shadow.looks_like_ad(t)]
        self.assertLess(len(hits), len(texts) * 0.02)


if __name__ == "__main__":
    unittest.main()
