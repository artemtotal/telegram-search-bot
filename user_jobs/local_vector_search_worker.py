"""Isolated local embedding plus Chroma query worker."""

import json
import os
import sys
from datetime import datetime, timedelta

import chromadb
from chromadb.config import Settings

from user_jobs.local_embeddings import LOCAL_COLLECTION, embed_in_subprocess


def main() -> None:
    payload = json.load(sys.stdin)
    query = str(payload["query"])
    n_results = max(1, int(payload.get("n_results", 50)))
    since_days = payload.get("since_days")
    min_score = float(os.getenv("VEC_MIN_SCORE", "0.35"))

    client = chromadb.PersistentClient(
        path=os.getenv("CHROMA_PATH", "/app/chroma"),
        settings=Settings(anonymized_telemetry=False),
    )
    col = client.get_collection(LOCAL_COLLECTION)
    count = col.count()
    if count == 0:
        json.dump([], sys.stdout)
        return

    where = None
    if since_days is not None:
        cutoff = int((datetime.utcnow() - timedelta(days=int(since_days))).timestamp())
        where = {"timestamp": {"$gte": cutoff}}
    vectors = embed_in_subprocess([query], timeout=45)
    results = col.query(
        query_embeddings=[vectors[0]],
        n_results=min(n_results, count),
        where=where,
        include=["documents", "metadatas", "distances"],
    )

    messages = []
    for meta, doc, distance in zip(
        results.get("metadatas", [[]])[0],
        results.get("documents", [[]])[0],
        results.get("distances", [[]])[0],
    ):
        score = 1.0 - distance
        if score < min_score:
            continue
        date_text = meta.get("date") or datetime.fromtimestamp(
            meta.get("timestamp", 0)
        ).strftime("%Y-%m-%d %H:%M")
        messages.append({
            "id": int(meta.get("msg_id", 0)),
            "chat_id": meta.get("chat_id"),
            "date": date_text,
            "date_obj": None,
            "text": doc,
            "user": meta.get("user", ""),
            "first_name": meta.get("first_name", ""),
            "link": meta.get("link", ""),
            "short_link": meta.get("link", ""),
            "score": round(score, 3),
        })
    json.dump(messages, sys.stdout, ensure_ascii=False)


if __name__ == "__main__":
    main()