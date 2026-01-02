import html
import re
import secrets
from typing import Dict, List, Optional, Tuple

from sqlalchemy import desc, func
from telegram import InlineQueryResultArticle, InputTextMessageContent, Update
from telegram.ext import (
    CallbackContext,
    CommandHandler,
    Filters,
    InlineQueryHandler,
    MessageHandler,
)

# Import your models.
# Ensure Message has the reactions_total field in database.py if you want reactions-based sorting/filtering.
from database import Chat, DBSession, Message, User

# --- Configuration ---
INLINE_PAGE_SIZE = 25
MAX_QUERY_LEN = 120
DEFAULT_MIN_REACTIONS = 1


# --- Helpers ---

def _norm(s: str) -> str:
    """Normalize whitespace and line breaks for consistent output."""
    if not s:
        return ""
    s = s.replace("\r", " ").replace("\n", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _split_page(raw: str) -> Tuple[str, int]:
    """
    Parse the trailing page number if present.

    Example:
      "hello world 2" -> ("hello world", 2)
      "hello"         -> ("hello", 1)
    """
    raw = _norm(raw)
    if not raw:
        return "", 1

    parts = raw.split(" ")
    page = 1

    if parts and parts[-1].isdigit():
        try:
            page = max(1, int(parts[-1]))
            parts = parts[:-1]
        except ValueError:
            pass

    q = " ".join(parts).strip()
    if len(q) > MAX_QUERY_LEN:
        q = q[:MAX_QUERY_LEN].strip()

    return q, page


def _parse_uname(raw: str) -> Tuple[Optional[str], str]:
    """
    Extract an @username if it is the first token.

    Returns:
      (username_or_none, remaining_query)
    """
    raw = (raw or "").strip()
    if not raw:
        return None, ""

    parts = raw.split(" ", 1)
    first = parts[0]

    if first.startswith("@") and len(first) > 1:
        rest = parts[1] if len(parts) > 1 else ""
        return first, rest.strip()

    return None, raw


def _parse_tokens(raw: str) -> Tuple[List[str], int]:
    """
    Parse keywords and a reactions filter token.

    Supported formats:
      r:5
      likes:10
      reactions>=5

    Returns:
      (keywords_list, min_reactions)
    """
    toks = [t for t in (raw or "").split() if t]
    keywords: List[str] = []
    min_r = DEFAULT_MIN_REACTIONS

    for t in toks:
        m = re.match(r"^(?:r|likes|reactions)(?::|>=)(\d+)$", t.lower())
        if m:
            try:
                min_r = max(0, int(m.group(1)))
            except Exception:
                pass
        else:
            keywords.append(t)

    return keywords, min_r


def _format_date(d) -> str:
    """Return YYYY-MM-DD for datetime-like values."""
    if d is None:
        return ""
    if hasattr(d, "strftime"):
        return d.strftime("%Y-%m-%d")
    if isinstance(d, str):
        return d[:10]
    return ""


def get_filter_chats(session, user_id: Optional[int] = None) -> List[int]:
    """
    Return IDs of chats where search/recording is enabled.
    You can extend this to enforce per-user access control if needed.
    """
    rows = session.query(Chat).filter(Chat.enable == 1).all()
    return [c.id for c in rows]


def _lookup_user_ids(session, uname: str) -> List[int]:
    """Find matching user IDs by username (case-insensitive substring)."""
    if not uname:
        return []

    clean_name = uname[1:] if uname.startswith("@") else uname
    like = f"%{clean_name.lower()}%"

    rows = (
        session.query(User)
        .filter(User.username.isnot(None))
        .filter(User.username.ilike(like))
        .limit(50)
        .all()
    )
    return [u.id for u in rows]


# --- Core search logic ---

def search_messages_orm(
    session,
    keywords: List[str],
    chat_ids: List[int],
    user_ids: List[int],
    min_reactions: int,
    page: int,
    page_size: int,
) -> List[Dict]:
    """
    Search messages using SQLAlchemy ORM.

    Sorting:
      1) reactions_total DESC (NULL treated as 0) if available
      2) date DESC
    """
    if not chat_ids:
        return []

    q = session.query(Message).filter(Message.from_chat.in_(chat_ids))

    # Usually we only want messages that have text.
    q = q.filter(Message.text.isnot(None)).filter(Message.text != "")

    if user_ids:
        q = q.filter(Message.from_id.in_(user_ids))

    # AND filtering across keywords
    for kw in keywords:
        q = q.filter(Message.text.ilike(f"%{kw}%"))

    has_reactions_col = hasattr(Message, "reactions_total")

    if has_reactions_col:
        if min_reactions > 0:
            q = q.filter(Message.reactions_total >= min_reactions)

        q = q.order_by(
            desc(func.coalesce(Message.reactions_total, 0)),
            Message.date.desc(),
        )
    else:
        q = q.order_by(Message.date.desc())

    offset = (page - 1) * page_size
    msgs = q.limit(page_size).offset(offset).all()

    results: List[Dict] = []
    for m in msgs:
        r_total = getattr(m, "reactions_total", 0) or 0
        results.append(
            {
                "id": m.id,
                "text": m.text,
                "link": m.link,
                "date": m.date,
                "user": m.from_id,
                "chat": m.from_chat,
                "reactions_total": r_total,
            }
        )

    return results


# --- Telegram handlers ---

def inline_caps(update: Update, context: CallbackContext) -> None:
    iq = update.inline_query
    if not iq:
        return

    raw = (iq.query or "").strip()
    if not raw:
        return

    # 1) Page number
    query_text, page = _split_page(raw)

    # 2) Optional @username
    uname, query_text = _parse_uname(query_text)

    # 3) Keywords + reactions filter token
    keywords, min_reactions = _parse_tokens(query_text)

    session = DBSession()
    try:
        chat_ids = get_filter_chats(session)

        user_ids: List[int] = []
        if uname:
            user_ids = _lookup_user_ids(session, uname)
            if not user_ids:
                context.bot.answer_inline_query(iq.id, [], cache_time=5)
                return

        msgs = search_messages_orm(
            session,
            keywords=keywords,
            chat_ids=chat_ids,
            user_ids=user_ids,
            min_reactions=min_reactions,
            page=page,
            page_size=INLINE_PAGE_SIZE,
        )

        results: List[InlineQueryResultArticle] = []
        for m in msgs:
            text_msg = _norm(m.get("text") or "")
            link = str(m.get("link") or "")
            likes = int(m.get("reactions_total") or 0)
            date_str = _format_date(m.get("date"))

            title = text_msg[:60] if text_msg else "Message"

            # Description: reactions | date | user | chat
            desc_parts: List[str] = []
            if likes > 0:
                desc_parts.append(f"❤️{likes}")
            desc_parts.append(date_str)
            desc_parts.append(f"u:{m.get('user')}")
            desc_parts.append(f"c:{m.get('chat')}")
            description = " | ".join(desc_parts)

            # Message content sent on selection (HTML safe)
            content_text = html.escape(text_msg[:1000])
            if link:
                content_text += f"\n\n<a href='{link}'>🔗 Message link</a>"

            rid = f"{m.get('id')}-{secrets.token_hex(2)}"

            results.append(
                InlineQueryResultArticle(
                    id=rid,
                    title=title,
                    description=description,
                    input_message_content=InputTextMessageContent(
                        message_text=content_text,
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                    ),
                )
            )

        context.bot.answer_inline_query(iq.id, results, cache_time=2, is_personal=True)

    except Exception:
        # Always respond to Telegram to avoid client hanging
        try:
            context.bot.answer_inline_query(iq.id, [], cache_time=10)
        except Exception:
            pass
    finally:
        session.close()


def search_cmd(update: Update, context: CallbackContext) -> None:
    """Hint users to use inline mode."""
    try:
        update.message.reply_text("🔎 Use inline search: type @botname <query> in any chat.")
    except Exception:
        pass


def search_text(update: Update, context: CallbackContext) -> None:
    """Ignore normal text messages to avoid spam."""
    return


# Handlers exported for robot.py
inline_handler = InlineQueryHandler(inline_caps)
search_cmd_handler = CommandHandler("search", search_cmd)
search_text_handler = MessageHandler(Filters.text & (~Filters.command), search_text)

handler = inline_handler
