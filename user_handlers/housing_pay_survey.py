"""Опрос: «✍️ Написати докладніше» (25.11.2026).

После отрицательного ответа в опросе («незручно / незрозуміло» и любое его
уточнение, а также «приходить замало») человеку предлагается написать
своими словами, что именно. По нажатию бот ждёт ОДИН текст (до 24 часов,
отмена кнопкой), сохраняет его (housing_survey_more), пересылает админу с
пометкой, из какого ответа опроса он вырос, и благодарит.

Ожидание текста встроено так же, как остальные диалоги бота (флаг в
context.user_data): оно не перехватывает чек оплаты, свою сумму доната,
анкету письма, мастер фильтра и обратную связь - если любой из них открыт или
человек нажал кнопку нижнего меню, ожидание тихо снимается.

Отключить: HOUSING_SURVEY_MORE_ENABLED=0.
"""

import html
import logging
import os
from datetime import datetime, timedelta
from typing import Callable, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackContext

import i18n
from user_jobs import housing_pay_store

logger = logging.getLogger(__name__)

STATE_KEY = "pay_more"
WAIT = timedelta(hours=24)
MIN_LENGTH = 3
MAX_LENGTH = 1500
CALLBACK = "housing:survey_more"
CANCEL_CALLBACK = "housing:survey_more_cancel"
# Диалоги, которые тоже ждут текст: пока любой открыт, наше ожидание уступает.
OTHER_STATE_KEYS = (
    "feedback", "anonymous", "housing_admin", "housing_access_admin", "housing_broadcast", "housing_clone_source",
)


def enabled() -> bool:
    return (os.getenv("HOUSING_SURVEY_MORE_ENABLED", "1") or "1").strip() != "0"


def is_negative(answer: str) -> bool:
    """Отрицательные ответы: hard (и любое уточнение hard:<код>) и few."""
    answer = str(answer or "")
    return answer == "few" or answer == "hard" or answer.startswith("hard:")


def button_row(lang: str) -> list:
    return [InlineKeyboardButton(i18n.t("pay.survey.btn_more", lang), callback_data=CALLBACK)]


def extend_keyboard(keyboard: Optional[InlineKeyboardMarkup], lang: str) -> Optional[InlineKeyboardMarkup]:
    """Клавиатура ответа опроса + кнопка «Написати докладніше» (если функция включена)."""
    if not enabled():
        return keyboard
    rows = [list(row) for row in keyboard.inline_keyboard] if keyboard is not None else []
    rows.append(button_row(lang))
    return InlineKeyboardMarkup(rows)


def _cancel_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(i18n.t("pay.survey.btn_cancel", lang), callback_data=CANCEL_CALLBACK)]])


def _utc_now() -> datetime:
    return datetime.utcnow()


def start(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    user = update.effective_user
    if not query or not user:
        return
    lang = i18n.get_lang(user.id)
    if not enabled():
        query.answer()
        return
    answer = housing_pay_store.survey_answer(user.id)
    from user_jobs import housing_text_wait
    housing_text_wait.claim(context.user_data, STATE_KEY)
    context.user_data[STATE_KEY] = {"at": _utc_now(), "answer": answer}
    query.answer()
    context.bot.send_message(
        chat_id=int(user.id), text=i18n.t("pay.survey.more_prompt", lang), reply_markup=_cancel_keyboard(lang),
    )


def cancel(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    user = update.effective_user
    if not query or not user:
        return
    context.user_data.pop(STATE_KEY, None)
    query.answer()
    try:
        query.edit_message_text(i18n.t("pay.survey.more_cancelled", i18n.get_lang(user.id)))
    except Exception:
        logger.debug("Could not edit the cancelled survey prompt", exc_info=True)


def _other_dialog_open(context: CallbackContext) -> bool:
    data = context.user_data
    if any(data.get(key) for key in OTHER_STATE_KEYS):
        return True
    if (data.get("hcard") or {}).get("input"):
        return True
    try:
        from user_handlers import housing_letter, support

        if data.get(housing_letter.STATE_KEY) or data.get(support._STATE_KEY):
            return True
    except Exception:
        logger.debug("Could not read the other dialog states", exc_info=True)
    return False


def _answer_label(answer: str) -> str:
    """Из какого ответа опроса вырос текст - по-украински, для админа."""
    if not answer:
        return "—"
    if answer.startswith("hard:"):
        detail = answer.split(":", 1)[1]
        return f"{i18n.t('housing.survey.opt.hard', 'uk')} → {i18n.t(f'housing.survey.hard.opt.{detail}', 'uk')}"
    return i18n.t(f"housing.survey.opt.{answer}", "uk")


def handle_text(update: Update, context: CallbackContext, is_menu_text: Optional[Callable[[str], bool]] = None) -> bool:
    """Один текст после «Написати докладніше». False - ожидания нет, оно
    истекло, уступило другому диалогу или это кнопка меню (тогда текст
    обрабатывают остальные)."""
    state = context.user_data.get(STATE_KEY) if context.user_data is not None else None
    if not state or not update.message or not update.message.text or not update.effective_user:
        return False
    if _utc_now() - state["at"] > WAIT or not enabled():
        context.user_data.pop(STATE_KEY, None)
        return False
    text = update.message.text.strip()
    if _other_dialog_open(context) or (is_menu_text is not None and is_menu_text(text)):
        context.user_data.pop(STATE_KEY, None)
        return False
    user = update.effective_user
    lang = i18n.get_lang(user.id)
    if len(text) < MIN_LENGTH:
        update.message.reply_text(i18n.t("pay.survey.more_prompt", lang), reply_markup=_cancel_keyboard(lang))
        return True
    text = text[:MAX_LENGTH]
    context.user_data.pop(STATE_KEY, None)
    answer = str(state.get("answer") or "")
    housing_pay_store.save_survey_more(user.id, answer, text)
    update.message.reply_text(i18n.t("pay.survey.more_thanks", lang))
    _forward_to_admin(context.bot, user, answer, text)
    return True


def _forward_to_admin(bot, user, answer: str, text: str) -> None:
    admin_id = int(os.getenv("ADMIN_ID", "0") or 0)
    if not admin_id:
        return
    from user_handlers import housing_monitor

    try:
        bot.send_message(
            chat_id=admin_id,
            text=(
                f"📝 <b>Опитування, докладніше</b> (з відповіді: {html.escape(_answer_label(answer))})\n"
                f"👤 {html.escape(housing_monitor._display_name(user))} · <code>{int(user.id)}</code>\n\n"
                f"{html.escape(text)}"
            ),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("💌 Написати", url=f"tg://user?id={int(user.id)}"),
            ]]),
        )
    except Exception:
        logger.exception("Could not forward the written survey answer of %s", user.id)


def handle_callback(update: Update, context: CallbackContext) -> bool:
    query = update.callback_query
    if not query or not query.data:
        return False
    if query.data == CALLBACK:
        start(update, context)
        return True
    if query.data == CANCEL_CALLBACK:
        cancel(update, context)
        return True
    return False
