"""Qdrant vector search worker with isolated local ONNX embedding."""

import json
import sys

from user_jobs.local_embeddings import embed_in_subprocess
from user_jobs.qdrant_store import ensure_collection, search, wait_until_ready


def main() -> None:
    payload = json.load(sys.stdin)
    query = str(payload["query"])
    n_results = max(1, int(payload.get("n_results", 50)))
    since_days = payload.get("since_days")

    wait_until_ready(timeout=15)
    ensure_collection()
    vectors = embed_in_subprocess([query], timeout=45)
    messages = search(
        vectors[0],
        limit=n_results,
        since_days=since_days,
    )
    json.dump(messages, sys.stdout, ensure_ascii=False)


if __name__ == "__main__":
    main()
