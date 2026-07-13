import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

try:
    import requests  # noqa: F401
except ModuleNotFoundError:
    sys.modules["requests"] = SimpleNamespace()

from user_jobs import embed_updater, reindex_queue


class ReindexResolutionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_path = reindex_queue.REINDEX_QUEUE_PATH
        reindex_queue.REINDEX_QUEUE_PATH = str(
            Path(self.temp_dir.name) / "reindex.jsonl"
        )

    def tearDown(self):
        reindex_queue.REINDEX_QUEUE_PATH = self.original_path
        self.temp_dir.cleanup()

    @staticmethod
    def collection(chunk_id="c-1001_40"):
        class Collection:
            def get(self, **kwargs):
                return {
                    "ids": [chunk_id],
                    "metadatas": [{
                        "chat_id": -1001,
                        "msg_id": 40,
                        "last_msg_id": 47,
                    }],
                }

        return Collection()

    def test_raw_request_is_resolved_by_worker_after_chroma_recovers(self):
        request_id = reindex_queue.enqueue_message_reindex(-1001, 42)
        requests = reindex_queue.load_reindex_requests()

        groups = embed_updater._resolve_reindex_groups(
            self.collection(), requests,
        )
        persisted = reindex_queue.load_reindex_requests()[0]

        self.assertIn("c-1001_40", groups)
        self.assertEqual(groups["c-1001_40"]["request_ids"], {request_id})
        self.assertEqual(persisted["chunk_id"], "c-1001_40")

    def test_raw_request_remains_unresolved_during_chroma_failure(self):
        class BrokenCollection:
            def get(self, **kwargs):
                raise RuntimeError("temporary Chroma lock")

        request_id = reindex_queue.enqueue_message_reindex(-1001, 42)
        requests = reindex_queue.load_reindex_requests()

        groups = embed_updater._resolve_reindex_groups(
            BrokenCollection(), requests,
        )
        persisted = reindex_queue.load_reindex_requests()[0]

        self.assertEqual(groups, {})
        self.assertEqual(persisted["request_id"], request_id)
        self.assertNotIn("chunk_id", persisted)

    def test_second_edit_attaches_to_already_resolved_deleted_chunk(self):
        first_id = reindex_queue.enqueue_message_reindex(-1001, 42)
        reindex_queue.resolve_reindex_request(
            first_id, "c-1001_40", -1001, 40, 47,
        )
        second_id = reindex_queue.enqueue_message_reindex(-1001, 43)

        class MustNotQueryCollection:
            def get(self, **kwargs):
                raise AssertionError("covering queued chunk should be reused")

        groups = embed_updater._resolve_reindex_groups(
            MustNotQueryCollection(),
            reindex_queue.load_reindex_requests(),
        )

        self.assertEqual(
            groups["c-1001_40"]["request_ids"],
            {first_id, second_id},
        )


if __name__ == "__main__":
    unittest.main()
