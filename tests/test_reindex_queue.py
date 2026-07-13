import tempfile
import unittest
from pathlib import Path

from user_jobs import reindex_queue


class ReindexQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_path = reindex_queue.REINDEX_QUEUE_PATH
        reindex_queue.REINDEX_QUEUE_PATH = str(
            Path(self.temp_dir.name) / "reindex.jsonl"
        )

    def tearDown(self):
        reindex_queue.REINDEX_QUEUE_PATH = self.original_path
        self.temp_dir.cleanup()

    def test_raw_request_is_durable_before_chunk_resolution(self):
        request_id = reindex_queue.enqueue_message_reindex(-1001, 42)

        requests = reindex_queue.load_reindex_requests()

        self.assertIsNotNone(request_id)
        self.assertEqual(requests, [{
            "request_id": request_id,
            "chat_id": -1001,
            "message_pk": 42,
        }])

    def test_request_resolution_persists_chunk_boundaries(self):
        request_id = reindex_queue.enqueue_message_reindex(-1001, 42)

        resolved = reindex_queue.resolve_reindex_request(
            request_id, "c-1001_40", -1001, 40, 47,
        )
        request = reindex_queue.load_reindex_requests()[0]

        self.assertTrue(resolved)
        self.assertEqual(request["chunk_id"], "c-1001_40")
        self.assertEqual(request["msg_id"], 40)
        self.assertEqual(request["last_msg_id"], 47)

    def test_acknowledgement_preserves_requests_appended_after_snapshot(self):
        first_id = reindex_queue.enqueue_message_reindex(-1001, 42)
        snapshot = reindex_queue.load_reindex_requests()
        second_id = reindex_queue.enqueue_message_reindex(-1001, 43)

        reindex_queue.remove_reindex_requests({snapshot[0]["request_id"]})
        remaining = reindex_queue.load_reindex_requests()

        self.assertEqual(first_id, snapshot[0]["request_id"])
        self.assertEqual([request["request_id"] for request in remaining], [second_id])

    def test_chroma_metadata_lookup_uses_message_range(self):
        class Collection:
            def get(self, **kwargs):
                self.kwargs = kwargs
                return {
                    "ids": ["c-1001_40"],
                    "metadatas": [{
                        "chat_id": -1001,
                        "msg_id": 40,
                        "last_msg_id": 47,
                    }],
                }

        collection = Collection()
        chunks = reindex_queue.find_chroma_chunks(collection, -1001, 42)

        self.assertEqual(chunks[0]["chunk_id"], "c-1001_40")
        filters = collection.kwargs["where"]["$and"]
        self.assertIn({"msg_id": {"$lte": 42}}, filters)
        self.assertIn({"last_msg_id": {"$gte": 42}}, filters)


if __name__ == "__main__":
    unittest.main()
