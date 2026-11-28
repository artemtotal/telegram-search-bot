"""«✅ Подався» and «📋 Мої заявки» - a small tracker of where the person applied
(24.11.2026).

Under every flat: «✍️ Лист · ✅ Подався». The second one is a deep link
t.me/<bot>?start=ap<ref> (same numeric reference as the letter, prefix `ap`;
buttons don't show on albums). Tapping it records the application with a copy
of the flat's title/address/price/link; «📋 Мої заявки» lists them with a
status, a viewing date and reminders (19:00 Berlin the evening before, and 2
hours before - subscribers only). Free people can mark and see their last
FREE_LIMIT applications.

The same table gives the honest «подались: N» in housing_links.

Off switch: HOUSING_APPLY_TRACKER=0 (the footer goes back to the old letter
link, the menu button and the reminders stop).
"""

import html
import logging
import os
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import pytz
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackContext, CallbackQueryHandler

import i18n
from database import DBSession, HousingApplication
from user_jobs import housing_dateparse, housing_letter_store as store, housing_tier

logger = logging.getLogger(__name__)

BERLIN = pytz.timezone("Europe/Berlin")
START_PREFIX = "ap"
STATE_KEY = "housing_apply_date"
FREE_LIMIT = 5
PAGE_SIZE = 8
EVE_HOUR = 19
STATUSES = ("applied", "replied", "viewing", "rejected", "found")
STATUS_ICON = {"applied": "📨", "replied": "💬", "viewing": "📅", "rejected": "✖", "found": "🏠"}
_ACTION_STATUS = {"re": "replied", "rj": "rejected"}
# Модуль потоку A: mark_found(user_id) - пауза пошуків, вітання, подяка.
FOUND_MODULES = ("user_jobs.housing_found", "user_handlers.housing_found")


def utc_now() -> datetime:
    return datetime.utcnow()


def enabled() -> bool:
    return str(os.getenv("HOUSING_APPLY_TRACKER", "1") or "").strip().lower() not in ("", "0", "false", "no", "off")


def _lang(user_id: int) -> str:
    return i18n.get_lang(int(user_id))


def _t(key: str, user_id: int, **kwargs) -> str:
    return i18n.t(key, _lang(user_id), **kwargs)


# ---------- footer ----------

def footer_line(user_id: int, source: str, listing_key: str, premium: bool) -> str:
    """«✍️ Лист · ✅ Подався» under a flat; "" when the bot's name or the
    reference isn't available (no broken link)."""
    username = getattr(store, "_bot_username", None)
    if not username or not listing_key:
        return ""
    ref_id = store.ref_for(source, listing_key)
    if ref_id is None:
        return ""
    lang = _lang(user_id)
    if premium:
        letter_key = "housing.track.footer_letter_paid"
    else:       # одно письмо бесплатно, пока проба не потрачена
        letter_key = "housing.track.footer_letter_trial" if store.trial_unused(user_id) else "housing.track.footer_letter_free"
    letter = i18n.t(letter_key, lang)
    apply_label = i18n.t("housing.track.footer_apply", lang)
    return (
        f'\n\n<a href="{store.letter_href(username, ref_id)}">{letter}</a>'
        f' · <a href="https://t.me/{username}?start={START_PREFIX}{ref_id}">{apply_label}</a>'
    )


# ---------- storage ----------

def listing_snapshot(source: str, listing_key: str) -> Optional[Dict[str, str]]:
    from user_handlers import housing_letter

    data = housing_letter._listing(source, listing_key)
    if data is None:
        return None
    price = data.get("price_eur")
    try:
        price_text = f"{float(price):g} €" if price not in (None, "", 0) else ""
    except (TypeError, ValueError):
        price_text = ""
    return {
        "title": str(data.get("title") or "Wohnung")[:200], "address": str(data.get("address") or "")[:200],
        "price_text": price_text, "url": str(data.get("detail_url") or "")[:2000],
    }


def mark_applied(user_id: int, source: str, listing_key: str) -> Optional[Dict[str, object]]:
    """Records (or finds) the application. {'id', 'created', 'title'} or None."""
    snap = listing_snapshot(source, listing_key)
    if snap is None:
        return None
    session = DBSession()
    try:
        row = session.query(HousingApplication).filter(
            HousingApplication.user_id == int(user_id), HousingApplication.source == source,
            HousingApplication.listing_key == listing_key,
        ).first()
        created = row is None
        if row is None:
            now = utc_now()
            row = HousingApplication(
                user_id=int(user_id), source=source, listing_key=listing_key, status="applied", hidden=False,
                remind_eve_sent=False, remind_2h_sent=False, created_at=now, updated_at=now, **snap,
            )
            session.add(row)
        elif row.hidden:
            row.hidden = False
            row.updated_at = utc_now()
        session.commit()
        return {"id": int(row.id), "created": created, "title": row.title}
    except Exception:
        session.rollback()
        logger.exception("Could not record an application for %s", user_id)
        return None
    finally:
        session.close()


def _as_dict(row) -> Dict[str, object]:
    return {name: getattr(row, name) for name in (
        "id", "user_id", "source", "listing_key", "title", "address", "price_text", "url", "status",
        "viewing_at", "hidden", "created_at", "remind_eve_sent", "remind_2h_sent",
    )}


def get_app(user_id: int, app_id: int) -> Optional[Dict[str, object]]:
    session = DBSession()
    try:
        row = session.query(HousingApplication).filter(
            HousingApplication.id == int(app_id), HousingApplication.user_id == int(user_id),
            HousingApplication.hidden.is_(False),
        ).first()
        return _as_dict(row) if row is not None else None
    finally:
        session.close()


def list_apps(user_id: int, limit: int, offset: int = 0) -> List[Dict[str, object]]:
    session = DBSession()
    try:
        rows = (session.query(HousingApplication)
                .filter(HousingApplication.user_id == int(user_id), HousingApplication.hidden.is_(False))
                .order_by(HousingApplication.created_at.desc(), HousingApplication.id.desc())
                .offset(int(offset)).limit(int(limit)).all())
        return [_as_dict(row) for row in rows]
    finally:
        session.close()


def count_apps(user_id: int) -> int:
    session = DBSession()
    try:
        return session.query(HousingApplication).filter(
            HousingApplication.user_id == int(user_id), HousingApplication.hidden.is_(False),
        ).count()
    finally:
        session.close()


def set_status(user_id: int, app_id: int, status: str) -> bool:
    if status not in STATUSES:
        return False
    session = DBSession()
    try:
        row = session.query(HousingApplication).filter(
            HousingApplication.id == int(app_id), HousingApplication.user_id == int(user_id),
        ).first()
        if row is None:
            return False
        row.status = status
        row.updated_at = utc_now()
        session.commit()
        return True
    except Exception:
        session.rollback()
        logger.exception("Could not set the application status")
        return False
    finally:
        session.close()


def set_viewing(user_id: int, app_id: int, moment: datetime, now: Optional[datetime] = None) -> bool:
    """Saves the viewing time. A reminder whose moment has already passed
    when the date is set is marked as sent: «yesterday evening» makes no sense."""
    now = now or utc_now()
    session = DBSession()
    try:
        row = session.query(HousingApplication).filter(
            HousingApplication.id == int(app_id), HousingApplication.user_id == int(user_id),
        ).first()
        if row is None:
            return False
        row.viewing_at = moment
        row.status = "viewing"
        row.remind_eve_sent = now >= eve_moment(moment)
        row.remind_2h_sent = now >= moment - timedelta(hours=2)
        row.updated_at = now
        session.commit()
        return True
    except Exception:
        session.rollback()
        logger.exception("Could not save the viewing time")
        return False
    finally:
        session.close()


def hide_app(user_id: int, app_id: int) -> bool:
    session = DBSession()
    try:
        row = session.query(HousingApplication).filter(
            HousingApplication.id == int(app_id), HousingApplication.user_id == int(user_id),
        ).first()
        if row is None:
            return False
        row.hidden = True  # рядок лишається: він ще входить у «подались»
        row.updated_at = utc_now()
        session.commit()
        return True
    except Exception:
        session.rollback()
        return False
    finally:
        session.close()


def eve_moment(viewing_utc: datetime) -> datetime:
    """19:00 Berlin on the day before the viewing, as naive UTC."""
    local = pytz.utc.localize(viewing_utc).astimezone(BERLIN)
    day = local.date() - timedelta(days=1)
    return BERLIN.localize(datetime(day.year, day.month, day.day, EVE_HOUR, 0)).astimezone(pytz.utc).replace(tzinfo=None)


def _when(moment: datetime) -> str:
    return pytz.utc.localize(moment).astimezone(BERLIN).strftime("%d.%m. %H:%M")


# ---------- screens ----------

def _reply(update: Update, context: CallbackContext, text: str, markup=None, edit: bool = False) -> None:
    query = update.callback_query
    if edit and query is not None:
        try:
            query.edit_message_text(text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
            return
        except Exception:
            logger.debug("edit failed, sending a new message", exc_info=True)
    context.bot.send_message(
        chat_id=int(update.effective_user.id), text=text, parse_mode="HTML", reply_markup=markup,
        disable_web_page_preview=True,
    )


def _status_text(app: Dict[str, object], lang: str) -> str:
    text = i18n.t(f"housing.track.status.{app['status']}", lang)
    if app["status"] == "viewing" and app.get("viewing_at"):
        text += " " + _when(app["viewing_at"])
    return text


def _card_keyboard(app_id: int, lang: str, in_list: bool) -> InlineKeyboardMarkup:
    def b(key: str, data: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(i18n.t(key, lang), callback_data=data)

    rows = [
        [b("housing.track.btn.replied", f"hap:st:{app_id}:re"), b("housing.track.btn.viewing", f"hap:vw:{app_id}")],
        [b("housing.track.btn.rejected", f"hap:st:{app_id}:rj"), b("housing.track.btn.found", f"hap:fd:{app_id}")],
        [b("housing.track.btn.remove", f"hap:rm:{app_id}")],
        [b("housing.track.btn.list", "hap:list:0")],
    ]
    return InlineKeyboardMarkup(rows)


def _card_text(app: Dict[str, object], lang: str) -> str:
    lines = [f"<b>{html.escape(str(app['title'] or ''))}</b>"]
    if app.get("address"):
        lines.append("📍 " + html.escape(str(app["address"])))
    if app.get("price_text"):
        lines.append("💶 " + html.escape(str(app["price_text"])))
    lines.append(i18n.t("housing.track.card_status", lang, status=_status_text(app, lang)))
    if app.get("url"):
        lines.append(f'<a href="{html.escape(str(app["url"]))}">{i18n.t("housing.matches.open_link", lang)}</a>')
    return "\n".join(lines)


def show_card(update: Update, context: CallbackContext, app_id: int, edit: bool = True) -> None:
    user_id = int(update.effective_user.id)
    lang = _lang(user_id)
    app = get_app(user_id, app_id)
    if app is None:
        _reply(update, context, i18n.t("housing.track.gone", lang), _list_only_keyboard(lang), edit=edit)
        return
    _reply(update, context, _card_text(app, lang), _card_keyboard(app_id, lang, True), edit=edit)


def _list_only_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(i18n.t("housing.track.btn.list", lang), callback_data="hap:list:0")],
        [InlineKeyboardButton(i18n.t("housing.btn.back_to_monitor", lang), callback_data="housing:menu")],
    ])


def show_list(update: Update, context: CallbackContext, page: int = 0, edit: bool = True) -> None:
    user_id = int(update.effective_user.id)
    lang = _lang(user_id)
    premium = housing_tier.is_premium(user_id)
    total = count_apps(user_id)
    back = [InlineKeyboardButton(i18n.t("housing.btn.back_to_monitor", lang), callback_data="housing:menu")]
    if total == 0:
        _reply(update, context, i18n.t("housing.track.empty", lang), InlineKeyboardMarkup([back]), edit=edit)
        return
    page = max(0, int(page))
    size = PAGE_SIZE if premium else FREE_LIMIT
    apps = list_apps(user_id, size, offset=page * size if premium else 0)
    if not apps:
        page, apps = 0, list_apps(user_id, size)
    lines = [i18n.t("housing.track.list_header", lang)]
    rows = []
    for number, app in enumerate(apps, start=page * size + 1 if premium else 1):
        status = _status_text(app, lang)
        lines.append(f"\n{number}. {html.escape(str(app['title'] or '')[:60])}\n{STATUS_ICON.get(app['status'], '')} {status}")
        label = f"{number} · {str(app['title'] or '')[:28]}"
        rows.append([InlineKeyboardButton(label, callback_data=f"hap:card:{app['id']}")])
    if premium:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("⬅", callback_data=f"hap:list:{page - 1}"))
        if (page + 1) * size < total:
            nav.append(InlineKeyboardButton("➡", callback_data=f"hap:list:{page + 1}"))
        if nav:
            rows.append(nav)
    else:
        hidden_count = total - len(apps)
        if hidden_count >= 1:
            lines.append("\n" + i18n.t("housing.track.free_more", lang, n=hidden_count))
        lines.append("\n" + i18n.t("housing.track.free_sub", lang))
        rows.append([InlineKeyboardButton(i18n.t("housing.btn.request_access", lang), callback_data="housing:access_request")])
    rows.append(back)
    _reply(update, context, "\n".join(lines), InlineKeyboardMarkup(rows), edit=edit)


def _after_mark_keyboard(app_id: int, lang: str) -> InlineKeyboardMarkup:
    def b(key: str, data: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(i18n.t(key, lang), callback_data=data)

    return InlineKeyboardMarkup([
        [b("housing.track.btn.replied", f"hap:st:{app_id}:re"), b("housing.track.btn.viewing", f"hap:vw:{app_id}")],
        [b("housing.track.btn.rejected", f"hap:st:{app_id}:rj"), b("housing.track.btn.list", "hap:list:0")],
    ])


def _mark_and_answer(update: Update, context: CallbackContext, ref_id: int) -> None:
    user_id = int(update.effective_user.id)
    lang = _lang(user_id)
    ref = store.resolve_ref(ref_id)
    result = mark_applied(user_id, ref["source"], ref["listing_key"]) if ref else None
    if result is None:
        context.bot.send_message(chat_id=user_id, text=i18n.t("housing.track.unknown", lang))
        return
    key = "housing.track.marked" if result["created"] else "housing.track.already"
    context.bot.send_message(
        chat_id=user_id, parse_mode="HTML", reply_markup=_after_mark_keyboard(int(result["id"]), lang),
        text=i18n.t(key, lang, title=html.escape(str(result["title"] or ""))),
    )


def handle_start(update: Update, context: CallbackContext, payload: str) -> None:
    """/start ap<ref>: «✅ Подався» under a flat."""
    user_id = int(update.effective_user.id)
    try:
        ref_id = int(payload[len(START_PREFIX):])
    except ValueError:
        ref_id = 0
    from user_handlers import housing_monitor

    if not enabled() or not housing_monitor.is_allowed(user_id):
        context.bot.send_message(chat_id=user_id, text=_t("housing.track.unknown", user_id))
        return
    _mark_and_answer(update, context, ref_id)


def handle_text(update: Update, context: CallbackContext) -> bool:
    """The viewing date typed by the person; False if none is expected."""
    state = context.user_data.get(STATE_KEY) if context.user_data is not None else None
    if not state or not update.message or not update.message.text or not update.effective_user:
        return False
    user_id = int(update.effective_user.id)
    lang = _lang(user_id)
    app_id = int(state)
    moment = housing_dateparse.parse_viewing(update.message.text)
    cancel = InlineKeyboardMarkup([[InlineKeyboardButton(i18n.t("housing.track.btn.cancel", lang), callback_data=f"hap:card:{app_id}")]])
    if moment is None:
        context.bot.send_message(chat_id=user_id, text=i18n.t("housing.track.date_bad", lang), reply_markup=cancel)
        return True
    context.user_data.pop(STATE_KEY, None)
    if not set_viewing(user_id, app_id, moment):
        context.bot.send_message(chat_id=user_id, text=i18n.t("housing.track.gone", lang))
        return True
    premium = housing_tier.is_premium(user_id)
    text = i18n.t("housing.track.date_saved", lang, when=_when(moment)) + "\n" + i18n.t(
        "housing.track.reminders_on" if premium else "housing.track.reminders_free", lang,
    )
    rows = [[InlineKeyboardButton(i18n.t("housing.track.btn.list", lang), callback_data="hap:list:0")]]
    if not premium:
        rows.insert(0, [InlineKeyboardButton(i18n.t("housing.btn.request_access", lang), callback_data="housing:access_request")])
    context.bot.send_message(chat_id=user_id, text=text, reply_markup=InlineKeyboardMarkup(rows))
    return True


def _call_mark_found(user_id: int, bot=None) -> bool:
    """Поток A: mark_found(user_id) (пауза пошуків) + congrats(bot, user_id)
    (вітання з подякою). True - модуль є й усе зроблено (або людина вже
    позначена «знайшла» - тоді вдруге не вітаємо); False - модуля немає,
    він вимкнений (HOUSING_FOUND_ENABLED=0) чи впав - вітаємо самі."""
    import importlib

    for name in FOUND_MODULES:
        try:
            module = importlib.import_module(name)
        except ImportError:
            continue
        func = getattr(module, "mark_found", None)
        if func is None:
            continue
        try:
            is_on = getattr(module, "enabled", None)
            if is_on is not None and not is_on():
                return False
            already = getattr(module, "is_found", None)
            if already is not None and already(int(user_id)):
                return True
            func(int(user_id))
            congrats = getattr(module, "congrats", None)
            if bot is not None and congrats is not None:
                congrats(bot, int(user_id))
            return True
        except Exception:
            logger.exception("mark_found failed for %s", user_id)
            return False
    return False


def handle_callback(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    user_id = int(update.effective_user.id)
    lang = _lang(user_id)
    parts = str(query.data or "").split(":")
    action = parts[1] if len(parts) > 1 else ""
    arg = parts[2] if len(parts) > 2 else ""

    def num(value) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    if not enabled():
        query.answer()
        return
    if action == "list":
        query.answer()
        context.user_data.pop(STATE_KEY, None)
        show_list(update, context, page=num(arg))
    elif action == "card":
        query.answer()
        context.user_data.pop(STATE_KEY, None)
        show_card(update, context, num(arg))
    elif action == "mark":  # після «✍️ Лист»: «позначити як подану»
        query.answer()
        _mark_and_answer(update, context, num(arg))
    elif action == "st" and len(parts) > 3 and parts[3] in _ACTION_STATUS:
        set_status(user_id, num(arg), _ACTION_STATUS[parts[3]])
        query.answer(i18n.t("housing.track.saved", lang))
        show_card(update, context, num(arg))
    elif action == "vw":
        if get_app(user_id, num(arg)) is None:
            query.answer(i18n.t("housing.track.gone", lang), show_alert=True)
            return
        query.answer()
        from user_jobs import housing_text_wait
        housing_text_wait.claim(context.user_data, STATE_KEY)
        context.user_data[STATE_KEY] = num(arg)
        context.bot.send_message(
            chat_id=user_id, text=i18n.t("housing.track.date_ask", lang),
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                i18n.t("housing.track.btn.cancel", lang), callback_data=f"hap:card:{num(arg)}")]]),
        )
    elif action == "fd":
        if get_app(user_id, num(arg)) is None:
            query.answer(i18n.t("housing.track.gone", lang), show_alert=True)
            return
        set_status(user_id, num(arg), "found")
        query.answer()
        if not _call_mark_found(user_id, context.bot):
            context.bot.send_message(chat_id=user_id, text=i18n.t("housing.track.found_congrats", lang))
        show_card(update, context, num(arg), edit=False)
    elif action == "rm":
        hide_app(user_id, num(arg))
        query.answer(i18n.t("housing.track.removed", lang))
        show_list(update, context)
    else:
        query.answer()


callback_handler = CallbackQueryHandler(handle_callback, pattern=r"^hap:")


# ---------- reminders ----------

def reminders_job(context: CallbackContext) -> None:
    if not enabled():
        return
    send_due_reminders(context.bot)


def send_due_reminders(bot, now: Optional[datetime] = None) -> int:
    """Evening-before and 2-hours-before reminders for subscribers. Returns
    how many were sent."""
    now = now or utc_now()
    # 25.11.2026 (злиття): уночі (22-09 за Берліном) не пишемо. Нагадування «за 2 год»
    # для раннього перегляду прийде о 9:00, якщо перегляд ще попереду; вечірнє о 19:00 - як було.
    berlin_hour = pytz.utc.localize(now).astimezone(BERLIN).hour
    if berlin_hour >= 22 or berlin_hour < 9:
        return 0
    session = DBSession()
    try:
        rows = [_as_dict(r) for r in session.query(HousingApplication).filter(
            HousingApplication.viewing_at.isnot(None), HousingApplication.viewing_at > now,
            HousingApplication.status == "viewing", HousingApplication.hidden.is_(False),
        ).all()]
    except Exception:
        logger.exception("Could not read viewings for reminders")
        return 0
    finally:
        session.close()
    sent = 0
    for app in rows:
        uid = int(app["user_id"])
        if not housing_tier.is_premium(uid):
            continue
        viewing = app["viewing_at"]
        due_2h = now >= viewing - timedelta(hours=2)
        due_eve = now >= eve_moment(viewing)
        kind = None
        if due_2h and not app["remind_2h_sent"]:
            kind = "2h"
        elif due_eve and not due_2h and not app["remind_eve_sent"]:
            kind = "eve"
        if kind is None:
            continue
        lang = _lang(uid)
        lines = [i18n.t("housing.track.remind_2h" if kind == "2h" else "housing.track.remind_eve", lang,
                        time=pytz.utc.localize(viewing).astimezone(BERLIN).strftime("%H:%M")),
                 html.escape(str(app["title"] or ""))]
        if app.get("address"):
            lines.append("📍 " + html.escape(str(app["address"])))
        gone = False
        try:
            bot.send_message(chat_id=uid, text="\n".join(lines), parse_mode="HTML")
            sent += 1
        except Exception as exc:
            gone = housing_tier.recipient_gone(exc)
            if not gone:
                logger.exception("Could not send a viewing reminder to %s", uid)
                continue
        _mark_reminded(int(app["id"]), kind)
    return sent


def _mark_reminded(app_id: int, kind: str) -> None:
    session = DBSession()
    try:
        row = session.query(HousingApplication).get(int(app_id))
        if row is None:
            return
        row.remind_eve_sent = True  # після 2-годинного вечірнього вже не шлемо
        if kind == "2h":
            row.remind_2h_sent = True
        session.commit()
    except Exception:
        session.rollback()
        logger.exception("Could not mark a reminder as sent")
    finally:
        session.close()
