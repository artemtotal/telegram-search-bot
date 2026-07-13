import unittest
from datetime import datetime, timedelta

from user_jobs.chunking import MAX_CHUNK_CHARS, chunk_messages


class ChunkMessagesTests(unittest.TestCase):
    def setUp(self):
        self.base = datetime(2026, 7, 13, 8, 0)

    def message(self, pk, telegram_id, text, minute, reply_to=None, user="user"):
        date = self.base + timedelta(minutes=minute)
        return {
            "_id": pk,
            "message_id": telegram_id,
            "reply_to_msg_id": reply_to,
            "chat_id": -1001,
            "text": text,
            "user": user,
            "date": date,
            "date_str": date.strftime("%Y-%m-%d %H:%M"),
            "timestamp": int(date.timestamp()),
            "link": "",
        }

    def test_external_replies_never_exceed_chunk_limit_or_drop_answers(self):
        messages = []
        for index in range(4):
            messages.append(self.message(
                index + 1,
                101 + index,
                f"source-{index}-" + "s" * 390,
                index,
                user=f"asker{index}",
            ))
        for index in range(4):
            messages.append(self.message(
                index + 5,
                201 + index,
                f"answer-{index}-" + "a" * 390,
                30 + index,
                reply_to=101 + index,
                user=f"helper{index}",
            ))

        chunks = chunk_messages(messages)
        documents = "\n".join(chunk["doc"] for chunk in chunks)

        self.assertTrue(all(len(chunk["doc"]) <= MAX_CHUNK_CHARS for chunk in chunks))
        for index in range(4):
            self.assertIn(f"answer-{index}-", documents)

    def test_source_in_same_chunk_is_not_duplicated(self):
        source = self.message(1, 101, "question", 0, user="asker")
        reply = self.message(2, 102, "answer", 5, reply_to=101, user="helper")

        chunk = chunk_messages([source, reply])[0]

        self.assertNotIn("[reply to]", chunk["doc"])
        self.assertEqual(chunk["doc"].count("question"), 1)

    def test_missing_reply_source_is_ignored(self):
        reply = self.message(2, 102, "answer", 30, reply_to=101, user="helper")

        chunk = chunk_messages([reply])[0]

        self.assertNotIn("[reply to]", chunk["doc"])
        self.assertIn("answer", chunk["doc"])


if __name__ == "__main__":
    unittest.main()
