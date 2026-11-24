"""«🏠 Я знайшов(ла) житло» (25.11.2026).

Підтвердження -> пошуки на паузу (фільтри вимикаються, нічого не видаляється),
вітання з блоком подяки/донату (як після «✅ Я записався» в термін-ботах),
подія `found` у housing_leave_event (піде у вечірнє зведення адміну, миттєво
адміну нічого). Підписку НЕ забираємо: людина може повернутись і зняти паузу.

`mark_found(user_id)` - окрема функція: її викликає і трекер заявок (потік B),
коли людина там відмічає «отримав квартиру».

Вимкнути: HOUSING_FOUND_ENABLED=0 (кнопки зникають; mark_found при цьому
працює, якщо її викликає інший код).
"""

import logging
import os
from typing import Dict

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackContext

import i18n
from user_jobs import housing_pay_store

logger = logging.getLogger(__name__)

CALLBACK_PREFIX = "housing:found"


def enabled() -> bool:
    return (os.getenv("HOUSING_FOUND_ENABLED", "1") or "1").strip() != "0"


def mark_found(user_id: int) -> Dict[str, object]:
    """Позначає, що людина знайшла житло: подія found, пошуки на паузу.
    Повертає {"paused": скільки записів фільтрів вимкнено, "had_filters": bool}."""
    from user_handlers import housing_leave, housing_monitor

    uid = int(user_id)
    had_filters = bool(housing_monitor.user_filters(uid))
    housing_leave.record(uid, "found", had_filters=had_filters)
    housing_pay_store.set_found(uid)
    paused = 0
    try:
        paused = housing_monitor._set_all_filters_active_for_user(uid, False)
    except Exception:
        logger.exception("Could not pause the searches of %s after 'found'", uid)
    return {"paused": paused, "had_filters": had_filters}


def resume(user_id: int) -> bool:
    """Знімає паузу: пошуки знову активні. False, якщо паузи й не було."""
    from user_handlers import housing_monitor

    uid = int(user_id)
    if not housing_pay_store.clear_found(uid):
        return False
    try:
        housing_monitor._set_all_filters_active_for_user(uid, True)
    except Exception:
        logger.exception("Could not resume the searches of %s", uid)
    return True


def is_found(user_id: int) -> bool:
    return housing_pay_store.is_found(int(user_id))


def _donation_rows(lang: str) -> list:
    from user_handlers import support

    if not support.enabled():
        return []
    amounts = [
        InlineKeyboardButton(
            i18n.t("support.amount_btn", lang, eur=eur, uah=support._uah(eur * 100)),
            callback_data=f"support:a:{eur * 100}",
        )
        for eur in support.BOOKED_AMOUNTS_EUR
    ]
    return [amounts[:3], amounts[3:], [InlineKeyboardButton(i18n.t("support.btn.custom", lang), callback_data="support:custom")]]


def _resume_row(lang: str) -> list:
    return [InlineKeyboardButton(i18n.t("pay.found.btn.resume", lang), callback_data=f"{CALLBACK_PREFIX}:resume")]


def congrats(bot, user_id: int) -> None:
    """Вітання + блок подяки (суми 1…5 € і «своя сума»)."""
    from user_handlers import support

    lang = i18n.get_lang(user_id)
    rows = _donation_rows(lang) + [_resume_row(lang)]
    key = "pay.found.congrats" if support.enabled() else "pay.found.congrats_plain"
    try:
        bot.send_message(chat_id=int(user_id), text=i18n.t(key, lang), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))
    except Exception:
        logger.exception("Could not congratulate %s on finding a flat", user_id)


def menu_rows(user_id: int, lang: str, premium: bool) -> list:
    """Строка меню жилья: «🏠 Я знайшов(ла) житло» (подписчикам) или, пока поиски
    на паузе, «▶️ Відновити пошуки». Кнопка «🎁 Запроси друга» стоит в одном
    ряду с «🌐 Сайт» (housing_referral.menu_buttons) - меню не должно расти."""
    if not enabled() or not premium:
        return []
    if is_found(user_id):
        return [[InlineKeyboardButton(i18n.t("housing.menu.btn.resume", lang), callback_data=f"{CALLBACK_PREFIX}:resume")]]
    return [[InlineKeyboardButton(i18n.t("housing.menu.btn.found", lang), callback_data=CALLBACK_PREFIX)]]


def free_resume_buttons(user_id: int, lang: str, premium: bool) -> list:
    """«▶️ Відновити пошуки» для тех, у кого нет подписки (подписчикам эта
    кнопка стоит в своей строке): встаёт в строку «🌐 Сайт» вместо «Запроси друга»."""
    if enabled() and not premium and is_found(user_id):
        return [InlineKeyboardButton(i18n.t("housing.menu.btn.resume", lang), callback_data=f"{CALLBACK_PREFIX}:resume")]
    return []


def ask(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    user = update.effective_user
    if not query or not user:
        return
    lang = i18n.get_lang(user.id)
    query.answer()
    query.edit_message_text(
        i18n.t("pay.found.confirm", lang), parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(i18n.t("pay.found.btn.yes", lang), callback_data=f"{CALLBACK_PREFIX}:yes")],
            [InlineKeyboardButton(i18n.t("pay.found.btn.no", lang), callback_data="housing:menu")],
        ]),
    )


def handle_callback(update: Update, context: CallbackContext) -> bool:
    """housing:found, housing:found:yes, housing:found:resume."""
    query = update.callback_query
    user = update.effective_user
    if not query or not query.data or not query.data.startswith(CALLBACK_PREFIX):
        return False
    if not user:
        return True
    parts = query.data.split(":")
    action = parts[2] if len(parts) > 2 else ""
    if not enabled():
        query.answer()
        return True
    if action == "":
        ask(update, context)
    elif action == "yes":
        query.answer()
        if is_found(user.id):
            # Двічі натиснули: вітання вже було.
            return True
        mark_found(user.id)
        try:
            query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            logger.debug("Could not drop the buttons of the 'found' confirmation", exc_info=True)
        congrats(context.bot, user.id)
    elif action == "resume":
        from user_handlers import housing_monitor

        lang = i18n.get_lang(user.id)
        if resume(user.id):
            query.answer(i18n.t("pay.found.resumed", lang), show_alert=True)
        else:
            query.answer()
        housing_monitor.show_menu(update, context, edit=False)
    else:
        query.answer()
    return True
