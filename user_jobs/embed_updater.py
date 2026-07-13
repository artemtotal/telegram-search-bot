"""
Incremental embedding updater job.
Runs every hour, indexes messages not yet in ChromaDB.
Registered in robot.py as a scheduled job.
"""

import logging
import os
import sys
import time
from typing import List, Dict

import requests

sys.path.insert(0, "/app")

log = logging.getLogger(__name__)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
CHROMA_PATH    = os.getenv("CHROMA_PATH", "/app/config/chroma")
EMBED_MODEL    = "gemini-embedding-001"
BATCH_SIZE     = 80
MIN_TEXT_LEN   = 10
# Messages addressed to the bot are questions, not community knowledge —
# they must never enter the vector index (they pollute search results).
BOT_PREFIXES   = ("потсдамбот", "потбот", "потсдам бот")

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
            resp = requests.post(url, json=payload, timeout=30)
            if resp.status_code == 429:
                time.sleep(60)
                continue
            resp.raise_for_status()
            return [e["values"] for e in resp.json().get("embeddings", [])]
        except Exception as e:
            log.warning(f"Embed attempt {attempt+1} failed: {e}")
            time.sleep(5)
    return []


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

        client = chromadb.PersistentClient(path=CHROMA_PATH)
        col = client.get_or_create_collection(
            name="messages",
            metadata={"hnsw:space": "cosine"},
        )

        existing_ids = set(col.get(include=[])["ids"])
        session = DBSession()
        chat_ids = [c.id for c in session.query(Chat).filter(Chat.enable == 1).all()]

        rows = (
            session.query(Message, User)
            .outerjoin(User, Message.from_id == User.id)
            .filter(Message.from_chat.in_(chat_ids))
            .filter(Message.text.isnot(None))
            .filter(Message.text != "")
            .order_by(Message._id.desc())
            .limit(5000)  # only check recent 5000
            .all()
        )

        to_index: List[Dict] = []
        for msg, user in rows:
            sid = str(msg._id)
            if sid in existing_ids:
                continue
            text = (msg.text or "").strip()
            if len(text) < MIN_TEXT_LEN:
                continue
            if text.lower().startswith(BOT_PREFIXES):
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

        if not to_index:
            log.debug("Embed updater: nothing new to index")
            return

        log.info(f"Embed updater: indexing {len(to_index)} new messages")
        indexed = 0
        for batch_start in range(0, len(to_index), BATCH_SIZE):
            batch = to_index[batch_start: batch_start + BATCH_SIZE]
            embeddings = _batch_embed([b["text"] for b in batch])
            if embeddings and len(embeddings) == len(batch):
                col.add(
                    ids=[b["id"] for b in batch],
                    embeddings=embeddings,
                    documents=[b["text"] for b in batch],
                    metadatas=[b["metadata"] for b in batch],
                )
                indexed += len(batch)
            time.sleep(0.15)

        # NOTE: col.count() hangs on WSL2 Docker volumes (C++ HNSW mutex deadlock).
        # Use len(existing_ids) + indexed as approximation instead.
        approx_total = len(existing_ids) + indexed
        log.info(f"Embed updater: done, indexed {indexed}, total~={approx_total}")

    except Exception as e:
        log.error(f"Embed updater error: {e}", exc_info=True)
