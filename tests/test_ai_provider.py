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


if __name__ == "__main__":
    unittest.main()
