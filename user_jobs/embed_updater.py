"""
Incremental embedding updater job (chunk-based).
Runs hourly: takes messages newer than the saved high-water mark,
groups them into conversation windows and indexes into the "chunks"
collection. State (last processed message pk) is kept in a JSON file
next to the ChromaDB data.

Registered in robot.py as a scheduled job.
"""

import json
import logging
import os
import sys
import threading
import time
from datetime import datetime, timedelta
from typing import List

import requests

sys.path.insert(0, "/app")

from user_jobs.reindex_queue import (
    find_chroma_chunks,
    load_reindex_requests,
    remove_reindex_requests,
    resolve_reindex_request,
)

log = logging.getLogger(__name__)
_embed_update_lock = threading.Lock()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
CHROMA_PATH    = os.getenv("CHROMA_PATH", "/app/chroma")
EMBED_MODEL    = "gemini-embedding-001"
BATCH_SIZE     = 40
MAX_PER_RUN    = 3000   # messages per hourly run (self-bootstraps gradually)
COLLECTION     = "chunks"
STATE_PATH     = os.path.join(CHROMA_PATH, "embed_state.json")
BOOTSTRAP_DAYS = int(os.getenv("EMBED_BOOTSTRAP_DAYS", "730"))

EMBED_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"{EMBED_MODEL}:batchEmbedContents?key={{api_key}}"
)


def _batch_embed(texts: List[str]) -> List[List[float]]:
    url = EMBED_URL.format(api_key=GEMINI_API_KEY)
    payload = {
        "requests": [
            {
                "model": f"models/{EMBED_MODEL}",
                "content": {"parts": [{"text": t[:2000]}]},
                "taskType": "RETRIEVAL_DOCUMENT",
                "outputDimensionality": 768,
            }
            for t in texts
        ]
    }
    for attempt in range(3):
        try:
            resp = requests.post(url, json=payload, timeout=60)
            if resp.status_code == 429:
                time.sleep(60)
                continue
            resp.raise_for_status()
            return [e["values"] for e in resp.json().get("embeddings", [])]
        except Exception as e:
            log.warning(f"Embed attempt {attempt+1} failed: {e}")
            time.sleep(5)
    return []


def _load_state() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"last_id": 0}


def _plan_index_window(state_exists: bool, collection_count: int, last_id: int) -> dict:
    """Choose safe startup mode for a missing, existing, or tracked index."""
    if state_exists:
        return {"mode": "incremental", "after_id": last_id, "since": None}
    if collection_count > 0:
        # Deterministic chunk ids make this full repair idempotent: existing
        # chunks are skipped, while gaps anywhere in history are filled.
        return {"mode": "repair_full", "after_id": 0, "since": None}
    return {
        "mode": "bootstrap_recent",
        "after_id": 0,
        "since": datetime.utcnow() - timedelta(days=BOOTSTRAP_DAYS),
    }


def _can_advance_state(todo_count: int, indexed_count: int) -> bool:
    """Advance the high-water mark only after every missing chunk succeeds."""
    return todo_count == indexed_count


def _save_state(state: dict) -> None:
    try:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except Exception as e:
        log.error(f"Failed to save embed state: {e}")


def run_embed_update(context=None):
    """Scheduler entry point — immediately hands off to a daemon thread."""
    t = threading.Thread(
        target=_run_embed_update_once,
        args=(context,),
        daemon=True,
        name="embed-update",
    )
    t.start()


def _run_embed_update_once(context=None) -> bool:
    """Run one worker at a time; skip overlapping hourly invocations."""
    if not _embed_update_lock.acquire(blocking=False):
        log.warning("Embed updater is already running; skipping overlapping invocation")
        return False
    try:
        _embed_update_worker(context)
        return True
    finally:
        _embed_update_lock.release()


def _add_reindex_group(groups, request):
    chunk_id = request["chunk_id"]
    group = groups.setdefault(chunk_id, {"request": request, "request_ids": set()})
    group["request_ids"].add(request["request_id"])
    return group


def _resolve_reindex_groups(col, requests_to_process):
    """Resolve raw edit requests to durable Chroma chunk ranges."""
    groups = {}
    unresolved = []
    chunk_fields = {"chunk_id", "msg_id", "last_msg_id"}

    for request in requests_to_process:
        if chunk_fields.issubset(request):
            _add_reindex_group(groups, request)
        else:
            unresolved.append(request)

    for request in unresolved:
        try:
            chat_id = int(request["chat_id"])
            message_pk = int(request["message_pk"])
        except (KeyError, TypeError, ValueError) as exc:
            log.error("Invalid raw reindex request %s: %s", request, exc)
            continue

        covering_group = next((
            group for group in groups.values()
            if int(group["request"]["chat_id"]) == chat_id
            and int(group["request"]["msg_id"]) <= message_pk
            and int(group["request"]["last_msg_id"]) >= message_pk
        ), None)
        if covering_group is not None:
            covering_group["request_ids"].add(request["request_id"])
            continue

        try:
            candidates = find_chroma_chunks(col, chat_id, message_pk)
        except Exception as exc:
            log.warning("Could not resolve queued message %s in Chroma: %s", message_pk, exc)
            continue
        if not candidates:
            log.warning("Queued message %s has no Chroma chunk yet", message_pk)
            continue

        candidate = candidates[0]
        if not resolve_reindex_request(
            request["request_id"],
            candidate["chunk_id"],
            candidate["chat_id"],
            candidate["msg_id"],
            candidate["last_msg_id"],
        ):
            continue

        resolved_request = dict(request)
        resolved_request.update(candidate)
        _add_reindex_group(groups, resolved_request)

    return groups


def _process_reindex_queue(col) -> int:
    """Rebuild queued edited-message chunks without touching the high-water mark."""
    requests_to_process = load_reindex_requests()
    if not requests_to_process:
        return 0

    from database import DBSession, Message, User
    from user_jobs.chunking import chunk_messages, rows_to_msg_dicts

    grouped = _resolve_reindex_groups(col, requests_to_process)

    acknowledged = set()
    rebuilt = 0
    for chunk_id, group in grouped.items():
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
        except Exception as exc:
            log.error("Failed to load queued Chroma chunk %s: %s", chunk_id, exc)
            continue
        finally:
            session.close()

        try:
            msgs = rows_to_msg_dicts(rows)
            msgs.sort(key=lambda message: (message["chat_id"], message["_id"]))
            chunks = chunk_messages(msgs)

            if not chunks:
                col.delete(ids=[chunk_id])
                acknowledged.update(group["request_ids"])
                continue

            embeddings = _batch_embed([chunk["doc"] for chunk in chunks])
            if not embeddings or len(embeddings) != len(chunks):
                log.warning("Embedding failed for queued Chroma chunk %s", chunk_id)
                continue

            col.delete(ids=[chunk_id])
            col.upsert(
                ids=[chunk["id"] for chunk in chunks],
                embeddings=embeddings,
                documents=[chunk["doc"] for chunk in chunks],
                metadatas=[chunk["metadata"] for chunk in chunks],
            )
            acknowledged.update(group["request_ids"])
            rebuilt += len(chunks)
            time.sleep(0.15)
        except Exception as exc:
            log.error("Failed to rebuild queued Chroma chunk %s: %s", chunk_id, exc)

    remove_reindex_requests(acknowledged)
    log.info(
        "Embed updater: rebuilt %s edited chunks, %s requests remain queued",
        rebuilt,
        len(requests_to_process) - len(acknowledged),
    )
    return rebuilt


def _embed_update_worker(context=None):
    """Called by APScheduler every hour."""
    if not GEMINI_API_KEY:
        return

    try:
        import chromadb
        from database import DBSession, Message, User, Chat
        from user_jobs.chunking import chunk_messages, rows_to_msg_dicts

        client = chromadb.PersistentClient(path=CHROMA_PATH)
        col = client.get_or_create_collection(
            name=COLLECTION,
            metadata={"hnsw:space": "cosine"},
        )

        _process_reindex_queue(col)

        state_exists = os.path.isfile(STATE_PATH)
        state = _load_state()
        last_id = int(state.get("last_id", 0))
        existing_ids = set(col.get(include=[])["ids"])
        plan = _plan_index_window(state_exists, len(existing_ids), last_id)
        log.info(
            "Embed updater mode=%s after_id=%s since=%s existing_chunks=%s",
            plan["mode"], plan["after_id"], plan["since"], len(existing_ids),
        )

        session = DBSession()
        chat_ids = [c.id for c in session.query(Chat).filter(Chat.enable == 1).all()]
        query = (
            session.query(Message, User)
            .outerjoin(User, Message.from_id == User.id)
            .filter(Message.from_chat.in_(chat_ids))
            .filter(Message.text.isnot(None))
            .filter(Message.text != "")
            .filter(Message._id > plan["after_id"])
        )
        if plan["since"] is not None:
            query = query.filter(Message.date >= plan["since"])
        rows = query.order_by(Message._id.asc()).limit(MAX_PER_RUN).all()
        session.close()

        if not rows:
            log.debug("Embed updater: nothing new to index")
            if plan["mode"] != "incremental":
                _save_state({"last_id": last_id})
            return

        max_id = max(msg._id for msg, _ in rows)

        msgs = rows_to_msg_dicts(rows)
        # Chunker expects (chat_id, _id) ordering
        msgs.sort(key=lambda m: (m["chat_id"], m["_id"]))
        chunks = chunk_messages(msgs)

        todo = [c for c in chunks if c["id"] not in existing_ids]

        log.info(f"Embed updater: {len(rows)} new messages → {len(todo)} chunks to index")

        indexed = 0
        for batch_start in range(0, len(todo), BATCH_SIZE):
            batch = todo[batch_start: batch_start + BATCH_SIZE]
            embeddings = _batch_embed([c["doc"] for c in batch])
            if embeddings and len(embeddings) == len(batch):
                col.add(
                    ids=[c["id"] for c in batch],
                    embeddings=embeddings,
                    documents=[c["doc"] for c in batch],
                    metadatas=[c["metadata"] for c in batch],
                )
                indexed += len(batch)
            time.sleep(0.15)

        if not _can_advance_state(len(todo), indexed):
            log.error(
                "Embed updater: indexed only %s/%s chunks; keeping last_id=%s for retry",
                indexed, len(todo), last_id,
            )
            return

        _save_state({"last_id": max_id})
        log.info(f"Embed updater: done, indexed {indexed} chunks, last_id={max_id}")

    except Exception as e:
        log.error(f"Embed updater error: {e}", exc_info=True)
