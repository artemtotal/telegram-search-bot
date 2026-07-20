"""
One-off cleanup: remove bot-addressed messages ("потсдамбот ...") from the
existing ChromaDB index. They are user questions, not community knowledge,
and they pollute vector search results.

Run inside the container:
    docker exec tgbot python3 /app/tools/clean_bot_queries_chroma.py
"""

import logging
import os
import re
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

CHROMA_PATH  = os.getenv("CHROMA_PATH", "/app/chroma")
BOT_PREFIXES = ("потсдамбот", "посдамбот", "потсдам бот")
BOT_ADDRESS_RE = re.compile(
    r"(?<!\w)(?:" + "|".join(map(re.escape, BOT_PREFIXES)) + r")(?!\w)",
    re.IGNORECASE,
)
PAGE_SIZE    = 1000


def main():
    import chromadb
    from chromadb.config import Settings

    client = chromadb.PersistentClient(
        path=CHROMA_PATH,
        settings=Settings(anonymized_telemetry=False),
    )
    try:
        col = client.get_collection("messages")
    except Exception:
        cols = client.list_collections()
        if not cols:
            log.error("No collections found at %s", CHROMA_PATH)
            sys.exit(1)
        col = cols[0]

    log.info("Collection: %s, scanning for bot-addressed messages...", col.name)

    to_delete = []
    offset = 0
    scanned = 0
    while True:
        batch = col.get(include=["documents"], limit=PAGE_SIZE, offset=offset)
        ids = batch.get("ids", [])
        if not ids:
            break
        docs = batch.get("documents", [])
        for _id, doc in zip(ids, docs):
            scanned += 1
            if BOT_ADDRESS_RE.search(doc or ""):
                to_delete.append(_id)
        offset += len(ids)

    log.info("Scanned %d vectors, found %d bot-addressed", scanned, len(to_delete))

    if not to_delete:
        log.info("Nothing to delete — index is clean.")
        return

    # Delete in chunks to avoid oversized requests
    CHUNK = 500
    deleted = 0
    for i in range(0, len(to_delete), CHUNK):
        chunk = to_delete[i:i + CHUNK]
        col.delete(ids=chunk)
        deleted += len(chunk)
        log.info("Deleted %d/%d", deleted, len(to_delete))

    log.info("Done. Removed %d vectors from the index.", deleted)


if __name__ == "__main__":
    main()
