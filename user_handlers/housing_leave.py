"""07.11.2026 (Артём): у вечірньому зведенні фільтрів - хто за добу заблокував бота або вимкнув моніторинг житла.

Блок: Telegram сам надсилає my_chat_member, коли людина в особистому чаті блокує бота (статус kicked) або
повертається (member). Вимкнув моніторинг: видалив останній фільтр житла (`housing_monitor._delete_one_filter`).
Миттєво адміну нічого не йде - тільки в зведенні 21:00 (див. feedback: рутина - лише вечірні зведення).
"""
import html
import logging
from datetime import datetime
from typing import Dict, List, Optional

from telegram import Update
from telegram.ext import CallbackContext, ChatMemberHandler

from database import DBSession, HousingLeaveEvent

logger = logging.getLogger(__name__)
SHOWN = 10


def _has_filters(user_id: int) -> bool:
    try:
        from user_handlers import housing_monitor  # noqa: PLC0415 - цикл імпортів
        return bool(housing_monitor.user_filters(int(user_id)))
    except Exception:
        logger.exception("Could not count filters of %s", user_id)
        return False


def record(user_id: int, kind: str, had_filters: Optional[bool] = None) -> None:
    session = DBSession()
    try:
        session.add(HousingLeaveEvent(
            user_id=int(user_id), kind=kind,
            had_filters=_has_filters(user_id) if had_filters is None else bool(had_filters),
            created_at=datetime.utcnow(),
        ))
        session.commit()
    except Exception:
        session.rollback()
        logger.exception("Could not record leave event %s of %s", kind, user_id)
    finally:
        session.close()


def on_my_chat_member(update: Update, context: CallbackContext) -> None:
    m = update.my_chat_member
    if not m or m.chat.type != "private":
        return
    old, new = m.old_chat_member.status, m.new_chat_member.status
    if new == "kicked" and old != "kicked":
        record(m.chat.id, "blocked")
    elif old == "kicked" and new == "member":
        record(m.chat.id, "unblocked")


my_chat_member_handler = ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER)


def after_filter_deleted(user_id: int) -> None:
    """Викликається після видалення фільтра людиною: не лишилось жодного - моніторинг вимкнено."""
    if not _has_filters(user_id):
        record(user_id, "stopped", had_filters=False)


def events(since: datetime, until: datetime) -> List[Dict]:
    session = DBSession()
    try:
        rows = (session.query(HousingLeaveEvent)
                .filter(HousingLeaveEvent.created_at >= since, HousingLeaveEvent.created_at < until)
                .order_by(HousingLeaveEvent.created_at).all())
        return [{"user_id": r.user_id, "kind": r.kind, "had_filters": bool(r.had_filters)} for r in rows]
    finally:
        session.close()


def summary_lines(bot, since: datetime, until: datetime, display_name) -> List[str]:
    """Рядки для зведення: заблокували (і скільки з них мали фільтри), повернулись, вимкнули моніторинг."""
    evs = events(since, until)
    if not evs:
        return []
    last: Dict[int, Dict] = {}
    for ev in evs:   # за добу міг заблокувати й повернутись - рахуємо останній стан
        if ev["kind"] in ("blocked", "unblocked"):
            last[ev["user_id"]] = ev
    blocked = [e for e in last.values() if e["kind"] == "blocked"]
    back = [e for e in last.values() if e["kind"] == "unblocked"]
    stopped = list({e["user_id"]: e for e in evs if e["kind"] == "stopped"}.values())
    lines = []

    def names(items) -> str:
        out = []
        for e in items[:SHOWN]:
            try:
                name = display_name(bot.get_chat(e["user_id"]))
            except Exception:
                name = str(e["user_id"])
            out.append(f"{html.escape(name)} · <code>{e['user_id']}</code>" + (" (мав фільтри)" if e.get("had_filters") else ""))
        if len(items) > SHOWN:
            out.append(f"…і ще {len(items) - SHOWN}")
        return "\n".join("   " + x for x in out)

    if blocked:
        with_f = sum(1 for e in blocked if e["had_filters"])
        lines.append(f"🚫 Заблокували бота: <b>{len(blocked)}</b> (з фільтрами житла: {with_f})")
        lines.append(names(blocked))
    if stopped:
        lines.append(f"🔕 Вимкнули моніторинг (видалили всі фільтри): <b>{len(stopped)}</b>")
        lines.append(names(stopped))
    if back:
        lines.append(f"↩️ Повернулись (розблокували): <b>{len(back)}</b>")
    return lines
