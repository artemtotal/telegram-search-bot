"""
Chunk-based embedding builder: reads all messages from SQLite, groups them
into conversation windows (see user_jobs/chunking.py), embeds via Gemini
and stores them in the ChromaDB collection "chunks".

Run once inside the container:
    docker exec tgbot python3 /app/tools/build_embeddings.py

Full rebuild (drops the "chunks" collection first):
    docker exec tgbot python3 /app/tools/build_embeddings.py --rebuild

Progress is saved — safe to interrupt and resume (chunk ids are
deterministic, already-indexed ids are skipped).
"""

import logging
import os
import sys
import time
from typing import List

import requests

sys.path.insert(0, "/app")
from database import DBSession, Message, User, Chat
from user_jobs.chunking import chunk_messages, rows_to_msg_dicts

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
CHROMA_PATH    = os.getenv("CHROMA_PATH", "/app/chroma")
EMBED_MODEL    = "gemini-embedding-001"
BATCH_SIZE     = 40    # chunks per API call (chunks are larger than single messages)
SLEEP_BETWEEN  = 0.15  # seconds between batches (rate-limit safety)
COLLECTION     = "chunks"

EMBED_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"{EMBED_MODEL}:batchEmbedContents?key={{api_key}}"
)


def _batch_embed(texts: List[str]) -> List[List[float]]:
    """Call Google batchEmbedContents. Returns list of embedding vectors."""
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
                log.warning("Rate limit hit, sleeping 60s...")
                time.sleep(60)
                continue
            resp.raise_for_status()
            return [e["values"] for e in resp.json().get("embeddings", [])]
        except Exception as e:
            log.warning(f"Embed attempt {attempt+1} failed: {e}")
            time.sleep(5)
    return []


def main():
    if not GEMINI_API_KEY:
        log.error("GEMINI_API_KEY not set!")
        sys.exit(1)

    import chromadb
    client = chromadb.PersistentClient(path=CHROMA_PATH)

    if "--rebuild" in sys.argv:
        try:
            client.delete_collection(COLLECTION)
            log.info("Dropped existing '%s' collection (--rebuild)", COLLECTION)
        except Exception:
            pass

    col = client.get_or_create_collection(
        name=COLLECTION,
        metadata={"hnsw:space": "cosine"},
    )

    already_indexed = set(col.get(include=[])["ids"])
    log.info(f"Already in ChromaDB ({COLLECTION}): {len(already_indexed)} chunks")

    session = DBSession()
    chat_ids = [c.id for c in session.query(Chat).filter(Chat.enable == 1).all()]
    log.info(f"Active chat IDs: {chat_ids}")

    rows = (
        session.query(Message, User)
        .outerjoin(User, Message.from_id == User.id)
        .filter(Message.from_chat.in_(chat_ids))
        .filter(Message.text.isnot(None))
        .filter(Message.text != "")
        .order_by(Message.from_chat.asc(), Message._id.asc())
        .all()
    )
    session.close()

    msgs = rows_to_msg_dicts(rows)
    chunks = chunk_messages(msgs)
    todo = [c for c in chunks if c["id"] not in already_indexed]

    total = len(todo)
    log.info(f"Messages: {len(msgs)} → chunks: {len(chunks)}, to index: {total}")
    if total == 0:
        log.info("Nothing to do!")
        return

    indexed = 0
    errors = 0
    start = time.time()

    for batch_start in range(0, total, BATCH_SIZE):
        batch = todo[batch_start: batch_start + BATCH_SIZE]
        docs = [c["doc"] for c in batch]

        embeddings = _batch_embed(docs)
        if not embeddings or len(embeddings) != len(batch):
            log.warning(f"Batch {batch_start}-{batch_start+len(batch)}: embedding failed, skipping")
            errors += len(batch)
            time.sleep(2)
            continue

        col.add(
            ids=[c["id"] for c in batch],
            embeddings=embeddings,
            documents=docs,
            metadatas=[c["metadata"] for c in batch],
        )
        indexed += len(batch)

        elapsed = time.time() - start
        rate = indexed / elapsed if elapsed > 0 else 1
        remaining = (total - indexed) / rate if rate > 0 else 0
        log.info(
            f"Progress: {indexed}/{total} ({100*indexed//total}%) "
            f"| speed: {rate:.0f} chunks/s "
            f"| ETA: {remaining/60:.1f} min"
        )
        time.sleep(SLEEP_BETWEEN)

    log.info(f"Done! Indexed: {indexed}, Errors: {errors}")


if __name__ == "__main__":
    main()
