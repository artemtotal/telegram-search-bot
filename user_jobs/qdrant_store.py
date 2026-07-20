"""Small REST client for the local Qdrant vector store."""

import os
import uuid
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import requests


QDRANT_URL = os.getenv("QDRANT_URL", "http://qdrant:6333").rstrip("/")
QDRANT_COLLECTION = os.getenv("QDRANT_COLLECTION", "chunks_minilm_v1")
QDRANT_VECTOR_SIZE = 384
QDRANT_TIMEOUT = float(os.getenv("QDRANT_TIMEOUT", "30"))
QDRANT_MIN_SCORE = float(os.getenv("VEC_MIN_SCORE", "0.35"))
_POINT_NAMESPACE = uuid.UUID("0d6a8735-33ec-42ae-83b1-d980842fc3d4")


def _request(method: str, path: str, **kwargs):
    timeout = kwargs.pop("timeout", QDRANT_TIMEOUT)
    response = requests.request(
        method,
        f"{QDRANT_URL}{path}",
        timeout=timeout,
        **kwargs,
    )
    return response


def point_id_for_chunk(chunk_id: str) -> str:
    return str(uuid.uuid5(_POINT_NAMESPACE, str(chunk_id)))


def wait_until_ready(timeout: int = 60) -> None:
    import time

    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            response = _request("GET", "/readyz", timeout=3)
            if response.status_code == 200:
                return
        except Exception as exc:
            last_error = exc
        time.sleep(1)
    raise RuntimeError(f"Qdrant did not become ready: {last_error}")


def _ensure_payload_indexes() -> None:
    for field_name, field_schema in (
        ("timestamp", "integer"),
        ("chat_id", "integer"),
        ("msg_id", "integer"),
        ("last_msg_id", "integer"),
    ):
        response = _request(
            "PUT",
            f"/collections/{QDRANT_COLLECTION}/index?wait=true",
            json={"field_name": field_name, "field_schema": field_schema},
        )
        response.raise_for_status()


def ensure_collection() -> None:
    path = f"/collections/{QDRANT_COLLECTION}"
    response = _request("GET", path)
    if response.status_code == 200:
        info = response.json().get("result", {})
        vectors = info.get("config", {}).get("params", {}).get("vectors", {})
        if vectors:
            size = vectors.get("size")
            distance = str(vectors.get("distance", "")).lower()
            if size != QDRANT_VECTOR_SIZE or distance != "cosine":
                raise RuntimeError(
                    f"Qdrant collection has incompatible vector config: {vectors}"
                )
        _ensure_payload_indexes()
        return
    if response.status_code != 404:
        response.raise_for_status()
    response = _request(
        "PUT",
        path,
        json={
            "vectors": {"size": QDRANT_VECTOR_SIZE, "distance": "Cosine"},
            "on_disk_payload": True,
        },
    )
    response.raise_for_status()
    _ensure_payload_indexes()


def collection_info() -> Dict:
    response = _request("GET", f"/collections/{QDRANT_COLLECTION}")
    response.raise_for_status()
    return response.json().get("result", {})


def collection_count() -> int:
    info = collection_info()
    return int(info.get("points_count") or 0)


def upsert_chunks(chunks: List[Dict], vectors: List[List[float]]) -> None:
    if len(chunks) != len(vectors):
        raise ValueError("chunk/vector count mismatch")
    points = []
    for chunk, vector in zip(chunks, vectors):
        if len(vector) != QDRANT_VECTOR_SIZE:
            raise ValueError(f"expected {QDRANT_VECTOR_SIZE}-dimensional vector")
        metadata = dict(chunk["metadata"])
        payload = {
            **metadata,
            "chunk_id": chunk["id"],
            "document": chunk["doc"],
        }
        points.append({
            "id": point_id_for_chunk(chunk["id"]),
            "vector": vector,
            "payload": payload,
        })
    if not points:
        return
    response = _request(
        "PUT",
        f"/collections/{QDRANT_COLLECTION}/points?wait=true",
        json={"points": points},
        timeout=max(QDRANT_TIMEOUT, 120),
    )
    response.raise_for_status()


def delete_chunks(chunk_ids: List[str]) -> None:
    if not chunk_ids:
        return
    response = _request(
        "POST",
        f"/collections/{QDRANT_COLLECTION}/points/delete?wait=true",
        json={"points": [point_id_for_chunk(chunk_id) for chunk_id in chunk_ids]},
    )
    response.raise_for_status()


def find_chunks_covering(chat_id: int, message_pk: int, limit: int = 32) -> List[Dict]:
    response = _request(
        "POST",
        f"/collections/{QDRANT_COLLECTION}/points/scroll",
        json={
            "filter": {"must": [
                {"key": "chat_id", "match": {"value": int(chat_id)}},
                {"key": "msg_id", "range": {"lte": int(message_pk)}},
                {"key": "last_msg_id", "range": {"gte": int(message_pk)}},
            ]},
            "limit": limit,
            "with_payload": True,
            "with_vector": False,
        },
    )
    response.raise_for_status()
    points = response.json().get("result", {}).get("points", [])
    return [
        {
            "chunk_id": point["payload"]["chunk_id"],
            "chat_id": int(point["payload"]["chat_id"]),
            "msg_id": int(point["payload"]["msg_id"]),
            "last_msg_id": int(point["payload"]["last_msg_id"]),
        }
        for point in points
    ]


def search(vector: List[float], limit: int = 50,
           since_days: Optional[int] = None) -> List[Dict]:
    if len(vector) != QDRANT_VECTOR_SIZE:
        raise ValueError(f"expected {QDRANT_VECTOR_SIZE}-dimensional query vector")
    body = {
        "vector": vector,
        "limit": max(1, int(limit)),
        "score_threshold": QDRANT_MIN_SCORE,
        "with_payload": True,
        "with_vector": False,
    }
    if since_days is not None:
        cutoff = int((datetime.utcnow() - timedelta(days=int(since_days))).timestamp())
        body["filter"] = {
            "must": [{"key": "timestamp", "range": {"gte": cutoff}}]
        }
    response = _request(
        "POST",
        f"/collections/{QDRANT_COLLECTION}/points/search",
        json=body,
    )
    response.raise_for_status()
    hits = response.json().get("result", [])
    results = []
    for hit in hits:
        payload = hit.get("payload", {})
        timestamp = int(payload.get("timestamp") or 0)
        date_text = payload.get("date") or datetime.fromtimestamp(timestamp).strftime(
            "%Y-%m-%d %H:%M"
        )
        link = payload.get("link", "")
        results.append({
            "id": int(payload.get("msg_id", 0)),
            "chat_id": payload.get("chat_id"),
            "date": date_text,
            "date_obj": None,
            "text": payload.get("document", ""),
            "user": payload.get("user", ""),
            "first_name": payload.get("first_name", ""),
            "link": link,
            "short_link": link,
            "score": round(float(hit.get("score", 0)), 3),
        })
    return results
