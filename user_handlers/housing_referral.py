"""«🎁 Запроси друга» (25.11.2026).

Особиста посилання t.me/<бот>?start=rf<код>. Привʼязка - лише для нової для бота
людини (жодного запису про неї до переходу), себе запросити не можна, один
запрошувач на людину. Винагорода - лише коли друг ОПЛАТИВ: при схваленні
першої оплати друга запрошувачу +1 місяць. Один раз за друга. Якщо в запрошувача
пакет «Поки не знайду» або безстроковий доступ - не продовжуємо, лише дякуємо.
Другу за перехід нічого додатково не обіцяємо.

Вимкнути: HOUSING_REFERRAL_ENABLED=0 (кнопка зникає, переходи ігноруються,
винагорода не нараховується).
"""

import html
import logging
import os
from datetime import datetime
from typing import Optional
from urllib.parse import quote

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackContext

import i18n
from database import DBSession, HousingAccessUser, HousingPayment
from user_jobs import housing_letter_store, housing_pay_store

logger = logging.getLogger(__name__)

# Параметр /start: «lt…» вже займають листи орендодавцю, тож тут «rf…».
START_PREFIX = "rf"
REWARD_MONTHS = 1
CALLBACK = "housing:invite"


def enabled() -> bool:
    return (os.getenv("HOUSING_REFERRAL_ENABLED", "1") or "1").strip() != "0"


def _bot_username() -> Optional[str]:
    return getattr(housing_letter_store, "_bot_username", None) or None


def link_for(user_id: int) -> Optional[str]:
    username = _bot_username()
    if not username:
        return None
    # Параметр /start - до 64 символів: «rf» + 8 hex.
    return f"https://t.me/{username}?start={START_PREFIX}{housing_pay_store.referral_code(user_id)}"


def handle_start(update: Update, context: CallbackContext, payload: str) -> bool:
    """/start rf<код>: тихо привʼязує нову людину до запрошувача. Нічого не
    надсилає й нічого не обіцяє. Викликати ДО того, як бот створить записи про людину."""
    if not enabled() or not update.effective_user:
        return False
    inviter = housing_pay_store.inviter_by_code(str(payload)[len(START_PREFIX):])
    if inviter is None:
        return False
    try:
        return housing_pay_store.bind_referral(int(update.effective_user.id), inviter)
    except Exception:
        logger.exception("Could not bind a referral for %s", update.effective_user.id)
        return False


def _no_extension(inviter_id: int) -> bool:
    """Продовжувати нічого: пакет «Поки не знайду» вже діє, безстроковий
    доступ або адмін."""
    from user_handlers import housing_payment

    if housing_payment.ADMIN_ID and int(inviter_id) == housing_payment.ADMIN_ID:
        return True
    if housing_payment.until_found_expiry(inviter_id) is not None:
        return True
    session = DBSession()
    try:
        row = session.query(HousingAccessUser).get(int(inviter_id))
        return bool(row is not None and row.active and not row.is_trial and row.expires_at is None)
    finally:
        session.close()


def admin_card_line(payment: HousingPayment) -> str:
    """Рядок у картці чека: друга привів запрошувач."""
    if not enabled():
        return ""
    ref = housing_pay_store.get_referral(payment.user_id)
    if ref is None:
        return ""
    line = f"🎁 Прийшов за запрошенням від <code>{int(ref.inviter_id)}</code>."
    first_payment = housing_pay_store.approved_payments_except(payment.user_id, payment.id) == 0
    if payment.status != "approved" and ref.rewarded_at is None and first_payment:
        if _no_extension(ref.inviter_id):
            line += " Після «Дозволити» запрошувач отримає лише подяку (пакет чи безстроковий доступ)."
        else:
            line += f" Після «Дозволити» запрошувачу +{REWARD_MONTHS} міс."
    return line


def reward_on_approval(bot, payment: HousingPayment, friend_expires_at: Optional[datetime] = None) -> str:
    """Викликається після схвалення оплати. Повертає рядок для картки адміна
    (порожній, якщо нагороджувати нікого)."""
    if not enabled():
        return ""
    friend_id = int(payment.user_id)
    ref = housing_pay_store.get_referral(friend_id)
    if ref is None or ref.rewarded_at is not None:
        return ""
    if housing_pay_store.approved_payments_except(friend_id, payment.id) > 0:
        return ""   # не перша оплата друга
    inviter_id = int(ref.inviter_id)
    kind = "thanks" if _no_extension(inviter_id) else "month"
    if housing_pay_store.claim_reward(friend_id, kind) is None:
        return ""
    new_expiry = None
    if kind == "month":
        from user_handlers import housing_monitor

        try:
            new_expiry = housing_monitor.grant_paid_months(inviter_id, "", REWARD_MONTHS, resume_filters=False)
        except Exception:
            logger.exception("Could not extend the subscription of inviter %s", inviter_id)
            housing_pay_store.unclaim_reward(friend_id)
            return f"⚠️ Не вдалося додати місяць запрошувачу <code>{inviter_id}</code> — додайте вручну."
    inviter_lang = i18n.get_lang(inviter_id)
    friend_lang = i18n.get_lang(friend_id)
    try:
        if kind == "month":
            text = i18n.t("pay.ref.inviter_month", inviter_lang, date=new_expiry.strftime("%d.%m.%Y"))
        else:
            text = i18n.t("pay.ref.inviter_thanks", inviter_lang)
        bot.send_message(chat_id=inviter_id, text=text)
    except Exception:
        logger.exception("Could not tell inviter %s about the reward", inviter_id)
    try:
        bot.send_message(chat_id=friend_id, text=i18n.t(f"pay.ref.friend_{kind}", friend_lang))
    except Exception:
        logger.exception("Could not thank invited %s", friend_id)
    if kind == "month":
        return (
            f"🎁 Запрошувачу <code>{inviter_id}</code> додано +{REWARD_MONTHS} міс. "
            f"(до {new_expiry.strftime('%d.%m.%Y')})."
        )
    return f"🎁 Запрошувач <code>{inviter_id}</code> отримав лише подяку (пакет чи безстроковий доступ)."


def menu_buttons(lang: str) -> list:
    """Кнопка меню жилья «🎁 Запроси друга» (пусто, если функция выключена)."""
    if not enabled():
        return []
    return [InlineKeyboardButton(i18n.t("housing.menu.btn.invite", lang), callback_data=CALLBACK)]


def menu_text_and_keyboard(user_id: int):
    lang = i18n.get_lang(user_id)
    link = link_for(user_id)
    back = [InlineKeyboardButton(i18n.t("housing.btn.back_to_monitor", lang), callback_data="housing:menu")]
    if not link:
        return i18n.t("pay.ref.no_link", lang), InlineKeyboardMarkup([back])
    lines = [i18n.t("pay.ref.menu", lang, link=html.escape(link))]
    invited, paid = housing_pay_store.referral_counts(user_id)
    if invited:
        lines.append("")
        lines.append(i18n.t("pay.ref.invited", lang, n=invited))
        if paid:
            lines.append(i18n.t("pay.ref.paid", lang, n=paid))
    share = "https://t.me/share/url?url=%s&text=%s" % (quote(link, safe=""), quote(i18n.t("pay.ref.share_text", lang)))
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(i18n.t("pay.ref.btn.share", lang), url=share)],
        back,
    ])
    return "\n".join(lines), keyboard


def show_menu(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    user = update.effective_user
    if not query or not user:
        return
    if not enabled():
        query.answer()
        return
    text, keyboard = menu_text_and_keyboard(user.id)
    query.answer()
    query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard, disable_web_page_preview=True)
