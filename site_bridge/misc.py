"""Мост: прочее. Файл принадлежит пакету H5 и общему каркасу; остальные пакеты сюда не пишут.

Операции (адрес POST /int/misc/<имя>):
  ping                      — проверка связи (без человека, ничего не пишет)
  me                        — язык человека из настроек бота и подписан ли он на новости (только чтение, строку настроек НЕ создаёт)
  news {on: bool}           — включить/выключить получение новостей бота (флаг news_subscribed)
  anon_info                 — анонимный вопрос: правила, лимиты, можно ли сейчас, темы, «мої запитання» (только чтение)
  anon_start                — начало (как кнопка «Поставити анонімне запитання»): проверки и капча
  anon_captcha {token, answer} — ответ на капчу; верно: ticket + темы; неверно: новая капча (passed=false); три ошибки: блокировка на 15 минут
  anon_check {ticket, topic_id, text} — правила текста и повтор за 30 дней, выбор темы; готово к предпросмотру
  anon_send {ticket}        — публикация ТЕМ ЖЕ путём, что в боте (anonymous_posts._publish): резерв лимита, пост в чат от имени бота,
                              «можно удалить в течение часа», уведомление админу. Модерации до публикации в боте нет, на сайте тоже
  anon_delete {id}          — удалить свой пост в пределах окна (anonymous_posts._delete_post)

Правила (длина, ссылки, повторы, кулдаун, капча, блокировка, тексты) остаются в user_handlers/anonymous_posts.py: здесь только вызовы и
перевод результата в JSON. Бот-функции написаны под Telegram-кнопки (query.answer / edit_message_text), поэтому им подаётся подставной
query, который записывает ответы; сам Telegram при этом не трогается, кроме публикации в чат (как в боте).
"""

import hmac
import html
import logging
import re
import threading
import time
from types import SimpleNamespace

import i18n
from site_bridge import op, Refuse

logger = logging.getLogger(__name__)

FLOW_TTL = 30 * 60          # сколько живёт начатый вопрос (капча -> текст -> публикация)
FLOW_MAX = 2000
POSTS_SHOWN = 5             # как в боте («Останні анонімні публікації»)
TEXT_SHOWN = 400            # сколько знаков своего вопроса показываем в списке
_FLOW = {}
_LOCK = threading.Lock()


@op("misc", "ping", need_user=False)
def ping(user_id, lang, params):
    """Проверка сквозной связи сайт -> мост -> бот. Ничего не читает и не пишет."""
    return {"pong": True, "ts": int(time.time()), "user_id": user_id, "lang": lang}


# ---------------------------------------------------------------- настройки человека

@op("misc", "me")
def me(user_id, lang, params):
    """Язык из настроек бота и подписка на новости. Читает строку напрямую: user_settings_store.get_language создаёт строку
    (и тем самым вносит человека в рассылку), а здесь это не нужно."""
    from database import DBSession, UserSettings

    session = DBSession()
    try:
        row = session.query(UserSettings).filter(UserSettings.user_id == int(user_id)).first()
        if row is None:
            return {"known": False, "lang": None, "news": True}
        return {"known": True, "lang": row.language if row.language in i18n.SUPPORTED_LANGS else None,
                "news": bool(row.news_subscribed)}
    finally:
        session.close()


@op("misc", "news")
def news(user_id, lang, params):
    from user_jobs import user_settings_store

    value = params.get("on")
    if not isinstance(value, bool):
        raise _bad(lang)
    user_settings_store.set_news_subscribed(int(user_id), value)
    return {"news": user_settings_store.get_news_subscribed(int(user_id))}


# ---------------------------------------------------------------- общее для анонимного вопроса

def _bad(lang):
    return Refuse("bad_request", i18n.t("ask.err.bad", lang))


def _plain(text) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", str(text or ""))).strip()


def _ap():
    from user_handlers import anonymous_posts

    return anonymous_posts


class _Query:
    """Подставной CallbackQuery: функции бота зовут answer/edit_message_text, мы только записываем."""

    def __init__(self, user):
        self.from_user = user
        self.answers = []
        self.edits = []
        self.markups = []
        self.data = ""
        self.message = None

    def answer(self, text=None, show_alert=False, **kwargs):
        self.answers.append(text or "")

    def edit_message_text(self, text=None, reply_markup=None, **kwargs):
        self.edits.append(text or "")
        self.markups.append(reply_markup)

    def last(self) -> str:
        return (self.answers[-1] if self.answers and self.answers[-1] else (self.edits[-1] if self.edits else ""))


def _who(user_id: int):
    """Объект «пользователь» (id, username, full_name): имя берём из того, что бот уже знает; нового человека пробуем спросить у Telegram."""
    from database import AnonymousUser, DBSession, User

    username, name = "", ""
    session = DBSession()
    try:
        row = session.query(AnonymousUser).get(user_id)
        if row is not None:
            username, name = row.username or "", row.display_name or ""
        else:
            known = session.query(User).filter(User.id == user_id).first()
            if known is not None:
                username, name = known.username or "", known.fullname or ""
    finally:
        session.close()
    if not username and not name:
        try:
            from site_bridge import bot

            chat = bot().get_chat(user_id)
            username = getattr(chat, "username", "") or ""
            name = getattr(chat, "full_name", "") or ""
        except Exception:
            logger.info("misc: could not read the profile of %s from Telegram", user_id)
    return SimpleNamespace(id=int(user_id), username=username, full_name=name, first_name=name, last_name="")


def _context(state_user_data=None):
    from site_bridge import bot

    return SimpleNamespace(user_data=state_user_data if state_user_data is not None else {}, bot=bot())


def _gate(user_id: int, lang: str):
    """Можно ли сейчас начать вопрос: (код, текст) или None. Только чтение; те же условия и тексты, что в anonymous_posts._start_new."""
    from database import AnonymousUser, DBSession

    ap = _ap()
    session = DBSession()
    try:
        row = session.query(AnonymousUser).get(user_id)
        if row is None:
            return None
        if row.is_blocked:
            return "blocked", i18n.t("anon.new.blocked", lang)
        now = ap.utc_now()
        if row.captcha_locked_until and row.captcha_locked_until > now:
            minutes = max(1, int((row.captcha_locked_until - now).total_seconds() // 60) + 1)
            return "locked", i18n.t("anon.new.too_many_failures", lang, n=minutes)
        cooldown = ap._cooldown_text(row, lang)
        if cooldown:
            return "cooldown", cooldown
        return None
    finally:
        session.close()


def _topics(sync: bool = False):
    from database import AnonymousTopic, DBSession

    ap = _ap()
    if sync:
        ap._sync_topics_from_ad_bot()
    session = DBSession()
    try:
        chat_id = ap._target_chat_id(session)
        if not chat_id:
            return []
        rows = (session.query(AnonymousTopic)
                .filter(AnonymousTopic.chat_id == chat_id, AnonymousTopic.is_active == 1)
                .order_by(AnonymousTopic.name).all())
        return [{"id": int(t.id), "name": t.name} for t in rows if ap.is_named_topic(t)]
    finally:
        session.close()


def _posts(user_id: int, lang: str):
    from database import AnonymousPost, DBSession

    ap = _ap()
    session = DBSession()
    try:
        now = ap.utc_now()
        rows = (session.query(AnonymousPost)
                .filter(AnonymousPost.user_id == user_id, AnonymousPost.status.in_(("published", "deleted")))
                .order_by(AnonymousPost.created_at.desc()).limit(POSTS_SHOWN).all())
        out = []
        for post in rows:
            published = post.status == "published"
            left = 0
            if published and post.can_delete_until and post.can_delete_until >= now:
                left = max(1, int((post.can_delete_until - now).total_seconds() // 60) + 1)
            out.append({
                "id": int(post.id),
                "status": post.status,
                "status_text": i18n.t("anon.myposts.status_deleted" if not published else "anon.myposts.status_published", lang),
                "created": post.created_at.strftime("%d.%m.%Y %H:%M"),
                "text": (post.text or "")[:TEXT_SHOWN],
                "link": (post.message_link or "") if published else "",
                "can_delete": left > 0,
                "delete_left_min": left,
            })
        return out
    finally:
        session.close()


def _captcha_payload(ctx, lang: str):
    state = ctx.user_data.get("anonymous") or {}
    question = state.get("captcha_question", "")
    return {"token": state.get("captcha_token", ""), "question": question,
            "text": _plain(i18n.t("anon.captcha.question", lang, question=question)),
            "choices": list(state.get("_choices") or [])}


def _remember_choices(ctx, keyboard):
    """Варианты ответа капчи берём из клавиатуры, которую собрала функция бота (она их перемешивает)."""
    choices = []
    for row in keyboard.inline_keyboard:
        for button in row:
            data = button.callback_data or ""
            if data.startswith("anon:captcha:"):
                choices.append(int(data.split(":")[3]))
    ctx.user_data["anonymous"]["_choices"] = choices


def _flow(user_id: int):
    now = time.time()
    with _LOCK:
        for key in [k for k, v in _FLOW.items() if v["exp"] < now]:
            _FLOW.pop(key, None)
        entry = _FLOW.get(user_id)
        if entry is not None:
            entry["exp"] = now + FLOW_TTL
        return entry


def _flow_put(user_id: int, ctx):
    with _LOCK:
        if len(_FLOW) >= FLOW_MAX:
            oldest = min(_FLOW, key=lambda k: _FLOW[k]["exp"])
            _FLOW.pop(oldest, None)
        _FLOW[user_id] = {"ctx": ctx, "exp": time.time() + FLOW_TTL}


def _flow_drop(user_id: int):
    with _LOCK:
        _FLOW.pop(user_id, None)


def _need_flow(user_id: int, lang: str, ticket=None, steps=None):
    entry = _flow(user_id)
    state = entry["ctx"].user_data.get("anonymous") if entry else None
    if not state or (steps and state.get("step") not in steps):
        raise Refuse("stale", i18n.t("anon.publish.stale_draft", lang))
    if ticket is not None and (not isinstance(ticket, str) or not hmac.compare_digest(ticket.encode(), str(state.get("submit_token", "")).encode())):
        raise Refuse("stale", i18n.t("anon.publish.stale_draft", lang))
    return entry["ctx"], state


def _text_param(params, lang):
    text = params.get("text")
    if not isinstance(text, str) or len(text) > 6000:
        raise _bad(lang)
    return text


def _int_param(params, name, lang):
    value = params.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0 or value > 2 ** 31:
        raise _bad(lang)
    return value


# ---------------------------------------------------------------- анонимный вопрос

@op("misc", "anon_info")
def anon_info(user_id, lang, params):
    ap = _ap()
    gate = _gate(user_id, lang)
    topics = _topics()
    return {
        "state": gate[0] if gate else "ok",
        "message": gate[1] if gate else "",
        "intro": _plain(i18n.t("anon.home.text", lang, days=ap.COOLDOWN_DAYS)).split("\n", 1)[-1].strip(),
        "rules": i18n.t("ask.rules", lang, min=ap.MIN_LENGTH, max=ap.MAX_LENGTH),
        "tips": [i18n.t("ask.tip1", lang), i18n.t("ask.tip2", lang), i18n.t("ask.tip3", lang)],
        "min": ap.MIN_LENGTH, "max": ap.MAX_LENGTH,
        "cooldown_days": ap.COOLDOWN_DAYS, "delete_minutes": ap.DELETE_MINUTES,
        "topics": len(topics),
        "no_topics": "" if topics else i18n.t("ask.no_topics", lang),
        "posts": _posts(user_id, lang),
    }


@op("misc", "anon_start")
def anon_start(user_id, lang, params):
    ap = _ap()
    gate = _gate(user_id, lang)
    if gate:
        raise Refuse(gate[0], gate[1])
    if not _topics(sync=False) and not _topics(sync=True):
        raise Refuse("no_topics", i18n.t("ask.no_topics", lang))
    user = _who(user_id)
    ctx = _context()
    q = _Query(user)
    ap._start_new(q, ctx)           # те же проверки и капча, что у кнопки в боте
    state = ctx.user_data.get("anonymous")
    if not state or state.get("step") != "captcha":
        raise Refuse("refused", q.last() or i18n.t("ask.err.bad", lang))
    _remember_choices(ctx, q.markups[-1])               # варианты ответа: числа с кнопок, которые собрала функция бота
    _flow_put(user_id, ctx)
    return {"captcha": _captcha_payload(ctx, lang)}


@op("misc", "anon_captcha")
def anon_captcha(user_id, lang, params):
    ap = _ap()
    token = params.get("token")
    answer = params.get("answer")
    if not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{6}", token) or isinstance(answer, bool) or not isinstance(answer, int):
        raise _bad(lang)
    entry = _flow(user_id)
    if not entry:
        raise Refuse("stale", i18n.t("anon.captcha.expired", lang))
    ctx = entry["ctx"]
    state = ctx.user_data.get("anonymous") or {}
    if state.get("step") != "captcha" or token != state.get("captcha_token"):
        raise Refuse("stale", i18n.t("anon.captcha.expired", lang))
    q = _Query(_who(user_id))
    ap._handle_captcha(q, ctx, ["anon", "captcha", token, str(answer)])
    after = ctx.user_data.get("anonymous")
    if after is None:                                    # три ошибки подряд: блокировка
        _flow_drop(user_id)
        raise Refuse("locked", i18n.t("anon.captcha.locked", lang, n=ap.CAPTCHA_LOCK_MINUTES))
    if after.get("step") == "topic":                     # верно, темы загружены
        return {"passed": True, "ticket": after["submit_token"], "topics": _topics()}
    if after.get("captcha_token") and after.get("captcha_token") != token:     # неверно: функция бота выдала новую капчу
        _remember_choices(ctx, q.markups[-1])
        return {"passed": False, "message": i18n.t("anon.captcha.wrong", lang), "captcha": _captcha_payload(ctx, lang)}
    raise Refuse("no_topics", i18n.t("ask.no_topics", lang))


def _duplicate(text: str) -> bool:
    from database import AnonymousPost, DBSession
    from datetime import timedelta as td
    from user_handlers.anonymous_validation import text_fingerprint

    ap = _ap()
    session = DBSession()
    try:
        return session.query(AnonymousPost).filter(
            AnonymousPost.text_fingerprint == text_fingerprint(text),
            AnonymousPost.status.in_(("published", "deleted")),
            AnonymousPost.created_at >= ap.utc_now() - td(days=30),
        ).first() is not None
    finally:
        session.close()


@op("misc", "anon_check")
def anon_check(user_id, lang, params):
    ap = _ap()
    text = _text_param(params, lang)
    topic_id = _int_param(params, "topic_id", lang)
    ctx, state = _need_flow(user_id, lang, params.get("ticket"), steps=("topic", "text", "confirm"))
    error = ap.validate_submission(text, lang)
    if error:
        raise Refuse("invalid", error)
    if _duplicate(text):
        raise Refuse("duplicate", i18n.t("anon.submit.duplicate", lang))
    q = _Query(_who(user_id))
    ap._select_topic(q, ctx, topic_id)                  # проверка темы и запись выбора, как кнопка темы в боте
    state = ctx.user_data.get("anonymous") or {}
    if state.get("step") != "text" or state.get("topic_id") != topic_id:
        raise Refuse("topic_gone", i18n.t("anon.topic.gone", lang))
    state["text"] = text.strip()
    state["step"] = "confirm"
    return {"topic_id": topic_id, "topic": state.get("topic_name", ""), "text": state["text"],
            "note": i18n.t("ask.preview_note", lang, minutes=ap.DELETE_MINUTES)}


@op("misc", "anon_send")
def anon_send(user_id, lang, params):
    from database import AnonymousPost, DBSession

    ap = _ap()
    ctx, state = _need_flow(user_id, lang, params.get("ticket"), steps=("confirm",))
    gate = _gate(user_id, lang)                         # блокировка/кулдаун могли появиться за время заполнения
    if gate and gate[0] in ("blocked", "cooldown"):
        _flow_drop(user_id)
        raise Refuse(gate[0], gate[1])
    token = state["submit_token"]
    q = _Query(_who(user_id))
    ap._publish(q, ctx)                                 # резерв лимита, пост в чат, запись, уведомление админу: путь бота
    session = DBSession()
    try:
        post = session.query(AnonymousPost).filter(AnonymousPost.submit_token == token).first()
        status = post.status if post else None
        item = None
        if post is not None and status == "published":
            item = {"id": int(post.id), "link": post.message_link or "", "created": post.created_at.strftime("%d.%m.%Y %H:%M"),
                    "delete_left_min": ap.DELETE_MINUTES}
    finally:
        session.close()
    if item:
        _flow_drop(user_id)
        return {"post": item, "message": i18n.t("anon.publish.success", lang, days=ap.COOLDOWN_DAYS)}
    if status == "failed":
        _flow_drop(user_id)
        raise Refuse("failed", i18n.t("anon.publish.failed", lang))
    raise Refuse("refused", q.last() or i18n.t("anon.publish.failed", lang))


@op("misc", "anon_delete")
def anon_delete(user_id, lang, params):
    from database import AnonymousPost, DBSession

    ap = _ap()
    post_id = _int_param(params, "id", lang)
    session = DBSession()
    try:
        before = session.query(AnonymousPost).get(post_id)
        was_deleted = before is not None and before.status == "deleted"
    finally:
        session.close()
    ctx = _context()
    q = _Query(_who(user_id))
    ap._delete_post(q, ctx, post_id)                  # чужой/просроченный/уже удалённый отказывает сама функция бота
    session = DBSession()
    try:
        post = session.query(AnonymousPost).get(post_id)
        done = post is not None and post.user_id == user_id and post.status == "deleted" and not was_deleted
    finally:
        session.close()
    if not done:
        raise Refuse("refused", q.last() or i18n.t("anon.delete.failed", lang, error=""))
    return {"deleted": True, "message": i18n.t("anon.delete.success", lang)}


# ---------------------------------------------------------------- пакет H6: язык бота и «Написати нам»

FEEDBACK_PER_HOUR = 5
_feedback_log = {}


@op("misc", "lang_set")
def lang_set(user_id, lang, params):
    """Язык бота (сообщения, уведомления): то же, что кнопка «🌐 Мова» в боте (user_settings_store.set_language). Язык самой страницы не затрагивается."""
    from user_jobs import user_settings_store

    value = params.get("lang")
    if not isinstance(value, str) or value not in i18n.SUPPORTED_LANGS:
        raise _bad(lang)
    user_settings_store.set_language(int(user_id), value)
    return {"lang": value, "label": i18n.LANG_LABELS[value]}


@op("misc", "feedback")
def feedback(user_id, lang, params):
    """«💬 Написати нам»: одно сообщение лично админу, тот же текст и та же функция, что в боте (anonymous_posts._handle_feedback_text)."""
    ap = _ap()
    text = params.get("text")
    if not isinstance(text, str) or len(text) > 4 * ap.FEEDBACK_MAX_LENGTH:
        raise _bad(lang)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text).strip()
    if len(text) < ap.FEEDBACK_MIN_LENGTH:
        raise Refuse("too_short", i18n.t("anon.feedback.too_short", lang, n=ap.FEEDBACK_MIN_LENGTH))
    now = time.time()
    with _LOCK:
        recent = [t for t in _feedback_log.get(user_id, []) if now - t < 3600]
        if len(recent) >= FEEDBACK_PER_HOUR:
            raise Refuse("too_many", i18n.t("h6.feedback.rate", lang))
        recent.append(now)
        _feedback_log[user_id] = recent
        if len(_feedback_log) > 5000:
            _feedback_log.clear()
    replies = []
    message = SimpleNamespace(text=text, reply_text=lambda body, **kw: replies.append(body))
    update = SimpleNamespace(message=message, effective_user=_who(user_id))
    ap._handle_feedback_text(update, _context({"feedback": {"step": "text"}}))
    # функция бота отвечает на языке из настроек бота, а страница может быть на другом: сравниваем со всеми тремя
    sent = bool(replies) and replies[0] in {i18n.t("anon.feedback.delivered", code) for code in i18n.SUPPORTED_LANGS}
    if not sent:
        raise Refuse("failed", i18n.t("anon.feedback.failed", lang))
    return {"sent": True, "message": i18n.t("anon.feedback.delivered", lang)}
