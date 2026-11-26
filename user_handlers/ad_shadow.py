"""«Тіньовий» підрахунок ділових оголошень повз оплату (26.11.2026, прохання Артема).

Бот НІЧОГО не пише в чат і авторам. Він лише лічить повідомлення, схожі на рекламу послуг і товарів:
є контакт або ціна, є пропозиція (не прохання й не питання), автор не адмін чату й не клієнт рекламного
бота, тема не «Барахолка». Зберігаємо тільки номери (чат, повідомлення, автор, тема) і посилання, тексти
не зберігаємо. Раз на добу (зведення адміну о 21:00, housing_monitor.daily_filter_summary_job) у зведення
додається один рядок: скільки схожих повідомлень і скільки авторів, плюс до п'яти посилань.

Приватний продаж однієї речі без ознак магазину («продам велосипед 25 євро») не рахуємо: це не бізнес-реклама.

Вимикач: AD_SHADOW_ENABLED або {"ad_shadow": false} у /app/config/bot_switches.json (типово увімкнено).
"""
import logging
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set

from telegram.ext import Filters, MessageHandler

from user_handlers import switches

logger = logging.getLogger(__name__)

DB_PATH = os.environ.get("AD_SHADOW_DB", "/app/config/ad_shadow.db")
ADBOT_DB = os.environ.get("ANON_TOPIC_SOURCE_DB", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)

KEEP_DAYS = 30
MAX_LINKS = 5
MIN_LEN = 40
MAX_LEN = 1800
CLIENTS_TTL = 600
TOPICS_TTL = 600
ADMINS_TTL = 3600
TS_FORMAT = "%Y-%m-%d %H:%M:%S"

_lock = threading.Lock()
_clients_cache = {"at": 0.0, "ids": None}      # type: Dict[str, object]
_topics_cache = {"at": 0.0, "names": None}     # type: Dict[str, object]
_admins_cache = {}                             # type: Dict[int, tuple]


def enabled() -> bool:
    return switches.flag("AD_SHADOW_ENABLED", "ad_shadow", True)


# --- правила: що схоже на рекламу ------------------------------------------------------

_PHONE_RE = re.compile(
    r"(?<![\w])(?:\+\s?\d[\d\s\-()]{8,}\d|0\d{2,4}[\s\-]?\d{3}[\s\-]?\d{2,4}[\s\-]?\d{0,4}|\(\d{3,4}\)\s?\d[\d\s\-]{5,})"
)
_HANDLE_RE = re.compile(
    r"(?<![\w/])@[A-Za-z]\w{4,31}\b|t\.me/|wa\.me/|instagram\.com/|(?:^|\s)inst(?:agram|a)?\s*[:@]|viber|вайбер|вайбері|"
    r"whats\s?app|ватсап|[\w.\-]+@[\w\-]+\.[a-z]{2,}",
    re.IGNORECASE,
)
_PRICE_RE = re.compile(
    r"\d[\d\s.,]*\s?(?:€|eur\b|euro|євро|евро|грн|uah|\$|usd)|(?:€|\$)\s?\d|\b(?:ціна|цiна|цена|вартість|стоимость|прайс|price)\b",
    re.IGNORECASE,
)
# Сильні ознаки пропозиції: людина сама пропонує послугу, набір, знижку
_STRONG_RE = re.compile(
    r"(?<![\w/])(?:пропоную|пропонуємо|пропонує|предлагаю|предлагаем|надаю|надаємо|надає|оказываю|оказываем|виконую|виконуємо|"
    r"запрошую|запрошуємо|приглашаю|приглашаем|набір на|набор на|розпочав набір|веду набір|продовжуємо набір|записую|"
    r"запис відкрито|записи открыты|запис відкритий|вільні вікна|вільні місця|є вільні|свободные окошки|свободные места|"
    r"послуги|услуги|на замовлення|під замовлення|під ключ|знижк|скидк|промокод|мы предлагаем|ми пропонуємо|"
    r"(?:допоможу|помогу) (?:з|с|вам|вас|оформ|забезпеч|отрим|получ|підібр|подобр)|"
    r"записуйтесь|записывайтесь|запис за номером|запись по)"
)
# Слабкі ознаки: назва фаху чи послуги; потрібен явний контакт
_WEAK_RE = re.compile(
    r"(?<!\w)(?:майстер|мастер|студі[яюї]|студи[яю]|салон|курси|курсы|репетитор|викладач|преподаватель|перевезення|перевозки|"
    r"вантажоперевезення|грузоперевозки|переїзд|прибирання|уборка|клінінг|ремонт|монтаж|манікюр|педикюр|маникюр|"
    r"стрижк|перукар|парикмахер|масаж|брови|вії|ресницы|депіляц|шугарінг|косметолог|макіяж|візаж|фотограф|відеозйомк|"
    r"перекладач|переводчик|присяжний|нотаріус|юрист|адвокат|бухгалтер|страхуванн|страховк|кейтеринг|торти|випічка|"
    r"rechtsanwalt|kanzlei|zahnarzt|steuerberater|umzug|reinigung|friseur|kosmetik|nagelstudio|"
    r"евакуатор|таксі|трансфер|мебел|меблі|шиномонтаж|автосервіс|автосервис)",
    re.IGNORECASE,
)
_SALE_RE = re.compile(r"(?:продам|продаю|продається|продаємо|продается|продаем|продажа)", re.IGNORECASE)
# Ознаки магазину: без них продаж однієї речі вважається приватним
_SHOP_RE = re.compile(
    r"(?:в наявності|в наличии|на замовлення|під замовлення|доставляю|доставляємо|опт\b|оптом|знижк|скидк|інтернет-магазин|"
    r"нова колекція|новая коллекция|прайс|асортимент|ассортимент|оригінал|замовляйте|заказывайте|партія|партия)",
    re.IGNORECASE,
)
# Прохання, питання, «шукаю/потрібен»: це не пропозиція
_ASK_RE = re.compile(
    r"(?<!\w)(?:підкаж|подскаж|порад|посовет|порекоменд|рекоменд|хто знає|кто знает|хто може|кто может|хто робить|кто делает|"
    r"хто займається|де знайти|где найти|де можна|где можно|чи є |чи може|чи можна|есть ли|є хтось|есть кто|шукаю|шукаємо|ищу|ищем|"
    r"нужен|нужна|нужны|потрібен|потрібна|потрібні|потрібно|треба|допоможіть|помогите|можливо хтось|можливо, хтось|"
    r"яка (?:приблизна )?ціна|яка вартість|скільки коштує|скільки варт|сколько стоит|за скільки|за сколько|"
    r"хто-небудь|хто-нибудь|кого можете|кому потрібно)",
    re.IGNORECASE,
)
# Безкоштовне, збір допомоги, пошук речей: не комерційна реклама
_NONCOMM_RE = re.compile(
    r"(?<!\w)(?:безкоштовно|бесплатно|віддам|отдам|даром|безоплатно|збір коштів|збираємо кошти|для зсу|волонтер|благодійн|"
    r"загубил|загубила|знайшов|знайшла|знайдено|пропал|пропав|зник)",
    re.IGNORECASE,
)
# Вакансії й набір працівників: окрема тема чату, не реклама послуг чи товарів
_JOB_RE = re.compile(
    r"(?<!\w)(?:требуются|требуется|потрібні працівники|потрібні\b|вакансі|набираємо|набираем|шукаємо працівників|заробляй|"
    r"заработок|заробіток|доход от|дохід від|оплата:|ставка|графік роботи|график работы|робочий графік|рекрутинг|роботодавець|работодатель)",
    re.IGNORECASE,
)


def has_contact(text: str) -> bool:
    return bool(_PHONE_RE.search(text) or _HANDLE_RE.search(text))


def looks_like_ad(text: str) -> bool:
    """Чиста перевірка тексту: контакт або ціна + пропозиція, не питання й не прохання."""
    text = (text or "").strip()
    if not (MIN_LEN <= len(text) <= MAX_LEN):
        return False
    if _ASK_RE.search(text) or _NONCOMM_RE.search(text) or _JOB_RE.search(text):
        return False
    contact = has_contact(text)
    price = bool(_PRICE_RE.search(text))
    if not (contact or price):
        return False
    if "?" in text and not contact:
        return False
    if _STRONG_RE.search(text):
        return True
    if _WEAK_RE.search(text) and contact:
        return True
    if _SALE_RE.search(text) and _SHOP_RE.search(text):
        return True
    return False


# --- сховище -------------------------------------------------------------------------

def _db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=5)
    con.execute(
        "CREATE TABLE IF NOT EXISTS shadow_ad (chat_id INTEGER NOT NULL, msg_id INTEGER NOT NULL, user_id INTEGER NOT NULL, "
        "thread_id INTEGER, ts TEXT NOT NULL, link TEXT, PRIMARY KEY (chat_id, msg_id))"
    )
    con.execute("CREATE INDEX IF NOT EXISTS shadow_ad_ts ON shadow_ad (ts)")
    return con


def record(chat_id: int, msg_id: int, user_id: int, thread_id: Optional[int], link: str, now: Optional[datetime] = None) -> None:
    ts = (now or datetime.utcnow()).strftime(TS_FORMAT)
    with _lock:
        con = _db()
        try:
            con.execute(
                "INSERT OR IGNORE INTO shadow_ad (chat_id, msg_id, user_id, thread_id, ts, link) VALUES (?,?,?,?,?,?)",
                (int(chat_id), int(msg_id), int(user_id), thread_id, ts, link or ""),
            )
            con.commit()
        finally:
            con.close()


# --- рекламний бот: клієнти й теми (лише читання) --------------------------------------

def _adbot_ro() -> sqlite3.Connection:
    return sqlite3.connect("file:{}?mode=ro".format(ADBOT_DB), uri=True, timeout=3)


def client_ids() -> Optional[Set[int]]:
    """Усі, хто колись був клієнтом рекламного бота. None, якщо їх не вдалося дізнатися (тоді нічого не рахуємо)."""
    now = time.time()
    if _clients_cache["ids"] is not None and now - float(_clients_cache["at"]) < CLIENTS_TTL:
        return _clients_cache["ids"]  # type: ignore[return-value]
    if not ADBOT_DB or not os.path.isfile(ADBOT_DB):
        return _clients_cache["ids"]  # type: ignore[return-value]
    try:
        con = _adbot_ro()
        try:
            ids = {int(r[0]) for r in con.execute("SELECT user_id FROM ad_clients")}
            ids |= {int(r[0]) for r in con.execute("SELECT DISTINCT client_user_id FROM published_posts WHERE client_user_id IS NOT NULL")}
        finally:
            con.close()
    except (sqlite3.Error, OSError):
        logger.warning("ad_shadow: could not read the clients of the ad bot")
        return _clients_cache["ids"]  # type: ignore[return-value]
    _clients_cache.update(at=now, ids=ids)
    return ids


def topic_names() -> Dict[int, str]:
    """thread_id -> назва теми (з бази рекламного бота), порожньо, якщо прочитати не вдалося."""
    now = time.time()
    if _topics_cache["names"] is not None and now - float(_topics_cache["at"]) < TOPICS_TTL:
        return _topics_cache["names"]  # type: ignore[return-value]
    names = dict(_topics_cache["names"] or {})  # type: ignore[arg-type]
    if ADBOT_DB and os.path.isfile(ADBOT_DB):
        try:
            con = _adbot_ro()
            try:
                names = {int(t or 0): str(n or "") for n, t in con.execute("SELECT name, message_thread_id FROM topics")}
            finally:
                con.close()
        except (sqlite3.Error, OSError):
            logger.warning("ad_shadow: could not read the topics of the ad bot")
    _topics_cache.update(at=now, names=names)
    return names


def is_flea_topic(message) -> bool:
    """Тема «Барахолка»: за назвою з бази рекламного бота або за заголовком теми, на який відповідає повідомлення."""
    thread_id = int(getattr(message, "message_thread_id", None) or 0)
    if thread_id and "барахолк" in topic_names().get(thread_id, "").lower():
        return True
    replied = getattr(message, "reply_to_message", None)
    created = getattr(replied, "forum_topic_created", None) if replied is not None else None
    name = getattr(created, "name", "") or ""
    return "барахолк" in name.lower()


def chat_admin_ids(bot, chat_id: int) -> Optional[Set[int]]:
    now = time.time()
    cached = _admins_cache.get(chat_id)
    if cached and now - cached[0] < ADMINS_TTL:
        return cached[1]
    try:
        ids = {int(m.user.id) for m in bot.get_chat_administrators(chat_id)}
    except Exception:
        logger.warning("ad_shadow: could not read the admins of chat %s", chat_id)
        return cached[1] if cached else None
    _admins_cache[chat_id] = (now, ids)
    return ids


# --- обробник чату ---------------------------------------------------------------------

def _handle(update, context) -> bool:
    """True, якщо повідомлення записано як схоже на рекламу. Нічого нікому не надсилає."""
    if not enabled():
        return False
    message = update.effective_message
    if message is None or not message.text:
        return False
    user = message.from_user
    if user is None or user.is_bot:
        return False
    text = message.text
    if not looks_like_ad(text):
        return False
    if ADMIN_ID and user.id == ADMIN_ID:
        return False
    if is_flea_topic(message):
        return False
    clients = client_ids()
    if clients is None or user.id in clients:
        return False
    admins = chat_admin_ids(context.bot, message.chat_id)
    if admins is None or user.id in admins:
        return False
    try:
        link = message.link or ""
    except Exception:
        link = ""
    record(message.chat_id, message.message_id, user.id, getattr(message, "message_thread_id", None), link)
    logger.info("ad_shadow: counted message %s", message.message_id)
    return True


def on_group_message(update, context) -> None:
    try:
        _handle(update, context)
    except Exception:
        logger.exception("ad_shadow failed")


group_handler = MessageHandler(Filters.text & Filters.chat_type.groups & ~Filters.command, on_group_message, run_async=True)


# --- рядок у вечірнє зведення ----------------------------------------------------------

def summary_text(since: datetime, until: datetime) -> str:
    """Один блок для зведення адміну: порожньо, якщо вимкнено чи нічого не знайдено."""
    if not enabled():
        return ""
    with _lock:
        con = _db()
        try:
            rows = con.execute(
                "SELECT user_id, link FROM shadow_ad WHERE ts >= ? AND ts < ? ORDER BY ts DESC",
                (since.strftime(TS_FORMAT), until.strftime(TS_FORMAT)),
            ).fetchall()
            con.execute("DELETE FROM shadow_ad WHERE ts < ?", ((until - timedelta(days=KEEP_DAYS)).strftime(TS_FORMAT),))
            con.commit()
        finally:
            con.close()
    if not rows:
        return ""
    authors = {int(r[0]) for r in rows}
    lines = ["📢 Сьогодні схожих на рекламу повз оплату: {}, авторів: {}".format(len(rows), len(authors))]
    seen = set()
    links = []   # type: List[str]
    for user_id, link in rows:
        if user_id in seen or not link:
            continue
        seen.add(user_id)
        links.append('<a href="{}">{}</a>'.format(link, len(links) + 1))
        if len(links) >= MAX_LINKS:
            break
    if links:
        lines.append("Повідомлення: " + " · ".join(links))
    return "\n".join(lines)
