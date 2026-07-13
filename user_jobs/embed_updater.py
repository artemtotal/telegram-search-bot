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
import time
from typing import List

import requests

sys.path.insert(0, "/app")

log = logging.getLogger(__name__)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
CHROMA_PATH    = os.getenv("CHROMA_PATH", "/app/chroma")
EMBED_MODEL    = "gemini-embedding-001"
BATCH_SIZE     = 40
MAX_PER_RUN    = 3000   # messages per hourly run (self-bootstraps gradually)
COLLECTION     = "chunks"
STATE_PATH     = os.path.join(CHROMA_PATH, "embed_state.json")

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


def _save_state(state: dict) -> None:
    try:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except Exception as e:
        log.error(f"Failed to save embed state: {e}")


def run_embed_update(context=None):
    """Scheduler entry point — immediately hands off to a daemon thread."""
    import threading
    t = threading.Thread(target=_embed_update_worker, args=(context,), daemon=True, name="embed-update")
    t.start()


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

        state = _load_state()
        last_id = int(state.get("last_id", 0))

        session = DBSession()
        chat_ids = [c.id for c in session.query(Chat).filter(Chat.enable == 1).all()]
        rows = (
            session.query(Message, User)
            .outerjoin(User, Message.from_id == User.id)
            .filter(Message.from_chat.in_(chat_ids))
            .filter(Message.text.isnot(None))
            .filter(Message.text != "")
            .filter(Message._id > last_id)
            .order_by(Message._id.asc())
            .limit(MAX_PER_RUN)
            .all()
        )
        session.close()

        if not rows:
            log.debug("Embed updater: nothing new to index")
            return

        max_id = max(msg._id for msg, _ in rows)

        msgs = rows_to_msg_dicts(rows)
        # Chunker expects (chat_id, _id) ordering
        msgs.sort(key=lambda m: (m["chat_id"], m["_id"]))
        chunks = chunk_messages(msgs)

        existing = set(col.get(include=[])["ids"])
        todo = [c for c in chunks if c["id"] not in existing]

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

        # Advance high-water mark even if some batches failed:
        # failed chunks will be caught by the manual build script if needed,
        # and a stuck mark would block all future updates.
        _save_state({"last_id": max_id})
        log.info(f"Embed updater: done, indexed {indexed} chunks, last_id={max_id}")

    except Exception as e:
        log.error(f"Embed updater error: {e}", exc_info=True)
