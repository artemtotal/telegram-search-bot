"""
AI-powered chat assistant handler.
Search priority: 1) FAQ knowledge base, 2) chat message history with chains.
"""

import json
import logging
import os
import re
from datetime import datetime, timedelta
from typing import List, Dict, Optional

import requests
from sqlalchemy import or_
from telegram import Update
from telegram.ext import CallbackContext, MessageHandler, Filters

from database import Chat, DBSession, Message, User

logger = logging.getLogger(__name__)

TRIGGER_WORD = os.getenv("AI_TRIGGER_WORD", "потсдамбот").lower()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
FAQ_PATH = os.getenv("FAQ_PATH", "/app/config/faq.json")
CHAT_USERNAME = os.getenv("CHAT_USERNAME", "")

CONTEXT_MESSAGES_KEYWORD = 60
CONTEXT_MESSAGES_RECENT = 20
CHAIN_WINDOW = 3
RECENCY_CUTOFF_DAYS = 365
RECENCY_PENALTY_DAYS = 730

GEMINI_API_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "{model}:generateContent?key={api_key}"
)

SYSTEM_PROMPT_FAQ = """
Ты — бот-помощник жителей Потсдама. Тебе дана готовая справочная информация.
Ответь на вопрос пользователя на основе этой информации.
ВАЖНО: вопрос задан на русском — отвечай ТОЛЬКО на русском. Не используй украинский в ответе. Максимум 1500 символов. Буду краток и конкретен.
Если в справке есть ссылки — обязательно включи их в ответ.
""".strip()

SYSTEM_PROMPT_CHAT = """
Ты — бот-помощник группового чата жителей Потсдама.
Сообщения в чате на русском, украинском и немецком — используй все одинаково.

Формат ответа: связный текст без нумерации, 3-5 предложений, только самое полезное.

Правила:
- Максимум 1500 символов.
- Приоритет свежим сообщениям (смотри на дату).
- Не придумывай того, чего нет в контексте.
- Если информации нет совсем — скажи честно одной строкой.
- ВАЖНО: вопрос задан на русском — отвечай ТОЛЬКО на русском. Не используй украинский в ответе.
- Если в контексте есть данные об источнике сообщения (автор, дата) — упомяни их в конце ответа в формате: "Источник: @username, YYYY-MM-DD"
""".strip()


def _load_faq() -> List[Dict]:
    try:
        with open(FAQ_PATH, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        logger.warning(f"FAQ file not found: {FAQ_PATH}")
        return []
    except Exception as e:
        logger.error(f"Failed to load FAQ: {e}")
        return []


def _search_faq(query: str, faq: List[Dict]) -> Optional[str]:
    if not faq:
        return None
    query_lower = query.lower()
    query_words = set(query_lower.split())
    best_score = 0
    best_answer = None
    for entry in faq:
        keywords = [k.lower() for k in entry.get("keywords", [])]
        score = 0
        for kw in keywords:
            if kw in query_lower:
                score += 2
            elif any(kw in w or w in kw for w in query_words if len(w) > 2):
                score += 1
        if score > best_score:
            best_score = score
            best_answer = entry.get("answer", "")
    return best_answer if best_score > 2 else None


def _extract_query(text: str) -> Optional[str]:
    if not text:
        return None
    lower = text.lower().strip()
    if not lower.startswith(TRIGGER_WORD):
        return None
    query = text.strip()[len(TRIGGER_WORD):].strip()
    return query if query else None


def _get_message_ids_by_keywords(session, chat_ids, keywords, limit=CONTEXT_MESSAGES_KEYWORD):
    if not keywords or not chat_ids:
        return []
    q = (
        session.query(Message._id)
        .filter(Message.from_chat.in_(chat_ids))
        .filter(Message.text.isnot(None))
        .filter(Message.text != "")
        .filter(or_(*[Message.text.ilike(f"%{kw}%") for kw in keywords]))
        .order_by(Message.date.desc())
        .limit(limit)
    )
    return [row[0] for row in q.all()]


def _fetch_chain(session, center_ids, window=CHAIN_WINDOW):
    if not center_ids:
        return []
    all_pks = set()
    for pk in center_ids:
        for offset in range(-window, window + 1):
            all_pks.add(pk + offset)
    rows = (
        session.query(Message, User)
        .outerjoin(User, Message.from_id == User.id)
        .filter(Message._id.in_(all_pks))
        .filter(Message.text.isnot(None))
        .filter(Message.text != "")
        .order_by(Message.date.asc())
        .all()
    )
    return _rows_to_dicts(rows)


def _search_recent(session, chat_ids, limit=CONTEXT_MESSAGES_RECENT):
    if not chat_ids:
        return []
    cutoff = datetime.utcnow() - timedelta(days=RECENCY_CUTOFF_DAYS)
    rows = (
        session.query(Message, User)
        .outerjoin(User, Message.from_id == User.id)
        .filter(Message.from_chat.in_(chat_ids))
        .filter(Message.text.isnot(None))
        .filter(Message.text != "")
        .filter(Message.date >= cutoff)
        .order_by(Message.date.desc())
        .limit(limit)
        .all()
    )
    return _rows_to_dicts(rows)


def _rows_to_dicts(rows):
    results = []
    for msg, user in rows:
        username = (user.username or user.fullname or "") if user else ""
        msg_date = msg.date if hasattr(msg.date, "year") else None
        date_str = msg.date.strftime("%Y-%m-%d %H:%M") if hasattr(msg.date, "strftime") else str(msg.date)[:16]
        tg_link = ""
        if CHAT_USERNAME and hasattr(msg, "message_id") and msg.message_id:
            tg_link = f"https://t.me/{CHAT_USERNAME}/{msg.message_id}"
        results.append({
            "text": (msg.text or "").strip(),
            "user": username,
            "date": date_str,
            "date_obj": msg_date,
            "id": msg._id,
            "link": tg_link,
        })
    return results


def _recency_score(msg):
    d = msg.get("date_obj")
    if not d:
        return 0
    age_days = (datetime.utcnow() - d).days
    if age_days <= RECENCY_CUTOFF_DAYS:
        return 2
    if age_days <= RECENCY_PENALTY_DAYS:
        return 1
    return 0


def _build_context(chain_msgs, recent_msgs):
    seen_ids = set()
    combined = []
    for m in chain_msgs + recent_msgs:
        if m["id"] in seen_ids:
            continue
        seen_ids.add(m["id"])
        combined.append(m)
    combined.sort(key=lambda m: (_recency_score(m), m["date"]), reverse=True)
    lines = []
    for m in combined:
        line = f"[{m['date']}] @{m['user']}: {m['text']}"
        if m.get("link"):
            line += f" [ссылка: {m['link']}]"
        lines.append(line)
    return "\n".join(lines)


def _call_gemini(prompt, max_tokens=1024):
    if not GEMINI_API_KEY:
        return ""
    url = GEMINI_API_URL.format(model=GEMINI_MODEL, api_key=GEMINI_API_KEY)
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0.2},
    }
    try:
        resp = requests.post(url, json=payload, timeout=45)
        resp.raise_for_status()
        data = resp.json()
        candidates = data.get("candidates", [])
        if not candidates:
            return ""
        parts = candidates[0].get("content", {}).get("parts", [])
        return "".join(p.get("text", "") for p in parts).strip()
    except requests.exceptions.Timeout:
        logger.error("Gemini API timeout")
        return ""
    except requests.exceptions.HTTPError as e:
        logger.error(f"Gemini HTTP error: {e.response.status_code}")
        return "RATE_LIMIT" if e.response.status_code == 429 else ""
    except Exception as e:
        logger.error(f"Gemini error: {e}")
        return ""


def handle_ai_query(update: Update, context: CallbackContext) -> None:
    if not update.message or not update.message.text:
        return
    query = _extract_query(update.message.text)
    if query is None:
        return

    try:
        context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    except Exception:
        pass

    faq = _load_faq()
    faq_answer = _search_faq(query, faq)

    if faq_answer:
        logger.info(f"FAQ hit for query: {query!r}")
        prompt = (
            f"{SYSTEM_PROMPT_FAQ}\n\n"
            f"--- СПРАВОЧНАЯ ИНФОРМАЦИЯ ---\n{faq_answer}\n"
            f"--- КОНЕЦ ---\n\n"
            f"Вопрос: {query}"
        )
        answer = _call_gemini(prompt)
        if answer and answer != "RATE_LIMIT":
            if len(answer) > 3900:
                answer = answer[:3900] + "..."
            update.message.reply_text(answer, parse_mode=None)
            return

    session = DBSession()
    try:
        chat_ids = [c.id for c in session.query(Chat).filter(Chat.enable == 1).all()]
        if not chat_ids:
            update.message.reply_text("Нет активных чатов для поиска.")
            return

        keywords = [w for w in query.split() if len(w) > 1]
        logger.info(f"Chat search for query: {query!r} keywords: {keywords}")

        matched_ids = _get_message_ids_by_keywords(session, chat_ids, keywords)
        chain_msgs = _fetch_chain(session, matched_ids)
        recent_msgs = _search_recent(session, chat_ids)

        if not chain_msgs and not recent_msgs:
            update.message.reply_text("В базе нет сообщений для ответа на этот вопрос.")
            return

        ctx = _build_context(chain_msgs, recent_msgs)
        prompt = (
            f"{SYSTEM_PROMPT_CHAT}\n\n"
            f"--- КОНТЕКСТ ИЗ ЧАТА ---\n{ctx}\n"
            f"--- КОНЕЦ КОНТЕКСТА ---\n\n"
            f"Вопрос: {query}"
        )
        answer = _call_gemini(prompt)

        if answer == "RATE_LIMIT":
            update.message.reply_text("Превышен лимит запросов к AI. Попробуйте через минуту.")
            return
        if not answer:
            update.message.reply_text("AI не смог сформировать ответ. Попробуйте позже.")
            return
        if len(answer) > 3900:
            answer = answer[:3900] + "..."
        update.message.reply_text(answer, parse_mode=None)

    except Exception as e:
        logger.error(f"AI handler error: {e}", exc_info=True)
        try:
            update.message.reply_text("Произошла ошибка. Попробуйте позже.")
        except Exception:
            pass
    finally:
        session.close()


handler = MessageHandler(
    Filters.regex(rf"(?i)^{re.escape(TRIGGER_WORD)}\b") & (~Filters.command),
    handle_ai_query,
)
