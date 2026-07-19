"""Resumable SQLite-to-Qdrant embedding indexer."""

import argparse
import json
import logging
import os
import sys
import time
from typing import Dict, List

sys.path.insert(0, "/app")

from user_jobs.local_embeddings import embed_in_subprocess
from user_jobs.qdrant_store import (
    collection_count,
    ensure_collection,
    find_chunks_covering,
    delete_chunks,
    upsert_chunks,
    wait_until_ready,
)
from user_jobs.reindex_queue import (
    load_reindex_requests,
    remove_reindex_requests,
    resolve_reindex_request,
)

log = logging.getLogger(__name__)

STATE_PATH = os.getenv("QDRANT_STATE_PATH", "/app/qdrant-state/embed_state.json")
BATCH_SIZE = int(os.getenv("QDRANT_EMBED_BATCH_SIZE", "40"))
MAX_PER_RUN = int(os.getenv("QDRANT_MAX_PER_RUN", "12000"))
OVERLAP_MESSAGES = int(os.getenv("QDRANT_OVERLAP_MESSAGES", "16"))


def load_state() -> Dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as state_file:
            state = json.load(state_file)
            return {
                "last_id": int(state.get("last_id", 0)),
                "history_mode": state.get("history_mode", "building"),
            }
    except Exception:
        return {"last_id": 0, "history_mode": "building"}


def save_state(state: Dict) -> None:
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    temp_path = STATE_PATH + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as state_file:
        json.dump(state, state_file)
        state_file.flush()
        os.fsync(state_file.fileno())
    os.replace(temp_path, STATE_PATH)


def next_state(previous: Dict, max_id: int, exhausted: bool) -> Dict:
    return {
        "last_id": max(int(previous.get("last_id", 0)), int(max_id)),
        "history_mode": "full" if exhausted else "building",
    }


def query_after_id(state: Dict) -> int:
    """Overlap source rows so batch-boundary chunks are rebuilt safely."""
    last_id = int(state.get("last_id", 0))
    if last_id <= 0:
        return 0
    return max(0, last_id - OVERLAP_MESSAGES)


def _batch_embed(texts: List[str]) -> List[List[float]]:
    try:
        return embed_in_subprocess([text[:2000] for text in texts], timeout=240)
    except Exception as exc:
        log.error("Qdrant embedding subprocess failed: %s", exc)
        return []


def _resolve_reindex_groups(requests):
    groups = {}
    for request in requests:
        if {"chunk_id", "msg_id", "last_msg_id"}.issubset(request):
            candidates = [request]
        else:
            try:
                candidates = find_chunks_covering(
                    int(request["chat_id"]), int(request["message_pk"])
                )
            except Exception as exc:
                log.warning("Could not resolve edited message in Qdrant: %s", exc)
                continue
            if candidates:
                candidate = candidates[0]
                if not resolve_reindex_request(
                    request["request_id"], candidate["chunk_id"],
                    candidate["chat_id"], candidate["msg_id"],
                    candidate["last_msg_id"],
                ):
                    continue
        for candidate in candidates[:1]:
            group = groups.setdefault(candidate["chunk_id"], {
                "request": candidate,
                "request_ids": set(),
            })
            group["request_ids"].add(request["request_id"])
    return groups


def process_reindex_queue() -> int:
    requests = load_reindex_requests()
    if not requests:
        return 0

    from database import DBSession, Message, User
    from user_jobs.chunking import chunk_messages, rows_to_msg_dicts

    acknowledged = set()
    rebuilt = 0
    for chunk_id, group in _resolve_reindex_groups(requests).items():
        request = group["request"]
        session = DBSession()
        try:
            rows = (
                session.query(Message, User)
                .outerjoin(User, Message.from_id == User.id)
                .filter(Message.from_chat == int(request["chat_id"]))
                .filter(Message._id >= int(request["msg_id"]))
                .filter(Message._id <= int(request["last_msg_id"]))
                .filter(Message.text.isnot(None))
                .filter(Message.text != "")
                .order_by(Message._id.asc())
                .all()
            )
        finally:
            session.close()
        chunks = chunk_messages(rows_to_msg_dicts(rows))
        if not chunks:
            delete_chunks([chunk_id])
            acknowledged.update(group["request_ids"])
            continue
        vectors = _batch_embed([chunk["doc"] for chunk in chunks])
        if len(vectors) != len(chunks):
            continue
        delete_chunks([chunk_id])
        upsert_chunks(chunks, vectors)
        acknowledged.update(group["request_ids"])
        rebuilt += len(chunks)
    remove_reindex_requests(acknowledged)
    return rebuilt


def run_once() -> Dict:
    from database import DBSession, Message, User, Chat
    from user_jobs.chunking import chunk_messages, rows_to_msg_dicts

    wait_until_ready()
    ensure_collection()
    state = load_state()
    last_id = int(state.get("last_id", 0))
    after_id = query_after_id(state)

    session = DBSession()
    try:
        chat_ids = [chat.id for chat in session.query(Chat).filter(Chat.enable == 1).all()]
        if not chat_ids:
            raise RuntimeError("No enabled chats available for Qdrant indexing")
        query = (
            session.query(Message, User)
            .outerjoin(User, Message.from_id == User.id)
            .filter(Message.from_chat.in_(chat_ids))
            .filter(Message.text.isnot(None))
            .filter(Message.text != "")
            .filter(Message._id > after_id)
            .order_by(Message._id.asc())
        )
        rows = query.limit(MAX_PER_RUN + 1).all()
    finally:
        session.close()

    exhausted = len(rows) <= MAX_PER_RUN
    rows = rows[:MAX_PER_RUN]
    if not rows:
        completed = next_state(state, last_id, exhausted=True)
        save_state(completed)
        process_reindex_queue()
        return completed

    max_id = max(message._id for message, _ in rows)
    messages = rows_to_msg_dicts(rows)
    messages.sort(key=lambda message: (message["chat_id"], message["_id"]))
    chunks = chunk_messages(messages)
    log.info(
        "Qdrant updater: %s source messages -> %s chunks after_id=%s",
        len(rows), len(chunks), after_id,
    )

    indexed = 0
    for start in range(0, len(chunks), BATCH_SIZE):
        batch = chunks[start:start + BATCH_SIZE]
        vectors = _batch_embed([chunk["doc"] for chunk in batch])
        if len(vectors) != len(batch):
            raise RuntimeError(
                f"Qdrant indexed only {indexed}/{len(chunks)} chunks; state unchanged"
            )
        upsert_chunks(batch, vectors)
        indexed += len(batch)
        if indexed % 400 == 0:
            log.info("Qdrant updater progress: %s/%s chunks", indexed, len(chunks))

    state = next_state(state, max_id, exhausted)
    save_state(state)
    if state["history_mode"] == "full":
        process_reindex_queue()
    log.info(
        "Qdrant updater done: indexed=%s last_id=%s mode=%s points=%s",
        indexed, state["last_id"], state["history_mode"], collection_count(),
    )
    return state


def build_until_full() -> None:
    while True:
        state = run_once()
        if state["history_mode"] == "full":
            return
        time.sleep(1)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO
    )
    if args.once:
        run_once()
    else:
        build_until_full()


if __name__ == "__main__":
    main()
