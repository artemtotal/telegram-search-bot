"""Chat knowledge base ("довідник із чату"), 31.10.2026.

An LLM reads the whole chat history (2022+) window by window and writes
knowledge cards: who was recommended for what, contacts, what people said
about them, how-tos and warnings, each with dates and links to the source
messages. Search then runs over these cards (full text + Gemini vectors in
Qdrant) instead of hand-written keyword rules, across all years.

This module holds the storage, the LLM / embedding calls and the Qdrant
client. kb_builder.py fills it, user_handlers/kb_search.py answers from it.
"""

import json
import logging
import math
import os
import sqlite3
import time
from typing import Dict, Iterable, List, Optional, Sequence

import requests

log = logging.getLogger(__name__)

KB_DB_PATH = os.getenv("KB_DB_PATH", "/app/config/kb.db")
MAIN_DB_PATH = os.getenv("MAIN_DB_PATH", "/app/config/bot.db")
AI_URL = os.getenv("OPENROUTER_URL", "http://host.docker.internal:20128/v1/chat/completions")
EMBED_URL = os.getenv("KB_EMBED_URL", AI_URL.replace("/chat/completions", "/embeddings"))
EMBED_MODEL = os.getenv("KB_EMBED_MODEL", "gemini/gemini-embedding-001")
# gemini-embedding is trained so that a prefix of the vector is itself a good
# embedding; 768 of 3072 dims keeps Qdrant small (the server is short on RAM).
EMBED_DIM = int(os.getenv("KB_EMBED_DIM", "768"))
# Fast model for reading the chat and for understanding questions.
FAST_MODELS = [m for m in os.getenv(
    "KB_FAST_MODELS", "codex/gpt-5.6-luna-low,gemini/gemini-3-flash-preview,cx/gpt-5.6-terra-low"
).split(",") if m.strip()]
# Strong model for the final answer, fast one as fallback.
ANSWER_MODELS = [m for m in os.getenv(
    "KB_ANSWER_MODELS", "opus-first,gemini/gemini-3-flash-preview"
).split(",") if m.strip()]
QDRANT_URL = os.getenv("QDRANT_URL", "http://qdrant:6333").rstrip("/")
KB_COLLECTION = os.getenv("KB_COLLECTION", "kb_cards_gemini_v1")

SCHEMA = """
CREATE TABLE IF NOT EXISTS cards (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    topic       TEXT NOT NULL DEFAULT '',
    kind        TEXT NOT NULL DEFAULT '',   -- recommendation|offer|howto|warning|info|event
    title       TEXT NOT NULL DEFAULT '',
    name        TEXT NOT NULL DEFAULT '',
    contacts    TEXT NOT NULL DEFAULT '',
    details     TEXT NOT NULL DEFAULT '',
    opinion     TEXT NOT NULL DEFAULT '',   -- + / - / 0
    first_date  TEXT NOT NULL DEFAULT '',
    last_date   TEXT NOT NULL DEFAULT '',
    msg_ids     TEXT NOT NULL DEFAULT '[]', -- Telegram message ids
    links       TEXT NOT NULL DEFAULT '[]',
    embedded    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_cards_last_date ON cards(last_date);
CREATE INDEX IF NOT EXISTS idx_cards_embedded ON cards(embedded);
CREATE VIRTUAL TABLE IF NOT EXISTS cards_fts USING fts5(
    title, topic, name, contacts, details,
    content='cards', content_rowid='id', tokenize='unicode61'
);
CREATE TRIGGER IF NOT EXISTS cards_ai AFTER INSERT ON cards BEGIN
    INSERT INTO cards_fts(rowid, title, topic, name, contacts, details)
    VALUES (new.id, new.title, new.topic, new.name, new.contacts, new.details);
END;
CREATE TRIGGER IF NOT EXISTS cards_ad AFTER DELETE ON cards BEGIN
    INSERT INTO cards_fts(cards_fts, rowid, title, topic, name, contacts, details)
    VALUES ('delete', old.id, old.title, old.topic, old.name, old.contacts, old.details);
END;
CREATE TABLE IF NOT EXISTS batches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    first_rowid INTEGER NOT NULL,
    last_rowid  INTEGER NOT NULL,
    messages    INTEGER NOT NULL,
    cards       INTEGER NOT NULL,
    model       TEXT NOT NULL DEFAULT '',
    seconds     REAL NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS proactive (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     INTEGER NOT NULL,
    msg_id      INTEGER NOT NULL,
    user_id     INTEGER,
    question    TEXT NOT NULL,
    answer      TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL,             -- skipped|preview|sent|rejected|error
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_proactive_created ON proactive(created_at);
"""


def connect(path: str = KB_DB_PATH) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.executescript(SCHEMA)
    return connection


def main_db(read_only: bool = True) -> sqlite3.Connection:
    uri = f"file:{MAIN_DB_PATH}?mode=ro" if read_only else f"file:{MAIN_DB_PATH}"
    connection = sqlite3.connect(uri, uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    return connection


def get_meta(connection: sqlite3.Connection, key: str, default: str = "") -> str:
    row = connection.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(connection: sqlite3.Connection, key: str, value) -> None:
    connection.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


# ── LLM ───────────────────────────────────────────────────────────────────

def chat(prompt: str, models: Sequence[str], timeout: float = 60, max_tokens: int = 8192,
         system: str = "") -> tuple:
    """Return (text, model) from the first model that answers; ("", "") if none did."""
    messages = ([{"role": "system", "content": system}] if system else []) + [
        {"role": "user", "content": prompt}
    ]
    for model in models:
        try:
            response = requests.post(
                AI_URL,
                json={"model": model, "messages": messages, "temperature": 0.1,
                      "max_tokens": max_tokens, "stream": False},
                timeout=timeout,
            )
            if response.status_code != 200:
                log.warning("LLM %s: HTTP %s %s", model, response.status_code, response.text[:200])
                continue
            text = (response.json()["choices"][0]["message"].get("content") or "").strip()
            if text:
                return text, model
        except Exception as exc:
            log.warning("LLM %s failed: %s", model, exc)
    return "", ""


def parse_json(text: str):
    """Tolerant JSON extraction from an LLM reply (code fences, prose around it)."""
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("\n") + 1:] if "\n" in text else text
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = text.find(opener), text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except ValueError:
                continue
    return None


# ── Embeddings ────────────────────────────────────────────────────────────

def _normalize(vector: Sequence[float]) -> List[float]:
    cut = list(vector[:EMBED_DIM])
    norm = math.sqrt(sum(x * x for x in cut)) or 1.0
    return [x / norm for x in cut]


def embed(texts: List[str], retries: int = 4) -> List[List[float]]:
    """Gemini embeddings through the local proxy, cut to EMBED_DIM and normalised."""
    vectors: List[List[float]] = []
    for start in range(0, len(texts), 50):
        chunk = [t[:6000] or "-" for t in texts[start:start + 50]]
        for attempt in range(retries):
            try:
                response = requests.post(EMBED_URL, json={"model": EMBED_MODEL, "input": chunk}, timeout=60)
                data = response.json().get("data") if response.status_code == 200 else None
                if data and len(data) == len(chunk):
                    vectors.extend(_normalize(item["embedding"]) for item in data)
                    break
                log.warning("Embedding HTTP %s: %s", response.status_code, response.text[:200])
            except Exception as exc:
                log.warning("Embedding failed: %s", exc)
            time.sleep(5 * (attempt + 1))
        else:
            raise RuntimeError("embedding service unavailable")
    return vectors


# ── Qdrant ────────────────────────────────────────────────────────────────

def ensure_collection() -> None:
    response = requests.get(f"{QDRANT_URL}/collections/{KB_COLLECTION}", timeout=15)
    if response.status_code == 200:
        return
    response = requests.put(
        f"{QDRANT_URL}/collections/{KB_COLLECTION}",
        json={"vectors": {"size": EMBED_DIM, "distance": "Cosine", "on_disk": True}},
        timeout=30,
    )
    response.raise_for_status()


def upsert(points: Iterable[Dict]) -> None:
    points = list(points)
    if not points:
        return
    response = requests.put(
        f"{QDRANT_URL}/collections/{KB_COLLECTION}/points?wait=true",
        json={"points": points},
        timeout=60,
    )
    response.raise_for_status()


def vector_search(vector: List[float], limit: int = 50) -> List[tuple]:
    """[(card_id, score)] best first."""
    response = requests.post(
        f"{QDRANT_URL}/collections/{KB_COLLECTION}/points/search",
        json={"vector": vector, "limit": limit, "with_payload": False},
        timeout=20,
    )
    if response.status_code != 200:
        log.warning("KB vector search HTTP %s", response.status_code)
        return []
    return [(int(hit["id"]), float(hit["score"])) for hit in response.json().get("result", [])]


def card_text(card) -> str:
    """What gets embedded / shown to the answer model for one card."""
    parts = [card["title"], card["topic"], card["name"], card["contacts"], card["details"]]
    return " | ".join(p for p in parts if p)


def year_span(card) -> str:
    first, last = str(card["first_date"])[:4], str(card["last_date"])[:4]
    return first if first == last else f"{first}–{last}"


def fts_query(terms: Iterable[str], stems: Iterable[str] = ()) -> Optional[str]:
    """OR-query for FTS5.

    ``terms`` are free words (endings cut for words of 6+ letters, prefix
    match); ``stems`` are already stems (from the LLM) and are used as-is.
    """
    tokens: List[str] = []

    def add(token: str) -> None:
        if token not in tokens:
            tokens.append(token)

    def clean(word: str) -> str:
        word = "".join(ch for ch in word.lower().replace("ё", "е") if ch.isalnum() or ch in "-'")
        return word.strip("-'").replace('"', "")

    for stem in stems:
        stem = clean(str(stem).split()[0]) if str(stem).split() else ""
        if len(stem) >= 3:
            add(f'"{stem}"*')
    for term in terms:
        for word in str(term).split():
            word = clean(word)
            if len(word) < 3:
                continue
            if len(word) >= 6:
                add(f'"{word[: len(word) - 2]}"*')
            elif len(word) >= 5:
                add(f'"{word}"*')
            else:
                add(f'"{word}"')
    return " OR ".join(tokens[:45]) or None
