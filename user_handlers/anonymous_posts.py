"""Anonymous forum posts with captcha, cooldown, undo, and reply notifications."""

import html
import logging
import os
import random
import secrets
import sqlite3
from datetime import datetime, timedelta

from sqlalchemy.exc import IntegrityError
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.ext import CallbackContext, CallbackQueryHandler, CommandHandler, Filters, MessageHandler

import i18n
from database import AnonymousPost, AnonymousTopic, AnonymousUser, Chat, DBSession, Message, User
from user_handlers.anonymous_validation import (
    cooldown_text,
    message_link,
    text_fingerprint,
    validate_submission as validate_submission_text,
)
from user_handlers import equeue_monitor, housing_monitor
from user_jobs import user_settings_store
from user_jobs.reindex_queue import enqueue_message_reindex


logger = logging.getLogger(__name__)

ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
TARGET_CHAT_ID = int(os.getenv("ANON_TARGET_CHAT_ID", "0") or 0)
TOPIC_SOURCE_DB = os.getenv("ANON_TOPIC_SOURCE_DB", "").strip()
COOLDOWN_DAYS = max(1, int(os.getenv("ANON_COOLDOWN_DAYS", "7") or 7))
DELETE_MINUTES = max(1, int(os.getenv("ANON_DELETE_MINUTES", "60") or 60))
MIN_LENGTH = max(1, int(os.getenv("ANON_MIN_LENGTH", "15") or 15))
MAX_LENGTH = min(3500, max(MIN_LENGTH, int(os.getenv("ANON_MAX_LENGTH", "1500") or 1500)))
CAPTCHA_LOCK_MINUTES = 15
TOPICS_PER_PAGE = 8
BTN_HOME = "🏠 Меню"
BTN_EQUEUE = "🛂 ДП Документ"
BTN_HOUSING = "🏠 Моніторинг житла"
BTN_FEEDBACK = "💬 Зворотній звʼязок"
FEEDBACK_MIN_LENGTH = 5
FEEDBACK_MAX_LENGTH = 2000

def utc_now() -> datetime:
    return datetime.utcnow()


def validate_submission(text: str, lang: str = "uk"):
    return validate_submission_text(text, MIN_LENGTH, MAX_LENGTH, lang)


def _home_keyboard(user_id=None) -> InlineKeyboardMarkup:
    lang = i18n.get_lang(user_id) if user_id else "uk"
    rows = [
        [InlineKeyboardButton(i18n.t("anon.btn.menu", lang), callback_data="anon:menu")],
    ]
    rows.extend(equeue_monitor.private_home_rows(user_id))
    rows.extend(housing_monitor.private_home_rows(user_id))
    rows.append([InlineKeyboardButton(i18n.t("anon.btn.feedback", lang), callback_data="anon:feedback")])
    rows.append([InlineKeyboardButton("🌐 Мова / Язык / Sprache", callback_data="anon:lang:menu")])
    return InlineKeyboardMarkup(rows)


def _anon_submenu_keyboard(lang: str = "uk") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(i18n.t("anon.btn.ask_question", lang), callback_data="anon:new")],
        [InlineKeyboardButton(i18n.t("anon.btn.my_posts", lang), callback_data="anon:mine")],
        [InlineKeyboardButton(i18n.t("anon.btn.back_home", lang), callback_data="anon:home")],
    ])


def show_anon_submenu(update: Update, context: CallbackContext, edit: bool = False) -> None:
    user_id = update.effective_user.id if update.effective_user else None
    lang = i18n.get_lang(user_id) if user_id else "uk"
    text = i18n.t("anon.submenu.text", lang)
    keyboard = _anon_submenu_keyboard(lang)
    query = update.callback_query
    if edit and query:
        query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)
    else:
        update.effective_message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)


def _lang_picker_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(i18n.LANG_LABELS[code], callback_data=f"anon:lang:set:{code}")]
        for code in i18n.SUPPORTED_LANGS
    ]
    rows.append([InlineKeyboardButton("⬅ Назад / Назад / Zurück", callback_data="anon:home")])
    return InlineKeyboardMarkup(rows)


def reply_menu_keyboard(user_id=None) -> ReplyKeyboardMarkup:
    lang = i18n.get_lang(user_id) if user_id else "uk"
    rows = [[i18n.t("anon.btn.home", lang), i18n.t("anon.btn.menu", lang)]]
    if equeue_monitor.is_allowed(user_id):
        rows.append([i18n.t("anon.btn.equeue", lang)])
    # Shown to everyone, allowed or not: housing_monitor.show_menu() renders
    # its own locked screen (pricing + "request access") for people without
    # access yet, same as the top inline menu already does.
    rows.append([i18n.t("anon.btn.housing", lang)])
    rows.append([i18n.t("anon.btn.feedback", lang)])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=False)


def _cancel_keyboard(lang: str = "uk") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(i18n.t("anon.btn.cancel", lang), callback_data="anon:cancel")],
    ])


def _feedback_cancel_keyboard(lang: str = "uk") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(i18n.t("anon.btn.cancel", lang), callback_data="anon:feedback_cancel")],
    ])


def start_feedback(update: Update, context: CallbackContext) -> None:
    """Одне повідомлення від людини йде особисто адміністратору.

    Раніше зв'язатися з адміном можна було лише випадково натрапивши на нього
    в чаті — жодного окремого каналу для скарги, ідеї чи помилки не було.
    """
    context.user_data["feedback"] = {"step": "text"}
    lang = i18n.get_lang(update.effective_user.id) if update.effective_user else "uk"
    text = i18n.t("anon.feedback.intro", lang)
    query = update.callback_query
    if query:
        query.answer()
        query.edit_message_text(text, parse_mode="HTML", reply_markup=_feedback_cancel_keyboard(lang))
    else:
        update.effective_message.reply_text(text, parse_mode="HTML", reply_markup=_feedback_cancel_keyboard(lang))


def _handle_feedback_text(update: Update, context: CallbackContext) -> None:
    text = (update.message.text or "").strip()
    user = update.effective_user
    lang = i18n.get_lang(user.id) if user else "uk"
    if len(text) < FEEDBACK_MIN_LENGTH:
        update.message.reply_text(
            i18n.t("anon.feedback.too_short", lang, n=FEEDBACK_MIN_LENGTH),
            reply_markup=_feedback_cancel_keyboard(lang),
        )
        return
    text = text[:FEEDBACK_MAX_LENGTH]
    context.user_data.pop("feedback", None)
    username = getattr(user, "username", None) if user else None
    sender = f"@{username}" if username else (getattr(user, "full_name", None) or "Невідомо")
    user_id = user.id if user else 0
    delivered = False
    if ADMIN_ID:
        try:
            context.bot.send_message(
                ADMIN_ID,
                f"💬 <b>Зворотній звʼязок</b>\nВід: {html.escape(sender)} (ID {user_id})\n\n{html.escape(text)}",
                parse_mode="HTML",
            )
            delivered = True
        except Exception:
            logger.exception("Could not forward feedback to admin")
    reply = i18n.t("anon.feedback.delivered", lang) if delivered else i18n.t("anon.feedback.failed", lang)
    update.message.reply_text(reply, reply_markup=_home_keyboard(user_id))


def cancel_feedback(update: Update, context: CallbackContext) -> None:
    context.user_data.pop("feedback", None)
    show_home(update, context, edit=True)


def _main_menu_keyboard(lang: str = "uk") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(i18n.t("anon.btn.back_home", lang), callback_data="anon:home")],
    ])


def _target_chat_id(session) -> int:
    if TARGET_CHAT_ID:
        return TARGET_CHAT_ID
    chat = session.query(Chat).filter(Chat.enable == 1).order_by(Chat.id).first()
    return int(chat.id) if chat else 0


def _sync_topics_from_ad_bot() -> int:
    """Import bound topics from the advertising bot's read-only SQLite database."""
    if not TOPIC_SOURCE_DB or not os.path.isfile(TOPIC_SOURCE_DB):
        return 0
    try:
        source = sqlite3.connect(f"file:{TOPIC_SOURCE_DB}?mode=ro", uri=True)
        try:
            rows = source.execute(
                "SELECT name, chat_id, message_thread_id FROM topics ORDER BY name"
            ).fetchall()
        finally:
            source.close()
    except (OSError, sqlite3.Error) as exc:
        logger.warning("Could not import anonymous topics from %s: %s", TOPIC_SOURCE_DB, exc)
        return 0
    for name, chat_id, thread_id in rows:
        _upsert_topic(int(chat_id), int(thread_id or 0), str(name))
    return len(rows)


def _get_or_create_user(session, telegram_user) -> AnonymousUser:
    now = utc_now()
    row = session.query(AnonymousUser).get(telegram_user.id)
    if row is None:
        row = AnonymousUser(
            user_id=telegram_user.id,
            username=telegram_user.username or "",
            display_name=telegram_user.full_name or "",
            is_blocked=False,
            captcha_failures=0,
            created_at=now,
            updated_at=now,
        )
        session.add(row)
    else:
        row.username = telegram_user.username or ""
        row.display_name = telegram_user.full_name or ""
        row.updated_at = now
    session.flush()
    return row


def _cooldown_text(user: AnonymousUser, lang: str = "uk") -> str:
    return cooldown_text(user.last_submission_at, COOLDOWN_DAYS, utc_now(), lang)


def show_home(update: Update, context: CallbackContext, edit: bool = False) -> None:
    """Show the anonymous posting landing screen in a private chat."""
    context.user_data.pop("anonymous", None)
    user_id = update.effective_user.id if update.effective_user else None
    lang = i18n.get_lang(user_id) if user_id else "uk"
    text = i18n.t("anon.home.text", lang, days=COOLDOWN_DAYS)
    if edit and update.callback_query:
        update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=_home_keyboard(user_id))
    else:
        update.effective_message.reply_text(
            i18n.t("anon.home.quick_menu_note", lang),
            reply_markup=reply_menu_keyboard(user_id),
        )
        update.effective_message.reply_text(text, parse_mode="HTML", reply_markup=_home_keyboard(user_id))


def _new_captcha(context: CallbackContext, lang: str = "uk") -> InlineKeyboardMarkup:
    left = random.randint(2, 9)
    right = random.randint(1, 9)
    answer = left + right
    token = secrets.token_hex(3)
    choices = {answer}
    while len(choices) < 4:
        choices.add(max(1, answer + random.randint(-5, 5)))
    values = list(choices)
    random.shuffle(values)
    state = context.user_data.setdefault("anonymous", {})
    state.update({"step": "captcha", "captcha_token": token, "captcha_answer": answer})
    state["captcha_question"] = f"{left} + {right}"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(str(value), callback_data=f"anon:captcha:{token}:{value}") for value in values[:2]],
        [InlineKeyboardButton(str(value), callback_data=f"anon:captcha:{token}:{value}") for value in values[2:]],
        [InlineKeyboardButton(i18n.t("anon.btn.cancel", lang), callback_data="anon:cancel")],
    ])


def _show_captcha(query, context: CallbackContext, prefix: str = "", lang: str = "uk") -> None:
    keyboard = _new_captcha(context, lang)
    question = context.user_data["anonymous"]["captcha_question"]
    text = (prefix + "\n\n" if prefix else "") + i18n.t("anon.captcha.question", lang, question=question)
    query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)


def _topics_keyboard(session, page: int = 0, lang: str = "uk") -> InlineKeyboardMarkup:
    chat_id = _target_chat_id(session)
    topics = (
        session.query(AnonymousTopic)
        .filter(AnonymousTopic.chat_id == chat_id, AnonymousTopic.is_active == 1)
        .order_by(AnonymousTopic.name)
        .all()
    )
    page_count = max(1, (len(topics) + TOPICS_PER_PAGE - 1) // TOPICS_PER_PAGE)
    page = max(0, min(page, page_count - 1))
    visible = topics[page * TOPICS_PER_PAGE:(page + 1) * TOPICS_PER_PAGE]
    rows = [[InlineKeyboardButton(topic.name[:55], callback_data=f"anon:topic:{topic.id}")] for topic in visible]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅", callback_data=f"anon:topics:{page - 1}"))
    if page + 1 < page_count:
        nav.append(InlineKeyboardButton("➡", callback_data=f"anon:topics:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(i18n.t("anon.btn.cancel", lang), callback_data="anon:cancel")])
    return InlineKeyboardMarkup(rows)


def _show_topics(query, context: CallbackContext, page: int = 0) -> None:
    _sync_topics_from_ad_bot()
    session = DBSession()
    try:
        chat_id = _target_chat_id(session)
        count = session.query(AnonymousTopic).filter(
            AnonymousTopic.chat_id == chat_id,
            AnonymousTopic.is_active == 1,
        ).count()
        lang = i18n.get_lang(query.from_user.id)
        if not chat_id or not count:
            query.edit_message_text(
                i18n.t("anon.topics.not_loaded", lang),
                reply_markup=_main_menu_keyboard(lang),
            )
            return
        context.user_data.setdefault("anonymous", {})["step"] = "topic"
        query.edit_message_text(i18n.t("anon.topics.pick", lang), reply_markup=_topics_keyboard(session, page, lang))
    finally:
        session.close()


def _start_new(query, context: CallbackContext) -> None:
    session = DBSession()
    try:
        user = _get_or_create_user(session, query.from_user)
        session.commit()
        lang = i18n.get_lang(query.from_user.id)
        if user.is_blocked:
            query.answer(i18n.t("anon.new.blocked", lang), show_alert=True)
            return
        if user.captcha_locked_until and user.captcha_locked_until > utc_now():
            minutes = max(1, int((user.captcha_locked_until - utc_now()).total_seconds() // 60) + 1)
            query.answer(i18n.t("anon.new.too_many_failures", lang, n=minutes), show_alert=True)
            return
        cooldown = _cooldown_text(user, lang)
        if cooldown:
            query.answer(cooldown, show_alert=True)
            return
        context.user_data["anonymous"] = {"submit_token": secrets.token_urlsafe(12)}
        _show_captcha(query, context, lang=lang)
    finally:
        session.close()


def _handle_captcha(query, context: CallbackContext, parts) -> None:
    state = context.user_data.get("anonymous") or {}
    lang = i18n.get_lang(query.from_user.id)
    if len(parts) != 4 or state.get("step") != "captcha" or parts[2] != state.get("captcha_token"):
        query.answer(i18n.t("anon.captcha.expired", lang), show_alert=True)
        return
    try:
        selected = int(parts[3])
    except ValueError:
        query.answer(i18n.t("anon.captcha.invalid_answer", lang), show_alert=True)
        return

    session = DBSession()
    try:
        user = _get_or_create_user(session, query.from_user)
        if selected != int(state.get("captcha_answer", -1)):
            user.captcha_failures = int(user.captcha_failures or 0) + 1
            if user.captcha_failures >= 3:
                user.captcha_failures = 0
                user.captcha_locked_until = utc_now() + timedelta(minutes=CAPTCHA_LOCK_MINUTES)
                session.commit()
                context.user_data.pop("anonymous", None)
                query.edit_message_text(
                    i18n.t("anon.captcha.locked", lang, n=CAPTCHA_LOCK_MINUTES),
                    reply_markup=_main_menu_keyboard(lang),
                )
                return
            session.commit()
            _show_captcha(query, context, i18n.t("anon.captcha.wrong", lang), lang)
            return
        user.captcha_failures = 0
        user.captcha_locked_until = None
        user.captcha_passed_at = utc_now()
        session.commit()
        query.answer(i18n.t("anon.captcha.passed", lang))
        _show_topics(query, context)
    finally:
        session.close()


def _select_topic(query, context: CallbackContext, topic_id: int) -> None:
    session = DBSession()
    try:
        topic = session.query(AnonymousTopic).get(topic_id)
        lang = i18n.get_lang(query.from_user.id)
        if not topic or not topic.is_active or topic.chat_id != _target_chat_id(session):
            query.answer(i18n.t("anon.topic.gone", lang), show_alert=True)
            return
        state = context.user_data.setdefault("anonymous", {})
        state.update({"step": "text", "topic_id": topic.id, "topic_name": topic.name})
        query.edit_message_text(
            i18n.t("anon.topic.prompt", lang, topic=html.escape(topic.name), min=MIN_LENGTH, max=MAX_LENGTH),
            parse_mode="HTML",
            reply_markup=_cancel_keyboard(lang),
        )
    finally:
        session.close()


def _preview_text(state, lang: str = "uk") -> str:
    return i18n.t(
        "anon.preview.text", lang,
        topic=html.escape(state['topic_name']), text=html.escape(state['text']),
    )


def handle_private_text(update: Update, context: CallbackContext) -> None:
    if not update.message or update.message.chat.type != "private" or not update.message.text:
        return
    feedback_state = context.user_data.get("feedback") or {}
    if feedback_state.get("step") == "text":
        _handle_feedback_text(update, context)
        return
    state = context.user_data.get("anonymous") or {}
    text = update.message.text.strip()
    user_id = update.effective_user.id if update.effective_user else None
    lang = i18n.get_lang(user_id) if user_id else "uk"
    if state.get("step") != "text":
        if text == i18n.t("anon.btn.home", lang):
            show_home(update, context)
            return
        if text == i18n.t("anon.btn.feedback", lang):
            start_feedback(update, context)
            return
        if text == i18n.t("anon.btn.equeue", lang) and equeue_monitor.is_allowed(user_id):
            equeue_monitor.show_menu(update, context)
            return
        if text == i18n.t("anon.btn.housing", lang):
            housing_monitor.show_menu(update, context)
            return
        if housing_monitor.handle_private_text(update, context):
            return
        if text == i18n.t("anon.btn.menu", lang):
            show_anon_submenu(update, context)
            return
        show_home(update, context)
        return
    if state.get("step") != "text":
        return
    error = validate_submission(update.message.text, lang)
    if error:
        update.message.reply_text(i18n.t("anon.submit.validation_error", lang, error=error), reply_markup=_cancel_keyboard(lang))
        return

    session = DBSession()
    try:
        duplicate_after = utc_now() - timedelta(days=30)
        duplicate = session.query(AnonymousPost).filter(
            AnonymousPost.text_fingerprint == text_fingerprint(update.message.text),
            AnonymousPost.status.in_(("published", "deleted")),
            AnonymousPost.created_at >= duplicate_after,
        ).first()
        if duplicate:
            update.message.reply_text(
                i18n.t("anon.submit.duplicate", lang),
                reply_markup=_cancel_keyboard(lang),
            )
            return
    finally:
        session.close()

    state["text"] = update.message.text.strip()
    state["step"] = "confirm"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(i18n.t("anon.preview.btn_publish", lang), callback_data="anon:confirm")],
        [InlineKeyboardButton(i18n.t("anon.preview.btn_edit", lang), callback_data="anon:edit_text")],
        [InlineKeyboardButton(i18n.t("anon.preview.btn_change_topic", lang), callback_data="anon:change_topic")],
        [InlineKeyboardButton(i18n.t("anon.btn.cancel", lang), callback_data="anon:cancel")],
    ])
    update.message.reply_text(_preview_text(state, lang), parse_mode="HTML", reply_markup=keyboard)


def _message_link(message) -> str:
    return message_link(message)


def _reserve_post(query, state, lang: str = "uk"):
    session = DBSession()
    try:
        session.execute("BEGIN IMMEDIATE")
        user = _get_or_create_user(session, query.from_user)
        if user.is_blocked:
            session.rollback()
            return None, i18n.t("anon.new.blocked", lang)
        cooldown = _cooldown_text(user, lang)
        if cooldown:
            session.rollback()
            return None, cooldown
        topic = session.query(AnonymousTopic).get(int(state["topic_id"]))
        if not topic or not topic.is_active:
            session.rollback()
            return None, i18n.t("anon.reserve.topic_gone", lang)
        now = utc_now()
        post = AnonymousPost(
            submit_token=state["submit_token"],
            user_id=query.from_user.id,
            topic_id=topic.id,
            chat_id=topic.chat_id,
            message_thread_id=topic.message_thread_id,
            text=state["text"],
            text_fingerprint=text_fingerprint(state["text"]),
            status="pending",
            created_at=now,
            updated_at=now,
        )
        user.last_submission_at = now
        session.add(post)
        session.commit()
        return int(post.id), None
    except IntegrityError:
        session.rollback()
        existing = session.query(AnonymousPost).filter(
            AnonymousPost.submit_token == state.get("submit_token", "")
        ).first()
        if existing and existing.status == "published":
            return int(existing.id), i18n.t("anon.reserve.already_published", lang)
        return None, i18n.t("anon.reserve.processing", lang)
    finally:
        session.close()


def _release_failed_reservation(post_id: int, error: str) -> None:
    session = DBSession()
    try:
        post = session.query(AnonymousPost).get(post_id)
        if not post:
            return
        post.status = "failed"
        post.updated_at = utc_now()
        user = session.query(AnonymousUser).get(post.user_id)
        if user and user.last_submission_at == post.created_at:
            latest = session.query(AnonymousPost).filter(
                AnonymousPost.user_id == post.user_id,
                AnonymousPost.status.in_(("published", "deleted")),
            ).order_by(AnonymousPost.created_at.desc()).first()
            user.last_submission_at = latest.created_at if latest else None
        logger.warning("Anonymous post %s failed: %s", post_id, error)
        session.commit()
    finally:
        session.close()


def _finish_post(post_id: int, sent) -> AnonymousPost:
    session = DBSession()
    try:
        post = session.query(AnonymousPost).get(post_id)
        link = _message_link(sent)
        post.target_message_id = sent.message_id
        post.message_link = link
        post.can_delete_until = utc_now() + timedelta(minutes=DELETE_MINUTES)
        post.status = "published"
        post.updated_at = utc_now()
        indexed = session.query(Message).filter(
            Message.from_chat == post.chat_id,
            Message.id == sent.message_id,
        ).first()
        if indexed is None:
            bot_user_id = int(getattr(getattr(sent, "from_user", None), "id", 0) or 0)
            session.add(Message(
                id=sent.message_id,
                link=link,
                type="text",
                category="anonymous_question",
                text=post.text,
                text_lower=post.text.lower(),
                reply_to_msg_id=None,
                video="",
                photo="",
                audio="",
                voice="",
                date=getattr(sent, "date", utc_now()),
                from_id=bot_user_id,
                from_chat=post.chat_id,
            ))
            if bot_user_id and session.query(User).get(bot_user_id) is None:
                session.add(User(id=bot_user_id, fullname="Анонімний користувач", username=""))
        session.commit()
        session.refresh(post)
        session.expunge(post)
        return post
    finally:
        session.close()


def _notify_admin(context: CallbackContext, post: AnonymousPost, telegram_user) -> None:
    if not ADMIN_ID:
        return
    username = f"@{telegram_user.username}" if telegram_user.username else "без username"
    rows = []
    if post.message_link:
        rows.append([InlineKeyboardButton("🔗 Відкрити", url=post.message_link)])
    rows.append(
        [
            InlineKeyboardButton("🗑 Видалити", callback_data=f"anon:admin_delete:{post.id}"),
            InlineKeyboardButton("⛔ Видалити і заблокувати", callback_data=f"anon:admin_block:{post.id}"),
        ]
    )
    keyboard = InlineKeyboardMarkup(rows)
    try:
        context.bot.send_message(
            ADMIN_ID,
            "🛡 Новий анонімний пост\n\n"
            f"Автор: {html.escape(telegram_user.full_name or '—')} ({username}, ID <code>{telegram_user.id}</code>)\n"
            f"Текст: {html.escape(post.text[:500])}",
            parse_mode="HTML",
            reply_markup=keyboard,
        )
    except Exception:
        logger.exception("Could not notify admin about anonymous post %s", post.id)


def _publish(query, context: CallbackContext) -> None:
    state = context.user_data.get("anonymous") or {}
    lang = i18n.get_lang(query.from_user.id)
    if state.get("step") != "confirm" or not state.get("text") or not state.get("topic_id"):
        query.answer(i18n.t("anon.publish.stale_draft", lang), show_alert=True)
        return
    post_id, error = _reserve_post(query, state, lang)
    if error:
        query.answer(error, show_alert=True)
        if post_id:
            context.user_data.pop("anonymous", None)
        return
    query.answer(i18n.t("anon.publish.publishing_toast", lang))
    session = DBSession()
    try:
        post = session.query(AnonymousPost).get(post_id)
        # Posted into the shared group chat - stays in Ukrainian regardless of
        # the anonymous author's own language, since it's public-facing text
        # read by the whole chat, not a private reply to this one user.
        body = (
            "🙈 <b>Анонімне запитання</b>\n\n"
            f"{html.escape(post.text)}\n\n"
            "────────\n"
            "💬 Відповідьте на це повідомлення — автор отримає сповіщення."
        )
        kwargs = {}
        if post.message_thread_id:
            kwargs["message_thread_id"] = post.message_thread_id
        sent = context.bot.send_message(
            chat_id=post.chat_id,
            text=body,
            parse_mode="HTML",
            disable_web_page_preview=True,
            **kwargs,
        )
    except Exception as exc:
        _release_failed_reservation(post_id, str(exc))
        logger.exception("Could not publish anonymous post")
        query.edit_message_text(
            i18n.t("anon.publish.failed", lang),
            reply_markup=_main_menu_keyboard(lang),
        )
        return
    finally:
        session.close()

    post = _finish_post(post_id, sent)
    context.user_data.pop("anonymous", None)
    rows = []
    if post.message_link:
        rows.append([InlineKeyboardButton(i18n.t("anon.publish.btn_open", lang), url=post.message_link)])
    rows.append([InlineKeyboardButton(i18n.t("anon.publish.btn_delete", lang), callback_data=f"anon:delete:{post.id}")])
    rows.append([InlineKeyboardButton(i18n.t("anon.btn.back_home", lang), callback_data="anon:home")])
    query.edit_message_text(
        i18n.t("anon.publish.success", lang, days=COOLDOWN_DAYS),
        reply_markup=InlineKeyboardMarkup(rows),
    )
    _notify_admin(context, post, query.from_user)


def _delete_post(query, context: CallbackContext, post_id: int, admin: bool = False, block: bool = False) -> None:
    session = DBSession()
    indexed_message_pk = None
    try:
        post = session.query(AnonymousPost).get(post_id)
        lang = i18n.get_lang(query.from_user.id)
        if not post:
            query.answer(i18n.t("anon.delete.not_found", lang), show_alert=True)
            return
        if not admin and post.user_id != query.from_user.id:
            query.answer(i18n.t("anon.delete.no_access", lang), show_alert=True)
            return
        if admin and query.from_user.id != ADMIN_ID:
            query.answer(i18n.t("anon.delete.no_access", lang), show_alert=True)
            return
        if post.status == "deleted":
            query.answer(i18n.t("anon.delete.already_deleted", lang), show_alert=True)
            return
        if not admin and (not post.can_delete_until or post.can_delete_until < utc_now()):
            query.answer(i18n.t("anon.delete.too_late", lang), show_alert=True)
            return
        try:
            context.bot.delete_message(post.chat_id, post.target_message_id)
        except Exception as exc:
            query.answer(i18n.t("anon.delete.failed", lang, error=exc), show_alert=True)
            return
        post.status = "deleted"
        post.deleted_at = utc_now()
        post.updated_at = utc_now()
        indexed = session.query(Message).filter(
            Message.from_chat == post.chat_id,
            Message.id == post.target_message_id,
        ).first()
        if indexed:
            indexed.text = "[Видалене анонімне запитання]"
            indexed.text_lower = indexed.text.lower()
            indexed.category = "anonymous_deleted"
            indexed_message_pk = indexed._id
        if block:
            user = session.query(AnonymousUser).get(post.user_id)
            if user:
                user.is_blocked = True
                user.updated_at = utc_now()
        session.commit()
        if indexed_message_pk is not None:
            enqueue_message_reindex(post.chat_id, indexed_message_pk)
        query.answer(
            i18n.t("anon.delete.toast_blocked", lang) if block else i18n.t("anon.delete.toast", lang),
            show_alert=True,
        )
        query.edit_message_text(
            i18n.t("anon.delete.success_blocked", lang) if block else i18n.t("anon.delete.success", lang),
            reply_markup=_main_menu_keyboard(lang) if not admin else None,
        )
    finally:
        session.close()


def _show_my_posts(query) -> None:
    session = DBSession()
    try:
        lang = i18n.get_lang(query.from_user.id)
        posts = session.query(AnonymousPost).filter(
            AnonymousPost.user_id == query.from_user.id,
            AnonymousPost.status.in_(("published", "deleted")),
        ).order_by(AnonymousPost.created_at.desc()).limit(5).all()
        if not posts:
            query.edit_message_text(i18n.t("anon.myposts.empty", lang), reply_markup=_main_menu_keyboard(lang))
            return
        rows = []
        lines = [i18n.t("anon.myposts.header", lang), ""]
        for post in posts:
            status = i18n.t("anon.myposts.status_deleted", lang) if post.status == "deleted" else i18n.t("anon.myposts.status_published", lang)
            lines.append(f"#{post.id} · {status} · {post.created_at.strftime('%d.%m.%Y %H:%M')}")
            lines.append(html.escape(post.text[:100]))
            lines.append("")
            if post.status == "published" and post.message_link:
                rows.append([InlineKeyboardButton(i18n.t("anon.myposts.btn_open", lang, id=post.id), url=post.message_link)])
            if post.status == "published" and post.can_delete_until and post.can_delete_until >= utc_now():
                rows.append([InlineKeyboardButton(i18n.t("anon.myposts.btn_delete", lang, id=post.id), callback_data=f"anon:delete:{post.id}")])
        rows.append([InlineKeyboardButton(i18n.t("anon.btn.back_home", lang), callback_data="anon:home")])
        query.edit_message_text("\n".join(lines), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))
    finally:
        session.close()


def handle_callback(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    if not query or not query.data:
        return
    data = query.data
    parts = data.split(":")
    if data == "anon:home" or data == "anon:cancel":
        query.answer()
        show_home(update, context, edit=True)
    elif data == "anon:menu":
        query.answer()
        show_anon_submenu(update, context, edit=True)
    elif data == "anon:lang:menu":
        query.answer()
        query.edit_message_text(
            "🌐 Оберіть мову / Выберите язык / Sprache wählen",
            reply_markup=_lang_picker_keyboard(),
        )
    elif data.startswith("anon:lang:set:"):
        lang = parts[3]
        if lang in i18n.SUPPORTED_LANGS:
            user_settings_store.set_language(query.from_user.id, lang)
            query.answer(i18n.LANG_LABELS[lang])
        else:
            query.answer()
        show_home(update, context, edit=True)
    elif data == "anon:feedback":
        start_feedback(update, context)
    elif data == "anon:feedback_cancel":
        query.answer()
        cancel_feedback(update, context)
    elif data == "anon:new":
        _start_new(query, context)
    elif data.startswith("anon:captcha:"):
        _handle_captcha(query, context, parts)
    elif data.startswith("anon:topics:"):
        query.answer()
        _show_topics(query, context, int(parts[2]))
    elif data.startswith("anon:topic:"):
        query.answer()
        _select_topic(query, context, int(parts[2]))
    elif data == "anon:edit_text":
        state = context.user_data.get("anonymous") or {}
        state["step"] = "text"
        query.answer()
        lang = i18n.get_lang(query.from_user.id)
        query.edit_message_text(i18n.t("anon.edit.prompt", lang), reply_markup=_cancel_keyboard(lang))
    elif data == "anon:change_topic":
        query.answer()
        _show_topics(query, context)
    elif data == "anon:confirm":
        _publish(query, context)
    elif data == "anon:mine":
        query.answer()
        _show_my_posts(query)
    elif data.startswith("anon:delete:"):
        _delete_post(query, context, int(parts[2]))
    elif data.startswith("anon:admin_delete:"):
        _delete_post(query, context, int(parts[2]), admin=True)
    elif data.startswith("anon:admin_block:"):
        _delete_post(query, context, int(parts[2]), admin=True, block=True)


def bind_topic(update: Update, context: CallbackContext) -> None:
    """Bind or rename the current forum topic; admin-only."""
    message = update.message
    if not message or message.from_user.id != ADMIN_ID:
        return
    if message.chat.type != "supergroup":
        message.reply_text("Команду потрібно надіслати всередині теми супергрупи.")
        return
    name = " ".join(context.args).strip()
    if not name:
        message.reply_text("Використання: /anon_topic Назва теми")
        return
    thread_id = int(message.message_thread_id or 0)
    _upsert_topic(message.chat_id, thread_id, name)
    message.reply_text(f"✅ Тема «{name}» доступна для анонімних запитань.")


def list_topics(update: Update, context: CallbackContext) -> None:
    if not update.message or update.message.from_user.id != ADMIN_ID:
        return
    imported = _sync_topics_from_ad_bot()
    session = DBSession()
    try:
        chat_id = _target_chat_id(session)
        topics = session.query(AnonymousTopic).filter(
            AnonymousTopic.chat_id == chat_id,
            AnonymousTopic.is_active == 1,
        ).order_by(AnonymousTopic.name).all()
        lines = ["📌 Теми анонімних запитань:"]
        if imported:
            lines.append(f"Синхронізовано з рекламного бота: {imported}.")
        lines.extend(f"• {topic.name} — thread_id={topic.message_thread_id}" for topic in topics)
        update.message.reply_text("\n".join(lines) if topics else "Тем поки не знайдено.")
    finally:
        session.close()


def reset_user(update: Update, context: CallbackContext) -> None:
    if not update.message or update.message.from_user.id != ADMIN_ID:
        return
    if len(context.args) != 1 or not context.args[0].isdigit():
        update.message.reply_text("Використання: /anon_reset USER_ID")
        return
    session = DBSession()
    try:
        user = session.query(AnonymousUser).get(int(context.args[0]))
        if not user:
            update.message.reply_text("Користувача не знайдено.")
            return
        user.is_blocked = False
        user.last_submission_at = None
        user.captcha_failures = 0
        user.captcha_locked_until = None
        user.updated_at = utc_now()
        session.commit()
        update.message.reply_text("✅ Блокування та тижневий ліміт скинуто.")
    finally:
        session.close()


def _upsert_topic(chat_id: int, thread_id: int, name: str) -> None:
    session = DBSession()
    try:
        now = utc_now()
        topic = session.query(AnonymousTopic).filter(
            AnonymousTopic.chat_id == chat_id,
            AnonymousTopic.message_thread_id == thread_id,
        ).first()
        if topic is None:
            topic = AnonymousTopic(
                chat_id=chat_id,
                message_thread_id=thread_id,
                name=name[:100],
                is_active=True,
                created_at=now,
                updated_at=now,
            )
            session.add(topic)
        else:
            if name and not name.startswith("Тема #"):
                topic.name = name[:100]
            topic.is_active = True
            topic.updated_at = now
        session.commit()
    except IntegrityError:
        session.rollback()
    finally:
        session.close()


def observe_forum(update: Update, context: CallbackContext) -> None:
    """Discover active forum topics and notify anonymous authors about replies."""
    message = update.effective_message
    if not message or message.chat.type != "supergroup":
        return
    session = DBSession()
    try:
        target_chat_id = _target_chat_id(session)
    finally:
        session.close()
    if target_chat_id and message.chat_id != target_chat_id:
        return

    thread_id = int(message.message_thread_id or 0)
    if message.is_topic_message or message.forum_topic_created:
        created = message.forum_topic_created
        name = created.name if created and created.name else f"Тема #{thread_id}"
        _upsert_topic(message.chat_id, thread_id, name)

    if not message.reply_to_message or not message.from_user or message.from_user.is_bot:
        return
    reply_to_id = message.reply_to_message.message_id
    session = DBSession()
    try:
        post = session.query(AnonymousPost).filter(
            AnonymousPost.chat_id == message.chat_id,
            AnonymousPost.target_message_id == reply_to_id,
            AnonymousPost.status == "published",
        ).first()
        if not post:
            return
        user_id = post.user_id
    finally:
        session.close()
    lang = i18n.get_lang(user_id)
    preview = (message.text or message.caption or i18n.t("anon.reply_notify.media_fallback", lang)).strip()[:500]
    link = _message_link(message)
    rows = [[InlineKeyboardButton(i18n.t("anon.reply_notify.btn_open", lang), url=link)]] if link else []
    try:
        context.bot.send_message(
            user_id,
            i18n.t("anon.reply_notify.text", lang, preview=html.escape(preview)),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(rows) if rows else None,
        )
    except Exception:
        logger.exception("Could not notify anonymous author %s about reply", user_id)


private_start_handler = CommandHandler("anonymous", show_home, Filters.chat_type.private)
bind_topic_handler = CommandHandler("anon_topic", bind_topic)
list_topics_handler = CommandHandler("anon_topics", list_topics)
reset_user_handler = CommandHandler("anon_reset", reset_user)
callback_handler = CallbackQueryHandler(handle_callback, pattern=r"^anon:")
private_text_handler = MessageHandler(Filters.chat_type.private & Filters.text & (~Filters.command), handle_private_text)
forum_observer_handler = MessageHandler(Filters.chat_type.supergroup, observe_forum)
