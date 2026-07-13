"""Persistent file queue for rebuilding edited-message Chroma chunks."""

import fcntl
import json
import logging
import os
import uuid
from typing import Dict, List, Set


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
            required = {
                "request_id", "chunk_id", "chat_id", "msg_id", "last_msg_id",
            }
            if required.issubset(request):
                requests.append(request)
            else:
                log.warning("Skipping incomplete reindex request: %s", request)
        except (TypeError, ValueError) as exc:
            log.warning("Skipping malformed reindex request: %s", exc)
    return requests


def enqueue_reindex_request(chunk_id: str, chat_id: int,
                            msg_id: int, last_msg_id: int) -> bool:
    """Append a chunk rebuild request without raising into message handlers."""
    request = {
        "request_id": uuid.uuid4().hex,
        "chunk_id": str(chunk_id),
        "chat_id": int(chat_id),
        "msg_id": int(msg_id),
        "last_msg_id": int(last_msg_id),
    }
    try:
        queue_dir = os.path.dirname(REINDEX_QUEUE_PATH) or "."
        os.makedirs(queue_dir, exist_ok=True)
        with open(REINDEX_QUEUE_PATH, "a", encoding="utf-8") as queue_file:
            fcntl.flock(queue_file.fileno(), fcntl.LOCK_EX)
            queue_file.write(json.dumps(request, ensure_ascii=False) + "\n")
            queue_file.flush()
            os.fsync(queue_file.fileno())
            fcntl.flock(queue_file.fileno(), fcntl.LOCK_UN)
        return True
    except Exception as exc:
        log.error("Failed to enqueue Chroma chunk %s: %s", chunk_id, exc)
        return False


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


def find_queued_chunks(chat_id: int, message_pk: int) -> List[Dict]:
    """Find already queued chunks that contain a subsequently edited message."""
    matches = {}
    for request in load_reindex_requests():
        if (
            int(request["chat_id"]) == int(chat_id)
            and int(request["msg_id"]) <= int(message_pk)
            and int(request["last_msg_id"]) >= int(message_pk)
        ):
            matches[request["chunk_id"]] = request
    return list(matches.values())


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
