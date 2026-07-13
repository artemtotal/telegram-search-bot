"""
Conversation-window chunking for embeddings.

One message = one vector loses context: an answer like "here is her
number @xxx" is semantically meaningless without the question above it.
Instead we embed short conversation windows: same chat, small time gap,
capped size. This is the single biggest lever for retrieval quality.

Used by tools/build_embeddings.py (initial build) and
user_jobs/embed_updater.py (hourly incremental updates).
"""

from datetime import timedelta
from typing import Dict, List

MAX_CHUNK_MSGS  = 8      # messages per window
MAX_CHUNK_CHARS = 1500   # approx chars per window
MAX_GAP_MINUTES = 20     # time gap that starts a new window
MSG_TRUNCATE    = 400    # per-message text cap inside a chunk

# Messages addressed to the bot are questions, not community knowledge —
# they must never enter the vector index.
BOT_PREFIXES = ("потсдамбот", "потбот", "потсдам бот")


def _fmt_line(m: Dict) -> str:
    text = (m["text"] or "").strip().replace("\n", " ")[:MSG_TRUNCATE]
    return f"[{m['date_str']}] @{m['user']}: {text}"


def _fmt_reply_line(m: Dict) -> str:
    text = (m["text"] or "").strip().replace("\n", " ")[:MSG_TRUNCATE]
    return f"[reply to] @{m['user']}: {text}"


def chunk_messages(msgs: List[Dict]) -> List[Dict]:
    """Group messages into conversation windows.

    Input dicts must contain: _id, message_id, reply_to_msg_id, chat_id,
    text, user, date (datetime or None), date_str, timestamp, link — sorted
    by (chat_id, _id).
    Returns chunk dicts: {"id", "doc", "metadata"}.
    Chunk ids are deterministic (chat + first message pk), so re-running
    the builder on the same data never produces duplicates.
    """
    chunks: List[Dict] = []
    cur: List[Dict] = []
    cur_lines: List[str] = []
    cur_message_keys = set()
    included_reply_sources = set()
    cur_chars = 0
    message_lookup = {
        (message["chat_id"], message.get("message_id")): message
        for message in msgs
        if message.get("message_id") is not None
    }

    def _lines_for_message(message: Dict):
        message_line = _fmt_line(message)[:MAX_CHUNK_CHARS]
        reply_to_msg_id = message.get("reply_to_msg_id")
        reply_key = (message["chat_id"], reply_to_msg_id)
        source = message_lookup.get(reply_key) if reply_to_msg_id is not None else None
        if (
            source is None
            or reply_key in cur_message_keys
            or reply_key in included_reply_sources
        ):
            return [message_line], None

        # Preserve the answer first; trim only the quoted source if needed.
        reply_budget = MAX_CHUNK_CHARS - len(message_line) - 1
        if reply_budget <= 0:
            return [message_line], None
        reply_line = _fmt_reply_line(source)[:reply_budget]
        return [reply_line, message_line], reply_key

    def _addition_chars(lines: List[str]) -> int:
        separators = len(lines) - 1
        if cur_lines:
            separators += 1
        return sum(len(line) for line in lines) + separators

    def _flush():
        nonlocal cur, cur_chars
        if not cur:
            return
        first, last = cur[0], cur[-1]
        doc = "\n".join(cur_lines)
        chunks.append({
            "id": f"c{first['chat_id']}_{first['_id']}",
            "doc": doc,
            "metadata": {
                "msg_id": first["_id"],
                "last_msg_id": last["_id"],
                "date": last["date_str"],
                "timestamp": last["timestamp"],
                "user": first["user"],
                "link": first["link"],
                "chat_id": first["chat_id"],
                "n_msgs": len(cur),
            },
        })
        cur.clear()
        cur_lines.clear()
        cur_message_keys.clear()
        included_reply_sources.clear()
        cur_chars = 0

    prev = None
    for m in msgs:
        new_chat = prev is not None and m["chat_id"] != prev["chat_id"]
        big_gap = (
            prev is not None
            and m["date"] is not None and prev["date"] is not None
            and (m["date"] - prev["date"]) > timedelta(minutes=MAX_GAP_MINUTES)
        )
        if cur and (new_chat or big_gap
                    or len(cur) >= MAX_CHUNK_MSGS):
            _flush()

        lines, reply_key = _lines_for_message(m)
        added_chars = _addition_chars(lines)
        if cur and cur_chars + added_chars > MAX_CHUNK_CHARS:
            _flush()
            lines, reply_key = _lines_for_message(m)
            added_chars = _addition_chars(lines)

        cur.append(m)
        cur_lines.extend(lines)
        if m.get("message_id") is not None:
            cur_message_keys.add((m["chat_id"], m["message_id"]))
        if reply_key is not None:
            included_reply_sources.add(reply_key)
        cur_chars += added_chars
        prev = m
    _flush()
    return chunks


def rows_to_msg_dicts(rows) -> List[Dict]:
    """Convert (Message, User) SQLAlchemy rows into chunker input dicts.
    Drops empty texts and bot-addressed messages."""
    out: List[Dict] = []
    for msg, user in rows:
        text = (msg.text or "").strip()
        if not text:
            continue
        if text.lower().startswith(BOT_PREFIXES):
            continue
        username = (user.username or user.fullname or "") if user else ""
        has_dt = hasattr(msg.date, "strftime")
        out.append({
            "_id": msg._id,
            "message_id": msg.id,
            "reply_to_msg_id": getattr(msg, "reply_to_msg_id", None),
            "chat_id": msg.from_chat,
            "text": text,
            "user": username,
            "date": msg.date if has_dt else None,
            "date_str": msg.date.strftime("%Y-%m-%d %H:%M") if has_dt else "",
            "timestamp": int(msg.date.timestamp()) if has_dt else 0,
            "link": (msg.link or "") if hasattr(msg, "link") else "",
        })
    return out
