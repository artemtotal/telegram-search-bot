"""Одне нагадування тим, хто дійшов до екрана з карткою й не надіслав чек (26.11.2026).

Раз на 30 хвилин задання шукає оплати зі статусом «awaiting_receipt»,
відкриті 24-72 години тому (72 год - вікно, поки чек ще приймається,
housing_payment.RECEIPT_WINDOW), і пише людині ОДИН раз за весь час
(журнал housing_pay_nudge, ключ - user_id). Не пишемо тим, хто заблокував
бота, у нічні години (22-09 за Берліном), тим, у кого вже є схвалена оплата
після цієї, і тим, хто вже надіслав чек (статус «sent»).

ЗА ЗАМОВЧУВАННЯМ ВИМКНЕНО. Увімкнути: HOUSING_PAY_NUDGE_ENABLED=1 або {"housing_pay_nudge": true}
у /app/config/bot_switches.json (контейнер перестворювати не треба, достатньо перезапуску).
"""

import logging
import os
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import i18n
from database import DBSession, HousingPayment
from user_handlers import housing_winback, switches
from user_jobs import housing_pay_store, housing_tier

logger = logging.getLogger(__name__)

MIN_AGE = timedelta(hours=24)
MAX_AGE = timedelta(hours=72)
MAX_PER_RUN = 20
SEND_PAUSE_SECONDS = 0.1


def enabled() -> bool:
    """Змінна HOUSING_PAY_NUDGE_ENABLED=1 важливіша; інакше {"housing_pay_nudge": true} у /app/config/bot_switches.json."""
    return switches.flag("HOUSING_PAY_NUDGE_ENABLED", "housing_pay_nudge", False)


def utc_now() -> datetime:
    return datetime.utcnow()


def candidates(now: Optional[datetime] = None) -> List[HousingPayment]:
    """Оплати без чека віком 24-72 год, по одній (остання) на людину."""
    now = now or utc_now()
    session = DBSession()
    try:
        rows = (
            session.query(HousingPayment)
            .filter(HousingPayment.status == "awaiting_receipt")
            .filter(HousingPayment.created_at <= now - MIN_AGE, HousingPayment.created_at >= now - MAX_AGE)
            .order_by(HousingPayment.id.asc()).all()
        )
        newest: Dict[int, HousingPayment] = {}
        for row in rows:
            newest[int(row.user_id)] = row
        result = []
        for user_id, row in newest.items():
            later_paid = (
                session.query(HousingPayment.id)
                .filter(HousingPayment.user_id == user_id, HousingPayment.status.in_(("sent", "approved")))
                .filter(HousingPayment.created_at >= row.created_at).first()
            )
            if later_paid is not None:
                continue
            session.expunge(row)
            result.append(row)
        return result
    finally:
        session.close()


def build_message(user_id: int):
    """(текст, клавіатура): коротко, три пакети і кнопка «немає української карти»."""
    from user_handlers import housing_payment

    lang = i18n.get_lang(user_id)
    rows = list(housing_payment.package_buttons(lang))
    if housing_payment.ADMIN_ID:
        rows.append([InlineKeyboardButton(
            i18n.t("housing.pay.btn.cash", lang), url=f"tg://user?id={housing_payment.ADMIN_ID}",
        )])
    return i18n.t("pay.nudge.text", lang), InlineKeyboardMarkup(rows)


def pay_nudge_job(context, now: Optional[datetime] = None) -> Dict[str, int]:
    from user_handlers import housing_payment

    if not enabled() or not housing_payment.enabled():
        return {"sent": 0}
    now = now or utc_now()
    if housing_winback.is_night(now):
        return {"sent": 0}
    bot = context.bot
    sent = 0
    for payment in candidates(now):
        if sent >= MAX_PER_RUN:
            break
        user_id = int(payment.user_id)
        if user_id == housing_tier.ADMIN_ID or housing_pay_store.pay_nudge_sent(user_id):
            continue
        if housing_winback._blocked(user_id):
            housing_pay_store.mark_pay_nudge_sent(user_id, payment.id)
            continue
        try:
            text, keyboard = build_message(user_id)
            bot.send_message(chat_id=user_id, text=text, reply_markup=keyboard)
        except Exception as exc:
            if housing_tier.recipient_gone(exc):
                housing_pay_store.mark_pay_nudge_sent(user_id, payment.id)   # недосяжний - більше не пробуємо
            else:
                logger.exception("Could not send the payment nudge to %s", user_id)
            continue
        housing_pay_store.mark_pay_nudge_sent(user_id, payment.id)
        sent += 1
        time.sleep(SEND_PAUSE_SECONDS)
    if sent:
        logger.info("Payment nudge: sent %s messages", sent)
    return {"sent": sent}
