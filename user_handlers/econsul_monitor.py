"""Підписки на вільні терміни посольства України в Берліні (e-Consul).

Розклад читає Chrome-розширення під акаунтом адміна (e-Consul віддає його лише
після входу через Дію/BankID), рахує вільні часи й шле по днях у той самий
приймач, що й ДП Документ. Люди тут лише підписуються й отримують
сповіщення - записуються вони самі під своїм входом, бот їхніх даних не бачить.
"""

import html
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Dict, Iterable, List, Optional
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import CallbackContext, CallbackQueryHandler, CommandHandler, Filters

import i18n
from database import (
    DBSession,
    EconsulDay,
    EconsulService,
    EconsulState,
    EqueueAvailableSighting,
    EqueueSubscription,
)


logger = logging.getLogger(__name__)

SOURCE = "econsul_berlin"
SUB_PREFIX = SOURCE + ":"
SUB_ALL = SUB_PREFIX + "*"
BOOKING_URL = "https://e-consul.gov.ua/"
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
BERLIN_TZ = ZoneInfo("Europe/Berlin")
# Поки розрахунок не звірено з сайтом, меню бачать тільки адмін і перелічені ID.
PUBLIC = os.getenv("ECONSUL_PUBLIC", "0") == "1"
ADMIN_ALERT_COOLDOWN = timedelta(hours=3)
# За скільки до кінця входу e-Consul (JWT на добу) нагадати адміну перелогінитись.
TOKEN_REMINDER_BEFORE = timedelta(hours=1)
# Перевірка йде раз на 10-15 хвилин; без вдалої відповіді довше за це меню
# чесно каже, що дані застарілі.
STALE_AFTER = timedelta(minutes=45)
SIGHTINGS_SHOWN = 3
LIFETIME_WINDOW = timedelta(days=14)
LIFETIME_MIN_EPISODES = 3
DAYS_PER_SERVICE_IN_NOTIFY = 5
BROKEN_STATUSES = {"auth_required", "error"}


def utc_now() -> datetime:
    return datetime.utcnow()


def _berlin(value: datetime, fmt: str = "%d.%m.%Y %H:%M") -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(BERLIN_TZ).strftime(fmt)


def _date_dot(value: str) -> str:
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%d.%m.%Y")
    except ValueError:
        return value


def _allowed_user_ids() -> set:
    raw = os.getenv("ECONSUL_ALLOWED_USER_IDS", "").strip()
    ids = {ADMIN_ID} if ADMIN_ID else set()
    for part in re.split(r"[,;\s]+", raw):
        if part and part.lstrip("-").isdigit():
            ids.add(int(part))
    return ids


def is_allowed(user_id: Optional[int]) -> bool:
    if not user_id:
        return False
    return PUBLIC or user_id in _allowed_user_ids()


def private_home_rows(user_id: Optional[int]) -> Iterable[list]:
    if not is_allowed(user_id):
        return []
    return [[InlineKeyboardButton(i18n.t("econsul.btn.home", i18n.get_lang(user_id)), callback_data="econsul:menu")]]


def _parse_iso(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


# --- стан і підписки ---

def _get_state(session) -> EconsulState:
    row = session.query(EconsulState).filter(EconsulState.source == SOURCE).first()
    if row is None:
        row = EconsulState(source=SOURCE, baseline_done=False)
        session.add(row)
    return row


def _user_subscriptions(session, user_id: int) -> Dict[str, EqueueSubscription]:
    rows = (
        session.query(EqueueSubscription)
        .filter(EqueueSubscription.user_id == user_id, EqueueSubscription.service.like(SUB_PREFIX + "%"))
        .all()
    )
    return {row.service: row for row in rows}


def _active_codes(rows: Dict[str, EqueueSubscription]) -> set:
    """`{'*'}` для «усі послуги», інакше коди вибраних послуг."""
    return {service[len(SUB_PREFIX):] for service, row in rows.items() if row.active}


def _set_subscription(session, user, service: str, active: bool) -> None:
    now = utc_now()
    row = (
        session.query(EqueueSubscription)
        .filter(EqueueSubscription.user_id == user.id, EqueueSubscription.service == service)
        .first()
    )
    if row is None:
        if not active:
            return
        row = EqueueSubscription(
            user_id=user.id,
            service=service,
            created_at=now,
        )
        session.add(row)
    row.username = user.username or ""
    row.display_name = user.full_name or ""
    row.active = active
    row.updated_at = now


def _served_services(session) -> List[EconsulService]:
    return (
        session.query(EconsulService)
        .filter(EconsulService.served == 1)
        .order_by(EconsulService.name)
        .all()
    )


# --- приймання результату з браузера ---

def _day_line(day: Dict[str, object], lang: str) -> str:
    return i18n.t(
        "econsul.notify.day", lang,
        date=_date_dot(str(day["date"])), first=html.escape(str(day.get("first") or "")), count=day["count"],
    )


def _update_services(session, services: List[dict], now: datetime) -> None:
    seen = set()
    for item in services:
        code = str(item.get("code") or "").strip()
        if not code:
            continue
        seen.add(code)
        days = sorted(
            (day for day in item.get("days") or [] if int(day.get("count") or 0) > 0 and day.get("date")),
            key=lambda day: str(day["date"]),
        )
        row = session.query(EconsulService).filter(EconsulService.code == code).first()
        if row is None:
            row = EconsulService(code=code)
            session.add(row)
        row.name = str(item.get("name") or code)[:300]
        row.minutes = int(item.get("minutes") or 0) or None
        row.served = bool(item.get("served", True))
        row.free_count = sum(int(day["count"]) for day in days)
        row.nearest = f"{days[0]['date']} {days[0].get('first') or ''}".strip() if days else None
        row.updated_at = now
    if seen:
        session.query(EconsulService).filter(~EconsulService.code.in_(seen)).delete(synchronize_session=False)


def _track_days(session, services: List[dict], now: datetime, baseline: bool) -> Dict[str, List[dict]]:
    """Оновлює епізоди доступності; повертає нові дні по кодах послуг."""
    fresh: Dict[str, List[dict]] = {}
    current = {}
    for item in services:
        code = str(item.get("code") or "").strip()
        if not code:
            continue
        for day in item.get("days") or []:
            count = int(day.get("count") or 0)
            if count > 0 and day.get("date"):
                current[(code, str(day["date"]))] = {"date": str(day["date"]), "count": count, "first": day.get("first")}

    open_rows = session.query(EconsulDay).filter(EconsulDay.gone_at.is_(None)).all()
    by_key = {(row.service_code, row.date): row for row in open_rows}
    for key, row in by_key.items():
        if key not in current:
            row.gone_at = now
    for (code, date), day in sorted(current.items()):
        row = by_key.get((code, date))
        if row is None:
            row = EconsulDay(service_code=code, date=date, first_seen_at=now, notified_count=0)
            session.add(row)
        row.count = day["count"]
        row.first_time = str(day.get("first") or "")
        if baseline:
            row.notified_count = max(row.notified_count or 0, row.count)
            continue
        if row.count > (row.notified_count or 0):
            fresh.setdefault(code, []).append(day)
            row.notified_count = row.count
    return fresh


def _record_sighting(fresh: Dict[str, List[dict]], names: Dict[str, str]) -> None:
    summary = "; ".join(
        f"{names.get(code, code)}: {', '.join(_date_dot(day['date']) for day in days[:3])}"
        for code, days in fresh.items()
    )
    session = DBSession()
    try:
        session.add(EqueueAvailableSighting(service=SOURCE, found_at=utc_now(), reason=summary[:500]))
        session.commit()
    finally:
        session.close()


def _subscribers_for(fresh: Dict[str, List[dict]]) -> Dict[int, set]:
    session = DBSession()
    try:
        rows = (
            session.query(EqueueSubscription)
            .filter(EqueueSubscription.service.like(SUB_PREFIX + "%"), EqueueSubscription.active == 1)
            .all()
        )
        wanted: Dict[int, set] = {}
        for row in rows:
            code = row.service[len(SUB_PREFIX):]
            codes = set(fresh) if code == "*" else ({code} & set(fresh))
            if codes:
                wanted.setdefault(row.user_id, set()).update(codes)
        return wanted
    finally:
        session.close()


def _notify_text(fresh: Dict[str, List[dict]], codes: set, names: Dict[str, str], institution: str, lang: str) -> str:
    parts = [i18n.t("econsul.notify.title", lang, institution=html.escape(institution))]
    for code in sorted(codes, key=lambda item: names.get(item, item)):
        days = sorted(fresh[code], key=lambda day: day["date"])
        lines = [f"\n<b>{html.escape(names.get(code, code))}</b>"]
        lines.extend(_day_line(day, lang) for day in days[:DAYS_PER_SERVICE_IN_NOTIFY])
        if len(days) > DAYS_PER_SERVICE_IN_NOTIFY:
            lines.append(i18n.t("econsul.notify.more", lang, count=len(days) - DAYS_PER_SERVICE_IN_NOTIFY))
        parts.append("\n".join(lines))
    parts.append("\n" + i18n.t("econsul.notify.footer", lang))
    return "\n".join(parts)


def _notify_subscribers(bot, fresh: Dict[str, List[dict]], names: Dict[str, str], institution: str) -> int:
    sent = 0
    for user_id, codes in _subscribers_for(fresh).items():
        lang = i18n.get_lang(user_id)
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(i18n.t("econsul.btn.book", lang), url=BOOKING_URL)],
            [InlineKeyboardButton(i18n.t("econsul.btn.settings", lang), callback_data="econsul:menu")],
        ])
        try:
            bot.send_message(
                user_id,
                _notify_text(fresh, codes, names, institution, lang),
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=keyboard,
            )
            sent += 1
        except Exception:
            logger.exception("Could not notify e-Consul subscriber %s", user_id)
    return sent


def _send_admin(bot, key: str, **kwargs) -> bool:
    if not ADMIN_ID:
        return False
    try:
        bot.send_message(
            ADMIN_ID,
            i18n.t(key, i18n.get_lang(ADMIN_ID), **kwargs),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return True
    except Exception:
        logger.exception("Could not send e-Consul admin alert %s", key)
        return False


def handle_browser_result(bot, payload: Dict[str, object]) -> Dict[str, object]:
    if payload.get("source") != SOURCE:
        return {"ok": False, "error": "unsupported source"}
    status = str(payload.get("status") or "unknown")
    reason = str(payload.get("reason") or "")
    now = utc_now()
    session = DBSession()
    try:
        state = _get_state(session)
        previous = state.last_status
        state.last_checked_at = now
        state.last_status = status
        state.last_reason = reason[:500]
        token_expires_at = _parse_iso(payload.get("token_expires_at"))
        if token_expires_at is not None:
            state.token_expires_at = token_expires_at

        if status != "ok":
            alert_due = state.last_admin_alert_at is None or state.last_admin_alert_at <= now - ADMIN_ALERT_COOLDOWN
            key = "econsul.admin.auth_required" if status == "auth_required" else "econsul.admin.error"
            if alert_due and _send_admin(bot, key, reason=html.escape(reason or "—"), url=BOOKING_URL):
                state.last_admin_alert_at = now
            session.commit()
            return {"ok": True, "status": status}

        if previous in BROKEN_STATUSES and state.last_admin_alert_at is not None:
            _send_admin(bot, "econsul.admin.recovered")
        state.last_admin_alert_at = None
        state.last_ok_at = now
        expires = state.token_expires_at
        if (expires is not None and expires - now <= TOKEN_REMINDER_BEFORE
                and state.token_reminded_for != expires
                and _send_admin(bot, "econsul.admin.token_expiring", until=_berlin(expires), url=BOOKING_URL)):
            # Вхід живе добу; без нагадування перевірка просто зупинилась би.
            state.token_reminded_for = expires
        institution = payload.get("institution") or {}
        if isinstance(institution, dict) and institution.get("name"):
            state.institution_name = str(institution["name"])[:300]
        services = [item for item in payload.get("services") or [] if isinstance(item, dict)]
        baseline = not state.baseline_done
        _update_services(session, services, now)
        fresh = _track_days(session, services, now, baseline)
        state.baseline_done = True
        institution_name = state.institution_name or i18n.t("econsul.default_institution", "uk")
        session.commit()
    finally:
        session.close()

    if not fresh:
        return {"ok": True, "status": status, "baseline": baseline, "new_days": 0}
    names = {str(item.get("code")): str(item.get("name") or item.get("code")) for item in services}
    _record_sighting(fresh, names)
    sent = _notify_subscribers(bot, fresh, names, institution_name)
    return {
        "ok": True,
        "status": status,
        "new_days": sum(len(days) for days in fresh.values()),
        "notified": sent,
    }


# --- меню ---

def _duration_text(delta: timedelta, lang: str) -> str:
    minutes = max(int(delta.total_seconds() // 60), 1)
    if minutes < 60:
        return i18n.t("econsul.duration.minutes", lang, minutes=minutes)
    return i18n.t("econsul.duration.hours", lang, hours=minutes // 60, minutes=minutes % 60)


def typical_lifetime(session, now: Optional[datetime] = None) -> Optional[timedelta]:
    """Медіана того, скільки дата лишалась вільною, поки її не розібрали.

    Дні, що зникли лише тому, що настали (перевірка рахує від завтра), сюди не
    входять - їх ніхто не розбирав.
    """
    now = now or utc_now()
    rows = (
        session.query(EconsulDay)
        .filter(EconsulDay.gone_at.isnot(None), EconsulDay.gone_at >= now - LIFETIME_WINDOW)
        .all()
    )
    durations = []
    for row in rows:
        gone_day = _berlin(row.gone_at, "%Y-%m-%d")
        if row.date <= (datetime.strptime(gone_day, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d"):
            continue
        durations.append((row.gone_at - row.first_seen_at).total_seconds())
    if len(durations) < LIFETIME_MIN_EPISODES:
        return None
    return timedelta(seconds=median(durations))


def _status_text(state: Optional[EconsulState], lang: str) -> str:
    if state is None or state.last_checked_at is None:
        return i18n.t("econsul.status.never_checked", lang)
    if state.last_status in BROKEN_STATUSES:
        since = _berlin(state.last_ok_at) if state.last_ok_at else "—"
        return i18n.t("econsul.status.broken", lang, since=since)
    if state.last_ok_at is None or utc_now() - state.last_ok_at > STALE_AFTER:
        return i18n.t("econsul.status.stale", lang, checked=_berlin(state.last_ok_at) if state.last_ok_at else "—")
    return i18n.t("econsul.status.ok", lang, checked=_berlin(state.last_ok_at))


def _free_text(services: List[EconsulService], lang: str) -> str:
    free = [service for service in services if service.free_count]
    if not services:
        return ""
    if not free:
        return i18n.t("econsul.free.nothing", lang)
    lines = [i18n.t("econsul.free.title", lang)]
    for service in free:
        date, _, time = (service.nearest or "").partition(" ")
        lines.append(i18n.t(
            "econsul.free.line", lang,
            name=html.escape(service.name), nearest=f"{_date_dot(date)} {time}".strip(), count=service.free_count,
        ))
    return "\n".join(lines)


def _sightings_text(session, lang: str) -> str:
    rows = (
        session.query(EqueueAvailableSighting)
        .filter(EqueueAvailableSighting.service == SOURCE)
        .order_by(EqueueAvailableSighting.found_at.desc())
        .limit(SIGHTINGS_SHOWN)
        .all()
    )
    if not rows:
        return i18n.t("econsul.sightings.none", lang)
    lines = [i18n.t("econsul.sightings.title", lang)]
    lines.extend(f"• {_berlin(row.found_at)}" for row in rows)
    return "\n".join(lines)


def _short(name: str, limit: int = 48) -> str:
    return name if len(name) <= limit else name[: limit - 1] + "…"


def _nearest_text(service: Optional[EconsulService], lang: str) -> str:
    if service is None or not service.free_count or not service.nearest:
        return i18n.t("econsul.subs.nothing_free", lang)
    date, _, time = service.nearest.partition(" ")
    return i18n.t("econsul.subs.nearest", lang, nearest=f"{_date_dot(date)} {time}".strip())


def _subscriptions_block(session, codes: set, lang: str) -> str:
    if not codes:
        return i18n.t("econsul.subs.empty", lang)
    lines = [i18n.t("econsul.subs.title", lang)]
    if "*" in codes:
        lines.append(i18n.t("econsul.subs.all_line", lang))
        return "\n".join(lines)
    services = {row.code: row for row in session.query(EconsulService).all()}
    for code in sorted(codes, key=lambda item: (services[item].name if item in services else item)):
        service = services.get(code)
        name = html.escape(service.name if service else code)
        lines.append(f"• <b>{name}</b> — {_nearest_text(service, lang)}")
    return "\n".join(lines)


def _home_keyboard(codes: set, lang: str) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(i18n.t("econsul.btn.add", lang), callback_data="econsul:add")]]
    if codes:
        rows.append([InlineKeyboardButton(i18n.t("econsul.btn.manage", lang), callback_data="econsul:manage")])
    rows.append([InlineKeyboardButton(i18n.t("econsul.btn.free", lang), callback_data="econsul:free")])
    rows.append([InlineKeyboardButton(i18n.t("econsul.btn.open_site", lang), url=BOOKING_URL)])
    rows.append([InlineKeyboardButton(i18n.t("anon.btn.back_home", lang), callback_data="anon:home")])
    return InlineKeyboardMarkup(rows)


def render_menu(user_id: int, lang: str, prefix: str = ""):
    """Головний екран: коротко статус і власні підписки, без переліку всіх послуг."""
    session = DBSession()
    try:
        state = session.query(EconsulState).filter(EconsulState.source == SOURCE).first()
        codes = _active_codes(_user_subscriptions(session, user_id))
        institution = (state.institution_name if state else None) or i18n.t("econsul.default_institution", lang)
        blocks = [
            i18n.t("econsul.menu.text", lang, institution=html.escape(institution)),
            _status_text(state, lang),
            _subscriptions_block(session, codes, lang),
        ]
        if session.query(EqueueAvailableSighting).filter(EqueueAvailableSighting.service == SOURCE).count():
            blocks.append(_sightings_text(session, lang))
        lifetime = typical_lifetime(session)
        if lifetime is not None:
            blocks.append(i18n.t("econsul.lifetime", lang, duration=_duration_text(lifetime, lang)))
        text = "\n\n".join(block for block in blocks if block)
        if prefix:
            text = prefix + "\n\n" + text
        return text, _home_keyboard(codes, lang)
    finally:
        session.close()


def render_picker(user_id: int, lang: str, picked: set):
    """Вибір послуг для нової підписки: галочки, внизу одна кнопка «Підписатися»."""
    session = DBSession()
    try:
        codes = _active_codes(_user_subscriptions(session, user_id))
        back = [InlineKeyboardButton(i18n.t("econsul.btn.cancel", lang), callback_data="econsul:menu")]
        if "*" in codes:
            return i18n.t("econsul.pick.already_all", lang), InlineKeyboardMarkup([back])
        services = [service for service in _served_services(session) if service.code not in codes]
        if not services:
            return i18n.t("econsul.pick.nothing_left", lang), InlineKeyboardMarkup([back])
        rows = [[InlineKeyboardButton(i18n.t("econsul.btn.pick_all", lang), callback_data="econsul:add_all")]]
        for service in services:
            callback = f"econsul:p:{service.code}"
            if len(callback.encode("utf-8")) > 64:
                continue
            mark = "✅" if service.code in picked else "▫️"
            rows.append([InlineKeyboardButton(f"{mark} {_short(service.name)}", callback_data=callback)])
        chosen = [code for code in picked if any(service.code == code for service in services)]
        if chosen:
            rows.append([InlineKeyboardButton(
                i18n.t("econsul.btn.subscribe", lang, count=len(chosen)), callback_data="econsul:save",
            )])
        rows.append(back)
        return i18n.t("econsul.pick.text", lang), InlineKeyboardMarkup(rows)
    finally:
        session.close()


def render_manage(user_id: int, lang: str, prefix: str = ""):
    """Власні підписки з кнопкою ❌ біля кожної."""
    session = DBSession()
    try:
        codes = _active_codes(_user_subscriptions(session, user_id))
        names = {row.code: row.name for row in session.query(EconsulService).all()}
        rows = []
        for code in sorted(codes, key=lambda item: names.get(item, item)):
            label = i18n.t("econsul.subs.all", lang) if code == "*" else names.get(code, code)
            rows.append([InlineKeyboardButton(f"❌ {_short(label)}", callback_data=f"econsul:u:{code}")])
        if len(codes) > 1:
            rows.append([InlineKeyboardButton(i18n.t("econsul.btn.none", lang), callback_data="econsul:none")])
        rows.append([InlineKeyboardButton(i18n.t("econsul.btn.back", lang), callback_data="econsul:menu")])
        text = i18n.t("econsul.manage.text", lang) if codes else i18n.t("econsul.subs.empty", lang)
        if prefix:
            text = prefix + "\n\n" + text
        return text, InlineKeyboardMarkup(rows)
    finally:
        session.close()


def render_free(lang: str):
    """Повний перелік того, що вільно зараз - лише на окремий запит."""
    session = DBSession()
    try:
        text = _free_text(_served_services(session), lang) or i18n.t("econsul.status.never_checked", lang)
    finally:
        session.close()
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(i18n.t("econsul.btn.add", lang), callback_data="econsul:add")],
        [InlineKeyboardButton(i18n.t("econsul.btn.back", lang), callback_data="econsul:menu")],
    ])
    return text, keyboard


def _subscribe(session, user, codes: set) -> None:
    if "*" in codes:
        for service in _user_subscriptions(session, user.id):
            _set_subscription(session, user, service, False)
        _set_subscription(session, user, SUB_ALL, True)
        return
    for code in codes:
        _set_subscription(session, user, SUB_PREFIX + code, True)


def _unsubscribe(session, user, code: Optional[str]) -> None:
    """`None` - відписатися від усього."""
    for service in _user_subscriptions(session, user.id):
        if code is None or service == SUB_PREFIX + code:
            _set_subscription(session, user, service, False)


def _service_names(codes: Iterable[str], lang: str) -> str:
    session = DBSession()
    try:
        names = {row.code: row.name for row in session.query(EconsulService).all()}
    finally:
        session.close()
    return ", ".join(
        html.escape(i18n.t("econsul.subs.all", lang) if code == "*" else names.get(code, code))
        for code in sorted(codes, key=lambda item: names.get(item, item))
    )


def _edit(query, text: str, keyboard: InlineKeyboardMarkup) -> None:
    try:
        query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard, disable_web_page_preview=True)
    except BadRequest as exc:
        if "Message is not modified" not in str(exc):
            raise


def show_menu(update: Update, context: CallbackContext, edit: bool = False, prefix: str = "") -> None:
    user = update.effective_user
    lang = i18n.get_lang(user.id) if user else "uk"
    if not user or not is_allowed(user.id):
        text = i18n.t("econsul.not_allowed", lang)
        if edit and update.callback_query:
            update.callback_query.edit_message_text(text)
        else:
            update.effective_message.reply_text(text)
        return
    text, keyboard = render_menu(user.id, lang, prefix)
    if edit and update.callback_query:
        _edit(update.callback_query, text, keyboard)
    else:
        update.effective_message.reply_text(
            text, parse_mode="HTML", reply_markup=keyboard, disable_web_page_preview=True,
        )


def handle_callback(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    if not query or not query.data:
        return
    user = query.from_user
    lang = i18n.get_lang(user.id)
    if not is_allowed(user.id):
        query.answer(i18n.t("econsul.not_allowed", lang), show_alert=True)
        return
    data = query.data
    picked = context.user_data.setdefault("econsul_pick", set())

    if data == "econsul:menu":
        picked.clear()
        query.answer()
        show_menu(update, context, edit=True)
    elif data == "econsul:add":
        picked.clear()
        query.answer()
        _edit(query, *render_picker(user.id, lang, picked))
    elif data.startswith("econsul:p:"):
        code = data[len("econsul:p:"):]
        picked.symmetric_difference_update({code})
        query.answer()
        _edit(query, *render_picker(user.id, lang, picked))
    elif data in ("econsul:save", "econsul:add_all"):
        codes = {"*"} if data == "econsul:add_all" else set(picked)
        picked.clear()
        if not codes:
            query.answer()
            _edit(query, *render_picker(user.id, lang, picked))
            return
        session = DBSession()
        try:
            _subscribe(session, user, codes)
            session.commit()
        finally:
            session.close()
        query.answer(i18n.t("econsul.toast.saved", lang))
        prefix = i18n.t("econsul.prefix.subscribed", lang, names=_service_names(codes, lang))
        show_menu(update, context, edit=True, prefix=prefix)
    elif data == "econsul:manage":
        query.answer()
        _edit(query, *render_manage(user.id, lang))
    elif data.startswith("econsul:u:") or data == "econsul:none":
        code = data[len("econsul:u:"):] if data.startswith("econsul:u:") else None
        session = DBSession()
        try:
            _unsubscribe(session, user, code)
            session.commit()
            left = _active_codes(_user_subscriptions(session, user.id))
        finally:
            session.close()
        query.answer(i18n.t("econsul.toast.unsubscribed", lang))
        if left:
            _edit(query, *render_manage(user.id, lang))
        else:
            show_menu(update, context, edit=True, prefix=i18n.t("econsul.prefix.unsubscribed_all", lang))
    elif data == "econsul:free":
        query.answer()
        _edit(query, *render_free(lang))
    else:
        query.answer()


command_handler = CommandHandler("embassy", show_menu, Filters.chat_type.private)
callback_handler = CallbackQueryHandler(handle_callback, pattern=r"^econsul:")
