"""«⚡ Підписка» без листування в особистих (27.10.2026).

Людина тисне кнопку підписки, обирає 1-6 місяців і бачить суму в гривнях
(PREMIUM_PRICE_EUR × місяці × EUR_UAH_RATE) та номер карти. Після оплати
надсилає сюди скриншот або квитанцію; адміну приходить копія з кнопками
«✅ Дозволити» / «✖ Відхилити», і підписка вмикається одним натиском.

Щоб людина не чекала на перевірку, одразу після квитанції миттєві
сповіщення вмикаються на PROVISIONAL (1 день). Після «Дозволити» оплачені
місяці рахуються від моменту підтвердження; після «Відхилити» або коли день
минув без рішення - знову безкоштовний рівень.

Карта задається HOUSING_PAY_CARD / HOUSING_PAY_CARD_HOLDER у .env. Без карти
кнопка підписки працює по-старому: запит адміну, оплата домовляється вручну.
"""

import html
import logging
import math
import os
from datetime import datetime, timedelta
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackContext, Filters, MessageHandler

import i18n
from database import DBSession, HousingAccessUser, HousingPayment
from user_jobs import housing_access_store, housing_tier

logger = logging.getLogger(__name__)

ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
PAY_CARD = "".join(ch for ch in os.getenv("HOUSING_PAY_CARD", "") if ch.isdigit())
PAY_CARD_HOLDER = os.getenv("HOUSING_PAY_CARD_HOLDER", "").strip()
MONTH_OPTIONS = (1, 2, 3, 4, 5, 6)
# Сума округлюється вгору до цілих десятків гривень - так її простіше
# переказати й звірити з квитанцією.
UAH_ROUND_TO = 10
# Курс фіксований (рішення Артёма 27.10.2026): 5 € = 250 грн.
EUR_UAH_RATE = float(os.getenv("HOUSING_EUR_UAH_RATE", "50") or 50)
# Квитанція, надіслана пізніше, ніж за стільки після вибору місяців, уже не
# вважається оплатою - людина могла давно передумати й надіслати фото до
# чогось іншого.
RECEIPT_WINDOW = timedelta(days=3)
# Текст замість квитанції: нагадуємо, що потрібне фото, лише одразу після
# показу реквізитів, щоб не перехоплювати звичайні повідомлення пізніше.
TEXT_REMINDER_WINDOW = timedelta(hours=2)
# Миттєві сповіщення на час перевірки квитанції (рішення Артёма 27.10.2026:
# «одного дня не жалко»). Не частіше ніж раз на PROVISIONAL_COOLDOWN, щоб
# будь-яке фото не перетворювалось на безкоштовні дні.
PROVISIONAL = timedelta(days=1)
PROVISIONAL_COOLDOWN = timedelta(days=30)

def utc_now() -> datetime:
    return datetime.utcnow()


def enabled() -> bool:
    return bool(PAY_CARD) and bool(ADMIN_ID)


def _format_card(card: str) -> str:
    return " ".join(card[i:i + 4] for i in range(0, len(card), 4))


def _format_rate(rate: float) -> str:
    if float(rate).is_integer():
        return str(int(rate))
    return f"{rate:.2f}".replace(".", ",")


def eur_uah_rate() -> float:
    return EUR_UAH_RATE


def amount_uah(months: int, rate: float) -> int:
    raw = months * housing_tier.PREMIUM_PRICE_EUR * rate
    return int(math.ceil(raw / UAH_ROUND_TO) * UAH_ROUND_TO)


def _current_expiry(user_id: int) -> Optional[datetime]:
    session = DBSession()
    try:
        row = session.query(HousingAccessUser).get(int(user_id))
        if row is None or not row.active or row.is_trial or not row.expires_at:
            return None
        return row.expires_at if row.expires_at > utc_now() else None
    finally:
        session.close()


# --- сховище -----------------------------------------------------------------

def create_payment(user_id: int, display_name: str, months: int, rate: float) -> HousingPayment:
    session = DBSession()
    try:
        # Попередні незавершені вибори цієї людини більше не актуальні:
        # квитанція має прив'язатися до останньої показаної суми.
        session.query(HousingPayment).filter(
            HousingPayment.user_id == int(user_id),
            HousingPayment.status == "awaiting_receipt",
        ).update({"status": "cancelled", "decided_at": utc_now()}, synchronize_session=False)
        row = HousingPayment(
            user_id=int(user_id),
            display_name=str(display_name or "")[:120],
            months=int(months),
            amount_eur=float(months * housing_tier.PREMIUM_PRICE_EUR),
            amount_uah=amount_uah(months, rate),
            eur_uah_rate=float(rate),
            status="awaiting_receipt",
            created_at=utc_now(),
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        session.expunge(row)
        return row
    finally:
        session.close()


def get_payment(payment_id: int) -> Optional[HousingPayment]:
    session = DBSession()
    try:
        row = session.query(HousingPayment).get(int(payment_id))
        if row is not None:
            session.expunge(row)
        return row
    finally:
        session.close()


def open_payment(user_id: int, since: Optional[datetime] = None) -> Optional[HousingPayment]:
    """Останній вибір, до якого ще можна надіслати квитанцію."""
    session = DBSession()
    try:
        row = (
            session.query(HousingPayment)
            .filter(HousingPayment.user_id == int(user_id))
            .filter(HousingPayment.status.in_(("awaiting_receipt", "sent")))
            .filter(HousingPayment.created_at >= (since or utc_now() - RECEIPT_WINDOW))
            .order_by(HousingPayment.id.desc())
            .first()
        )
        if row is not None:
            session.expunge(row)
        return row
    finally:
        session.close()


def _set_status(payment_id: int, status: str, **fields) -> bool:
    """Повертає False, якщо оплату вже вирішено (подвійний натиск адміна)."""
    session = DBSession()
    try:
        row = session.query(HousingPayment).get(int(payment_id))
        if row is None:
            return False
        if status in ("approved", "rejected") and row.status in ("approved", "rejected"):
            return False
        row.status = status
        for key, value in fields.items():
            setattr(row, key, value)
        session.commit()
        return True
    finally:
        session.close()


def _provisional_running(payment: HousingPayment) -> bool:
    """Чи досі діє саме тимчасовий день цієї оплати (а не підписка, яку
    адмін тим часом видав інакше)."""
    if not payment.provisional_until:
        return False
    session = DBSession()
    try:
        row = session.query(HousingAccessUser).get(int(payment.user_id))
        if row is None or not row.active or not row.expires_at:
            return False
        return abs(row.expires_at - payment.provisional_until) < timedelta(minutes=1)
    finally:
        session.close()


def _grant_provisional(payment: HousingPayment) -> Optional[datetime]:
    """Вмикає миттєві сповіщення на PROVISIONAL, якщо людина ще не платна й
    не отримувала такого дня останні PROVISIONAL_COOLDOWN."""
    user_id = int(payment.user_id)
    if housing_tier.is_premium(user_id):
        return None
    session = DBSession()
    try:
        recent = (
            session.query(HousingPayment.id)
            .filter(HousingPayment.user_id == user_id)
            .filter(HousingPayment.provisional_until.isnot(None))
            .filter(HousingPayment.provisional_until >= utc_now() - PROVISIONAL_COOLDOWN)
            .first()
        )
    finally:
        session.close()
    if recent is not None:
        return None
    until = utc_now() + PROVISIONAL
    housing_access_store.grant_access(user_id, payment.display_name, expires_at=until)
    # Інакше щоденна перевірка одразу надішле «за 3 дні закінчується підписка».
    housing_access_store.mark_notice_sent(user_id)
    _set_status(payment.id, payment.status, provisional_until=until)
    payment.provisional_until = until
    return until


def expire_provisional(bot) -> int:
    """Тимчасовий день минув, а адмін ще не вирішив: тихо повертаємо
    безкоштовний рівень (без загального «підписка закінчилась») і нагадуємо
    адміну. Викликається кожні 10 хв і перед щоденною перевіркою підписок."""
    session = DBSession()
    try:
        due = (
            session.query(HousingPayment)
            .filter(HousingPayment.status == "sent")
            .filter(HousingPayment.provisional_until.isnot(None))
            .filter(HousingPayment.provisional_until <= utc_now())
            .all()
        )
        for row in due:
            session.expunge(row)
    finally:
        session.close()
    expired = 0
    for payment in due:
        if not _provisional_running(payment):
            continue
        housing_access_store.set_active(payment.user_id, False)
        expired += 1
        lang = i18n.get_lang(payment.user_id)
        try:
            bot.send_message(chat_id=payment.user_id, text=i18n.t("housing.pay.provisional_ended", lang))
        except Exception:
            logger.exception("Could not tell user %s the provisional day is over", payment.user_id)
        if ADMIN_ID:
            try:
                bot.send_message(
                    chat_id=ADMIN_ID,
                    text=(
                        f"⏳ Тимчасовий день для {html.escape(payment.display_name or str(payment.user_id))} "
                        f"(<code>{int(payment.user_id)}</code>) минув, а оплату {payment.amount_uah} грн "
                        f"за {payment.months} міс. ще не перевірено. Кнопки — під квитанцією вище."
                    ),
                    parse_mode="HTML",
                )
            except Exception:
                logger.exception("Could not remind the admin about payment %s", payment.id)
    return expired


def provisional_expiry_job(context) -> None:
    expire_provisional(context.bot)


def cancel_open(user_id: int) -> None:
    session = DBSession()
    try:
        session.query(HousingPayment).filter(
            HousingPayment.user_id == int(user_id),
            HousingPayment.status == "awaiting_receipt",
        ).update({"status": "cancelled", "decided_at": utc_now()}, synchronize_session=False)
        session.commit()
    finally:
        session.close()


# --- екрани для людини ---------------------------------------------------------

def _months_keyboard(lang: str, rate: float) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(
            i18n.t("housing.pay.month_btn", lang, months=months, uah=amount_uah(months, rate)),
            callback_data=f"housing:pay:m:{months}",
        )
        for months in MONTH_OPTIONS
    ]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows.append([InlineKeyboardButton(i18n.t("housing.pay.btn.cancel", lang), callback_data="housing:pay:cancel")])
    return InlineKeyboardMarkup(rows)


def show_months(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    user = update.effective_user
    if not user:
        return
    lang = i18n.get_lang(user.id)
    rate = eur_uah_rate()
    expiry = _current_expiry(user.id)
    current = i18n.t("housing.pay.current", lang, date=expiry.strftime("%d.%m.%Y")) if expiry else ""
    text = i18n.t(
        "housing.pay.choose", lang,
        price=housing_tier.PREMIUM_PRICE_EUR, rate=_format_rate(rate), current=current,
    )
    keyboard = _months_keyboard(lang, rate)
    if query:
        query.answer()
        query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)
    else:
        update.effective_message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)


def _pick_months(update: Update, context: CallbackContext) -> None:
    from user_handlers import housing_monitor

    query = update.callback_query
    user = update.effective_user
    try:
        months = int(query.data.split(":")[3])
    except (IndexError, ValueError):
        query.answer()
        return
    if months not in MONTH_OPTIONS:
        query.answer()
        return
    lang = i18n.get_lang(user.id)
    payment = create_payment(user.id, housing_monitor._display_name(user), months, eur_uah_rate())
    holder = i18n.t("housing.pay.holder", lang, holder=html.escape(PAY_CARD_HOLDER)) if PAY_CARD_HOLDER else ""
    query.answer()
    query.edit_message_text(
        i18n.t(
            "housing.pay.instructions", lang,
            months=months, uah=payment.amount_uah, eur=int(payment.amount_eur),
            card=_format_card(PAY_CARD), holder=holder,
        ),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(i18n.t("housing.pay.btn.other", lang), callback_data="housing:pay:start")],
            [InlineKeyboardButton(i18n.t("housing.pay.btn.cancel", lang), callback_data="housing:pay:cancel")],
        ]),
    )


def _cancel(update: Update, context: CallbackContext) -> None:
    from user_handlers import housing_monitor

    query = update.callback_query
    user = update.effective_user
    cancel_open(user.id)
    query.answer(i18n.t("housing.pay.cancelled", i18n.get_lang(user.id)))
    housing_monitor.show_menu(update, context, edit=True)


# --- квитанція ---------------------------------------------------------------

def _admin_caption(payment: HousingPayment, user_id: int, note: str = "") -> str:
    lines = [
        "💳 <b>Оплата підписки — перевірте квитанцію</b>",
        "",
        f"Користувач: {html.escape(payment.display_name or str(user_id))}",
        f"Telegram ID: <code>{int(user_id)}</code>",
        f"Термін: <b>{payment.months} міс.</b>",
        f"Сума: <b>{payment.amount_uah} грн</b> ({int(payment.amount_eur)} € за курсом {_format_rate(payment.eur_uah_rate)})",
    ]
    expiry = _current_expiry(user_id)
    if payment.provisional_until:
        lines.append(
            f"⏳ До перевірки видано 1 день миттєвих сповіщень "
            f"(до {payment.provisional_until.strftime('%d.%m %H:%M')} UTC)."
        )
    elif expiry:
        lines.append(f"Зараз підписка до {expiry.strftime('%d.%m.%Y')} — місяці додадуться після.")
    if note:
        lines += ["", f"Підпис: {html.escape(note[:300])}"]
    return "\n".join(lines)


def _admin_keyboard(payment: HousingPayment, user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"✅ Дозволити {payment.months} міс.", callback_data=f"housing:pay:ok:{payment.id}")],
        [
            InlineKeyboardButton("✖ Відхилити", callback_data=f"housing:pay:no:{payment.id}"),
            InlineKeyboardButton("💌 Написати в ЛС", url=f"tg://user?id={int(user_id)}"),
        ],
    ])


def handle_receipt(update: Update, context: CallbackContext) -> None:
    """Фото або файл у приватному чаті. Без відкритої оплати нічого не робить -
    фото в бота може бути надіслане й до чогось іншого."""
    message = update.effective_message
    user = update.effective_user
    if not message or not user or not enabled():
        return
    payment = open_payment(user.id)
    if payment is None:
        return
    lang = i18n.get_lang(user.id)
    provisional = None
    if payment.status == "awaiting_receipt":
        provisional = _grant_provisional(payment)
    try:
        context.bot.copy_message(
            chat_id=ADMIN_ID,
            from_chat_id=message.chat_id,
            message_id=message.message_id,
            caption=_admin_caption(payment, user.id, message.caption or ""),
            parse_mode="HTML",
            reply_markup=_admin_keyboard(payment, user.id),
        )
    except Exception:
        logger.exception("Could not forward a payment receipt of user %s to the admin", user.id)
        message.reply_text(i18n.t("housing.access.request_failed", lang))
        return
    if payment.status == "awaiting_receipt":
        _set_status(payment.id, "sent", receipt_at=utc_now())
    if provisional:
        message.reply_text(i18n.t("housing.pay.receipt_received_provisional", lang))
    else:
        message.reply_text(i18n.t("housing.pay.receipt_received", lang))


def remind_receipt(update: Update, context: CallbackContext) -> bool:
    """Текст, який більше ніхто не обробив, одразу після показу реквізитів:
    людина пише «оплатила» замість скриншота - підказуємо, що потрібне фото."""
    user = update.effective_user
    if not user or not enabled():
        return False
    payment = open_payment(user.id, since=utc_now() - TEXT_REMINDER_WINDOW)
    if payment is None or payment.status != "awaiting_receipt":
        return False
    update.effective_message.reply_text(i18n.t("housing.pay.receipt_reminder", i18n.get_lang(user.id)))
    return True


# --- рішення адміна ------------------------------------------------------------

def _edit_admin_message(query, text: str) -> None:
    try:
        query.edit_message_caption(caption=text, parse_mode="HTML")
    except Exception:
        # Квитанцію надіслали текстом-документом без підпису або повідомлення
        # вже змінене - кнопки все одно треба прибрати.
        try:
            query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            logger.debug("Could not update the admin payment message", exc_info=True)


def _decide(update: Update, context: CallbackContext, approve: bool) -> None:
    from user_handlers import housing_monitor

    query = update.callback_query
    user = update.effective_user
    if not user or int(user.id) != ADMIN_ID:
        query.answer()
        return
    try:
        payment = get_payment(int(query.data.split(":")[3]))
    except (IndexError, ValueError):
        payment = None
    if payment is None:
        query.answer("Оплату не знайдено.", show_alert=True)
        return
    target_id = int(payment.user_id)
    base = _admin_caption(payment, target_id)
    if payment.status in ("approved", "rejected"):
        verdict = "✅ вже дозволено" if payment.status == "approved" else "✖ вже відхилено"
        query.answer(f"Цю оплату {verdict}.", show_alert=True)
        _edit_admin_message(query, f"{base}\n\n<b>{verdict}</b>")
        return
    provisional_running = _provisional_running(payment)
    if approve:
        if provisional_running:
            # Оплачені місяці - від підтвердження, а не після тимчасового дня.
            housing_access_store.set_active(target_id, False)
        expires_at = housing_monitor.grant_paid_months(target_id, payment.display_name, payment.months)
        if not _set_status(payment.id, "approved", decided_at=utc_now(), expires_at=expires_at):
            query.answer("Цю оплату вже оброблено.", show_alert=True)
            return
        query.answer("✅ Підписку ввімкнено")
        _edit_admin_message(query, f"{base}\n\n<b>✅ Дозволено до {expires_at.strftime('%d.%m.%Y')}</b>")
        housing_monitor._notify_user_access_granted(context.bot, target_id, expires_at)
        return
    if not _set_status(payment.id, "rejected", decided_at=utc_now()):
        query.answer("Цю оплату вже оброблено.", show_alert=True)
        return
    if provisional_running:
        housing_access_store.set_active(target_id, False)
    query.answer("✖ Відхилено")
    _edit_admin_message(query, f"{base}\n\n<b>✖ Відхилено</b>")
    lang = i18n.get_lang(target_id)
    try:
        context.bot.send_message(
            chat_id=target_id,
            text=i18n.t("housing.pay.rejected", lang),
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(i18n.t("housing.pay.btn.retry", lang), callback_data="housing:pay:start"),
            ]]),
        )
    except Exception:
        logger.exception("Could not tell user %s their payment was rejected", target_id)


def handle_callback(update: Update, context: CallbackContext) -> bool:
    query = update.callback_query
    if not query or not query.data or not query.data.startswith("housing:pay:"):
        return False
    action = query.data.split(":")[2] if query.data.count(":") >= 2 else ""
    if action == "start":
        if enabled():
            show_months(update, context)
        else:
            from user_handlers import housing_monitor
            housing_monitor.request_access(update, context)
    elif action == "m":
        _pick_months(update, context)
    elif action == "cancel":
        _cancel(update, context)
    elif action == "ok":
        _decide(update, context, approve=True)
    elif action == "no":
        _decide(update, context, approve=False)
    else:
        query.answer()
    return True


receipt_handler = MessageHandler(
    Filters.chat_type.private & (Filters.photo | Filters.document),
    handle_receipt,
)
