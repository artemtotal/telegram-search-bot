"""Private subscriptions for Berlin DP Document e-queue availability."""

import html
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, Optional, Tuple
from zoneinfo import ZoneInfo

import requests
from telegram.error import BadRequest
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackContext, CallbackQueryHandler, CommandHandler, Filters

import i18n
from database import DBSession, EqueueAvailableSighting, EqueueStatus, EqueueSubscription


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
# Скільки останніх знахідок вільних термінів показувати в меню.
SIGHTINGS_SHOWN = 3
# Якщо вільних термінів не бачили довше за цей строк, попереджаємо адміна:
# сама перевірка при цьому може бути цілком справною (сайт чесно каже "місць
# немає"), тож це підказка "варто глянути", а не доказ поломки.
STALE_AFTER = timedelta(hours=int(os.getenv("PASSPORT_EQUEUE_STALE_HOURS", "24") or 24))
STALE_ALERT_COOLDOWN = timedelta(hours=12)
BROWSER_ONLY = os.getenv("PASSPORT_EQUEUE_BROWSER_ONLY", "1") == "1"
BERLIN_TZ = ZoneInfo("Europe/Berlin")


def utc_now() -> datetime:
    return datetime.utcnow()


def _format_berlin_time(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(BERLIN_TZ).strftime("%d.%m.%Y %H:%M")


def _allowed_user_ids() -> set:
    raw = os.getenv("PASSPORT_EQUEUE_ALLOWED_USER_IDS", "").strip()
    ids = {ADMIN_ID} if ADMIN_ID else set()
    for part in re.split(r"[,;\s]+", raw):
        if part and part.lstrip("-").isdigit():
            ids.add(int(part))
    return ids


def is_allowed(user_id: Optional[int]) -> bool:
    return bool(user_id)


def private_home_rows(user_id: Optional[int]) -> Iterable[list]:
    if not is_allowed(user_id):
        return []
    return [[InlineKeyboardButton(i18n.t("equeue.btn.home", i18n.get_lang(user_id)), callback_data="equeue:menu")]]


def _menu_keyboard(active: bool, lang: str = "uk") -> InlineKeyboardMarkup:
    rows = []
    if active:
        rows.append([InlineKeyboardButton(i18n.t("equeue.btn.unsubscribe", lang), callback_data="equeue:unsubscribe")])
    else:
        rows.append([InlineKeyboardButton(i18n.t("equeue.btn.subscribe", lang), callback_data="equeue:subscribe")])
    rows.append([InlineKeyboardButton(i18n.t("equeue.btn.check_now", lang), callback_data="equeue:check")])
    rows.append([InlineKeyboardButton(i18n.t("equeue.btn.open_site", lang), url=SERVICE_URL)])
    rows.append([InlineKeyboardButton(i18n.t("anon.btn.back_home", lang), callback_data="anon:home")])
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


def _latest_status_text(user_id: int, lang: str = "uk") -> str:
    """Return the latest browser-submitted status for the service.

    Browser-only checks are global: Chrome checks the site once and posts the
    result to the bot receiver. The menu must therefore show the newest browser
    result for the service, not a possibly stale per-user subscription row.
    """
    session = DBSession()
    try:
        row = session.query(EqueueStatus).filter(EqueueStatus.service == SERVICE_KEY).first()
        if row is None or row.last_checked_at is None:
            # Рядки підписок лишаються запасним джерелом для баз, які ще не
            # бачили жодного результату після появи equeue_status.
            row = (
                session.query(EqueueSubscription)
                .filter(
                    EqueueSubscription.service == SERVICE_KEY,
                    EqueueSubscription.last_checked_at.isnot(None),
                )
                .order_by(EqueueSubscription.last_checked_at.desc())
                .first()
            )
        if not row or row.last_checked_at is None:
            return i18n.t("equeue.status.never_checked", lang)
        checked = _format_berlin_time(row.last_checked_at)
        status = row.last_status or "unknown"
        if status == "available":
            return i18n.t("equeue.status.available", lang, checked=checked)
        if status == "none":
            return i18n.t("equeue.status.none", lang, checked=checked)
        if status == "blocked":
            return i18n.t("equeue.status.blocked", lang, checked=checked)
        return i18n.t("equeue.status.other", lang, checked=checked, status=html.escape(status))
    finally:
        session.close()


def _record_available_sighting(reason: str = "") -> None:
    session = DBSession()
    try:
        session.add(EqueueAvailableSighting(
            service=SERVICE_KEY, found_at=utc_now(), reason=str(reason or "")[:500],
        ))
        session.commit()
    finally:
        session.close()


def recent_sightings(limit: int = SIGHTINGS_SHOWN) -> list:
    session = DBSession()
    try:
        rows = (
            session.query(EqueueAvailableSighting)
            .filter(EqueueAvailableSighting.service == SERVICE_KEY)
            .order_by(EqueueAvailableSighting.found_at.desc())
            .limit(int(limit))
            .all()
        )
        return [row.found_at for row in rows]
    finally:
        session.close()


def _sightings_text(lang: str = "uk") -> str:
    """Кілька останніх моментів, коли терміни справді бачили.

    Сам по собі рядок "вільні терміни не підтверджені" не відрізняє "місць
    зараз немає" від "перевірка мовчки зламалась тиждень тому" - за списком
    останніх знахідок це видно одразу.
    """
    found = recent_sightings()
    if not found:
        return i18n.t("equeue.sightings.none", lang)
    lines = [i18n.t("equeue.sightings.title", lang)]
    lines.extend(f"• {_format_berlin_time(value)}" for value in found)
    ago_hours = int((utc_now() - found[0]).total_seconds() // 3600)
    lines.append(i18n.t("equeue.sightings.ago", lang, hours=ago_hours))
    return "\n".join(lines)


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


def _render_menu(active: bool, user_id: Optional[int] = None, prefix: str = "", lang: str = "uk") -> str:
    status = i18n.t("equeue.subscription.on", lang) if active else i18n.t("equeue.subscription.off", lang)
    latest = _latest_status_text(user_id, lang) if user_id else ""
    body = i18n.t("equeue.menu.text", lang, title=html.escape(SERVICE_TITLE), status=status)
    if latest:
        body += f"\n\n{latest}"
    if user_id:
        body += f"\n\n{_sightings_text(lang)}"
    return (prefix + "\n\n" + body) if prefix else body


def show_menu(update: Update, context: CallbackContext, edit: bool = False, prefix: str = "") -> None:
    user = update.effective_user
    if not user or not is_allowed(user.id):
        text = i18n.t("equeue.not_allowed", i18n.get_lang(user.id) if user else "uk")
        if edit and update.callback_query:
            update.callback_query.edit_message_text(text)
        else:
            update.effective_message.reply_text(text)
        return
    lang = i18n.get_lang(user.id)
    active = _is_active(user.id)
    text = _render_menu(active, user_id=user.id, prefix=prefix, lang=lang)
    if edit and update.callback_query:
        try:
            update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=_menu_keyboard(active, lang))
        except BadRequest as exc:
            if "Message is not modified" not in str(exc):
                raise
    else:
        update.effective_message.reply_text(text, parse_mode="HTML", reply_markup=_menu_keyboard(active, lang))


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


def _record_service_status(status: str, reason: str = "") -> None:
    """Зберігає останній браузерний результат незалежно від підписок.

    Раніше час писався лише в активні підписки, тож із вимкненою підпискою
    жоден рядок не оновлювався: меню показувало відмітку того моменту, коли
    підписку востаннє вмикали, і мовчазний простій виглядав як свіжа перевірка.
    """
    session = DBSession()
    try:
        row = session.query(EqueueStatus).filter(EqueueStatus.service == SERVICE_KEY).first()
        if row is None:
            row = EqueueStatus(service=SERVICE_KEY)
            session.add(row)
        row.last_checked_at = utc_now()
        row.last_status = str(status or "unknown")
        row.last_reason = str(reason or "")[:500]
        session.commit()
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
    """Попереджає адміна, коли браузерна перевірка не змогла визначити статус.

    Кулдаун звіряється з `EqueueStatus.last_admin_alert_at` - міткою САМЕ
    факту надсилання - а не з `EqueueSubscription.last_checked_at`, який
    оновлюється щоразу незалежно від статусу (див. `_update_status_for_active`).
    Раніше кулдаун звірявся якраз із цим полем, і поки статус лишався тим самим
    (наприклад, блокування Cloudflare тривало днями), "недавній" запис
    знаходився щоразу заново - адмін отримував рівно одне повідомлення при
    першому переході в проблемний стан і жодного нагадування після, аж доки
    статус хоч раз не зміниться.
    """
    if not ADMIN_ID:
        return
    now = utc_now()
    session = DBSession()
    try:
        row = session.query(EqueueStatus).filter(EqueueStatus.service == SERVICE_KEY).first()
        if row is None:
            row = EqueueStatus(service=SERVICE_KEY)
            session.add(row)
        if row.last_admin_alert_at is not None and row.last_admin_alert_at > now - ADMIN_ERROR_COOLDOWN:
            return
        status = str(result.get("status") or "")
        reason = html.escape(str(result.get("reason") or "невідома"))
        if status == "blocked":
            text = (
                "🔒 <b>ДП Документ: потрібна ручна перевірка Cloudflare</b>\n\n"
                f"Причина: {reason}\n\n"
                "Автоматична перевірка сама не може пройти капчу. Відкрийте сайт "
                "у тому ж профілі Chrome, де працює розширення-збирач, і пройдіть "
                "перевірку вручну - після цього автоматичні перевірки знову запрацюють.\n\n"
                f"Сайт: {SERVICE_URL}"
            )
        else:
            text = (
                "⚠️ <b>Перевірка ДП Документ не виконана</b>\n\n"
                f"Причина: {reason}\n"
                f"Сайт: {SERVICE_URL}"
            )
        try:
            bot.send_message(ADMIN_ID, text, parse_mode="HTML", disable_web_page_preview=True)
        except Exception:
            logger.exception("Could not notify admin about e-queue checker error")
            return
        row.last_admin_alert_at = now
        session.commit()
    finally:
        session.close()


def _notify_admin_stale(bot) -> None:
    """Попереджає, коли вільних термінів не бачили довше за STALE_AFTER.

    Викликається лише після успішної перевірки (сайт відповів), тож це саме
    "перевірка працює, але тиша підозріло довга", а не дубль до
    `_notify_admin_error`. Тиша може бути й правдою - місць справді нема, -
    тому текст пропонує перевірити, а не стверджує поломку.
    """
    if not ADMIN_ID:
        return
    now = utc_now()
    found = recent_sightings(limit=1)
    quiet_since = found[0] if found else None
    if quiet_since is not None and now - quiet_since < STALE_AFTER:
        return
    session = DBSession()
    try:
        row = session.query(EqueueStatus).filter(EqueueStatus.service == SERVICE_KEY).first()
        if row is None:
            row = EqueueStatus(service=SERVICE_KEY)
            session.add(row)
        if row.last_stale_alert_at is not None and row.last_stale_alert_at > now - STALE_ALERT_COOLDOWN:
            return
        if quiet_since is None:
            since = "жодного разу за весь час спостережень"
        else:
            hours = int((now - quiet_since).total_seconds() // 3600)
            since = f"востаннє {_format_berlin_time(quiet_since)} ({hours} год тому)"
        try:
            bot.send_message(
                ADMIN_ID,
                "🟡 <b>ДП Документ: давно не було вільних термінів</b>\n\n"
                f"Перевірка проходить успішно, але вільних місць не бачили {html.escape(since)}.\n\n"
                "Можливо, місць справді немає — але так само виглядає й перевірка, "
                "що мовчки почала читати не ту сторінку. Варто відкрити сайт руками "
                "й звірити з тим, що показує бот.\n\n"
                f"Сайт: {SERVICE_URL}",
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception:
            logger.exception("Could not notify admin about a stale e-queue")
            return
        row.last_stale_alert_at = now
        session.commit()
    finally:
        session.close()


def _notify_available(bot, subscribers, result: Dict[str, object]) -> None:
    for user_id, last_status, _last_notified_at in subscribers:
        if last_status == "available":
            continue
        lang = i18n.get_lang(user_id)
        text = i18n.t(
            "equeue.notify.available", lang,
            title=html.escape(SERVICE_TITLE),
            reason=html.escape(str(result.get('reason') or i18n.t("equeue.notify.default_reason", lang))),
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(i18n.t("equeue.btn.open_queue", lang), url=SERVICE_URL)],
            [InlineKeyboardButton(i18n.t("equeue.btn.unsubscribe_short", lang), callback_data="equeue:unsubscribe")],
        ])
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
    _record_service_status(status, str(result["reason"]))
    # A failed/blocked check is worth alerting the admin about regardless of
    # whether anyone is currently subscribed - it's an operational problem
    # with the checker itself, not something that only matters to subscribers.
    if not result["ok"]:
        _notify_admin_error(bot, result)
        _update_status_for_active(status, notified=False)
        return {"ok": True, "subscribers": len(subscribers), "status": status}
    # Знахідка - факт про сайт, а не про підписки: пишеться навіть коли
    # підписників нема, інакше історія в меню мала б дірки саме за ті періоди,
    # коли ніхто не був підписаний.
    if result["available"]:
        _record_available_sighting(str(result["reason"]))
    else:
        _notify_admin_stale(bot)
    if not subscribers:
        _update_status_for_active(status, notified=False)
        return {"ok": True, "subscribers": 0, "status": status}
    if result["available"]:
        _notify_available(bot, subscribers, result)
        _update_status_for_active(status, notified=True)
        return {"ok": True, "subscribers": len(subscribers), "status": status, "notified": True}
    _update_status_for_active(status, notified=False)
    return {"ok": True, "subscribers": len(subscribers), "status": status, "notified": False}


def check_job(context: CallbackContext) -> None:
    if BROWSER_ONLY:
        return
    result = check_equeue_availability()
    status = str(result.get("status") or "unknown")
    if not result.get("ok"):
        logger.warning("E-queue check failed: %s", result)
        _notify_admin_error(context.bot, result)
        _update_status_for_active(status, notified=False)
        return

    subscribers = _active_subscribers()
    if not subscribers:
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
    lang = i18n.get_lang(user.id)
    if not is_allowed(user.id):
        query.answer(i18n.t("equeue.no_access", lang), show_alert=True)
        return
    if query.data == "equeue:menu":
        query.answer()
        show_menu(update, context, edit=True)
    elif query.data == "equeue:subscribe":
        _upsert_subscription(user)
        query.answer(i18n.t("equeue.toast.subscribed", lang))
        show_menu(update, context, edit=True, prefix=i18n.t("equeue.prefix.subscribed", lang))
    elif query.data == "equeue:unsubscribe":
        _deactivate_subscription(user.id)
        query.answer(i18n.t("equeue.toast.unsubscribed", lang))
        show_menu(update, context, edit=True, prefix=i18n.t("equeue.prefix.unsubscribed", lang))
    elif query.data == "equeue:check":
        if BROWSER_ONLY:
            query.answer(i18n.t("equeue.toast.showing_latest", lang))
            show_menu(update, context, edit=True, prefix=_latest_status_text(user.id, lang))
            return
        query.answer(i18n.t("equeue.toast.checking", lang))
        result = check_equeue_availability()
        if result.get("available"):
            prefix = i18n.t("equeue.prefix.available_now", lang)
        elif result.get("ok"):
            prefix = i18n.t("equeue.prefix.none_now", lang)
        else:
            prefix = i18n.t("equeue.prefix.check_failed", lang, reason=result.get('reason'))
        show_menu(update, context, edit=True, prefix=prefix)


command_handler = CommandHandler("dps_document", show_menu, Filters.chat_type.private)
callback_handler = CallbackQueryHandler(handle_callback, pattern=r"^equeue:")
