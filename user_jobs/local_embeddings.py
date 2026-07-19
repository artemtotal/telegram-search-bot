"""Local multilingual text embeddings backed by ONNX Runtime."""

import os
import json
import subprocess
import sys
import threading
from typing import List, Optional

from fastembed import TextEmbedding

LOCAL_EMBED_MODEL = os.getenv(
    "LOCAL_EMBED_MODEL",
    "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
)
LOCAL_EMBED_CACHE = os.getenv("LOCAL_EMBED_CACHE", "/app/models")
LOCAL_EMBED_THREADS = int(os.getenv("LOCAL_EMBED_THREADS", "4"))
LOCAL_EMBED_BATCH_SIZE = int(os.getenv("LOCAL_EMBED_BATCH_SIZE", "40"))
LOCAL_EMBED_DIM = 384
LOCAL_COLLECTION = os.getenv("LOCAL_EMBED_COLLECTION", "chunks_local_v1")

_model: Optional[TextEmbedding] = None
_model_lock = threading.Lock()
_embed_lock = threading.Lock()


def _get_model() -> TextEmbedding:
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:
                _model = TextEmbedding(
                    model_name=LOCAL_EMBED_MODEL,
                    cache_dir=LOCAL_EMBED_CACHE,
                    threads=LOCAL_EMBED_THREADS,
                    local_files_only=os.getenv("LOCAL_EMBED_OFFLINE", "0") == "1",
                )
    return _model


def _embed_with_model(model: TextEmbedding, texts: List[str]) -> List[List[float]]:
    """Serialize ONNX inference; the shared FastEmbed session is not thread-safe."""
    with _embed_lock:
        vectors = model.embed(texts, batch_size=LOCAL_EMBED_BATCH_SIZE)
        result = [
            vector.tolist() if hasattr(vector, "tolist") else list(vector)
            for vector in vectors
        ]
    if any(len(vector) != LOCAL_EMBED_DIM for vector in result):
        raise ValueError(f"Unexpected local embedding dimension; expected {LOCAL_EMBED_DIM}")
    return result


def _embed(texts: List[str]) -> List[List[float]]:
    if not texts:
        return []
    return _embed_with_model(_get_model(), texts)


def embed_documents(texts: List[str]) -> List[List[float]]:
    return _embed(texts)


def embed_query(text: str) -> Optional[List[float]]:
    vectors = _embed([text])
    return vectors[0] if vectors else None


def embed_in_subprocess(texts: List[str], timeout: int = 120) -> List[List[float]]:
    """Run native ONNX inference outside the long-lived bot process."""
    if not texts:
        return []
    command = [
        sys.executable,
        "-m",
        "user_jobs.local_embedding_worker",
    ]
    result = subprocess.run(
        command,
        input=json.dumps(texts, ensure_ascii=False),
        text=True,
        capture_output=True,
        timeout=timeout,
        env=os.environ.copy(),
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "embedding subprocess failed").strip()
        raise RuntimeError(detail[-1000:])
    vectors = json.loads(result.stdout)
    if len(vectors) != len(texts):
        raise RuntimeError("embedding subprocess returned an unexpected vector count")
    return vectors


def _reset_model_for_tests() -> None:
    global _model
    _model = None
