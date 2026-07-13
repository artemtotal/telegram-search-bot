"""Persistent file queue for rebuilding edited-message Chroma chunks."""

import fcntl
import json
import logging
import os
import uuid
from typing import Dict, List, Optional, Set


log = logging.getLogger(__name__)

CHROMA_PATH = os.getenv("CHROMA_PATH", "/app/chroma")
REINDEX_QUEUE_PATH = os.getenv(
    "EMBED_REINDEX_QUEUE_PATH",
    os.path.join(CHROMA_PATH, "embed_reindex_queue.jsonl"),
)


def _parse_lines(lines) -> List[Dict]:
    requests = []
    for line in lines:
        if not line.strip():
            continue
        try:
            request = json.loads(line)
            has_base = {"request_id", "chat_id"}.issubset(request)
            has_message = "message_pk" in request
            has_chunk = {"chunk_id", "msg_id", "last_msg_id"}.issubset(request)
            if has_base and (has_message or has_chunk):
                requests.append(request)
            else:
                log.warning("Skipping incomplete reindex request: %s", request)
        except (TypeError, ValueError) as exc:
            log.warning("Skipping malformed reindex request: %s", exc)
    return requests


def enqueue_message_reindex(chat_id: int, message_pk: int) -> Optional[str]:
    """Persist an edit before any best-effort Chroma operation is attempted."""
    try:
        request = {
            "request_id": uuid.uuid4().hex,
            "chat_id": int(chat_id),
            "message_pk": int(message_pk),
        }
        queue_dir = os.path.dirname(REINDEX_QUEUE_PATH) or "."
        os.makedirs(queue_dir, exist_ok=True)
        with open(REINDEX_QUEUE_PATH, "a", encoding="utf-8") as queue_file:
            fcntl.flock(queue_file.fileno(), fcntl.LOCK_EX)
            queue_file.write(json.dumps(request, ensure_ascii=False) + "\n")
            queue_file.flush()
            os.fsync(queue_file.fileno())
            fcntl.flock(queue_file.fileno(), fcntl.LOCK_UN)
        return request["request_id"]
    except Exception as exc:
        log.error("Failed to enqueue edited message %s: %s", message_pk, exc)
        return None


def load_reindex_requests() -> List[Dict]:
    """Return a stable queue snapshot; requests stay queued until acknowledged."""
    try:
        with open(REINDEX_QUEUE_PATH, "a+", encoding="utf-8") as queue_file:
            fcntl.flock(queue_file.fileno(), fcntl.LOCK_SH)
            queue_file.seek(0)
            requests = _parse_lines(queue_file.readlines())
            fcntl.flock(queue_file.fileno(), fcntl.LOCK_UN)
            return requests
    except Exception as exc:
        log.error("Failed to read reindex queue: %s", exc)
        return []


def resolve_reindex_request(request_id: str, chunk_id: str, chat_id: int,
                            msg_id: int, last_msg_id: int) -> bool:
    """Atomically attach Chroma chunk boundaries to a durable edit request."""
    try:
        with open(REINDEX_QUEUE_PATH, "a+", encoding="utf-8") as queue_file:
            fcntl.flock(queue_file.fileno(), fcntl.LOCK_EX)
            queue_file.seek(0)
            requests = _parse_lines(queue_file.readlines())
            resolved = False
            for request in requests:
                if request["request_id"] != request_id:
                    continue
                request.update({
                    "chunk_id": str(chunk_id),
                    "chat_id": int(chat_id),
                    "msg_id": int(msg_id),
                    "last_msg_id": int(last_msg_id),
                })
                resolved = True
                break
            if not resolved:
                return False

            queue_file.seek(0)
            queue_file.truncate()
            for request in requests:
                queue_file.write(json.dumps(request, ensure_ascii=False) + "\n")
            queue_file.flush()
            os.fsync(queue_file.fileno())
            fcntl.flock(queue_file.fileno(), fcntl.LOCK_UN)
            return True
    except Exception as exc:
        log.error("Failed to resolve reindex request %s: %s", request_id, exc)
        return False


def find_chroma_chunks(collection, chat_id: int, message_pk: int) -> List[Dict]:
    """Return Chroma chunks whose metadata range contains one SQL message pk."""
    result = collection.get(
        where={
            "$and": [
                {"chat_id": {"$eq": int(chat_id)}},
                {"msg_id": {"$lte": int(message_pk)}},
                {"last_msg_id": {"$gte": int(message_pk)}},
            ]
        },
        include=["metadatas"],
    )
    return [
        {
            "chunk_id": chunk_id,
            "chat_id": int(metadata["chat_id"]),
            "msg_id": int(metadata["msg_id"]),
            "last_msg_id": int(metadata["last_msg_id"]),
        }
        for chunk_id, metadata in zip(
            result.get("ids", []), result.get("metadatas", []),
        )
    ]


def remove_reindex_requests(request_ids: Set[str]) -> None:
    """Acknowledge only snapshot requests that were rebuilt successfully."""
    if not request_ids:
        return
    try:
        with open(REINDEX_QUEUE_PATH, "a+", encoding="utf-8") as queue_file:
            fcntl.flock(queue_file.fileno(), fcntl.LOCK_EX)
            queue_file.seek(0)
            requests = _parse_lines(queue_file.readlines())
            remaining = [
                request for request in requests
                if request["request_id"] not in request_ids
            ]
            queue_file.seek(0)
            queue_file.truncate()
            for request in remaining:
                queue_file.write(json.dumps(request, ensure_ascii=False) + "\n")
            queue_file.flush()
            os.fsync(queue_file.fileno())
            fcntl.flock(queue_file.fileno(), fcntl.LOCK_UN)
    except Exception as exc:
        log.error("Failed to acknowledge reindex requests: %s", exc)
