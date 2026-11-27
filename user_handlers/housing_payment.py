"""«⚡ Підписка» без листування в особистих (27.10.2026).

Людина тисне кнопку підписки, обирає пакет (з 25.11.2026: 1 міс. / 3 міс. /
«Поки не знайду житло», user_jobs/housing_packages.py; HOUSING_PACKAGES_ENABLED=0
повертає вибір 1-6 місяців) і бачить суму в гривнях (ціна пакета × EUR_UAH_RATE,
вгору до 10 грн) та номер карти. Після оплати
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
from user_jobs import housing_access_store, housing_pay_store, housing_packages, housing_tier

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
        if row.expires_at <= utc_now():
            return None
    finally:
        session.close()
    # Подарункові дні - не оплачена підписка: нові місяці рахуються від оплати.
    if housing_pay_store.gift_running(user_id) is not None:
        return None
    return row.expires_at


def until_found_expiry(user_id: int) -> Optional[datetime]:
    """До коли діє пакет «Поки не знайду», якщо він зараз діє, інакше None.
    Пакет - це останній схвалений платіж із цим пакетом, що ще не минув, і
    доступ людини, який дотягує до його кінця."""
    now = utc_now()
    session = DBSession()
    try:
        paid = (
            session.query(HousingPayment)
            .filter(HousingPayment.user_id == int(user_id), HousingPayment.status == "approved")
            .filter(HousingPayment.package == housing_packages.UNTIL_FOUND)
            .filter(HousingPayment.expires_at.isnot(None), HousingPayment.expires_at > now)
            .order_by(HousingPayment.id.desc()).first()
        )
        if paid is None:
            return None
        access = session.query(HousingAccessUser).get(int(user_id))
        if access is None or not access.active or not access.expires_at or access.expires_at <= now:
            return None
        if access.expires_at < paid.expires_at - timedelta(minutes=1):
            return None
        return access.expires_at
    finally:
        session.close()


# --- сховище -----------------------------------------------------------------

def create_payment(
    user_id: int, display_name: str, months: int, rate: float, package: Optional[str] = None,
) -> HousingPayment:
    """`package` (m1/m3/until_found) задає місяці та ціну; без нього - старий
    вибір «N місяців» за ціною місяця."""
    if housing_packages.valid(package):
        months = housing_packages.months(package)
        amount_eur = float(housing_packages.price_eur(package))
        amount = housing_packages.amount_uah(package, rate)
    else:
        package = None
        amount_eur = float(months * housing_tier.PREMIUM_PRICE_EUR)
        amount = amount_uah(months, rate)
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
            amount_eur=amount_eur,
            amount_uah=amount,
            eur_uah_rate=float(rate),
            status="awaiting_receipt",
            created_at=utc_now(),
            package=package,
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
                        f"за {html.escape(housing_packages.admin_label(payment.package, payment.months))} ще не перевірено. "
                        f"Кнопки — під квитанцією вище."
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
    if ADMIN_ID:   # 03.11.2026: нет украинской карты — договориться с админом о наличных в Потсдаме
        rows.append([InlineKeyboardButton(i18n.t("housing.pay.btn.cash", lang), url=f"tg://user?id={ADMIN_ID}")])
    rows.append([InlineKeyboardButton(i18n.t("housing.pay.btn.cancel", lang), callback_data="housing:pay:cancel")])
    return InlineKeyboardMarkup(rows)


def _packages_keyboard(lang: str, rate: float) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(
            i18n.t(
                "pay.pkg.btn", lang, name=i18n.t(f"pay.pkg.name.{code}", lang),
                eur=housing_packages.price_eur(code), uah=housing_packages.amount_uah(code, rate),
            ),
            callback_data=f"housing:pay:p:{code}",
        )]
        for code in housing_packages.CODES
    ]
    if ADMIN_ID:   # нема української карти — домовитися з адміном про готівку в Потсдамі
        rows.append([InlineKeyboardButton(i18n.t("housing.pay.btn.cash", lang), url=f"tg://user?id={ADMIN_ID}")])
    rows.append([InlineKeyboardButton(i18n.t("housing.pay.btn.cancel", lang), callback_data="housing:pay:cancel")])
    return InlineKeyboardMarkup(rows)


def package_buttons(lang: str) -> list:
    """Три кнопки пакетів для повідомлень про кінець підписки: тап одразу веде
    до реквізитів (без ціни в гривнях - вона на наступному екрані)."""
    return [
        [InlineKeyboardButton(
            i18n.t(
                "pay.pkg.btn_short", lang, name=i18n.t(f"pay.pkg.name.{code}", lang),
                eur=housing_packages.price_eur(code),
            ),
            callback_data=f"housing:pay:p:{code}",
        )]
        for code in housing_packages.CODES
    ]


def _show_until_found_status(update: Update, expiry: datetime, lang: str) -> None:
    query = update.callback_query
    text = i18n.t("pay.pkg.has_until_found", lang, date=expiry.strftime("%d.%m.%Y"))
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(i18n.t("housing.btn.back_to_monitor", lang), callback_data="housing:menu"),
    ]])
    if query:
        query.answer()
        query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)
    else:
        update.effective_message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)


def show_months(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    user = update.effective_user
    if not user:
        return
    lang = i18n.get_lang(user.id)
    rate = eur_uah_rate()
    expiry = _current_expiry(user.id)
    if housing_packages.enabled():
        # Хто вже на пакеті «Поки не знайду» - кнопку оплати не показуємо, лише строк.
        found_until = until_found_expiry(user.id)
        if found_until is not None:
            _show_until_found_status(update, found_until, lang)
            return
        current = i18n.t("pay.pkg.current", lang, date=expiry.strftime("%d.%m.%Y")) if expiry else ""
        text = i18n.t(
            "pay.pkg.choose", lang,
            p1=housing_packages.price_eur(housing_packages.M1), p3=housing_packages.price_eur(housing_packages.M3),
            pf=housing_packages.price_eur(housing_packages.UNTIL_FOUND), rate=_format_rate(rate), current=current,
        )
        keyboard = _packages_keyboard(lang, rate)
        if query:
            query.answer()
            query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)
        else:
            update.effective_message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)
        return
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
    if housing_packages.enabled():
        # Кнопка зі старого повідомлення («N місяців»): 1 і 3 - це пакети, решти більше нема.
        package = housing_packages.for_months(months)
        if package is None:
            show_months(update, context)
            return
        _pick_package(update, context, package)
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
            *([[InlineKeyboardButton(i18n.t("housing.pay.btn.cash", lang), url=f"tg://user?id={ADMIN_ID}")]] if ADMIN_ID else []),
            [InlineKeyboardButton(i18n.t("housing.pay.btn.cancel", lang), callback_data="housing:pay:cancel")],
        ]),
    )


def _pick_package(update: Update, context: CallbackContext, package: str) -> None:
    """Пакет обрано: показуємо суму й карту, оплата заводиться з цим пакетом."""
    from user_handlers import housing_monitor

    query = update.callback_query
    user = update.effective_user
    if not query or not user or not housing_packages.valid(package):
        if query:
            query.answer()
        return
    lang = i18n.get_lang(user.id)
    found_until = until_found_expiry(user.id)
    if found_until is not None:
        _show_until_found_status(update, found_until, lang)
        return
    payment = create_payment(user.id, housing_monitor._display_name(user), 0, eur_uah_rate(), package=package)
    holder = i18n.t("housing.pay.holder", lang, holder=html.escape(PAY_CARD_HOLDER)) if PAY_CARD_HOLDER else ""
    query.answer()
    query.edit_message_text(
        i18n.t(
            "pay.pkg.instructions", lang,
            name=i18n.t(f"pay.pkg.name.{package}", lang), term=i18n.t(f"pay.pkg.term.{package}", lang),
            uah=payment.amount_uah, eur=int(payment.amount_eur), card=_format_card(PAY_CARD), holder=holder,
        ),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(i18n.t("pay.pkg.btn.other", lang), callback_data="housing:pay:start")],
            *([[InlineKeyboardButton(i18n.t("housing.pay.btn.cash", lang), url=f"tg://user?id={ADMIN_ID}")]] if ADMIN_ID else []),
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
        (f"Пакет: <b>{housing_packages.admin_label(payment.package, payment.months)}</b>"
         + (f" (технічно {payment.months} міс.)" if payment.package == housing_packages.UNTIL_FOUND else ""))
        if housing_packages.valid(payment.package) else f"Термін: <b>{payment.months} міс.</b>",
        f"Сума: <b>{payment.amount_uah} грн</b> ({int(payment.amount_eur)} € за курсом {_format_rate(payment.eur_uah_rate)})",
    ]
    try:
        from user_handlers import housing_referral
        referral_line = housing_referral.admin_card_line(payment)
    except Exception:
        logger.exception("Could not build the referral line of payment %s", payment.id)
        referral_line = ""
    if referral_line:
        lines.append(referral_line)
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
        [InlineKeyboardButton(
            f"✅ Дозволити {housing_packages.admin_label(payment.package, payment.months)}",
            callback_data=f"housing:pay:ok:{payment.id}",
        )],
        [
            InlineKeyboardButton("✖ Відхилити", callback_data=f"housing:pay:no:{payment.id}"),
            InlineKeyboardButton("💌 Написати в ЛС", url=f"tg://user?id={int(user_id)}"),
        ],
    ])


def submit_receipt(bot, payment: HousingPayment, user_id: int, note: str, send_media) -> Optional[str]:
    """Спільний крок «квитанцію отримано» для чату й сайту (28.11.2026, виніс із handle_receipt без змін поведінки):
    тимчасовий день, картка адміну з тими ж кнопками, статус sent. `send_media(caption, keyboard)` кладе саму квитанцію адміну
    (у чаті - copy_message, на сайті - send_photo/send_document). Повертає "provisional" (видано день миттєвих), "plain" або None,
    якщо адміну не вдалося нічого надіслати (людині тоді показати «не вдалося надіслати запит»)."""
    provisional = None
    if payment.status == "awaiting_receipt":
        provisional = _grant_provisional(payment)
    caption = _admin_caption(payment, user_id, note or "")
    try:
        send_media(caption, _admin_keyboard(payment, user_id))
    except Exception as exc:
        # 01.11.2026: квитанція Наталії не скопіювалась, вона побачила «Не вдалося
        # надіслати запит», а адмін — нічого; оплата висіла без рішення. Тепер адмін
        # отримує ту саму картку текстом із кнопками й просить фото в ЛС, якщо треба.
        logger.exception("Could not forward a payment receipt of user %s to the admin", user_id)
        try:
            bot.send_message(
                chat_id=ADMIN_ID,
                text=f"{caption}\n\n⚠️ Квитанцію не вдалося переслати ({html.escape(str(exc)[:200])}) — "
                     f"за потреби попросіть її в ЛС.",
                parse_mode="HTML",
                reply_markup=_admin_keyboard(payment, user_id),
            )
        except Exception:
            logger.exception("Could not tell the admin about a payment receipt of user %s", user_id)
            return None
    if payment.status == "awaiting_receipt":
        _set_status(payment.id, "sent", receipt_at=utc_now())
    return "provisional" if provisional else "plain"


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

    def copy_to_admin(caption, keyboard):
        context.bot.copy_message(
            chat_id=ADMIN_ID,
            from_chat_id=message.chat_id,
            message_id=message.message_id,
            caption=caption,
            parse_mode="HTML",
            reply_markup=keyboard,
        )

    result = submit_receipt(context.bot, payment, user.id, message.caption or "", copy_to_admin)
    if result is None:
        message.reply_text(i18n.t("housing.access.request_failed", lang))
        return
    if result == "provisional":
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
        return
    except Exception:
        pass
    try:
        # Картка без фото (квитанція не переслалась) — це текст, не підпис.
        query.edit_message_text(text=text, parse_mode="HTML")
    except Exception:
        # Квитанцію надіслали текстом-документом без підпису або повідомлення
        # вже змінене - кнопки все одно треба прибрати.
        try:
            query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            logger.debug("Could not update the admin payment message", exc_info=True)


def _tell_until_found(bot, user_id: int, expires_at: datetime) -> None:
    """Пакет «Поки не знайду»: після загального «підписку ввімкнено» - як він діє."""
    try:
        bot.send_message(
            chat_id=user_id,
            text=i18n.t("pay.pkg.granted_until_found", i18n.get_lang(user_id), date=expires_at.strftime("%d.%m.%Y")),
        )
    except Exception:
        logger.exception("Could not explain the until-found package to %s", user_id)


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
        if provisional_running or housing_pay_store.gift_running(target_id) is not None:
            # Оплачені місяці - від підтвердження, а не після тимчасового дня чи подарунка.
            housing_access_store.set_active(target_id, False)
        # Скільки давати - за пакетом цієї оплати (для старих записів без пакета - як збережено).
        months = housing_packages.effective_months(payment.package, payment.months)
        expires_at = housing_monitor.grant_paid_months(target_id, payment.display_name, months)
        if not _set_status(payment.id, "approved", decided_at=utc_now(), expires_at=expires_at):
            query.answer("Цю оплату вже оброблено.", show_alert=True)
            return
        query.answer("✅ Підписку ввімкнено")
        outcome = f"{base}\n\n<b>✅ Дозволено до {expires_at.strftime('%d.%m.%Y')}</b>"
        housing_monitor._notify_user_access_granted(context.bot, target_id, expires_at)
        if payment.package == housing_packages.UNTIL_FOUND:
            _tell_until_found(context.bot, target_id, expires_at)
        # «Приведи друга»: перша оплата друга - місяць запрошувачу (рядок адміну - у картку).
        try:
            from user_handlers import housing_referral
            reward_line = housing_referral.reward_on_approval(context.bot, payment, expires_at)
        except Exception:
            logger.exception("Referral reward failed after approving payment %s", payment.id)
            reward_line = ""
        if reward_line:
            outcome += f"\n{reward_line}"
        _edit_admin_message(query, outcome)
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
    elif action == "p":
        parts = query.data.split(":")
        _pick_package(update, context, parts[3] if len(parts) > 3 else "")
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
