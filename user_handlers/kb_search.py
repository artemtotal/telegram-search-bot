"""Answers from the chat knowledge base + proactive answers (31.10.2026).

* ``answer(query)`` — used by msg_ai for "потсдамбот …" questions once the
  knowledge base has read the whole chat (KB_SEARCH=auto) or always
  (KB_SEARCH=on). Cards are found by Gemini vectors and full text over all
  years, plus raw messages the builder has not read yet; one strong model
  writes the answer grouped by year.
* Proactive mode (KB_PROACTIVE): when someone asks the chat ("підкажіть
  стоматолога", "ищу грузчиков") without calling the bot, it prepares a short
  answer from the knowledge base. "preview" sends it to the admin with
  buttons, "on" replies in the chat directly, "off" does nothing. Strict
  limits: per day, per person, per chat.
"""

import json
import logging
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackContext, CallbackQueryHandler, Filters, MessageHandler

from user_jobs import kb_common as kb

logger = logging.getLogger(__name__)

KB_SEARCH = os.getenv("KB_SEARCH", "auto").lower()          # auto|on|off
KB_PROACTIVE = os.getenv("KB_PROACTIVE", "preview").lower()  # off|preview|on
PROACTIVE_MAX_PER_DAY = int(os.getenv("KB_PROACTIVE_MAX_PER_DAY", "12"))
PROACTIVE_USER_COOLDOWN_H = int(os.getenv("KB_PROACTIVE_USER_COOLDOWN_H", "6"))
PROACTIVE_CHAT_GAP_MIN = int(os.getenv("KB_PROACTIVE_CHAT_GAP_MIN", "5"))
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
CARDS_FOR_ANSWER = 40

_db_lock = threading.Lock()


def _store():
    return kb.connect()


def ready() -> bool:
    if KB_SEARCH == "off":
        return False
    if KB_SEARCH == "on":
        return True
    try:
        with _db_lock:
            store = _store()
            try:
                return bool(kb.get_meta(store, "caught_up"))
            finally:
                store.close()
    except Exception:
        return False


# ── Retrieval ─────────────────────────────────────────────────────────────

def _rrf(lists: List[List[int]], k: int = 60) -> List[int]:
    scores: Dict[int, float] = {}
    for ranked in lists:
        for rank, item in enumerate(ranked):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank + 1)
    return [item for item, _ in sorted(scores.items(), key=lambda kv: -kv[1])]


def _search_terms(query: str) -> List[str]:
    try:
        from user_handlers.msg_ai import _llm_search_terms
        return _llm_search_terms(query)
    except Exception as exc:
        logger.warning("KB search terms failed: %s", exc)
        return []


def retrieve(query: str) -> Tuple[List[dict], List[dict]]:
    """(cards, raw messages not yet read by the builder)."""
    stems = _search_terms(query)
    match = kb.fts_query([query], stems)
    with _db_lock:
        store = _store()
        try:
            fts_ids: List[int] = []
            if match:
                fts_ids = [r["rowid"] for r in store.execute(
                    "SELECT rowid FROM cards_fts WHERE cards_fts MATCH ? ORDER BY rank LIMIT 60",
                    (match,),
                )]
            vector_ids: List[int] = []
            try:
                vector_ids = [cid for cid, score in kb.vector_search(kb.embed([query])[0], 60)
                              if score >= 0.45]
            except Exception as exc:
                logger.warning("KB vector search failed: %s", exc)
            ids = _rrf([vector_ids, fts_ids])[:CARDS_FOR_ANSWER]
            cards = []
            if ids:
                rows = store.execute(
                    f"SELECT * FROM cards WHERE id IN ({','.join('?' * len(ids))})", ids
                ).fetchall()
                by_id = {r["id"]: dict(r) for r in rows}
                cards = [by_id[i] for i in ids if i in by_id]
            cursor = int(kb.get_meta(store, "cursor", "0"))
        finally:
            store.close()
    raw = _fresh_messages(match, cursor)
    logger.info("KB retrieve: %d vector + %d fts -> %d cards, %d fresh messages",
                len(vector_ids), len(fts_ids), len(cards), len(raw))
    return cards, raw


def _fresh_messages(match: Optional[str], cursor: int) -> List[dict]:
    """Messages after the builder's cursor that match the question."""
    if not match:
        return []
    try:
        main = kb.main_db()
        try:
            rows = main.execute(
                """
                SELECT m.date, m.link, m.text,
                       COALESCE(NULLIF(u.username, ''), u.fullname, '') AS author
                FROM message_fts f JOIN message m ON m.id = f.rowid
                LEFT JOIN user u ON u.id = m.from_id
                WHERE message_fts MATCH ? AND m._id > ?
                ORDER BY m.date DESC LIMIT 15
                """,
                (match, cursor),
            ).fetchall()
        finally:
            main.close()
        return [dict(r) for r in rows]
    except Exception as exc:
        logger.warning("KB fresh messages failed: %s", exc)
        return []


def _context(cards: List[dict], raw: List[dict]) -> str:
    lines = []
    for card in cards:
        links = json.loads(card["links"] or "[]")
        mentions = len(json.loads(card["msg_ids"] or "[]"))
        opinion = {"+": "хвалили", "-": "скаржились"}.get(card["opinion"], "")
        line = f"[{kb.year_span(card)}] ({card['kind']}{', ' + opinion if opinion else ''}) {card['title']}"
        if card["name"]:
            line += f" — {card['name']}"
        if card["contacts"]:
            line += f". Контакти: {card['contacts']}"
        if card["details"]:
            line += f". {card['details']}"
        line += f" (повідомлень: {mentions}, востаннє {card['last_date']})"
        if links:
            line += " →" + " →".join(links[:2])
        lines.append(line)
    for message in raw:
        text = re.sub(r"\s+", " ", message["text"] or "")[:600]
        lines.append(f"[{str(message['date'])[:10]}] (свіже повідомлення) {message['author'] or 'Учасник'}: "
                     f"{text} →{message['link']}")
    return "\n".join(lines)


ANSWER_PROMPT = """
Ти — Потсдамбот, помічник чату українців у Потсдамі. Сьогодні {today}.
Нижче — довідник, складений з історії чату за всі роки (картки з роком, контактами
й посиланнями), і кілька найсвіжіших повідомлень.

Правила:
- Відповідай ТІЛЬКИ українською, лише за довідником. Нічого не вигадуй, контакти — дослівно.
- Не здавайся: якщо свіжого немає, дай те, що радили раніше, з роком.
- Якщо знайдене з різних років — групуй за роками від свіжих до старих
  («2026:», «2024–2025:», «2022–2023:»). Одну й ту саму людину/сервіс з різних
  карток зведи в один пункт: скільки разів радили, чи хвалили/скаржились.
- Після кожного пункту — одне посилання →https://t.me/... з картки.
- Ігноруй картки не про це питання.
- Формат: коротко, список; до 2500 символів. Якщо нічого немає — скажи чесно одним реченням.
{extra}
Довідник:
{context}

Питання: {query}
""".strip()

PROACTIVE_EXTRA = """
- Це АВТОВІДПОВІДЬ на повідомлення в чаті, людина бота не кликала. Відповідай, ЛИШЕ якщо в
  довіднику є справді корисна конкретна відповідь саме на це питання (контакти, порада,
  інструкція). Інакше поверни рівно одне слово: SKIP
- Якщо повідомлення — не питання/прохання про пораду (оголошення, пропозиція послуг,
  продаж, балачка), поверни SKIP.
- До 900 символів, без вступів.
"""


def answer(query: str, proactive: bool = False) -> Tuple[str, List[dict]]:
    """(answer text, source dicts with link/date) — ("", []) if nothing usable."""
    cards, raw = retrieve(query)
    if not cards and not raw:
        return "", []
    prompt = ANSWER_PROMPT.format(
        today=datetime.utcnow().strftime("%Y-%m-%d"),
        extra=PROACTIVE_EXTRA if proactive else "",
        context=_context(cards, raw),
        query=query,
    )
    text, model = kb.chat(prompt, kb.ANSWER_MODELS, timeout=100, max_tokens=3000)
    logger.info("KB answer by %s (%d chars)", model, len(text))
    if not text or (proactive and text.strip().upper().startswith("SKIP")):
        return "", []
    sources = []
    for card in cards:
        for link in json.loads(card["links"] or "[]")[:1]:
            sources.append({"link": link, "date": card["last_date"]})
    sources += [{"link": m["link"], "date": str(m["date"])[:10]} for m in raw if m.get("link")]
    return text, sources


# ── Proactive answers in the group ────────────────────────────────────────

_ASK_RE = re.compile(
    r"(підкаж|подскаж|порад|посовет|порекоменд|рекоменд|кто знает|хто знає|хто може|кто может"
    r"|где найти|де знайти|где можно|де можна|есть ли|чи є |шукаю|ищу|нужен|нужна|нужно найти"
    r"|потрібен|потрібна|wer kennt|empfehl|kennt jemand)",
    re.IGNORECASE,
)
# \b matters: "Потсдам" contains "сдам".
_OFFER_RE = re.compile(
    r"\b(пропоную|предлагаю|продам|продаю|продається|сдам|здам|здаю|сдаю|віддам|отдам|вакансі|ваканси"
    r"|требуются|потрібні працівник|ищем сотрудник|шукаємо працівник|запрошуємо)",
    re.IGNORECASE,
)
_HOUSING_RE = re.compile(r"(квартир|житл|жиль|wohnung|wg\b|комнат|кімнат|оренд|аренд)", re.IGNORECASE)


def looks_like_question(text: str) -> bool:
    text = (text or "").strip()
    if len(text) < 15 or len(text) > 700:
        return False
    if _OFFER_RE.search(text):
        return False
    return bool(_ASK_RE.search(text)) and ("?" in text or bool(re.match(
        r"\s*(шукаю|ищу|нужен|нужна|потрібен|потрібна|порадьте|посоветуйте|подскажите|підкажіть)",
        text, re.IGNORECASE)))


def _limits_ok(store, chat_id: int, user_id: Optional[int]) -> bool:
    now = datetime.now(timezone.utc)
    day = (now - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    counted = "status IN ('preview', 'sent')"
    if store.execute(f"SELECT COUNT(*) FROM proactive WHERE {counted} AND created_at >= ?",
                     (day,)).fetchone()[0] >= PROACTIVE_MAX_PER_DAY:
        return False
    if user_id:
        since = (now - timedelta(hours=PROACTIVE_USER_COOLDOWN_H)).strftime("%Y-%m-%d %H:%M:%S")
        if store.execute(f"SELECT 1 FROM proactive WHERE {counted} AND user_id = ? AND created_at >= ?",
                         (user_id, since)).fetchone():
            return False
    since = (now - timedelta(minutes=PROACTIVE_CHAT_GAP_MIN)).strftime("%Y-%m-%d %H:%M:%S")
    if store.execute(f"SELECT 1 FROM proactive WHERE {counted} AND chat_id = ? AND created_at >= ?",
                     (chat_id, since)).fetchone():
        return False
    return True


def _proactive_text(body: str, question: str, bot_username: str) -> str:
    text = "🤖 У чаті раніше радили:\n\n" + body.strip()
    if _HOUSING_RE.search(question) and bot_username:
        text += (f"\n\n🏘 Шукаєте житло? Нові квартири в Потсдамі одразу в Telegram — "
                 f"моніторинг у @{bot_username} (кнопка «🏘 Житло»).")
    text += "\n\nПитайте ще: напишіть «потсдамбот» і своє питання."
    return text


def on_group_message(update: Update, context: CallbackContext) -> None:
    message = update.effective_message
    if KB_PROACTIVE == "off" or message is None or not message.text:
        return
    user = message.from_user
    if user is None or user.is_bot or message.reply_to_message is not None:
        return
    text = message.text
    if "потсдамбот" in text.lower() or "потсдам бот" in text.lower() or not looks_like_question(text):
        return
    if not ready():
        return
    chat_id = message.chat_id
    with _db_lock:
        store = _store()
        try:
            if not _limits_ok(store, chat_id, user.id):
                return
            row_id = store.execute(
                "INSERT INTO proactive(chat_id, msg_id, user_id, question, status) VALUES (?,?,?,?,?)",
                (chat_id, message.message_id, user.id, text[:1000], "working"),
            ).lastrowid
            store.commit()
        finally:
            store.close()
    try:
        body, sources = answer(text, proactive=True)
    except Exception:
        logger.exception("Proactive answer failed")
        body, sources = "", []
    status = "skipped"
    final = ""
    if body:
        from user_handlers.msg_ai import _normalize_source_line
        final = _normalize_source_line(_proactive_text(body, text, context.bot.username or ""), sources[:3])
        if KB_PROACTIVE == "on":
            try:
                message.reply_text(final, parse_mode=None, disable_web_page_preview=True)
                status = "sent"
            except Exception:
                logger.exception("Proactive reply failed")
                status = "error"
        elif ADMIN_ID:
            link = message.link or ""
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("📤 Відповісти в чаті", callback_data=f"kbp:send:{row_id}"),
                InlineKeyboardButton("✖ Ні", callback_data=f"kbp:skip:{row_id}"),
            ]])
            try:
                context.bot.send_message(
                    ADMIN_ID,
                    f"🤖 Автовідповідь (чернетка)\nПитання: {text[:500]}\n{link}\n\n— — —\n{final}"[:4000],
                    reply_markup=keyboard, parse_mode=None, disable_web_page_preview=True,
                )
                status = "preview"
            except Exception:
                logger.exception("Proactive preview to admin failed")
                status = "error"
    with _db_lock:
        store = _store()
        try:
            store.execute("UPDATE proactive SET status = ?, answer = ? WHERE id = ?", (status, final, row_id))
            store.commit()
        finally:
            store.close()
    logger.info("Proactive %s for message %s", status, message.message_id)


def on_admin_button(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    if query.from_user.id != ADMIN_ID:
        query.answer("Тільки для адміна", show_alert=True)
        return
    _, action, raw_id = query.data.split(":", 2)
    with _db_lock:
        store = _store()
        try:
            row = store.execute("SELECT * FROM proactive WHERE id = ?", (int(raw_id),)).fetchone()
            if row is None or row["status"] != "preview":
                query.answer("Вже оброблено")
                return
            status = "rejected"
            if action == "send":
                try:
                    context.bot.send_message(
                        row["chat_id"], row["answer"], reply_to_message_id=row["msg_id"],
                        parse_mode=None, disable_web_page_preview=True,
                        allow_sending_without_reply=True,
                    )
                    status = "sent"
                except Exception as exc:
                    logger.exception("Sending approved proactive answer failed")
                    query.answer(f"Не вдалося: {exc}"[:190], show_alert=True)
                    return
            store.execute("UPDATE proactive SET status = ? WHERE id = ?", (status, row["id"]))
            store.commit()
        finally:
            store.close()
    query.answer("Надіслано" if status == "sent" else "Пропущено")
    try:
        query.edit_message_reply_markup(reply_markup=None)
        query.message.reply_text("✅ Надіслано в чат" if status == "sent" else "✖ Пропущено")
    except Exception:
        pass


group_handler = MessageHandler(
    Filters.text & Filters.chat_type.groups & ~Filters.command,
    on_group_message,
    run_async=True,
)
callback_handler = CallbackQueryHandler(on_admin_button, pattern=r"^kbp:")
