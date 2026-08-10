"""Private subscriptions for Berlin DP Document e-queue availability."""

import html
import logging
import os
import re
from datetime import datetime, timedelta
from typing import Dict, Iterable, Optional, Tuple

import requests
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackContext, CallbackQueryHandler, CommandHandler, Filters

from database import DBSession, EqueueSubscription


logger = logging.getLogger(__name__)

SERVICE_KEY = "dp_document_berlin"
SERVICE_TITLE = "ДП Документ у Берліні"
SERVICE_URL = os.getenv(
    "PASSPORT_EQUEUE_URL",
    "https://berlin.pasport.org.ua/solutions/e-queue",
).strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
CHECK_TIMEOUT = int(os.getenv("PASSPORT_EQUEUE_TIMEOUT", "45") or 45)
ADMIN_ERROR_COOLDOWN = timedelta(hours=6)
BROWSER_ONLY = os.getenv("PASSPORT_EQUEUE_BROWSER_ONLY", "1") == "1"


def utc_now() -> datetime:
    return datetime.utcnow()


def _allowed_user_ids() -> set:
    raw = os.getenv("PASSPORT_EQUEUE_ALLOWED_USER_IDS", "").strip()
    ids = {ADMIN_ID} if ADMIN_ID else set()
    for part in re.split(r"[,;\s]+", raw):
        if part and part.lstrip("-").isdigit():
            ids.add(int(part))
    return ids


def is_allowed(user_id: Optional[int]) -> bool:
    return bool(user_id and user_id in _allowed_user_ids())


def private_home_rows(user_id: Optional[int]) -> Iterable[list]:
    if not is_allowed(user_id):
        return []
    return [[InlineKeyboardButton("🛂 Терміни ДП Документ", callback_data="equeue:menu")]]


def _menu_keyboard(active: bool) -> InlineKeyboardMarkup:
    rows = []
    if active:
        rows.append([InlineKeyboardButton("🔕 Відписатися від перевірки", callback_data="equeue:unsubscribe")])
    else:
        rows.append([InlineKeyboardButton("🔔 Підписатися на перевірку", callback_data="equeue:subscribe")])
    rows.append([InlineKeyboardButton("🔎 Перевірити зараз", callback_data="equeue:check")])
    rows.append([InlineKeyboardButton("🌐 Відкрити сайт", url=SERVICE_URL)])
    rows.append([InlineKeyboardButton("⬅ Головне меню", callback_data="anon:home")])
    return InlineKeyboardMarkup(rows)


def _get_subscription(session, user_id: int) -> Optional[EqueueSubscription]:
    return (
        session.query(EqueueSubscription)
        .filter(
            EqueueSubscription.user_id == user_id,
            EqueueSubscription.service == SERVICE_KEY,
        )
        .first()
    )


def _is_active(user_id: int) -> bool:
    session = DBSession()
    try:
        row = _get_subscription(session, user_id)
        return bool(row and row.active)
    finally:
        session.close()


def _upsert_subscription(user) -> None:
    session = DBSession()
    try:
        now = utc_now()
        row = _get_subscription(session, user.id)
        if row is None:
            row = EqueueSubscription(
                user_id=user.id,
                username=user.username or "",
                display_name=user.full_name or "",
                service=SERVICE_KEY,
                active=True,
                last_status="new",
                created_at=now,
                updated_at=now,
            )
            session.add(row)
        else:
            row.username = user.username or ""
            row.display_name = user.full_name or ""
            row.active = True
            row.updated_at = now
        session.commit()
    finally:
        session.close()


def _deactivate_subscription(user_id: int) -> None:
    session = DBSession()
    try:
        row = _get_subscription(session, user_id)
        if row:
            row.active = False
            row.updated_at = utc_now()
            session.commit()
    finally:
        session.close()


def _render_menu(active: bool, prefix: str = "") -> str:
    status = "увімкнена" if active else "вимкнена"
    body = (
        f"🛂 <b>{html.escape(SERVICE_TITLE)}</b>\n\n"
        "Бот може кожні 15 хвилин перевіряти електронну чергу й написати вам, "
        "якщо зʼявляться вільні терміни.\n\n"
        f"Статус підписки: <b>{status}</b>."
    )
    return (prefix + "\n\n" + body) if prefix else body


def show_menu(update: Update, context: CallbackContext, edit: bool = False, prefix: str = "") -> None:
    user = update.effective_user
    if not user or not is_allowed(user.id):
        text = "Ця перевірка зараз доступна тільки для дозволених користувачів."
        if edit and update.callback_query:
            update.callback_query.edit_message_text(text)
        else:
            update.effective_message.reply_text(text)
        return
    active = _is_active(user.id)
    text = _render_menu(active, prefix=prefix)
    if edit and update.callback_query:
        update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=_menu_keyboard(active))
    else:
        update.effective_message.reply_text(text, parse_mode="HTML", reply_markup=_menu_keyboard(active))


def _looks_like_cloudflare_challenge(text: str, status_code: int) -> bool:
    sample = text[:5000].lower()
    return (
        status_code in (403, 429, 503)
        and (
            "cf-chl" in sample
            or "cf_chl" in sample
            or "just a moment" in sample
            or "enable javascript and cookies" in sample
            or "challenges.cloudflare.com" in sample
        )
    )


def parse_availability(text: str) -> Tuple[bool, str]:
    """Return (available, reason) from visible page text.

    The site is dynamic, so this parser is intentionally conservative: it only
    returns True on explicit free-slot wording and otherwise treats the result
    as no confirmed appointments.
    """
    normalized = re.sub(r"\s+", " ", text).strip().lower()
    negative_patterns = [
        "вільних місць немає",
        "немає вільних",
        "відсутні вільні",
        "нет свободных",
        "свободных мест нет",
        "no available",
        "no slots",
    ]
    positive_patterns = [
        "є вільні",
        "вільні місця",
        "вільні терміни",
        "доступні терміни",
        "доступний час",
        "available slots",
        "available appointments",
    ]
    if any(pattern in normalized for pattern in negative_patterns):
        return False, "на сторінці явно написано, що вільних термінів немає"
    if any(pattern in normalized for pattern in positive_patterns):
        return True, "на сторінці знайдено ознаки вільних термінів"
    return False, "вільні терміни не підтверджені"


def check_equeue_availability() -> Dict[str, object]:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ),
        "Accept-Language": "uk-UA,uk;q=0.9,ru;q=0.8,en;q=0.7",
    }
    try:
        response = requests.get(SERVICE_URL, headers=headers, timeout=CHECK_TIMEOUT)
    except requests.RequestException as exc:
        return {"ok": False, "available": False, "status": "error", "reason": str(exc)}

    text = response.text or ""
    if _looks_like_cloudflare_challenge(text, response.status_code):
        return {
            "ok": False,
            "available": False,
            "status": "blocked",
            "status_code": response.status_code,
            "reason": "сайт повернув Cloudflare-перевірку; потрібен браузерний профиль із cookies",
        }
    if response.status_code >= 400:
        return {
            "ok": False,
            "available": False,
            "status": "http_error",
            "status_code": response.status_code,
            "reason": f"HTTP {response.status_code}",
        }
    available, reason = parse_availability(text)
    return {
        "ok": True,
        "available": available,
        "status": "available" if available else "none",
        "status_code": response.status_code,
        "reason": reason,
    }


def _active_subscribers():
    session = DBSession()
    try:
        rows = (
            session.query(EqueueSubscription)
            .filter(EqueueSubscription.service == SERVICE_KEY, EqueueSubscription.active == 1)
            .all()
        )
        return [(row.user_id, row.last_status, row.last_notified_at) for row in rows]
    finally:
        session.close()


def _update_status_for_active(status: str, notified: bool = False) -> None:
    session = DBSession()
    try:
        now = utc_now()
        rows = (
            session.query(EqueueSubscription)
            .filter(EqueueSubscription.service == SERVICE_KEY, EqueueSubscription.active == 1)
            .all()
        )
        for row in rows:
            row.last_status = status
            row.last_checked_at = now
            row.updated_at = now
            if notified:
                row.last_notified_at = now
        session.commit()
    finally:
        session.close()


def _notify_admin_error(bot, result: Dict[str, object]) -> None:
    if not ADMIN_ID:
        return
    session = DBSession()
    try:
        recent = (
            session.query(EqueueSubscription)
            .filter(
                EqueueSubscription.service == SERVICE_KEY,
                EqueueSubscription.last_status == str(result.get("status")),
                EqueueSubscription.last_checked_at.isnot(None),
            )
            .order_by(EqueueSubscription.last_checked_at.desc())
            .first()
        )
        now = utc_now()
        if recent and recent.last_checked_at > now - ADMIN_ERROR_COOLDOWN:
            return
    finally:
        session.close()
    try:
        bot.send_message(
            ADMIN_ID,
            "⚠️ Перевірка ДП Документ не виконана\n\n"
            f"Причина: {html.escape(str(result.get('reason') or 'невідома'))}\n"
            f"Сайт: {SERVICE_URL}",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception:
        logger.exception("Could not notify admin about e-queue checker error")


def _notify_available(bot, subscribers, result: Dict[str, object]) -> None:
    text = (
        f"🟢 <b>Є ознаки вільних термінів: {html.escape(SERVICE_TITLE)}</b>\n\n"
        "Відкрийте сайт і перевірте запис вручну.\n"
        f"Причина: {html.escape(str(result.get('reason') or 'знайдено доступність'))}"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🌐 Відкрити електронну чергу", url=SERVICE_URL)],
        [InlineKeyboardButton("🔕 Відписатися", callback_data="equeue:unsubscribe")],
    ])
    for user_id, last_status, _last_notified_at in subscribers:
        if last_status == "available":
            continue
        try:
            bot.send_message(
                user_id,
                text,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=keyboard,
            )
        except Exception:
            logger.exception("Could not notify e-queue subscriber %s", user_id)


def handle_browser_result(bot, payload: Dict[str, object]) -> Dict[str, object]:
    if payload.get("source") != SERVICE_KEY:
        return {"ok": False, "error": "unsupported source"}
    subscribers = _active_subscribers()
    status = str(payload.get("status") or "unknown")
    result = {
        "ok": status not in {"blocked", "error", "http_error"},
        "available": bool(payload.get("available")) or status == "available",
        "status": status,
        "reason": str(payload.get("reason") or ""),
    }
    if not subscribers:
        _update_status_for_active(status, notified=False)
        return {"ok": True, "subscribers": 0, "status": status}
    if not result["ok"]:
        _notify_admin_error(bot, result)
        _update_status_for_active(status, notified=False)
        return {"ok": True, "subscribers": len(subscribers), "status": status}
    if result["available"]:
        _notify_available(bot, subscribers, result)
        _update_status_for_active(status, notified=True)
        return {"ok": True, "subscribers": len(subscribers), "status": status, "notified": True}
    _update_status_for_active(status, notified=False)
    return {"ok": True, "subscribers": len(subscribers), "status": status, "notified": False}


def check_job(context: CallbackContext) -> None:
    subscribers = _active_subscribers()
    if not subscribers:
        return
    if BROWSER_ONLY:
        return
    result = check_equeue_availability()
    status = str(result.get("status") or "unknown")
    if not result.get("ok"):
        logger.warning("E-queue check failed: %s", result)
        _notify_admin_error(context.bot, result)
        _update_status_for_active(status, notified=False)
        return

    if not result.get("available"):
        _update_status_for_active(status, notified=False)
        return

    _notify_available(context.bot, subscribers, result)
    _update_status_for_active(status, notified=True)


def handle_callback(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    if not query or not query.data:
        return
    user = query.from_user
    if not is_allowed(user.id):
        query.answer("Немає доступу", show_alert=True)
        return
    if query.data == "equeue:menu":
        query.answer()
        show_menu(update, context, edit=True)
    elif query.data == "equeue:subscribe":
        _upsert_subscription(user)
        query.answer("Підписку увімкнено")
        show_menu(update, context, edit=True, prefix="✅ Підписку увімкнено. Перевірка йде кожні 15 хвилин.")
    elif query.data == "equeue:unsubscribe":
        _deactivate_subscription(user.id)
        query.answer("Підписку вимкнено")
        show_menu(update, context, edit=True, prefix="🔕 Підписку вимкнено.")
    elif query.data == "equeue:check":
        query.answer("Перевіряю…")
        result = check_equeue_availability()
        if result.get("available"):
            prefix = "🟢 Зараз є ознаки вільних термінів. Відкрийте сайт і перевірте вручну."
        elif result.get("ok"):
            prefix = "⚪ Вільні терміни зараз не підтверджені."
        else:
            prefix = f"⚠️ Перевірка не виконана: {result.get('reason')}"
        show_menu(update, context, edit=True, prefix=prefix)


command_handler = CommandHandler("dps_document", show_menu, Filters.chat_type.private)
callback_handler = CallbackQueryHandler(handle_callback, pattern=r"^equeue:")
