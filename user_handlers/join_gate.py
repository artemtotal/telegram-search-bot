"""Перевірка нових учасників групи «Потсдам» замість LOLS (07.11.2026, Артём).

Людина натискає «Подати заявку» в групі (у налаштуваннях групи ввімкнено «Схвалення нових учасників», бот — адмін із правом
запрошувати). Telegram надсилає боту chat_join_request — і впродовж 5 хвилин бот може написати людині першим (так це робить
LOLS). Бот пише: привітання + правила → «✅ Я прочитав(ла) правила» → одне питання з варіантами → правильно: заявку схвалено,
людина бачить, що вміє бот (житло, пошук по чату, терміни) і кнопку меню; неправильно двічі або мовчить довше timeout_minutes —
заявку відхилено (fail_action = "keep" — лишити адмінам). Усі кроки — редагуванням того самого повідомлення: писати першим
бот може лише 5 хвилин, а редагувати своє — завжди.

Тексти правил, питання й привітання редагуємо без перезбирання: /app/config/join_gate.json (створюється з чернеткою при першому
запуску; мови uk/ru/de, мова — з Telegram людини). Кнопки й службові фрази — translations/*.json (jg.*).
Адміну (ADMIN_ID): /join_gate — стан і лічильники, /join_gate on|off — увімкнути/вимкнути, /join_gate reload — перечитати файл.
Миттєвих сповіщень адміну немає (з 01.11 — лише вечірні зведення).
"""

import json
import logging
import os
import random
import threading
import time
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import CallbackContext, CallbackQueryHandler, ChatJoinRequestHandler, CommandHandler

import i18n
from user_jobs import user_settings_store

logger = logging.getLogger(__name__)

CONFIG_PATH = os.getenv("JOIN_GATE_CONFIG", "/app/config/join_gate.json")
STATE_PATH = os.getenv("JOIN_GATE_STATE", "/app/config/join_gate_state.json")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
_lock = threading.Lock()

DEFAULT_CONFIG = {
    "enabled": True,
    "chats": [],              # порожньо — усі групи, де бот адмін і приходять заявки; інакше перелік id
    "timeout_minutes": 1440,  # мовчить довше — заявку відхилено (або лишено адмінам, див. fail_action)
    "max_attempts": 2,
    "fail_action": "decline",  # decline | keep
    "texts": {
        "uk": {
            "hello": "👋 Привіт! Ви подали заявку в групу «{chat}».\n\nЩоб вас прийняли, прочитайте, будь ласка, правила й дайте"
                     " відповідь на одне просте питання — це захист від спаму й шахраїв.",
            "rules": "📌 <b>Правила групи</b>\n1. Поважайте одне одного — без образ, політики й сварок.\n2. Без спаму й реклами."
                     " Оголошення про послуги — лише з дозволу адміністратора.\n3. Житло, робота, речі — коротко й по суті: місто"
                     " чи район, ціна, контакт.\n4. Не публікуйте особисті дані інших людей.\n5. Підозрілі пропозиції (передоплата,"
                     " «квартира без огляду») — повідомляйте адміністраторам.",
            "welcome": "🎉 Ласкаво просимо в групу!\n\nДо речі, я — бот групи і можу допомогти:\n🏠 стежити за новими квартирами"
                       " на порталах Потсдама й надсилати їх вам одразу;\n🔎 знайти відповідь в історії чату — просто напишіть"
                       " мені питання;\n📅 повідомляти про вільні терміни (посольство, ДП «Документ»).",
        },
        "ru": {
            "hello": "👋 Привет! Вы подали заявку в группу «{chat}».\n\nЧтобы вас приняли, прочитайте, пожалуйста, правила и"
                     " ответьте на один простой вопрос — это защита от спама и мошенников.",
            "rules": "📌 <b>Правила группы</b>\n1. Уважайте друг друга — без оскорблений, политики и ссор.\n2. Без спама и"
                     " рекламы. Объявления об услугах — только с разрешения администратора.\n3. Жильё, работа, вещи — коротко"
                     " и по делу: город или район, цена, контакт.\n4. Не публикуйте личные данные других людей.\n5. Подозрительные"
                     " предложения (предоплата, «квартира без осмотра») — сообщайте администраторам.",
            "welcome": "🎉 Добро пожаловать в группу!\n\nКстати, я — бот группы и могу помочь:\n🏠 следить за новыми квартирами"
                       " на порталах Потсдама и сразу присылать их вам;\n🔎 найти ответ в истории чата — просто напишите мне"
                       " вопрос;\n📅 сообщать о свободных терминах (посольство, ГП «Документ»).",
        },
        "de": {
            "hello": "👋 Hallo! Sie haben eine Beitrittsanfrage für die Gruppe „{chat}“ gestellt.\n\nBitte lesen Sie die Regeln"
                     " und beantworten Sie eine kurze Frage — das schützt vor Spam und Betrug.",
            "rules": "📌 <b>Gruppenregeln</b>\n1. Respektvoller Umgang — keine Beleidigungen, keine Politik, kein Streit.\n2. Kein"
                     " Spam, keine Werbung. Dienstleistungsangebote nur mit Erlaubnis der Admins.\n3. Wohnung, Arbeit, Sachen —"
                     " kurz und konkret: Ort oder Stadtteil, Preis, Kontakt.\n4. Keine persönlichen Daten anderer veröffentlichen.\n"
                     "5. Verdächtige Angebote (Vorkasse, „Wohnung ohne Besichtigung“) bitte den Admins melden.",
            "welcome": "🎉 Willkommen in der Gruppe!\n\nÜbrigens bin ich der Bot der Gruppe und helfe gern:\n🏠 neue Wohnungen auf"
                       " den Potsdamer Portalen beobachten und sofort schicken;\n🔎 Antworten im Chatverlauf finden — schreiben"
                       " Sie mir einfach Ihre Frage;\n📅 über freie Termine informieren (Botschaft, DP „Dokument“).",
        },
    },
    "questions": [
        {"q": {"uk": "Чи можна публікувати рекламу послуг без дозволу адміністратора?",
               "ru": "Можно ли публиковать рекламу услуг без разрешения администратора?",
               "de": "Darf man ohne Erlaubnis der Admins Werbung für Dienstleistungen posten?"},
         "options": {"uk": ["Ні, лише з дозволу", "Так, можна"], "ru": ["Нет, только с разрешения", "Да, можно"],
                     "de": ["Nein, nur mit Erlaubnis", "Ja, klar"]},
         "correct": 0},
        {"q": {"uk": "Вам пропонують квартиру з передоплатою без огляду. Що робити?",
               "ru": "Вам предлагают квартиру с предоплатой без осмотра. Что делать?",
               "de": "Jemand bietet eine Wohnung gegen Vorkasse ohne Besichtigung an. Was tun?"},
         "options": {"uk": ["Повідомити адміністраторів", "Одразу переказати гроші"],
                     "ru": ["Сообщить администраторам", "Сразу перевести деньги"],
                     "de": ["Den Admins melden", "Sofort Geld überweisen"]},
         "correct": 0},
    ],
}


# ---------- файли конфігурації та стану ----------

def load_config() -> dict:
    if not os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
                json.dump(DEFAULT_CONFIG, fh, ensure_ascii=False, indent=1)
        except OSError:
            logger.warning("join_gate: не вдалося створити %s", CONFIG_PATH)
        return DEFAULT_CONFIG
    try:
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            cfg = json.load(fh)
        return {**DEFAULT_CONFIG, **cfg}
    except (OSError, ValueError):
        logger.exception("join_gate: зіпсований %s — беру чернетку", CONFIG_PATH)
        return DEFAULT_CONFIG


def _load_state() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {"pending": {}, "stats": {}}


def _save_state(st: dict) -> None:
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(st, fh, ensure_ascii=False)
    os.replace(tmp, STATE_PATH)


def _bump(st: dict, what: str) -> None:
    day = datetime.now().strftime("%Y-%m-%d")
    st.setdefault("stats", {}).setdefault(day, {}).setdefault(what, 0)
    st["stats"][day][what] += 1
    for old in sorted(st["stats"])[:-30]:   # тримаємо 30 днів
        st["stats"].pop(old, None)


def _key(chat_id, user_id) -> str:
    return f"{chat_id}:{user_id}"


def _lang_of(user) -> str:
    code = (getattr(user, "language_code", "") or "").lower()
    return "ru" if code.startswith("ru") else "de" if code.startswith("de") else "uk"


def _text(cfg: dict, lang: str, name: str) -> str:
    texts = cfg.get("texts") or {}
    return (texts.get(lang) or {}).get(name) or (texts.get("uk") or {}).get(name) or ""


# ---------- крок 1: заявка → привітання й правила ----------

def on_join_request(update: Update, context: CallbackContext) -> None:
    req = update.chat_join_request
    if req is None:
        return
    cfg = load_config()
    if not cfg.get("enabled") or (cfg.get("chats") and req.chat.id not in cfg["chats"]):
        return
    user, lang = req.from_user, _lang_of(req.from_user)
    try:   # мова з Telegram — щоб і меню бота потім було рідною мовою (лише для нових людей)
        if user_settings_store.get_language(user.id) == "uk" and lang != "uk":
            user_settings_store.set_language(user.id, lang)
    except Exception:  # noqa: BLE001
        logger.exception("join_gate: мова")
    text = _text(cfg, lang, "hello").format(chat=req.chat.title or "") + "\n\n" + _text(cfg, lang, "rules")
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(i18n.t("jg.btn.read", lang), callback_data=f"jg:ok:{req.chat.id}")]])
    try:
        msg = context.bot.send_message(chat_id=user.id, text=text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)
    except TelegramError as ex:
        logger.warning("join_gate: не зміг написати %s: %s — заявка лишається адмінам", user.id, ex)
        return
    with _lock:
        st = _load_state()
        st.setdefault("pending", {})[_key(req.chat.id, user.id)] = {
            "chat": req.chat.id, "user": user.id, "lang": lang, "at": time.time(), "attempts": 0, "msg": msg.message_id,
            "name": " ".join(x for x in (user.first_name, user.last_name) if x)[:80], "title": req.chat.title or ""}
        _bump(st, "requests")
        _save_state(st)
    logger.info("join_gate: заявка %s → група %s", user.id, req.chat.id)


# ---------- кроки 2–3: правила прочитано → питання → відповідь ----------

def _ask(query, cfg: dict, p: dict, prefix: str = "") -> None:
    questions = cfg.get("questions") or []
    if not questions:   # питань немає — досить прочитати правила
        return _approve(query, cfg, p)
    qi = random.randrange(len(questions))
    q, lang = questions[qi], p["lang"]
    opts = list(enumerate((q.get("options") or {}).get(lang) or (q.get("options") or {}).get("uk") or []))
    random.shuffle(opts)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=f"jg:a:{p['chat']}:{qi}:{oi}")] for oi, label in opts])
    question = (q.get("q") or {}).get(lang) or (q.get("q") or {}).get("uk") or ""
    query.edit_message_text(prefix + i18n.t("jg.question", lang, q=question), parse_mode="HTML", reply_markup=kb)


def _approve(query, cfg: dict, p: dict) -> None:
    bot, lang = query.bot, p["lang"]
    try:
        bot.approve_chat_join_request(chat_id=p["chat"], user_id=p["user"])
    except TelegramError as ex:   # уже схвалили вручну / заявку відкликали
        logger.warning("join_gate: approve %s: %s", p["user"], ex)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(i18n.t("jg.btn.menu", lang), url=f"https://t.me/{bot.username}?start=hello")]])
    query.edit_message_text(i18n.t("jg.ok", lang) + "\n\n" + _text(cfg, lang, "welcome"), parse_mode="HTML", reply_markup=kb,
                            disable_web_page_preview=True)
    _done(p, "approved")


def _done(p: dict, what: str) -> None:
    with _lock:
        st = _load_state()
        st.get("pending", {}).pop(_key(p["chat"], p["user"]), None)
        _bump(st, what)
        _save_state(st)
    logger.info("join_gate: %s %s → %s", what, p["user"], p["chat"])


def on_callback(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    parts = (query.data or "").split(":")
    try:
        chat_id = int(parts[2])
    except (IndexError, ValueError):
        return query.answer()
    with _lock:
        p = _load_state().get("pending", {}).get(_key(chat_id, query.from_user.id))
    if not p:
        query.answer()
        return query.edit_message_reply_markup(reply_markup=None)
    cfg, lang = load_config(), p["lang"]
    query.answer()
    if parts[1] == "ok":
        return _ask(query, cfg, p)
    if parts[1] != "a" or len(parts) != 5:
        return
    questions = cfg.get("questions") or []
    qi, oi = int(parts[3]), int(parts[4])
    if qi < len(questions) and oi == int(questions[qi].get("correct", 0)):
        return _approve(query, cfg, p)
    p["attempts"] = int(p.get("attempts", 0)) + 1
    if p["attempts"] < int(cfg.get("max_attempts", 2)):
        with _lock:
            st = _load_state()
            if _key(chat_id, query.from_user.id) in st.get("pending", {}):
                st["pending"][_key(chat_id, query.from_user.id)]["attempts"] = p["attempts"]
                _save_state(st)
        return _ask(query, cfg, p, prefix=i18n.t("jg.wrong", lang) + "\n\n")
    if cfg.get("fail_action", "decline") == "decline":
        try:
            context.bot.decline_chat_join_request(chat_id=chat_id, user_id=p["user"])
        except TelegramError as ex:
            logger.warning("join_gate: decline %s: %s", p["user"], ex)
        query.edit_message_text(i18n.t("jg.fail", lang))
    else:
        query.edit_message_text(i18n.t("jg.wait_admin", lang))
    _done(p, "failed")


# ---------- мовчить довше timeout_minutes ----------

def expire_job(context: CallbackContext) -> None:
    cfg = load_config()
    limit = time.time() - int(cfg.get("timeout_minutes", 1440)) * 60
    with _lock:
        old = [p for p in _load_state().get("pending", {}).values() if p.get("at", 0) < limit]
    for p in old:
        if cfg.get("fail_action", "decline") == "decline":
            try:
                context.bot.decline_chat_join_request(chat_id=p["chat"], user_id=p["user"])
            except TelegramError as ex:
                logger.info("join_gate: decline (час минув) %s: %s", p["user"], ex)
            try:
                context.bot.edit_message_text(chat_id=p["user"], message_id=p["msg"], text=i18n.t("jg.expired", p["lang"]))
            except TelegramError:
                pass
        _done(p, "expired")


# ---------- адміну ----------

def admin_cmd(update: Update, context: CallbackContext) -> None:
    if not update.effective_user or update.effective_user.id != ADMIN_ID:
        return
    arg = (context.args or [""])[0].lower()
    if arg in ("on", "off"):
        cfg = load_config()
        cfg["enabled"] = arg == "on"
        with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, ensure_ascii=False, indent=1)
    cfg = load_config()
    with _lock:
        st = _load_state()
    days = sorted(st.get("stats", {}))[-7:]
    lines = [f"🚪 Перевірка нових учасників: {'увімкнена' if cfg.get('enabled') else 'ВИМКНЕНА'}",
             f"Групи: {', '.join(map(str, cfg.get('chats') or [])) or 'усі, де бот адмін'} · питань: {len(cfg.get('questions') or [])}"
             f" · спроб: {cfg.get('max_attempts')} · чекаємо {cfg.get('timeout_minutes')} хв · провал: {cfg.get('fail_action')}",
             f"Зараз на перевірці: {len(st.get('pending', {}))}", "", "Останні дні (заявки / прийнято / провал / мовчали):"]
    for d in days:
        s = st["stats"][d]
        lines.append(f"{d}: {s.get('requests', 0)} / {s.get('approved', 0)} / {s.get('failed', 0)} / {s.get('expired', 0)}")
    lines.append(f"\nТексти й питання — {CONFIG_PATH}. Команди: /join_gate on | off | reload")
    update.effective_message.reply_text("\n".join(lines))


join_request_handler = ChatJoinRequestHandler(on_join_request)
callback_handler = CallbackQueryHandler(on_callback, pattern=r"^jg:")
command_handler = CommandHandler("join_gate", admin_cmd)
