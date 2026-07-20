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


class QdrantUpdaterCheckpointTests(unittest.TestCase):
    """A batch-embedding timeout must not discard already-indexed progress."""

    @staticmethod
    def _chunks(*spans):
        return [
            {"id": f"c-1001_{first}", "doc": "x",
             "metadata": {"msg_id": first, "last_msg_id": last, "chat_id": -1001}}
            for first, last in spans
        ]

    def test_checkpoint_last_id_covers_only_completed_chunks(self):
        chunks = self._chunks((100, 105), (106, 110), (111, 120), (121, 130))
        # First two batches (chunks 0..1) indexed; last completed chunk ends at 110,
        # first pending chunk starts at 111. Safe checkpoint is 110 (< 111).
        checkpoint = qdrant_updater.checkpoint_last_id(chunks, indexed_count=2)
        self.assertEqual(checkpoint, 110)

    def test_checkpoint_last_id_zero_when_nothing_indexed(self):
        chunks = self._chunks((100, 105), (106, 110))
        self.assertEqual(qdrant_updater.checkpoint_last_id(chunks, indexed_count=0), 0)

    def test_checkpoint_last_id_full_batch_uses_last_chunk_end(self):
        chunks = self._chunks((100, 105), (106, 110), (111, 120))
        self.assertEqual(
            qdrant_updater.checkpoint_last_id(chunks, indexed_count=3), 120
        )

    def test_checkpoint_never_regresses_past_pending_chunk_start(self):
        # A completed chunk may span past the next pending chunk's start; the
        # checkpoint must never exceed the first pending chunk's msg_id-1.
        chunks = self._chunks((100, 130), (105, 108), (140, 150))
        checkpoint = qdrant_updater.checkpoint_last_id(chunks, indexed_count=1)
        self.assertEqual(checkpoint, 104)  # min(pending msg_id)=105 -> 104


class QdrantBatchEmbedRetryTests(unittest.TestCase):
    """A transient embedding timeout should be retried with smaller sub-batches."""

    def test_empty_input_returns_empty(self):
        self.assertEqual(qdrant_updater._batch_embed([]), [])

    def test_successful_first_attempt_returns_vectors(self):
        with mock.patch.object(
            qdrant_updater, "embed_in_subprocess",
            return_value=[[0.0] * 3, [0.1] * 3],
        ) as embed:
            result = qdrant_updater._batch_embed(["a", "b"])
        self.assertEqual(len(result), 2)
        self.assertEqual(embed.call_count, 1)

    def test_timeout_splits_batch_and_recovers(self):
        # First whole-batch call fails; the two halves each succeed on retry.
        calls = []

        def fake_embed(texts, timeout=240):
            calls.append(list(texts))
            if len(texts) == 4:
                raise RuntimeError("timed out after 240 seconds")
            return [[0.0] * 3 for _ in texts]

        with mock.patch.object(qdrant_updater, "embed_in_subprocess", side_effect=fake_embed):
            result = qdrant_updater._batch_embed(["a", "b", "c", "d"])

        self.assertEqual(len(result), 4)
        # whole batch (4) tried, then two halves of 2
        self.assertEqual([len(c) for c in calls], [4, 2, 2])

    def test_single_item_failure_returns_empty(self):
        with mock.patch.object(
            qdrant_updater, "embed_in_subprocess",
            side_effect=RuntimeError("timed out after 240 seconds"),
        ):
            result = qdrant_updater._batch_embed(["x"])
        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
