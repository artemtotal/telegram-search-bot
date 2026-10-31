import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from user_jobs import kb_builder, kb_common as kb  # noqa: E402
from user_handlers import kb_search  # noqa: E402


class KbCommonTests(unittest.TestCase):
    def test_fts_query_uses_llm_stems_as_is_and_cuts_free_words(self):
        query = kb.fts_query(["грузчики с машиной"], ["перевез", "umzug"])
        self.assertEqual(query, '"перевез"* OR "umzug"* OR "грузчи"* OR "машин"*')

    def test_fts_query_empty(self):
        self.assertIsNone(kb.fts_query(["а и"], []))

    def test_parse_json_tolerates_fences_and_prose(self):
        self.assertEqual(kb.parse_json('```json\n{"cards": []}\n```'), {"cards": []})
        self.assertEqual(kb.parse_json('Ось: {"cards": [1]} готово'), {"cards": [1]})
        self.assertIsNone(kb.parse_json("нічого"))

    def test_embedding_is_cut_and_normalised(self):
        vector = kb._normalize([3.0, 4.0] + [0.0] * 3000)
        self.assertEqual(len(vector), kb.EMBED_DIM)
        self.assertAlmostEqual(vector[0], 0.6)


class KbBuilderTests(unittest.TestCase):
    def test_window_is_weekdays_daytime(self):
        self.assertTrue(kb_builder.in_window(datetime(2026, 11, 2, 9, 0)))     # Monday
        self.assertFalse(kb_builder.in_window(datetime(2026, 11, 2, 17, 0)))
        self.assertFalse(kb_builder.in_window(datetime(2026, 10, 31, 12, 0)))  # Saturday

    def test_cards_keep_only_ids_from_the_batch_and_take_dates_links(self):
        rows = [
            ({"id": 10, "date": "2026-05-01 10:00:00", "link": "https://t.me/x/10"}, ""),
            ({"id": 11, "date": "2026-05-03 10:00:00", "link": "https://t.me/x/11"}, ""),
        ]
        cards = kb_builder.build_cards(rows, {"cards": [
            {"kind": "offer", "title": "Перевезення", "contacts": ["+49 1", "@x"], "msg_ids": ["#11", 10, 99]},
            {"title": "Без джерел", "msg_ids": [99]},
            {"title": "", "msg_ids": [10]},
        ]})
        self.assertEqual(len(cards), 1)
        card = cards[0]
        self.assertEqual(json.loads(card["msg_ids"]), [11, 10])
        self.assertEqual((card["first_date"], card["last_date"]), ("2026-05-01", "2026-05-03"))
        self.assertEqual(card["contacts"], "+49 1, @x")
        self.assertEqual(json.loads(card["links"]), ["https://t.me/x/11", "https://t.me/x/10"])


class ProactiveTests(unittest.TestCase):
    def test_question_detection(self):
        yes = ["Подскажите, где найти грузчиков на субботу?", "Шукаю стоматолога в Потсдамі",
               "Wer kennt einen guten Kinderarzt?", "Порадьте, будь ласка, перукаря"]
        no = ["Пропоную послуги перевезення, пишіть", "Доброе утро всем", "Сдам квартиру, кто ищет?",
              "Ищу?", "Продам велосипед, кто ищет?"]
        for text in yes:
            self.assertTrue(kb_search.looks_like_question(text), text)
        for text in no:
            self.assertFalse(kb_search.looks_like_question(text), text)

    def test_limits(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = kb.connect(os.path.join(tmp, "kb.db"))
            self.assertTrue(kb_search._limits_ok(store, -1, 5))
            store.execute("INSERT INTO proactive(chat_id, msg_id, user_id, question, status)"
                          " VALUES (-1, 1, 5, 'q', 'preview')")
            self.assertFalse(kb_search._limits_ok(store, -1, 6))  # chat gap
            self.assertFalse(kb_search._limits_ok(store, -2, 5))  # same person
            self.assertTrue(kb_search._limits_ok(store, -2, 6))
            store.execute("UPDATE proactive SET status = 'skipped'")
            self.assertTrue(kb_search._limits_ok(store, -1, 5))   # skipped ones do not count
            store.close()

    def test_housing_question_gets_monitoring_hint(self):
        text = kb_search._proactive_text("1. Варіант", "Шукаю квартиру в Потсдамі", "Potsdambot")
        self.assertIn("@Potsdambot", text)
        self.assertNotIn("@Potsdambot", kb_search._proactive_text("1.", "Шукаю стоматолога", "Potsdambot"))

    def test_ready_modes(self):
        with patch.object(kb_search, "KB_SEARCH", "on"):
            self.assertTrue(kb_search.ready())
        with patch.object(kb_search, "KB_SEARCH", "off"):
            self.assertFalse(kb_search.ready())


if __name__ == "__main__":
    unittest.main()
