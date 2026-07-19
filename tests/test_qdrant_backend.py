import json
import os
import sys
import tempfile
from types import SimpleNamespace
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if os.name == "nt" and "fcntl" not in sys.modules:
    sys.modules["fcntl"] = SimpleNamespace(
        flock=lambda *args, **kwargs: None,
        LOCK_EX=1,
        LOCK_SH=2,
        LOCK_UN=8,
    )

from user_jobs import qdrant_store, qdrant_updater


class QdrantStoreTests(unittest.TestCase):
    def test_chunk_point_id_is_deterministic_uuid(self):
        first = qdrant_store.point_id_for_chunk("c-1001_42")
        second = qdrant_store.point_id_for_chunk("c-1001_42")

        self.assertEqual(first, second)
        self.assertRegex(first, r"^[0-9a-f-]{36}$")

    def test_ensure_collection_creates_384_cosine_collection(self):
        not_found = mock.Mock(status_code=404)
        created = mock.Mock(status_code=200)
        created.raise_for_status.return_value = None

        indexed = mock.Mock(status_code=200)
        indexed.raise_for_status.return_value = None

        with mock.patch.object(
            qdrant_store.requests, "request",
            side_effect=[not_found, created, indexed, indexed, indexed, indexed],
        ) as request:
            qdrant_store.ensure_collection()

        method, url = request.call_args_list[1].args[:2]
        payload = request.call_args_list[1].kwargs["json"]
        self.assertEqual(method, "PUT")
        self.assertTrue(url.endswith("/collections/chunks_minilm_v1"))
        self.assertEqual(payload["vectors"], {"size": 384, "distance": "Cosine"})

    def test_upsert_chunks_preserves_document_and_metadata(self):
        chunk = {
            "id": "c-1001_42",
            "doc": "[2026-01-01] @master: ремонт меблів",
            "metadata": {
                "msg_id": 42,
                "last_msg_id": 45,
                "timestamp": 1767225600,
                "chat_id": -1001,
                "user": "master",
                "date": "2026-01-01 10:00",
                "link": "https://t.me/example/42",
            },
        }
        response = mock.Mock(status_code=200)
        response.raise_for_status.return_value = None

        with mock.patch.object(qdrant_store.requests, "request", return_value=response) as request:
            qdrant_store.upsert_chunks([chunk], [[0.1] * 384])

        body = request.call_args.kwargs["json"]
        point = body["points"][0]
        self.assertEqual(point["payload"]["document"], chunk["doc"])
        self.assertEqual(point["payload"]["chunk_id"], chunk["id"])
        self.assertEqual(point["payload"]["msg_id"], 42)
        self.assertEqual(len(point["vector"]), 384)

    def test_search_normalizes_qdrant_hits(self):
        response = mock.Mock(status_code=200)
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "result": [{
                "id": "uuid",
                "score": 0.71,
                "payload": {
                    "msg_id": 42,
                    "chat_id": -1001,
                    "date": "2026-01-01 10:00",
                    "timestamp": 1767225600,
                    "document": "ремонт меблів",
                    "user": "master",
                    "link": "https://t.me/example/42",
                },
            }]
        }

        with mock.patch.object(qdrant_store.requests, "request", return_value=response) as request:
            results = qdrant_store.search([0.2] * 384, limit=12, since_days=365)

        self.assertEqual(results[0]["id"], 42)
        self.assertEqual(results[0]["text"], "ремонт меблів")
        body = request.call_args.kwargs["json"]
        self.assertEqual(body["limit"], 12)
        self.assertIn("filter", body)


class QdrantUpdaterStateTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_state = qdrant_updater.STATE_PATH
        qdrant_updater.STATE_PATH = str(Path(self.temp_dir.name) / "state.json")

    def tearDown(self):
        qdrant_updater.STATE_PATH = self.old_state
        self.temp_dir.cleanup()

    def test_missing_state_starts_full_shadow_build(self):
        self.assertEqual(qdrant_updater.load_state(), {
            "last_id": 0,
            "history_mode": "building",
        })

    def test_non_exhausted_batch_remains_building(self):
        state = qdrant_updater.next_state(
            previous={"last_id": 0, "history_mode": "building"},
            max_id=5000,
            exhausted=False,
        )
        self.assertEqual(state, {"last_id": 5000, "history_mode": "building"})

    def test_exhausted_batch_activates_incremental_mode(self):
        state = qdrant_updater.next_state(
            previous={"last_id": 5000, "history_mode": "building"},
            max_id=9000,
            exhausted=True,
        )
        self.assertEqual(state, {"last_id": 9000, "history_mode": "full"})

    def test_index_batches_overlap_to_preserve_boundary_chunks(self):
        with mock.patch.object(qdrant_updater, "OVERLAP_MESSAGES", 16):
            self.assertEqual(qdrant_updater.query_after_id({"last_id": 9000}), 8984)
            self.assertEqual(qdrant_updater.query_after_id({"last_id": 0}), 0)


if __name__ == "__main__":
    unittest.main()
