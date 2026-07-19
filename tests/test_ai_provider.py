import os
import sys
import unittest
from unittest.mock import Mock, patch

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from user_handlers import msg_ai


class AiProviderTests(unittest.TestCase):
    def test_calls_omniroute_without_api_key(self):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "choices": [{"message": {"content": "Працює"}}]
        }

        with patch.object(msg_ai, "OPENROUTER_API_KEY", ""), patch.object(
            msg_ai.requests, "post", return_value=response
        ) as post:
            result = msg_ai._call_ai("Як справи?", max_tokens=50, timeout=10)

        self.assertEqual(result, "Працює")
        headers = post.call_args.kwargs["headers"]
        payload = post.call_args.kwargs["json"]
        self.assertNotIn("Authorization", headers)
        self.assertFalse(payload["stream"])
        self.assertEqual(payload["model"], msg_ai.AI_MODEL)

    def test_returns_empty_when_omniroute_fails_and_gemini_is_unavailable(self):
        with patch.object(msg_ai, "GEMINI_API_KEY", ""), patch.object(
            msg_ai.requests, "post", side_effect=msg_ai.requests.RequestException("offline")
        ):
            result = msg_ai._call_ai("Як справи?", max_tokens=50, timeout=10)

        self.assertEqual(result, "")

    def test_provider_intent_recognizes_handyman_queries(self):
        queries = (
            "муж на час Дмитрий",
            "нужен бытовой мастер",
            "кто делает мелкий ремонт",
            "нужна сборка мебели и установка кухни",
            "подключение бытовой техники",
        )

        for query in queries:
            with self.subTest(query=query):
                self.assertTrue(msg_ai._is_service_provider_query(query))

    def test_extract_query_accepts_alias_and_trigger_anywhere(self):
        cases = {
            "посдамбот муж на час": "муж на час",
            "Подскажите, потсдамбот, кто ремонтирует мебель?": "Подскажите, кто ремонтирует мебель?",
            "Потсдам бот найди электрика": "найди электрика",
        }

        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(msg_ai._extract_query(text), expected)

    def test_extract_query_rejects_trigger_inside_another_word(self):
        self.assertIsNone(msg_ai._extract_query("это непотсдамботик"))

    def test_extract_query_does_not_claim_other_potbot(self):
        self.assertIsNone(msg_ai._extract_query("Потбот найди электрика"))

    def test_provider_query_always_supplements_recent_results_with_full_history(self):
        recent = list(range(1, 8))
        historical = [50, 51]
        expected = [{"id": value} for value in recent + historical]

        with patch.object(
            msg_ai, "_search_keyword_ids", side_effect=[recent, historical]
        ) as search, patch.object(msg_ai, "_fetch_chain", return_value=expected):
            result = msg_ai._search_keywords_with_fallback(
                Mock(), [-1001], ["ремонт"], provider_query=True,
            )

        self.assertEqual(search.call_count, 2)
        self.assertEqual(result, expected)
        self.assertIsNotNone(search.call_args_list[1].kwargs["before"])

    def test_author_search_matches_fullname_username_and_alias(self):
        session = Mock()
        query = session.query.return_value
        query.outerjoin.return_value = query
        query.filter.return_value = query
        query.order_by.return_value = query
        query.limit.return_value = query
        query.all.return_value = [
            (Mock(_id=42), Mock(fullname="Dmytro", username="Dmytriii")),
        ]

        rows = msg_ai._search_provider_authors(
            session, [-1001], ["дмитрий", "dmytriii"], limit=10,
        )

        self.assertEqual(rows[0][0]._id, 42)

    def test_provider_history_keeps_multiple_messages_per_author(self):
        messages = [
            {"id": value, "user": "Dmytriii", "date": f"2026-04-{value:02d}"}
            for value in range(1, 7)
        ] + [{"id": 99, "user": "Other", "date": "2026-05-01"}]

        grouped = msg_ai._group_provider_history(messages)

        self.assertEqual([item["id"] for item in grouped], [1, 2, 3, 4, 99])


if __name__ == "__main__":
    unittest.main()
