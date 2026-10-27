"""«☕ Підтримати проєкт» і «✅ Я записався» (27.10.2026).

Термін-боти (ДП Документ, посольство) лишаються безкоштовними. Під
сповіщенням про вільні місця є «✅ Я записався»: людина відписується від
сповіщень і в момент найбільшої вдячності бачить пропозицію підтримати
проєкт. Суми - в євро (1/2/3/5/10 або своя), переказ у гривнях на ту саму
карту, що й підписка на житло (housing_payment), за тим самим курсом.
Адмін отримує повідомлення, коли людина пише, що переказала.
"""

import html
import logging
import math
import re
from datetime import datetime, timedelta
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackContext, CallbackQueryHandler, CommandHandler, Filters

import i18n
from user_handlers import housing_payment

logger = logging.getLogger(__name__)

AMOUNTS_EUR = (1, 2, 3, 5, 10)
MAX_CUSTOM_EUR = 1000
# Скільки чекаємо на свою суму після «✏️ Своя сума»: пізніше число в чаті
# вже не вважаємо відповіддю.
CUSTOM_WINDOW = timedelta(minutes=30)
_STATE_KEY = "support_custom_at"


def enabled() -> bool:
    return housing_payment.enabled()


def _uah(cents: int) -> int:
    return int(math.ceil(cents * housing_payment.eur_uah_rate() / 100))


def _eur_text(cents: int) -> str:
    if cents % 100 == 0:
        return str(cents // 100)
    return f"{cents / 100:.2f}".replace(".", ",")


def _display_name(user) -> str:
    name = " ".join(part for part in (user.first_name or "", user.last_name or "") if part).strip()
    if user.username:
        name = f"{name} (@{user.username})".strip()
    return name[:120] or str(user.id)


def support_button(lang: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(i18n.t("support.btn.open", lang), callback_data="support:start")


def home_rows(user_id: Optional[int]) -> list:
    if not enabled():
        return []
    return [[support_button(i18n.get_lang(user_id) if user_id else "uk")]]


def _amounts_keyboard(lang: str) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(
            i18n.t("support.amount_btn", lang, eur=eur, uah=_uah(eur * 100)),
            callback_data=f"support:a:{eur * 100}",
        )
        for eur in AMOUNTS_EUR
    ]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows.append([InlineKeyboardButton(i18n.t("support.btn.custom", lang), callback_data="support:custom")])
    rows.append([InlineKeyboardButton(i18n.t("anon.btn.back_home", lang), callback_data="anon:home")])
    return InlineKeyboardMarkup(rows)


def _amounts_text(lang: str) -> str:
    return i18n.t("support.choose", lang, rate=housing_payment._format_rate(housing_payment.eur_uah_rate()))


def _card_text(cents: int, lang: str) -> str:
    holder = housing_payment.PAY_CARD_HOLDER
    return i18n.t(
        "support.card", lang,
        eur=_eur_text(cents), uah=_uah(cents),
        card=housing_payment._format_card(housing_payment.PAY_CARD),
        holder=i18n.t("housing.pay.holder", lang, holder=html.escape(holder)) if holder else "",
    )


def _card_keyboard(cents: int, lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(i18n.t("support.btn.done", lang), callback_data=f"support:done:{cents}")],
        [InlineKeyboardButton(i18n.t("support.btn.other", lang), callback_data="support:start")],
    ])


def show_amounts(update: Update, context: CallbackContext) -> None:
    user = update.effective_user
    lang = i18n.get_lang(user.id) if user else "uk"
    if not enabled():
        update.effective_message.reply_text(i18n.t("support.unavailable", lang))
        return
    context.user_data.pop(_STATE_KEY, None)
    query = update.callback_query
    if query:
        query.answer()
        query.edit_message_text(_amounts_text(lang), parse_mode="HTML", reply_markup=_amounts_keyboard(lang))
    else:
        update.effective_message.reply_text(_amounts_text(lang), parse_mode="HTML", reply_markup=_amounts_keyboard(lang))


def parse_eur(text: str) -> Optional[int]:
    """«4», «4,5», «4.50 €», «10 євро» -> центи; None, якщо це не сума."""
    match = re.fullmatch(r"\s*(\d{1,4})(?:[.,](\d{1,2}))?\s*(?:€|eur|euro|євро|евро|euro)?\s*", text.lower())
    if not match:
        return None
    cents = int(match.group(1)) * 100 + int((match.group(2) or "0").ljust(2, "0"))
    if cents < 100 or cents > MAX_CUSTOM_EUR * 100:
        return None
    return cents


def handle_text(update: Update, context: CallbackContext) -> bool:
    """Своя сума після «✏️ Своя сума». Все, що не схоже на суму, віддаємо
    іншим обробникам (кнопки меню тощо) і забуваємо про очікування."""
    asked_at = context.user_data.get(_STATE_KEY)
    if not asked_at or not update.message or not update.message.text:
        return False
    if datetime.utcnow() - asked_at > CUSTOM_WINDOW:
        context.user_data.pop(_STATE_KEY, None)
        return False
    cents = parse_eur(update.message.text)
    context.user_data.pop(_STATE_KEY, None)
    if cents is None:
        return False
    lang = i18n.get_lang(update.effective_user.id)
    update.message.reply_text(_card_text(cents, lang), parse_mode="HTML", reply_markup=_card_keyboard(cents, lang))
    return True


def _notify_admin_done(bot, user, cents: int) -> None:
    if not housing_payment.ADMIN_ID:
        return
    try:
        bot.send_message(
            chat_id=housing_payment.ADMIN_ID,
            text=(
                f"☕ <b>Підтримка проєкту</b>\n\n{html.escape(_display_name(user))} "
                f"(<code>{int(user.id)}</code>) пише, що переказав(ла) "
                f"<b>{_eur_text(cents)} € = {_uah(cents)} грн</b>."
            ),
            parse_mode="HTML",
        )
    except Exception:
        logger.exception("Could not tell the admin about support from %s", user.id)


def handle_callback(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    if not query or not query.data:
        return
    user = query.from_user
    lang = i18n.get_lang(user.id)
    parts = query.data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    if action == "start":
        show_amounts(update, context)
        return
    if not enabled():
        query.answer(i18n.t("support.unavailable", lang), show_alert=True)
        return
    if action == "custom":
        context.user_data[_STATE_KEY] = datetime.utcnow()
        query.answer()
        query.edit_message_text(
            i18n.t("support.custom_prompt", lang, max=MAX_CUSTOM_EUR),
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(i18n.t("support.btn.other", lang), callback_data="support:start"),
            ]]),
        )
        return
    try:
        cents = int(parts[2])
    except (IndexError, ValueError):
        query.answer()
        return
    if not 100 <= cents <= MAX_CUSTOM_EUR * 100:
        query.answer()
        return
    if action == "a":
        query.answer()
        query.edit_message_text(_card_text(cents, lang), parse_mode="HTML", reply_markup=_card_keyboard(cents, lang))
    elif action == "done":
        query.answer(i18n.t("support.thanks_toast", lang))
        query.edit_message_text(i18n.t("support.thanks", lang))
        _notify_admin_done(context.bot, user, cents)
    else:
        query.answer()


def booked(query, bot, service_title: str, menu_callback: str) -> None:
    """Людина натиснула «✅ Я записався» під сповіщенням: підписку на цей
    сервіс уже вимкнено (це робить викликач), тут - кнопки з повідомлення,
    вітання з пропозицією підтримати та рядок адміну."""
    user = query.from_user
    lang = i18n.get_lang(user.id)
    query.answer(i18n.t("support.booked_toast", lang))
    try:
        query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        logger.debug("Could not drop the buttons of a booked alert", exc_info=True)
    rows = []
    if enabled():
        rows.append([support_button(lang)])
    rows.append([InlineKeyboardButton(i18n.t("support.btn.resubscribe", lang), callback_data=menu_callback)])
    key = "support.booked" if enabled() else "support.booked_plain"
    try:
        bot.send_message(
            chat_id=user.id, text=i18n.t(key, lang, service=html.escape(service_title)),
            parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows),
        )
    except Exception:
        logger.exception("Could not congratulate %s on booking", user.id)
    if housing_payment.ADMIN_ID and int(user.id) != housing_payment.ADMIN_ID:
        try:
            bot.send_message(
                chat_id=housing_payment.ADMIN_ID,
                text=f"✅ {html.escape(_display_name(user))} (<code>{int(user.id)}</code>) "
                     f"записався через бота: {html.escape(service_title)}",
                parse_mode="HTML",
            )
        except Exception:
            logger.exception("Could not tell the admin that %s booked", user.id)


command_handler = CommandHandler("support", show_amounts, Filters.chat_type.private)
callback_handler = CallbackQueryHandler(handle_callback, pattern=r"^support:")
