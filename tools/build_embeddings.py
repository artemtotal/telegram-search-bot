"""
Initial embedding builder: reads all messages from SQLite → embeds via
Google text-embedding-004 → stores in ChromaDB.

Run once inside the container:
    docker exec tgbot python3 /app/tools/build_embeddings.py

Progress is saved — safe to interrupt and resume (skips already-indexed IDs).
"""

import logging
import os
import sys
import time
from datetime import datetime
from typing import List, Dict

import requests

sys.path.insert(0, "/app")
from database import DBSession, Message, User, Chat

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
CHROMA_PATH    = os.getenv("CHROMA_PATH", "/app/config/chroma")
EMBED_MODEL    = "gemini-embedding-001"
BATCH_SIZE     = 80    # texts per API call (max 100)
SLEEP_BETWEEN  = 0.15  # seconds between batches (rate-limit safety)
MIN_TEXT_LEN   = 10    # skip very short messages

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
                "content": {"parts": [{"text": t[:2000]}]},  # truncate very long texts
                "taskType": "RETRIEVAL_DOCUMENT",
                "outputDimensionality": 768,
            }
            for t in texts
        ]
    }
    for attempt in range(3):
        try:
            resp = requests.post(url, json=payload, timeout=30)
            if resp.status_code == 429:
                log.warning("Rate limit hit, sleeping 60s...")
                time.sleep(60)
                continue
            resp.raise_for_status()
            data = resp.json()
            return [e["values"] for e in data.get("embeddings", [])]
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
    col = client.get_or_create_collection(
        name="messages",
        metadata={"hnsw:space": "cosine"},
    )

    already_indexed = set(col.get(include=[])["ids"])
    log.info(f"Already in ChromaDB: {len(already_indexed)} vectors")

    session = DBSession()
    chat_ids = [c.id for c in session.query(Chat).filter(Chat.enable == 1).all()]
    log.info(f"Active chat IDs: {chat_ids}")

    # Load all messages not yet indexed
    rows = (
        session.query(Message, User)
        .outerjoin(User, Message.from_id == User.id)
        .filter(Message.from_chat.in_(chat_ids))
        .filter(Message.text.isnot(None))
        .filter(Message.text != "")
        .order_by(Message._id.asc())
        .all()
    )

    to_index: List[Dict] = []
    for msg, user in rows:
        sid = str(msg._id)
        if sid in already_indexed:
            continue
        text = (msg.text or "").strip()
        if len(text) < MIN_TEXT_LEN:
            continue
        username = (user.username or user.fullname or "") if user else ""
        date_str = msg.date.strftime("%Y-%m-%d %H:%M") if hasattr(msg.date, "strftime") else ""
        timestamp = int(msg.date.timestamp()) if hasattr(msg.date, "timestamp") else 0
        link = (msg.link or "") if hasattr(msg, "link") else ""
        to_index.append({
            "id": sid,
            "text": text,
            "metadata": {
                "msg_id": msg._id,
                "date": date_str,
                "timestamp": timestamp,
                "user": username,
                "link": link,
                "chat_id": msg.from_chat,
            },
        })

    session.close()
    total = len(to_index)
    log.info(f"Need to index: {total} messages")
    if total == 0:
        log.info("Nothing to do!")
        return

    indexed = 0
    errors = 0
    start = time.time()

    for batch_start in range(0, total, BATCH_SIZE):
        batch = to_index[batch_start: batch_start + BATCH_SIZE]
        texts = [b["text"] for b in batch]

        embeddings = _batch_embed(texts)
        if not embeddings or len(embeddings) != len(batch):
            log.warning(f"Batch {batch_start}-{batch_start+len(batch)}: embedding failed, skipping")
            errors += len(batch)
            time.sleep(2)
            continue

        col.add(
            ids=[b["id"] for b in batch],
            embeddings=embeddings,
            documents=texts,
            metadatas=[b["metadata"] for b in batch],
        )
        indexed += len(batch)

        elapsed = time.time() - start
        rate = indexed / elapsed if elapsed > 0 else 1
        remaining = (total - indexed) / rate if rate > 0 else 0
        log.info(
            f"Progress: {indexed}/{total} ({100*indexed//total}%) "
            f"| speed: {rate:.0f} msg/s "
            f"| ETA: {remaining/60:.1f} min"
        )
        time.sleep(SLEEP_BETWEEN)

    log.info(f"Done! Indexed: {indexed}, Errors: {errors}, Total in DB: {col.count()}")


if __name__ == "__main__":
    main()
