"""Dependency-free validation helpers for anonymous forum posts."""

import hashlib
import math
import re
from datetime import datetime, timedelta


_URL_RE = re.compile(r"(?i)(?:https?://|www\.|t\.me/|telegram\.me/|joinchat/|@[a-z0-9_]{4,})")
_EMAIL_RE = re.compile(r"(?i)\b[^\s@]+@[^\s@]+\.[^\s@]+\b")
_PHONE_CANDIDATE_RE = re.compile(r"(?<!\d)(?:\+?\d[\d\s().-]{7,}\d)(?!\d)")
_REPEATED_RE = re.compile(r"(.)\1{12,}", re.DOTALL)


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip()).lower()


def text_fingerprint(text: str) -> str:
    return hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()


def validate_submission(text: str, min_length: int = 15, max_length: int = 1500):
    """Return a user-facing validation error, or None when text is allowed."""
    cleaned = (text or "").strip()
    if len(cleaned) < min_length:
        return f"Вопрос слишком короткий. Нужно минимум {min_length} символов."
    if len(cleaned) > max_length:
        return f"Вопрос слишком длинный. Максимум {max_length} символов."
    if len(cleaned.splitlines()) > 25:
        return "Слишком много строк. Сократите и отправьте вопрос ещё раз."
    phone_found = any(
        sum(character.isdigit() for character in candidate) >= 9
        for candidate in _PHONE_CANDIDATE_RE.findall(cleaned)
    )
    if _URL_RE.search(cleaned) or _EMAIL_RE.search(cleaned) or phone_found:
        return "Ссылки, @имена, e-mail и номера телефонов в анонимных постах запрещены."
    if _REPEATED_RE.search(cleaned):
        return "В тексте слишком много повторяющихся символов."
    if len(set(normalize_text(cleaned))) < 5:
        return "Текст похож на спам. Сформулируйте вопрос обычными словами."
    return None


def cooldown_text(last_submission_at, cooldown_days: int, now: datetime = None) -> str:
    if not last_submission_at:
        return ""
    now = now or datetime.utcnow()
    available_at = last_submission_at + timedelta(days=cooldown_days)
    remaining = available_at - now
    if remaining.total_seconds() <= 0:
        return ""
    total_minutes = max(1, math.ceil(remaining.total_seconds() / 60))
    days, minute_remainder = divmod(total_minutes, 24 * 60)
    hours, minutes = divmod(minute_remainder, 60)
    if days:
        return f"Новый анонимный пост можно создать через {days} дн. {hours} ч."
    return f"Новый анонимный пост можно создать через {hours} ч. {minutes} мин."


def message_link(message) -> str:
    username = getattr(message.chat, "username", None)
    if username:
        base = f"https://t.me/{username.lstrip('@')}"
    else:
        chat_id = str(message.chat_id)
        internal_id = chat_id[4:] if chat_id.startswith("-100") else chat_id.lstrip("-")
        base = f"https://t.me/c/{internal_id}"
    thread_id = int(getattr(message, "message_thread_id", 0) or 0)
    if thread_id:
        return f"{base}/{thread_id}/{message.message_id}"
    return f"{base}/{message.message_id}"
