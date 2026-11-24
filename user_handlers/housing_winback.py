"""Возврат ушедших и подарок «🎁 2 дні миттєвих» (25.11.2026).

Раз в 30 минут (ночью 22-09 по Берлину не шлём) задание ищет людей, у которых
подписка (оплаченная или выданная вручную) закончилась 7-30 дней назад, они не
продлили, не заблокировали бота и фильтр активен, - и пишет каждому ОДИН раз
на одно окончание подписки. В сообщении - цифры за 7 дней после конца:
сколько квартир пришло с задержкой и сколько зникли с сайта до доставки (из
журнала «квартиру вже зняли»; чего посчитать нельзя - не пишем). Если за
7 дней квартир 0 - вместо цифр кнопки расширения фильтра. Плюс подарок:
кнопка включает 2 дня мгновенных, не чаще раза в 90 дней на человека; по
окончании - короткое сообщение с тремя пакетами.

Отключить: HOUSING_WINBACK_ENABLED=0. Длина подарка и пауза между подарками:
HOUSING_GIFT_DAYS (2), HOUSING_GIFT_COOLDOWN_DAYS (90).
"""

import logging
import os
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import pytz
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackContext

import i18n
from database import DBSession, HousingAccessUser, HousingLeaveEvent, HousingPayment, HousingSendLog
from user_handlers import housing_pay_report
from user_jobs import housing_access_store, housing_pay_store, housing_tier

logger = logging.getLogger(__name__)

BERLIN = pytz.timezone("Europe/Berlin")
WAIT = timedelta(days=7)
MAX_AGE = timedelta(days=30)
NIGHT_START_HOUR = 22
NIGHT_END_HOUR = 9
MAX_PER_RUN = 40
SEND_PAUSE_SECONDS = 0.1
GIFT_CALLBACK = "housing:gift:claim"


def enabled() -> bool:
    return (os.getenv("HOUSING_WINBACK_ENABLED", "1") or "1").strip() != "0"


def gift_days() -> int:
    try:
        value = int(float(os.getenv("HOUSING_GIFT_DAYS", "2") or 2))
    except ValueError:
        value = 2
    return value if value > 0 else 2


def gift_cooldown() -> timedelta:
    try:
        value = int(float(os.getenv("HOUSING_GIFT_COOLDOWN_DAYS", "90") or 90))
    except ValueError:
        value = 90
    return timedelta(days=value if value > 0 else 90)


def utc_now() -> datetime:
    return datetime.utcnow()


def is_night(now: Optional[datetime] = None) -> bool:
    hour = pytz.utc.localize(now or utc_now()).astimezone(BERLIN).hour
    return hour >= NIGHT_START_HOUR or hour < NIGHT_END_HOUR


def days_text(count: int, lang: str) -> str:
    """«2 дні» / «2 дня» / «2 Tage»: форма слова зависит от числа."""
    if lang == "de":
        key = "pay.days.1" if count == 1 else "pay.days.2"
    else:
        if count % 10 == 1 and count % 100 != 11:
            key = "pay.days.1"
        elif 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14:
            key = "pay.days.2"
        else:
            key = "pay.days.5"
    return i18n.t(key, lang, n=count)


def gift_allowed(user_id: int, now: Optional[datetime] = None) -> bool:
    last = housing_pay_store.last_gift(user_id)
    return last is None or (now or utc_now()) - last.started_at >= gift_cooldown()


# --- кого и что писать ---------------------------------------------------------------

def _blocked(user_id: int) -> bool:
    session = DBSession()
    try:
        row = (
            session.query(HousingLeaveEvent.kind)
            .filter(HousingLeaveEvent.user_id == int(user_id), HousingLeaveEvent.kind.in_(("blocked", "unblocked")))
            .order_by(HousingLeaveEvent.created_at.desc(), HousingLeaveEvent.id.desc()).first()
        )
        return bool(row is not None and row[0] == "blocked")
    finally:
        session.close()


def _provisional_end(user_id: int, ended_at: datetime) -> bool:
    """Доступ кончился не как подписка, а как временный день до проверки чека."""
    session = DBSession()
    try:
        for until, in session.query(HousingPayment.provisional_until).filter(
            HousingPayment.user_id == int(user_id), HousingPayment.provisional_until.isnot(None),
        ):
            if abs(until - ended_at) < timedelta(minutes=1):
                return True
        return False
    finally:
        session.close()


def candidates(now: Optional[datetime] = None) -> List[Tuple[int, datetime]]:
    """(user_id, ended_at): доступ закончился 7-30 дней назад, и это была подписка."""
    now = now or utc_now()
    session = DBSession()
    try:
        rows = (
            session.query(HousingAccessUser)
            .filter(HousingAccessUser.active.is_(False), HousingAccessUser.is_trial.is_(False))
            .filter(HousingAccessUser.expires_at.isnot(None))
            .filter(HousingAccessUser.expires_at <= now - WAIT, HousingAccessUser.expires_at >= now - MAX_AGE)
            .all()
        )
        found = [(int(r.user_id), r.expires_at) for r in rows]
    finally:
        session.close()
    result = []
    for user_id, ended_at in found:
        if user_id == housing_tier.ADMIN_ID or housing_pay_store.winback_sent(user_id, ended_at):
            continue
        if housing_pay_store.gift_matching(user_id, ended_at) is not None or _provisional_end(user_id, ended_at):
            continue
        result.append((user_id, ended_at))
    return result


def eligible(user_id: int) -> bool:
    """Не продлил (доступ неактивен - проверено выше), не заблокировал бота,
    фильтр активен, нет оплаты на проверке."""
    from user_handlers import housing_monitor, housing_payment

    if _blocked(user_id):
        return False
    if housing_payment.open_payment(user_id) is not None:
        return False
    return bool(housing_monitor.user_filters(user_id))


def _delayed_count(user_id: int, start: datetime, end: datetime) -> int:
    session = DBSession()
    try:
        rows = (
            session.query(HousingSendLog.source, HousingSendLog.listing_key)
            .filter(HousingSendLog.user_id == int(user_id), HousingSendLog.delayed.is_(True))
            .filter(HousingSendLog.sent_at >= start, HousingSendLog.sent_at < end)
            .distinct().all()
        )
        return len(rows)
    finally:
        session.close()


def build_message(user_id: int, ended_at: datetime, market=None, now: Optional[datetime] = None):
    """(текст, клавиатура) возврата. Цифры только настоящие: ноль не показываем,
    «нічого не з'явилось» - только если это подтвердил рынок."""
    now = now or utc_now()
    lang = i18n.get_lang(user_id)
    start, end = ended_at, min(ended_at + WAIT, now)
    delayed = _delayed_count(user_id, start, end)
    gone = housing_pay_store.missed_count(user_id, start, end)
    lines = [i18n.t("pay.winback.head", lang, date=ended_at.strftime("%d.%m.%Y"))]
    relax_rows: list = []
    if delayed or gone:
        lines.append("")
        lines.append(i18n.t("pay.winback.intro", lang))
        if delayed:
            lines.append(i18n.t("pay.winback.delayed", lang, n=delayed, hours=housing_tier.free_delay_hours()))
        if gone:
            lines.append(i18n.t("pay.winback.gone", lang, n=gone))
    else:
        verified, relax_rows, relax_text = housing_pay_report.relax_for_empty(user_id, market)
        if verified:
            lines.append("")
            lines.append(i18n.t("pay.winback.empty", lang))
        if relax_text:
            lines.append(relax_text)
    lines.append("")
    rows = list(relax_rows)
    if gift_allowed(user_id, now):
        text_days = days_text(gift_days(), lang)
        lines.append(i18n.t("pay.winback.tail", lang, days_text=text_days))
        rows.append([InlineKeyboardButton(
            i18n.t("pay.winback.btn.gift", lang, days_text=text_days), callback_data=GIFT_CALLBACK,
        )])
    else:
        lines.append(i18n.t("pay.winback.tail_plain", lang))
        lines.append(i18n.t("pay.report.choose", lang))
        from user_handlers import housing_payment
        rows += housing_payment.package_buttons(lang)
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def winback_job(context, now: Optional[datetime] = None) -> Dict[str, int]:
    """Раз в 30 минут. Одно сообщение на одно окончание подписки."""
    from user_handlers import housing_payment

    if not enabled() or not housing_payment.enabled():
        return {"sent": 0}
    now = now or utc_now()
    if is_night(now):
        return {"sent": 0}
    bot = context.bot
    sent = 0
    market = None
    for user_id, ended_at in candidates(now):
        if sent >= MAX_PER_RUN:
            break
        try:
            if not eligible(user_id):
                continue
            if market is None:
                try:
                    from user_handlers import housing_relax
                    market = housing_relax.Market()
                except Exception:
                    logger.exception("Could not read the market for winback messages")
                    market = False
            text, keyboard = build_message(user_id, ended_at, market or None, now)
            bot.send_message(chat_id=user_id, text=text, parse_mode="HTML", reply_markup=keyboard)
        except Exception as exc:
            if housing_tier.recipient_gone(exc):
                housing_pay_store.mark_winback_sent(user_id, ended_at)   # недоступен - больше не пробуем
            else:
                logger.exception("Could not send the winback message to %s", user_id)
            continue
        housing_pay_store.mark_winback_sent(user_id, ended_at)
        sent += 1
        time.sleep(SEND_PAUSE_SECONDS)
    logger.info("Winback: sent %s messages", sent)
    return {"sent": sent}


# --- подарок -------------------------------------------------------------------------

def claim_gift(update: Update, context: CallbackContext) -> None:
    from user_handlers import housing_monitor

    query = update.callback_query
    user = update.effective_user
    if not query or not user:
        return
    uid = int(user.id)
    lang = i18n.get_lang(uid)
    now = utc_now()
    if housing_tier.is_premium(uid):
        query.answer(i18n.t("pay.gift.premium", lang), show_alert=True)
        return
    last = housing_pay_store.last_gift(uid)
    if last is not None and not gift_allowed(uid, now):
        query.answer(
            i18n.t("pay.gift.already", lang, date=(last.started_at + gift_cooldown()).strftime("%d.%m.%Y")),
            show_alert=True,
        )
        return
    until = now + timedelta(days=gift_days())
    housing_access_store.grant_access(uid, housing_monitor._display_name(user), expires_at=until)
    # Иначе ежедневная проверка сразу пришлёт «через 3 дня закончится подписка».
    housing_access_store.mark_notice_sent(uid)
    housing_pay_store.add_gift(uid, now, until)
    local_until = pytz.utc.localize(until).astimezone(BERLIN).strftime("%d.%m %H:%M")
    query.answer()
    try:
        query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        logger.debug("Could not drop the gift button", exc_info=True)
    try:
        context.bot.send_message(chat_id=uid, text=i18n.t("pay.gift.on", lang, until=local_until))
    except Exception:
        logger.exception("Could not confirm the gift to %s", uid)


def handle_callback(update: Update, context: CallbackContext) -> bool:
    query = update.callback_query
    if not query or query.data != GIFT_CALLBACK:
        return False
    if not enabled():
        query.answer()
        return True
    claim_gift(update, context)
    return True


def expire_gifts(bot, now: Optional[datetime] = None) -> int:
    """Подарочные дни кончились: тихо возвращаем бесплатный уровень (без общего
    «подписка закончилась») и шлём короткое сообщение с тремя пакетами. Если
    человек тем временем заплатил, подарок молча закрывается."""
    now = now or utc_now()
    closed = 0
    if is_night(now):
        # 25.11.2026: уночі (22-09 за Берліном) не пишемо - подарунок тихо дотягне до ранку.
        return 0
    for gift in housing_pay_store.gifts_to_close(now):
        session = DBSession()
        try:
            access = session.query(HousingAccessUser).get(int(gift.user_id))
            still_gift = bool(
                access is not None and access.active and access.expires_at
                and abs(access.expires_at - gift.until) < timedelta(minutes=1)
            )
        finally:
            session.close()
        housing_pay_store.mark_gift_handled(gift.id)
        if not still_gift:
            continue
        housing_access_store.set_active(gift.user_id, False)
        closed += 1
        message = housing_pay_report.gift_end_message(gift.user_id, gift.started_at)
        if message is None:
            continue
        text, keyboard = message
        try:
            bot.send_message(chat_id=int(gift.user_id), text=text, parse_mode="HTML", reply_markup=keyboard)
        except Exception:
            logger.exception("Could not tell %s the gift days are over", gift.user_id)
    return closed


def gift_expiry_job(context) -> None:
    expire_gifts(context.bot)
