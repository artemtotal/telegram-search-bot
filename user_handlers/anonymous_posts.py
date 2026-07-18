"""Anonymous forum posts with captcha, cooldown, undo, and reply notifications."""

import html
import logging
import os
import random
import secrets
import sqlite3
from datetime import datetime, timedelta

from sqlalchemy.exc import IntegrityError
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackContext, CallbackQueryHandler, CommandHandler, Filters, MessageHandler

from database import AnonymousPost, AnonymousTopic, AnonymousUser, Chat, DBSession, Message, User
from user_handlers.anonymous_validation import (
    cooldown_text,
    message_link,
    text_fingerprint,
    validate_submission as validate_submission_text,
)
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

def utc_now() -> datetime:
    return datetime.utcnow()


def validate_submission(text: str):
    return validate_submission_text(text, MIN_LENGTH, MAX_LENGTH)


def _home_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✍️ Задать анонимный вопрос", callback_data="anon:new")],
        [InlineKeyboardButton("📋 Мои публикации", callback_data="anon:mine")],
    ])


def _cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✖ Отмена", callback_data="anon:cancel")],
    ])


def _main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅ В главное меню", callback_data="anon:home")],
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


def _cooldown_text(user: AnonymousUser) -> str:
    return cooldown_text(user.last_submission_at, COOLDOWN_DAYS, utc_now())


def show_home(update: Update, context: CallbackContext, edit: bool = False) -> None:
    """Show the anonymous posting landing screen in a private chat."""
    context.user_data.pop("anonymous", None)
    text = (
        "🙈 <b>Анонимный вопрос в чате Потсдама</b>\n\n"
        "Выберите тему, напишите вопрос и подтвердите публикацию. "
        f"Размещать анонимный пост можно не чаще одного раза в {COOLDOWN_DAYS} дней.\n\n"
        "Для участников чата автор не отображается. Администратор сохраняет Telegram ID автора "
        "только для защиты от спама и нарушений. Ссылки и контактные данные запрещены."
    )
    if edit and update.callback_query:
        update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=_home_keyboard())
    else:
        update.effective_message.reply_text(text, parse_mode="HTML", reply_markup=_home_keyboard())


def _new_captcha(context: CallbackContext) -> InlineKeyboardMarkup:
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
        [InlineKeyboardButton("✖ Отмена", callback_data="anon:cancel")],
    ])


def _show_captcha(query, context: CallbackContext, prefix: str = "") -> None:
    keyboard = _new_captcha(context)
    question = context.user_data["anonymous"]["captcha_question"]
    text = (prefix + "\n\n" if prefix else "") + f"🛡 Проверка от спама: сколько будет <b>{question}</b>?"
    query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)


def _topics_keyboard(session, page: int = 0) -> InlineKeyboardMarkup:
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
    rows.append([InlineKeyboardButton("✖ Отмена", callback_data="anon:cancel")])
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
        if not chat_id or not count:
            query.edit_message_text(
                "Темы ещё не загружены. Бот добавляет их автоматически после новых сообщений в темах. "
                "Администратор также может выполнить /anon_topic Название внутри нужной темы.",
                reply_markup=_main_menu_keyboard(),
            )
            return
        context.user_data.setdefault("anonymous", {})["step"] = "topic"
        query.edit_message_text("📌 Выберите тему для вопроса:", reply_markup=_topics_keyboard(session, page))
    finally:
        session.close()


def _start_new(query, context: CallbackContext) -> None:
    session = DBSession()
    try:
        user = _get_or_create_user(session, query.from_user)
        session.commit()
        if user.is_blocked:
            query.answer("Публикация для вашего аккаунта заблокирована.", show_alert=True)
            return
        if user.captcha_locked_until and user.captcha_locked_until > utc_now():
            minutes = max(1, int((user.captcha_locked_until - utc_now()).total_seconds() // 60) + 1)
            query.answer(f"Слишком много ошибок. Попробуйте через {minutes} мин.", show_alert=True)
            return
        cooldown = _cooldown_text(user)
        if cooldown:
            query.answer(cooldown, show_alert=True)
            return
        context.user_data["anonymous"] = {"submit_token": secrets.token_urlsafe(12)}
        _show_captcha(query, context)
    finally:
        session.close()


def _handle_captcha(query, context: CallbackContext, parts) -> None:
    state = context.user_data.get("anonymous") or {}
    if len(parts) != 4 or state.get("step") != "captcha" or parts[2] != state.get("captcha_token"):
        query.answer("Проверка устарела. Начните заново.", show_alert=True)
        return
    try:
        selected = int(parts[3])
    except ValueError:
        query.answer("Некорректный ответ.", show_alert=True)
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
                    f"🛑 Слишком много неверных ответов. Попробуйте через {CAPTCHA_LOCK_MINUTES} минут.",
                    reply_markup=_main_menu_keyboard(),
                )
                return
            session.commit()
            _show_captcha(query, context, "❌ Неверно. Попробуйте ещё раз.")
            return
        user.captcha_failures = 0
        user.captcha_locked_until = None
        user.captcha_passed_at = utc_now()
        session.commit()
        query.answer("Проверка пройдена")
        _show_topics(query, context)
    finally:
        session.close()


def _select_topic(query, context: CallbackContext, topic_id: int) -> None:
    session = DBSession()
    try:
        topic = session.query(AnonymousTopic).get(topic_id)
        if not topic or not topic.is_active or topic.chat_id != _target_chat_id(session):
            query.answer("Тема больше недоступна.", show_alert=True)
            return
        state = context.user_data.setdefault("anonymous", {})
        state.update({"step": "text", "topic_id": topic.id, "topic_name": topic.name})
        query.edit_message_text(
            f"Тема: <b>{html.escape(topic.name)}</b>\n\n"
            f"Напишите вопрос одним сообщением — от {MIN_LENGTH} до {MAX_LENGTH} символов. "
            "Не добавляйте ссылки, @имена, e-mail и номера телефонов.",
            parse_mode="HTML",
            reply_markup=_cancel_keyboard(),
        )
    finally:
        session.close()


def _preview_text(state) -> str:
    return (
        f"📌 Тема: <b>{html.escape(state['topic_name'])}</b>\n\n"
        f"🙈 <b>Предпросмотр</b>\n\n{html.escape(state['text'])}\n\n"
        "После подтверждения вопрос сразу появится в чате. Удалить его через бот можно в течение часа."
    )


def handle_private_text(update: Update, context: CallbackContext) -> None:
    if not update.message or update.message.chat.type != "private" or not update.message.text:
        return
    state = context.user_data.get("anonymous") or {}
    if state.get("step") != "text":
        return
    error = validate_submission(update.message.text)
    if error:
        update.message.reply_text(f"❌ {error}\n\nИсправьте текст или отмените публикацию.", reply_markup=_cancel_keyboard())
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
                "❌ Такой же текст уже публиковался за последние 30 дней. Сформулируйте вопрос иначе.",
                reply_markup=_cancel_keyboard(),
            )
            return
    finally:
        session.close()

    state["text"] = update.message.text.strip()
    state["step"] = "confirm"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Опубликовать", callback_data="anon:confirm")],
        [InlineKeyboardButton("✏️ Изменить текст", callback_data="anon:edit_text")],
        [InlineKeyboardButton("📌 Выбрать другую тему", callback_data="anon:change_topic")],
        [InlineKeyboardButton("✖ Отмена", callback_data="anon:cancel")],
    ])
    update.message.reply_text(_preview_text(state), parse_mode="HTML", reply_markup=keyboard)


def _message_link(message) -> str:
    return message_link(message)


def _reserve_post(query, state):
    session = DBSession()
    try:
        session.execute("BEGIN IMMEDIATE")
        user = _get_or_create_user(session, query.from_user)
        if user.is_blocked:
            session.rollback()
            return None, "Публикация для вашего аккаунта заблокирована."
        cooldown = _cooldown_text(user)
        if cooldown:
            session.rollback()
            return None, cooldown
        topic = session.query(AnonymousTopic).get(int(state["topic_id"]))
        if not topic or not topic.is_active:
            session.rollback()
            return None, "Выбранная тема больше недоступна."
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
            return int(existing.id), "Этот вопрос уже опубликован."
        return None, "Запрос уже обрабатывается. Подождите несколько секунд."
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
                session.add(User(id=bot_user_id, fullname="Анонимный пользователь", username=""))
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
        rows.append([InlineKeyboardButton("🔗 Открыть", url=post.message_link)])
    rows.append(
        [
            InlineKeyboardButton("🗑 Удалить", callback_data=f"anon:admin_delete:{post.id}"),
            InlineKeyboardButton("⛔ Удалить и блокировать", callback_data=f"anon:admin_block:{post.id}"),
        ]
    )
    keyboard = InlineKeyboardMarkup(rows)
    try:
        context.bot.send_message(
            ADMIN_ID,
            "🛡 Новый анонимный пост\n\n"
            f"Автор: {html.escape(telegram_user.full_name or '—')} ({username}, ID <code>{telegram_user.id}</code>)\n"
            f"Текст: {html.escape(post.text[:500])}",
            parse_mode="HTML",
            reply_markup=keyboard,
        )
    except Exception:
        logger.exception("Could not notify admin about anonymous post %s", post.id)


def _publish(query, context: CallbackContext) -> None:
    state = context.user_data.get("anonymous") or {}
    if state.get("step") != "confirm" or not state.get("text") or not state.get("topic_id"):
        query.answer("Черновик устарел. Начните заново.", show_alert=True)
        return
    post_id, error = _reserve_post(query, state)
    if error:
        query.answer(error, show_alert=True)
        if post_id:
            context.user_data.pop("anonymous", None)
        return
    query.answer("Публикую…")
    session = DBSession()
    try:
        post = session.query(AnonymousPost).get(post_id)
        body = (
            "🙈 <b>Анонимный вопрос</b>\n\n"
            f"{html.escape(post.text)}\n\n"
            "────────\n"
            "💬 Ответьте на это сообщение — автор получит уведомление."
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
            "❌ Не удалось опубликовать вопрос. Лимит не списан — попробуйте ещё раз позже.",
            reply_markup=_main_menu_keyboard(),
        )
        return
    finally:
        session.close()

    post = _finish_post(post_id, sent)
    context.user_data.pop("anonymous", None)
    rows = []
    if post.message_link:
        rows.append([InlineKeyboardButton("🔗 Открыть вопрос", url=post.message_link)])
    rows.append([InlineKeyboardButton("🗑 Удалить в течение часа", callback_data=f"anon:delete:{post.id}")])
    rows.append([InlineKeyboardButton("⬅ В главное меню", callback_data="anon:home")])
    query.edit_message_text(
        "✅ Вопрос опубликован анонимно. Ответы на него будут приходить сюда.\n\n"
        f"Следующий анонимный пост можно разместить через {COOLDOWN_DAYS} дней.",
        reply_markup=InlineKeyboardMarkup(rows),
    )
    _notify_admin(context, post, query.from_user)


def _delete_post(query, context: CallbackContext, post_id: int, admin: bool = False, block: bool = False) -> None:
    session = DBSession()
    indexed_message_pk = None
    try:
        post = session.query(AnonymousPost).get(post_id)
        if not post:
            query.answer("Публикация не найдена.", show_alert=True)
            return
        if not admin and post.user_id != query.from_user.id:
            query.answer("Нет доступа.", show_alert=True)
            return
        if admin and query.from_user.id != ADMIN_ID:
            query.answer("Нет доступа.", show_alert=True)
            return
        if post.status == "deleted":
            query.answer("Публикация уже удалена.", show_alert=True)
            return
        if not admin and (not post.can_delete_until or post.can_delete_until < utc_now()):
            query.answer("Час для удаления уже прошёл. Обратитесь к администратору.", show_alert=True)
            return
        try:
            context.bot.delete_message(post.chat_id, post.target_message_id)
        except Exception as exc:
            query.answer(f"Не удалось удалить: {exc}", show_alert=True)
            return
        post.status = "deleted"
        post.deleted_at = utc_now()
        post.updated_at = utc_now()
        indexed = session.query(Message).filter(
            Message.from_chat == post.chat_id,
            Message.id == post.target_message_id,
        ).first()
        if indexed:
            indexed.text = "[Удалённый анонимный вопрос]"
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
        query.answer("Удалено" + (" и заблокировано" if block else ""), show_alert=True)
        query.edit_message_text(
            "🗑 Публикация удалена." + (" Автор заблокирован." if block else ""),
            reply_markup=_main_menu_keyboard() if not admin else None,
        )
    finally:
        session.close()


def _show_my_posts(query) -> None:
    session = DBSession()
    try:
        posts = session.query(AnonymousPost).filter(
            AnonymousPost.user_id == query.from_user.id,
            AnonymousPost.status.in_(("published", "deleted")),
        ).order_by(AnonymousPost.created_at.desc()).limit(5).all()
        if not posts:
            query.edit_message_text("У вас ещё нет анонимных публикаций.", reply_markup=_main_menu_keyboard())
            return
        rows = []
        lines = ["📋 <b>Последние анонимные публикации</b>", ""]
        for post in posts:
            status = "🗑 удалён" if post.status == "deleted" else "✅ опубликован"
            lines.append(f"#{post.id} · {status} · {post.created_at.strftime('%d.%m.%Y %H:%M')}")
            lines.append(html.escape(post.text[:100]))
            lines.append("")
            if post.status == "published" and post.message_link:
                rows.append([InlineKeyboardButton(f"🔗 Открыть #{post.id}", url=post.message_link)])
            if post.status == "published" and post.can_delete_until and post.can_delete_until >= utc_now():
                rows.append([InlineKeyboardButton(f"🗑 Удалить #{post.id}", callback_data=f"anon:delete:{post.id}")])
        rows.append([InlineKeyboardButton("⬅ В главное меню", callback_data="anon:home")])
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
        query.edit_message_text("Отправьте исправленный текст вопроса.", reply_markup=_cancel_keyboard())
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
        message.reply_text("Команду нужно отправить внутри темы супергруппы.")
        return
    name = " ".join(context.args).strip()
    if not name:
        message.reply_text("Использование: /anon_topic Название темы")
        return
    thread_id = int(message.message_thread_id or 0)
    _upsert_topic(message.chat_id, thread_id, name)
    message.reply_text(f"✅ Тема «{name}» доступна для анонимных вопросов.")


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
        lines = ["📌 Темы анонимных вопросов:"]
        if imported:
            lines.append(f"Синхронизировано из рекламного бота: {imported}.")
        lines.extend(f"• {topic.name} — thread_id={topic.message_thread_id}" for topic in topics)
        update.message.reply_text("\n".join(lines) if topics else "Темы пока не найдены.")
    finally:
        session.close()


def reset_user(update: Update, context: CallbackContext) -> None:
    if not update.message or update.message.from_user.id != ADMIN_ID:
        return
    if len(context.args) != 1 or not context.args[0].isdigit():
        update.message.reply_text("Использование: /anon_reset USER_ID")
        return
    session = DBSession()
    try:
        user = session.query(AnonymousUser).get(int(context.args[0]))
        if not user:
            update.message.reply_text("Пользователь не найден.")
            return
        user.is_blocked = False
        user.last_submission_at = None
        user.captcha_failures = 0
        user.captcha_locked_until = None
        user.updated_at = utc_now()
        session.commit()
        update.message.reply_text("✅ Блокировка и недельный лимит сброшены.")
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
    preview = (message.text or message.caption or "[медиа]").strip()[:500]
    link = _message_link(message)
    rows = [[InlineKeyboardButton("🔗 Открыть ответ", url=link)]] if link else []
    try:
        context.bot.send_message(
            user_id,
            "💬 <b>Новый ответ на ваш анонимный вопрос</b>\n\n" + html.escape(preview),
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
