import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

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


class EmbedUpdaterBootstrapTests(unittest.TestCase):
    def test_missing_state_bootstraps_recent_history(self):
        plan = embed_updater._plan_index_window(
            state={}, collection_count=0,
        )

        self.assertEqual(plan["mode"], "bootstrap_recent")
        self.assertEqual(plan["after_id"], 0)
        self.assertIsNotNone(plan["since"])

    def test_existing_index_without_state_repairs_full_history(self):
        plan = embed_updater._plan_index_window(
            state={}, collection_count=1200,
        )

        self.assertEqual(plan["mode"], "repair_full")
        self.assertEqual(plan["after_id"], 0)
        self.assertIsNone(plan["since"])

    def test_saved_state_continues_incrementally(self):
        plan = embed_updater._plan_index_window(
            state={"last_id": 900, "history_mode": "full"},
            collection_count=1200,
        )

        self.assertEqual(plan["mode"], "incremental")
        self.assertEqual(plan["after_id"], 900)
        self.assertIsNone(plan["since"])

    def test_successful_recent_bootstrap_stays_recent_until_window_is_exhausted(self):
        next_state = embed_updater._next_index_state(
            plan={"mode": "bootstrap_recent"},
            previous_last_id=0,
            max_id=3000,
            source_window_exhausted=False,
        )

        self.assertEqual(next_state, {"last_id": 3000, "history_mode": "recent"})

    def test_exhausted_recent_bootstrap_switches_to_full_incremental_mode(self):
        next_state = embed_updater._next_index_state(
            plan={"mode": "bootstrap_recent"},
            previous_last_id=3000,
            max_id=5000,
            source_window_exhausted=True,
        )

        self.assertEqual(next_state, {"last_id": 5000, "history_mode": "full"})

    def test_full_repair_tracks_database_high_water_mark_during_repair(self):
        next_state = embed_updater._next_index_state(
            plan={"mode": "repair_full"},
            previous_last_id=162248,
            max_id=3000,
            source_window_exhausted=False,
            source_high_water_id=162400,
        )

        self.assertEqual(next_state, {
            "last_id": 162400,
            "history_mode": "repairing",
            "repair_cursor": 3000,
        })

    def test_full_repair_preserves_previous_database_high_water_mark(self):
        next_state = embed_updater._next_index_state(
            plan={"mode": "repair_full"},
            previous_last_id=162248,
            max_id=3000,
            source_window_exhausted=False,
            source_high_water_id=162100,
        )

        self.assertEqual(next_state, {
            "last_id": 162248,
            "history_mode": "repairing",
            "repair_cursor": 3000,
        })

    def test_exhausted_full_repair_advances_incremental_high_water_mark(self):
        next_state = embed_updater._next_index_state(
            plan={"mode": "repair_full", "after_id": 160000},
            previous_last_id=0,
            max_id=162300,
            source_window_exhausted=True,
        )

        self.assertEqual(next_state, {
            "last_id": 162300,
            "history_mode": "full",
        })

    def test_empty_full_repair_uses_repair_cursor_as_high_water_mark(self):
        next_state = embed_updater._next_index_state(
            plan={"mode": "repair_full", "after_id": 162300},
            previous_last_id=0,
            max_id=162300,
            source_window_exhausted=True,
        )

        self.assertEqual(next_state, {
            "last_id": 162300,
            "history_mode": "full",
        })

    def test_legacy_state_triggers_full_history_repair(self):
        plan = embed_updater._plan_index_window(
            state={"last_id": 162246}, collection_count=34397,
        )

        self.assertEqual(plan["mode"], "repair_full")
        self.assertEqual(plan["after_id"], 0)

    def test_full_history_repair_resumes_from_its_cursor(self):
        plan = embed_updater._plan_index_window(
            state={
                "last_id": 162248,
                "history_mode": "repairing",
                "repair_cursor": 3000,
            },
            collection_count=34397,
        )

        self.assertEqual(plan["mode"], "repair_full")
        self.assertEqual(plan["after_id"], 3000)

    def test_failed_embedding_must_not_advance_state(self):
        self.assertFalse(embed_updater._can_advance_state(todo_count=3, indexed_count=2))
        self.assertTrue(embed_updater._can_advance_state(todo_count=3, indexed_count=3))


class EmbedUpdaterLockTests(unittest.TestCase):
    def test_overlapping_worker_is_skipped(self):
        embed_updater._embed_update_lock.acquire()
        try:
            with mock.patch.object(embed_updater, "_embed_update_worker") as worker:
                completed = embed_updater._run_embed_update_once()
        finally:
            embed_updater._embed_update_lock.release()

        self.assertFalse(completed)
        worker.assert_not_called()

    def test_lock_is_released_after_worker_error(self):
        with mock.patch.object(
            embed_updater,
            "_embed_update_worker",
            side_effect=RuntimeError("worker failed"),
        ):
            with self.assertRaises(RuntimeError):
                embed_updater._run_embed_update_once()

        self.assertTrue(embed_updater._embed_update_lock.acquire(blocking=False))
        embed_updater._embed_update_lock.release()


if __name__ == "__main__":
    unittest.main()
