"""
AI-powered chat assistant handler.
Search priority: 1) FAQ, 2) vector search (ChromaDB), 3) keyword fallback.
"""

import json
import logging
import os
import re
import threading
from datetime import datetime, timedelta
from typing import List, Dict, Optional

import requests
from sqlalchemy import func, or_
from telegram import Update
from telegram.ext import CallbackContext, MessageHandler, Filters

from database import Chat, DBSession, Message, User

# ── ChromaDB (lazy init) ──────────────────────────────────────────────────
_chroma_col = None
_chroma_count = 0        # cached once at init; avoids repeated col.count() calls that hang on WSL2
_chroma_failed = False   # set to True after first col.query() timeout; skips ChromaDB thereafter

def _get_chroma():
    global _chroma_col, _chroma_count, _chroma_failed
    if _chroma_col is not None:
        return _chroma_col if not _chroma_failed else None
    if _chroma_failed:
        return None
    try:
        import chromadb
        from chromadb.config import Settings
        client = chromadb.PersistentClient(
            path=os.getenv("CHROMA_PATH", "/app/chroma"),
            settings=Settings(anonymized_telemetry=False),
        )
        col = None
        for name in ("chunks", "messages"):  # prefer chunked index
            try:
                col = client.get_collection(name)
                break
            except Exception:
                continue
        if col is None:
            cols = client.list_collections()
            col = cols[0] if cols else None
        if col is not None:
            # count with timeout guard (WSL2 safety)
            result = [0]
            def _do_count():
                try:
                    result[0] = col.count()
                except Exception:
                    pass
            t = threading.Thread(target=_do_count, daemon=True)
            t.start()
            t.join(timeout=10)
            _chroma_count = result[0]
            _chroma_col = col  # store the Collection, not the client
        logger.info(f"ChromaDB ready: {_chroma_count} messages")
        return _chroma_col
    except Exception as e:
        logger.warning(f"ChromaDB init failed: {e}")
        _chroma_col = None
        return None

logger = logging.getLogger(__name__)

TRIGGER_WORD       = os.getenv("AI_TRIGGER_WORD", "потсдамбот").lower()
TRIGGER_ALIASES    = tuple(dict.fromkeys((
    TRIGGER_WORD, "посдамбот", "потсдам бот",
)))
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
AI_MODEL           = os.getenv("AI_MODEL", "auto/best-fast")
GEMINI_API_KEY     = os.getenv("GEMINI_API_KEY", "")   # kept for embed_updater only
FAQ_PATH           = os.getenv("FAQ_PATH", "/app/config/faq.json")
CHAT_USERNAME      = os.getenv("CHAT_USERNAME", "")

# ── Search limits ─────────────────────────────────────────────────────────
CONTEXT_MESSAGES_RECENT = 20
CHAIN_WINDOW = 2
RECENCY_CUTOFF_DAYS = 365
RECENCY_PENALTY_DAYS = 730
# Vector results below this cosine-similarity score are noise and are
# dropped instead of polluting the LLM context (prevents "answers by vibes").
VEC_MIN_SCORE = float(os.getenv("VEC_MIN_SCORE", "0.35"))
PER_KW_ANCHOR = 12   # messages per anchor word (direct from user query)
PER_KW_BROAD  = 6    # messages per expanded keyword
MAX_CONTEXT   = 15000

OPENROUTER_URL = os.getenv(
    "OPENROUTER_URL", "https://openrouter.ai/api/v1/chat/completions"
)
GEMINI_DIRECT_MODEL = os.getenv("GEMINI_DIRECT_MODEL", "gemini-2.5-flash")
GEMINI_DIRECT_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "{model}:generateContent?key={api_key}"
)

EMBED_MODEL = "gemini-embedding-001"
EMBED_URL   = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"{EMBED_MODEL}:embedContent?key={{api_key}}"
)


def _embed_query(text: str) -> Optional[List[float]]:
    """Embed a single query string using Google text-embedding-004."""
    if not GEMINI_API_KEY:
        return None
    url = EMBED_URL.format(api_key=GEMINI_API_KEY)
    payload = {
        "model": f"models/{EMBED_MODEL}",
        "content": {"parts": [{"text": text[:2000]}]},
        "taskType": "RETRIEVAL_QUERY",
        "outputDimensionality": 768,
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        return resp.json()["embedding"]["values"]
    except Exception as e:
        logger.warning(f"Embed query failed: {e}")
        return None


def _vector_search(query: str, n_results: int = 50,
                   since_days: Optional[int] = None) -> List[Dict]:
    """Semantic search via ChromaDB. Returns message dicts sorted by relevance."""
    col = _get_chroma()
    if col is None or _chroma_count == 0:
        return []

    embedding = _embed_query(query)
    if not embedding:
        return []

    where = None
    if since_days is not None:
        cutoff_ts = int((datetime.utcnow() - timedelta(days=since_days)).timestamp())
        where = {"timestamp": {"$gte": cutoff_ts}}

    try:
        n = min(n_results, _chroma_count)
        _result: List = [None]
        _err: List = [None]

        def _run():
            try:
                _result[0] = col.query(
                    query_embeddings=[embedding],
                    n_results=n,
                    where=where,
                    include=["documents", "metadatas", "distances"],
                )
            except Exception as _e:
                _err[0] = _e

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        t.join(timeout=15)

        if t.is_alive():
            # col.query() is stuck (WSL2/mmap issue) — abandon the thread
            # and disable ChromaDB for all subsequent queries in this session.
            global _chroma_failed
            _chroma_failed = True
            logger.warning(
                "ChromaDB col.query() hung (>15s) — disabling vector search for this session"
            )
            return []

        if _err[0] is not None:
            raise _err[0]

        results = _result[0]
    except Exception as e:
        logger.warning(f"Vector search failed: {e}")
        return []

    msgs = []
    metas    = results.get("metadatas", [[]])[0]
    docs     = results.get("documents", [[]])[0]
    dists    = results.get("distances",  [[]])[0]

    for meta, doc, dist in zip(metas, docs, dists):
        try:
            date_obj = datetime.strptime(meta.get("date", ""), "%Y-%m-%d %H:%M")
        except Exception:
            date_obj = None
        full_link = meta.get("link", "")
        msgs.append({
            "text":       doc,
            "user":       meta.get("user", ""),
            "date":       meta.get("date", ""),
            "date_obj":   date_obj,
            "id":         int(meta.get("msg_id", 0)),
            "link":       full_link,
            "short_link": _shorten_link(full_link),
            "score":      round(1 - dist, 3),
        })

    kept = [m for m in msgs if m["score"] >= VEC_MIN_SCORE]
    logger.info(
        f"Vector search: {len(kept)}/{len(msgs)} above score {VEC_MIN_SCORE} "
        f"(top: {msgs[0]['score'] if msgs else '-'}, since_days={since_days})"
    )
    return kept

# ── Prompts ───────────────────────────────────────────────────────────────
SYSTEM_PROMPT_FAQ = """
Ти — бот-помічник мешканців Потсдама. Тобі надана готова довідкова інформація.
Дай відповідь на питання користувача на основі цієї інформації.
ВАЖНО: відповідай ТІЛЬКИ українською мовою, незалежно від мови запитання.
Максимум 1500 символів. Будь конкретним.
Якщо в довідці є посилання — обов'язково включи їх у відповідь.
""".strip()

SYSTEM_PROMPT_CHAT = """
Ти — бот-помічник групового чату мешканців Потсдама.
Сьогоднішня дата: {today}.
Повідомлення в чаті написані російською, українською та німецькою — використовуй всі однаково.

Правила відповіді:
- МОВА: відповідай ТІЛЬКИ українською мовою, незалежно від мови запитання.
- Пріоритет свіжим повідомленням — завжди дивись на дату.
- Якщо є ДОВІДКОВА ІНФОРМАЦІЯ (FAQ) — це перевірені дані, вони мають найвищий пріоритет.
- Якщо FAQ немає або він неповний — використовуй виключно КОНТЕКСТ З ЧАТУ (повідомлення учасників).
- Не вигадуй того, чого немає в контексті.
- В контексті чату можуть бути ЧУЖІ ПИТАННЯ без відповіді (люди теж шукають майстра/лікаря/тд) —
  це НЕ факти і НЕ контакти, ігноруй такі повідомлення, використовуй лише конкретні відповіді/пропозиції.
- Якщо є кілька джерел — узагальни все в один зв'язний текст.
- Якщо інформації немає взагалі — скажи чесно одним реченням.
- Пиши повні речення, не обривай текст.
- Якщо в контексті є →https://t.me/... посилання — постав його після відповідного факту.
- ФОРМАТ: підбирай залежно від запиту:
    • Просте питання → 2-4 речення.
    • Запит на контакти/список/кроки → нумерований або маркований список.
    • Зведення/аналіз → структурований текст з підзаголовками.
- В самому кінці — одна коротка строка з найрелевантнішим джерелом: "Джерело: @username, ДАТА".
- Для подій: дата, час, місце, як зареєструватися.

КРИТИЧНО ВАЖЛИВО щодо пошуку майстрів/послуг/контактів:
- Коли шукають "хто робить X", "майстри", "послуги" — ШУКАЙ В КОНТЕКСТІ:
  • Повідомлення де ХТОСЬ ПРОПОНУЄ послугу (я/роблю/роблю/приймаю/є вільний/писати в ЛС)
  • Рекомендації від інших ("рекомендую @username", "зверніться до")
  • Конкретні контакти та посилання
- НЕ подавай як відповідь повідомлення де люди ПИТАЮТЬ "хто робить?", "порадьте" — це не контакти!
- Якщо в контексті є і питання, і відповідь — наводь ТІЛЬКИ відповідь/пропозицію.

КРИТИЧНО ВАЖЛИВО — щорічні/повторювані події (свята, фестивалі, ярмарки):
- Перед відповіддю ОБОВ'ЯЗКОВО звір рік повідомлення з поточною датою ({today}).
- Якщо в контексті є деталі про подію МИНУЛИХ років (рік повідомлення < поточний рік) — це НЕ підтвердження що подія буде цього року.
- Якщо є питання людей "чи буде цього року" БЕЗ відповіді на нього — чесно скажи, що торік подія була (вкажи дату/місце), але підтвердження на поточний рік в чаті ще немає.
- НЕ стверджуй "так, подія відбудеться" лише на основі інформації за минулий рік.
""".strip()

# ── Temporal detection ────────────────────────────────────────────────────
TEMPORAL_WORDS = [
    "неделю", "недел", "тиждень", "woche",
    "сегодня", "сьогодні", "сегодні", "heute",
    "завтра", "morgen",
    "выходные", "вихідні", "wochenende",
    "ивент", "івент", "event", "veranstaltung",
    "мероприят", "афиша", "розклад", "расписание",
    "ближайш", "найближч",
    "в этом году", "цього року", "этот год", "цей рік",
    # summary/analysis queries — treat as temporal to pull recent messages
    "сводку", "сводка", "зведення", "за сутки", "за добу",
    "последние дни", "останні дні", "last week", "за тиждень",
]

# ── Chat-history intent detection (bypass FAQ, go directly to deep search) ──
# These phrases signal the user wants to search/analyse the actual chat, not get a canned FAQ answer.
_CHAT_HISTORY_PHRASES = [
    # "what did people say/write in the chat about X"
    "в чате", "из чата", "в переписке", "из переписки",
    "в чаті", "у чаті", "з чату", "у переписці",  # Ukrainian both prepositions
    "что писали", "что говорили", "что обсуждали", "что давали", "писали про",
    "що писали", "що говорили", "що обговорювали", "що давали", "писали про",
    "упоминал", "упомин", "згадувал", "згадув",
    "какие советы", "какие контакты", "какие рекомендации",
    "які поради", "які контакти", "які рекомендації",
    # imperative search / compile commands
    "найди", "знайди", "найдіть", "знайдіть",
    "собери", "зібери",
    "покажи", "покажіть",
    "составь", "склади", "підготуй", "подготовь",
    # analysis / summary requests
    "сводку", "сводка", "зведення",
    "проанализируй", "проаналізуй",
    "анализ переписк", "аналіз переписк",
    "частые вопросы", "часті питання",
    "закреплённ", "закріплен",
    # "I just arrived" → needs guide synthesised from chat
    "только приехал", "только прибыл", "только прибыла",
    "тільки приїхав", "тільки приїхала", "тільки прибув",
]


def _is_chat_history_query(query: str) -> bool:
    """True if the query is about the chat itself (history search, analysis, summaries).
    These must skip FAQ and go directly to the full keyword+Gemini pipeline.
    """
    q = query.lower()
    return any(phrase in q for phrase in _CHAT_HISTORY_PHRASES)

RU_MONTHS = ["", "января", "февраля", "марта", "апреля", "мая", "июня",
             "июля", "августа", "сентября", "октября", "ноября", "декабря"]
UA_MONTHS = ["", "січня", "лютого", "березня", "квітня", "травня", "червня",
             "липня", "серпня", "вересня", "жовтня", "листопада", "грудня"]

# Common words to exclude from anchor search
_STOP_WORDS = {
    # Russian
    "чаті", "чате", "чату", "буде", "немає", "нема", "треба", "можна",
    "знаєш", "знаете", "знаешь", "знает", "есть", "нету", "нет",
    "хочу", "хочете", "потсдам", "potsdam", "бота", "боте", "ботe",
    "скажи", "скажіть", "розкажи", "розкажіть", "покажи", "покажіть",
    # Ukrainian common words that pollute anchor search
    "добрий", "ранок", "робить", "роблю", "робить", "хтось", "хто",
    "будь ласка", "ласка", "порадьте", "порада", "поради",
    "який", "яка", "яке", "які", "якого", "якої",
    "мій", "моя", "моє", "мої", "твій", "твоя",
    "сьогодні", "завтра", "вчора", "нині",
    "може", "можливо", "треба", "треба", "варто",
    "привіт", "дякую", "дякую", "прошу",
    # German common
    "hallo", "danke", "bitte", "guten", "morgen",
    # Generic
    "когось", "кого", "чого", "нього",
}


# ── FAQ ───────────────────────────────────────────────────────────────────

def _load_faq() -> List[Dict]:
    try:
        with open(FAQ_PATH, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        logger.warning(f"FAQ file not found: {FAQ_PATH}")
        return []
    except Exception as e:
        logger.error(f"Failed to load FAQ: {e}")
        return []


def _search_faq(query: str, faq: List[Dict]) -> Optional[str]:
    """Find matching FAQ entries. Returns all entries with score >= 2, combined.
    For broad topic queries (e.g. 'AWO', 'Tafel') multiple entries are merged
    so the user gets a comprehensive answer, not just a single fact.
    """
    if not faq:
        return None
    query_lower = query.lower()
    query_words = set(query_lower.split())
    scored: List[tuple] = []
    for entry in faq:
        keywords = [k.lower() for k in entry.get("keywords", [])]
        score = 0
        for kw in keywords:
            if len(kw) < 3:  # skip tiny keywords ("ЕС", "ТК") — cause false substring hits
                continue
            if kw in query_lower:
                score += 2
            elif any(kw in w or w in kw for w in query_words if len(w) > 4):
                score += 1
        if score >= 3:
            scored.append((score, entry))
    if not scored:
        return None
    # Sort by score descending, keep top 8 to avoid context overflow
    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:8]
    if len(top) == 1:
        return top[0][1].get("answer", "")
    # Multiple entries: combine into one context block separated by "---"
    parts = []
    for _, entry in top:
        parts.append(entry.get("answer", ""))
    return "\n---\n".join(parts)


# ── Query parsing ─────────────────────────────────────────────────────────

def _extract_query(text: str) -> Optional[str]:
    if not text:
        return None
    trigger_re = re.compile(
        r"(?<!\w)(?:" + "|".join(
            re.escape(alias) for alias in sorted(TRIGGER_ALIASES, key=len, reverse=True)
        ) + r")(?!\w)",
        re.IGNORECASE,
    )
    match = trigger_re.search(text)
    if match is None:
        return None
    prefix = text[:match.start()].rstrip()
    suffix = text[match.end():].lstrip()
    if prefix and suffix and prefix[-1:] in ",.:;!?—-" and suffix[:1] in ",.:;!?—-":
        suffix = suffix[1:].lstrip()
    query = f"{prefix} {suffix}".strip()
    query = re.sub(r"^[\s,.:;!—-]+|[\s,.:;!—-]+$", "", query)
    query = re.sub(r"\s+([,.:;!?])", r"\1", query)
    return query if query else None


def _is_temporal_query(query: str) -> bool:
    q = query.lower()
    return any(w in q for w in TEMPORAL_WORDS)


_SERVICE_QUERY_HINTS = [
    "мастер", "мастера", "майстер", "майстри", "услуг", "послуг",
    "предлагал", "предлагали", "предлагает", "пропонував", "пропонували", "пропонує",
    "реклам", "контакт", "специалист", "спеціаліст", "парикмах", "перукар",
    "стриж", "зачіс", "причес", "уклад", "колорист", "барбер", "friseur", "friseurin",
    "муж на час", "чоловік на годину", "бытов", "побутов", "мелкий ремонт", "дрібний ремонт",
    "сборк", "збірк", "мебел", "мебл", "установк", "встановлен", "кухн",
    "подключ", "підключ", "электроприбор", "електроприлад", "техник", "технік",
]

_PROVIDER_NAME_ALIASES = {
    "дмитрий": ("дмитр", "dmitry", "dmitrii", "dmytro", "dmytr"),
    "дмитро": ("дмитр", "dmitry", "dmitrii", "dmytro", "dmytr"),
    "дима": ("дима", "dima", "dmytro", "dmytr"),
}

_SEEKER_PATTERNS = [
    r"\bищу\b", r"\bшукаю\b", r"\bищем\b", r"\bшукаємо\b",
    r"\bнужн\w*\b", r"\bпотрібн\w*\b", r"\bцікавить\b", r"\bинтересует\b",
    r"\bкто\b.*\b(делает|робить|стрижет|стриж|знает|знає|ищет|ищу|шукає)",
    r"\bхто\w*\b.*\b(робить|стриже|знає|може|шукає|шукаю)",
    r"порекомендуйте", r"порадьте", r"посоветуйте", r"підкажіть", r"подскажите",
    r"может кто", r"можливо хтось", r"kennt jemand", r"suche", r"gesucht",
]

_PROVIDER_PATTERNS = [
    r"\bроблю\b", r"\bделаю\b", r"\bнадаю\b", r"\bпредлагаю\b", r"\bпропоную\b",
    r"\bдопоможу\b", r"\bпомогу\b", r"\bвиконую\b", r"\bвыполняю\b",
    r"\bпрацюю\b", r"\bработаю\b", r"\bзапрошую\b", r"\bприглашаю\b",
    r"\bстригу\b", r"\bподстригу\b", r"\bпостригу\b", r"\bстригти\b",
    r"хто\w*\s+шука\w+\s+(барбер|перукар|парикмах|майстр)",
    r"\bзапис\b", r"\bзаписываю\b", r"\bприймаю\b", r"\bпринимаю\b",
    r"\bвільн[а-яіїє]*\b.*\b(час|місц)", r"\bсвободн[а-я]*\b.*\b(окн|мест|врем)",
    r"\bпишіть\b.*\b(лс|особист|direct)", r"\bпишите\b.*\b(лс|личк|direct)",
    r"friseur", r"friseurin", r"biete", r"mache", r"schneide",
]

_RECOMMEND_PATTERNS = [
    r"рекоменд", r"раджу", r"советую", r"зверніться", r"обратитесь",
    r"можу порадити", r"могу посоветовать", r"хороший мастер", r"гарний майстер",
    r"хорош[а-я]+\s+(стоматолог|врач|доктор|мастер|майстер|friseur|специалист|перукар|лікар|dentist)",
    r"гарн[а-я]+\s+(стоматолог|врач|лікар|майстер|перукар)",
    r"можна\s+(до|к)\s+(врач|доктор|лікар|стоматолог|zahnarzt|praxis)",
    r"є\s+(праксис|praxis|хорош)",
    r"есть\s+(праксис|praxis|хорош)",
    r"можете\s+(звернутись|обратиться|пойти|записатись)",
    r"напишіть\s+(йому|їй|в|у)",
    r"контакт\w*\s+(стоматолог|врач|лікар|майстер|перукар)",
]

_HAIR_TOPIC_RE = re.compile(
    r"(стриж|зачіс|зачес|причес|уклад|волос|перукар|парикмах|friseur|friseurin|barber|барбер|колорист|фарбув|окрашив|мелирован)",
    re.IGNORECASE,
)


def _is_hair_query(query: str) -> bool:
    return bool(_HAIR_TOPIC_RE.search(query))


_HAIR_SERVICE_KEYWORDS = [
    "барбер", "barber", "перукар", "парикмахер", "friseur", "friseurin",
    "стрижка", "стрижки", "стригу", "підстригаю", "подстригу", "постригу",
    "роблю стрижки", "делаю стрижки", "чоловічі стрижки", "мужские стрижки",
    "дитячі стрижки", "детские стрижки", "стрижка бороди", "борода",
    "зачіска", "зачіски", "укладка", "укладки", "фарбування волосся",
    "окрашивание волос", "колорист", "колорування", "мелірування", "мелирование",
]


def _matches_query_topic(query: str, msg: Dict) -> bool:
    """Avoid generic 'services' ads (moving, repair, etc.) when the user asked for hair/beauty."""
    if _is_hair_query(query):
        return bool(_HAIR_TOPIC_RE.search(msg.get("text") or ""))
    return True


def _is_service_provider_query(query: str) -> bool:
    """User asks for people who provide/advertised a service, not people who search for it."""
    q = query.lower()
    return any(h in q for h in _SERVICE_QUERY_HINTS)


def _provider_author_terms(query: str) -> List[str]:
    """Return possible name and username fragments from a provider query."""
    terms = []
    for word in re.findall(r"@?[\w.-]{3,}", query.lower(), flags=re.UNICODE):
        clean = word.lstrip("@").strip(".-")
        if not clean or clean in _STOP_WORDS:
            continue
        aliases = _PROVIDER_NAME_ALIASES.get(clean)
        if aliases:
            terms.extend(aliases)
        elif word.startswith("@"):
            terms.append(clean)
    return list(dict.fromkeys(terms))


def _provider_signal_score(msg: Dict) -> int:
    """Deterministic score: offers/recommendations/contacts beat seeker questions."""
    text = (msg.get("text") or "").lower()
    score = 0
    if any(re.search(p, text) for p in _PROVIDER_PATTERNS):
        score += 4
    if any(re.search(p, text) for p in _RECOMMEND_PATTERNS):
        score += 3
    if re.search(r"(@\w{3,}|https?://|t\.me/|instagram|insta|whatsapp|wa\.me|телеграм|telegram)", text):
        score += 2
    if re.search(r"(доктор|dr\.|praxis|pra\.|zahnarzt|стоматолог|стоматология|лікар|клініка|клиника|med\.)", text):
        score += 1
    if re.search(r"[\d\s\-\(\)\+]{6,}", text) and re.search(r"(тел|фон|call|anruf|дзвон)", text):
        score += 1
    if re.search(r"(стриж|зачіс|причес|уклад|перукар|парикмах|friseur|barber|барбер|колорист)", text):
        score += 1
    if any(re.search(p, text) for p in _SEEKER_PATTERNS):
        score -= 5
    return score


def _filter_provider_candidates(messages: List[Dict], query: str = "") -> List[Dict]:
    """For service-provider queries, drop pure seeker messages and sort likely providers first."""
    dedup: Dict[int, Dict] = {}
    for m in messages:
        dedup.setdefault(m.get("id"), m)
    provider_like = []
    for m in dedup.values():
        text = (m.get("text") or "").lower().strip()
        if text.startswith(TRIGGER_ALIASES):
            continue
        if "barberini" in text or "museum-barberini" in text:
            continue
        if re.search(r"(монтаж|напольн|покрыт|спортивн.*пол|работник|працівник|ваканс|командировк|трудоустр)", text):
            continue
        if _matches_query_topic(query, m) and _provider_signal_score(m) > 1:
            provider_like.append(m)
    provider_like.sort(key=lambda m: (_provider_signal_score(m), m.get("date", "")), reverse=True)
    return provider_like


def _group_provider_history(messages: List[Dict]) -> List[Dict]:
    """Keep several confirmations per provider instead of isolated posts."""
    groups: Dict[str, List[Dict]] = {}
    order: List[str] = []
    for message in messages:
        key = (message.get("user") or f"message:{message.get('id')}").lower()
        if key not in groups:
            groups[key] = []
            order.append(key)
        if len(groups[key]) < 4:
            groups[key].append(message)
    return [message for key in order for message in groups[key]]


def _get_anchor_words(query: str) -> List[str]:
    """Words from the original query worth anchoring DB search on."""
    return [
        w.lower().strip("?!.,;:") for w in query.split()
        if len(w.strip("?!.,;:")) > 4
        and w.lower().strip("?!.,;:") not in _STOP_WORDS
    ]


def _get_upcoming_date_patterns(days_ahead: int = 14) -> List[str]:
    """Date string patterns for today + N days to search in message text."""
    today = datetime.utcnow()
    patterns = []
    for delta in range(0, days_ahead + 1):
        d = today + timedelta(days=delta)
        day, month = d.day, d.month
        patterns.append(f"{day:02d}.{month:02d}")
        patterns.append(f"{day}.{month:02d}")
        if 1 <= month <= 12:
            patterns.append(f"{day} {RU_MONTHS[month]}")
            patterns.append(f"{day} {UA_MONTHS[month]}")
    seen = set()
    result = []
    for p in patterns:
        if p not in seen:
            seen.add(p)
            result.append(p)
    return result


# ── DB search ─────────────────────────────────────────────────────────────

# Messages addressed to the bot itself (questions, not community answers) must
# never end up in the answer context — they pollute it with unanswered queries.
_BOT_ADDRESS_PREFIXES = (f"{TRIGGER_WORD}%", "посдамбот%", "потсдам бот%")


# Cyrillic-safe lowercase text expression (SQLite lower() is ASCII-only,
# so Message.text.ilike() silently misses capitalized Cyrillic words).
_TL = func.coalesce(Message.text_lower, "")


def _exclude_bot_address(q):
    return q.filter(~or_(*[_TL.like(p) for p in _BOT_ADDRESS_PREFIXES]))


def _search_keyword_ids(session, chat_ids: List[int],
                        keywords: List[str],
                        anchor_words: Optional[List[str]] = None,
                        since: Optional[datetime] = None,
                        before: Optional[datetime] = None) -> List[int]:
    """Search message text with separate per-keyword quotas and date bounds."""
    if not chat_ids:
        return []

    seen: set = set()
    result: List[int] = []
    base_q = _exclude_bot_address(
        session.query(Message._id)
        .filter(Message.from_chat.in_(chat_ids))
        .filter(Message.text.isnot(None))
        .filter(Message.text != "")
    )
    if since is not None:
        base_q = base_q.filter(Message.date >= since)
    if before is not None:
        base_q = base_q.filter(Message.date < before)

    def _collect(words: List[str], per_kw: int) -> None:
        for word in words:
            if not word:
                continue
            rows = (
                base_q.filter(_TL.like(f"%{word.lower()}%"))
                .order_by(Message.date.desc())
                .limit(per_kw)
                .all()
            )
            for (pk,) in rows:
                if pk not in seen:
                    seen.add(pk)
                    result.append(pk)

    _collect(anchor_words or [], PER_KW_ANCHOR)
    _collect(keywords or [], PER_KW_BROAD)
    return result


def _get_message_ids_by_keywords(session, chat_ids: List[int],
                                  keywords: List[str],
                                  anchor_words: Optional[List[str]] = None) -> List[int]:
    """Backward-compatible all-history keyword search."""
    return _search_keyword_ids(session, chat_ids, keywords, anchor_words)


def _search_provider_authors(session, chat_ids: List[int],
                             terms: List[str], limit: int = 40):
    """Find all-history messages by matching provider fullname or username."""
    if not chat_ids or not terms:
        return []
    normalized_terms = [
        term.lower().lstrip("@").strip()
        for term in terms
        if term.lower().lstrip("@").strip()
    ]
    if not normalized_terms:
        return []

    matching_user_ids = []
    for user in session.query(User).all():
        fullname = (user.fullname or "").lower()
        username = (user.username or "").lower()
        if any(term in fullname or term in username for term in normalized_terms):
            matching_user_ids.append(user.id)
    if not matching_user_ids:
        return []

    return (
        session.query(Message, User)
        .outerjoin(User, Message.from_id == User.id)
        .filter(Message.from_chat.in_(chat_ids))
        .filter(Message.text.isnot(None))
        .filter(Message.text != "")
        .filter(Message.from_id.in_(matching_user_ids))
        .order_by(Message.date.desc())
        .limit(limit)
        .all()
    )


def _fetch_chain(session, center_ids: List[int], window: int = CHAIN_WINDOW) -> List[Dict]:
    """Fetch surrounding messages as conversation chains."""
    if not center_ids:
        return []
    all_pks = set()
    for pk in center_ids:
        for offset in range(-window, window + 1):
            all_pks.add(pk + offset)
    rows = (
        session.query(Message, User)
        .outerjoin(User, Message.from_id == User.id)
        .filter(Message._id.in_(all_pks))
        .filter(Message.text.isnot(None))
        .filter(Message.text != "")
        .order_by(Message.date.desc())
        .all()
    )
    return _rows_to_dicts(rows)


def _search_recently_posted(session, chat_ids: List[int],
                             days: int = 7, limit: int = 60) -> List[Dict]:
    """Messages posted in the last N days (for event queries)."""
    if not chat_ids:
        return []
    cutoff = datetime.utcnow() - timedelta(days=days)
    rows = (
        _exclude_bot_address(
            session.query(Message, User)
            .outerjoin(User, Message.from_id == User.id)
            .filter(Message.from_chat.in_(chat_ids))
            .filter(Message.text.isnot(None))
            .filter(Message.text != "")
        )
        .filter(Message.date >= cutoff)
        .order_by(Message.date.desc())
        .limit(limit)
        .all()
    )
    return _rows_to_dicts(rows)


def _search_recent(session, chat_ids: List[int],
                   limit: int = CONTEXT_MESSAGES_RECENT) -> List[Dict]:
    """Most recent messages for general context."""
    if not chat_ids:
        return []
    cutoff = datetime.utcnow() - timedelta(days=RECENCY_CUTOFF_DAYS)
    rows = (
        _exclude_bot_address(
            session.query(Message, User)
            .outerjoin(User, Message.from_id == User.id)
            .filter(Message.from_chat.in_(chat_ids))
            .filter(Message.text.isnot(None))
            .filter(Message.text != "")
        )
        .filter(Message.date >= cutoff)
        .order_by(Message.date.desc())
        .limit(limit)
        .all()
    )
    return _rows_to_dicts(rows)


def _search_keywords_with_fallback(session, chat_ids: List[int],
                                    keywords: List[str],
                                    anchor_words: Optional[List[str]] = None,
                                    provider_query: bool = False) -> List[Dict]:
    """Search recent text, preserving an independent all-history provider quota."""
    recent_days = int(os.getenv("SEARCH_RECENT_DAYS", "730"))
    cutoff = datetime.utcnow() - timedelta(days=recent_days)
    recent_ids = _search_keyword_ids(
        session, chat_ids, keywords, anchor_words, since=cutoff,
    )
    logger.info(f"Keyword search (last {recent_days}d): {len(recent_ids)} ids")

    if provider_query:
        historical_ids = _search_keyword_ids(
            session, chat_ids, keywords, anchor_words, before=cutoff,
        )
        logger.info(f"Provider keyword quota (older history): {len(historical_ids)} ids")
        ids = list(dict.fromkeys(recent_ids + historical_ids))
    elif len(recent_ids) < 5:
        logger.info("Too few recent results, falling back to full history search")
        ids = _search_keyword_ids(session, chat_ids, keywords, anchor_words)
        logger.info(f"Keyword search (all time): {len(ids)} ids")
    else:
        ids = recent_ids

    return _fetch_chain(session, ids)


def _shorten_link(full_link: str) -> str:
    """https://t.me/UkrainischesBrandenburg/255241  →  t.me/…/255241"""
    if not full_link:
        return ""
    # Extract message ID (last path segment)
    parts = full_link.rstrip("/").split("/")
    if len(parts) >= 2:
        return f"t.me/…/{parts[-1]}"
    return full_link


def _rows_to_dicts(rows) -> List[Dict]:
    results = []
    for msg, user in rows:
        username = (user.username or user.fullname or "") if user else ""
        msg_date = msg.date if hasattr(msg.date, "year") else None
        date_str = msg.date.strftime("%Y-%m-%d %H:%M") if hasattr(msg.date, "strftime") else str(msg.date)[:16]
        full_link = (msg.link or "") if hasattr(msg, "link") else ""
        results.append({
            "text": (msg.text or "").strip(),
            "user": username,
            "date": date_str,
            "date_obj": msg_date,
            "id": msg._id,
            "link": full_link,
            "short_link": _shorten_link(full_link),
        })
    return results


def _rrf_merge(ranked_lists: List[List[Dict]], k: int = 60) -> List[Dict]:
    """Reciprocal Rank Fusion: merge several ranked result lists into one.
    Items appearing high in multiple lists float to the top."""
    scores: Dict = {}
    first_seen: Dict = {}
    for lst in ranked_lists:
        for rank, m in enumerate(lst):
            mid = m["id"]
            scores[mid] = scores.get(mid, 0.0) + 1.0 / (k + rank + 1)
            first_seen.setdefault(mid, m)
    ordered = sorted(scores, key=scores.get, reverse=True)
    return [first_seen[mid] for mid in ordered]


def _build_context(msgs: List[Dict]) -> str:
    """Deduplicate, truncate by relevance order, present newest-first.

    Incoming order is relevance (RRF + rerank), so truncation keeps the
    most relevant items; the final chronological sort is only for display.
    """
    seen_ids: set = set()
    combined = []
    for m in msgs:
        if m["id"] in seen_ids:
            continue
        seen_ids.add(m["id"])
        combined.append(m)
    selected = []
    total_len = 0
    for m in combined:
        line = f"[{m['date']}] @{m['user']}: {m['text']}"
        if m.get("link"):
            line += f" →{m['link']}"
        if total_len + len(line) > MAX_CONTEXT:
            break
        selected.append((m, line))
        total_len += len(line) + 1
    selected.sort(key=lambda t: t[0]["date"], reverse=True)
    return "\n".join(line for _, line in selected)


# ── OpenRouter / OmniRoute calls ──────────────────────────────────────────

def _call_gemini_direct(prompt: str, max_tokens: int = 4096, timeout: int = 90) -> str:
    """Fallback: call Google Gemini API directly using GEMINI_API_KEY."""
    if not GEMINI_API_KEY:
        return ""

    url = GEMINI_DIRECT_URL.format(model=GEMINI_DIRECT_MODEL, api_key=GEMINI_API_KEY)
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "maxOutputTokens": min(max_tokens, 8192),
            "temperature": 0.2,
        },
    }
    try:
        resp = requests.post(url, json=payload, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        candidates = data.get("candidates", [])
        if not candidates:
            return ""
        parts = candidates[0].get("content", {}).get("parts", [])
        return "".join(p.get("text", "") for p in parts).strip()
    except requests.exceptions.Timeout:
        logger.error("Gemini direct API timeout")
        return ""
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else "?"
        body = e.response.text[:200] if e.response is not None else ""
        logger.error(f"Gemini direct HTTP error: {status} {body}")
        return "RATE_LIMIT" if status == 429 else ""
    except Exception as e:
        logger.error(f"Gemini direct error: {e}")
        return ""


def _call_ai(prompt: str, max_tokens: int = 8192, timeout: int = 90) -> str:
    """Call the configured OpenAI-compatible endpoint, then optionally fallback to Gemini."""
    headers = {
        "Content-Type": "application/json",
    }
    if OPENROUTER_API_KEY:
        headers["Authorization"] = f"Bearer {OPENROUTER_API_KEY}"
    payload = {
        "model": AI_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.2,
        "stream": False,
    }
    try:
        resp = requests.post(OPENROUTER_URL, headers=headers, json=payload, timeout=timeout)
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()
    except requests.exceptions.Timeout:
        logger.error("OpenRouter API timeout, using direct Gemini fallback")
        return _call_gemini_direct(prompt, max_tokens=max_tokens, timeout=timeout)
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else "?"
        body = e.response.text[:200] if e.response is not None else ""
        logger.error(f"OpenRouter HTTP error: {status} {body}")
        if status in (402, 403, 404, 429, 500, 502, 503, 504):
            logger.warning("Using direct Gemini fallback after OpenRouter failure")
            fallback = _call_gemini_direct(prompt, max_tokens=max_tokens, timeout=timeout)
            if fallback:
                return fallback
            return "RATE_LIMIT" if status == 429 else ""
        return ""
    except Exception as e:
        logger.error(f"OpenRouter error: {e}, using direct Gemini fallback")
        return _call_gemini_direct(prompt, max_tokens=max_tokens, timeout=timeout)


def _normalize_query(query: str) -> str:
    """Compress a long/messy query into a concise search intent (1-2 sentences).
    Only called when query is longer than 120 chars.
    """
    prompt = (
        f"Витягни основне питання з цього повідомлення одним коротким реченням (до 80 символів).\n"
        f"ПРАВИЛА:\n"
        f"- Тільки те, що є в тексті — не додавай нічого від себе.\n"
        f"- Якщо місто не вказане — НЕ додавай жодне місто.\n"
        f"- Без вступу і пояснень. Мова — як у питанні.\n\n"
        f"Повідомлення: {query}"
    )
    normalized = _call_ai(prompt, max_tokens=100, timeout=15)
    if normalized and normalized != "RATE_LIMIT":
        normalized = normalized.strip().strip('"')
        logger.info(f"Normalized query: {normalized!r}")
        return normalized
    logger.warning("Query normalization failed, using original query")
    return query


def _expand_keywords(query: str) -> List[str]:
    """Expand query into RU+UA+DE keywords for DB search using local synonym tables (free, no API calls)."""
    query_lower = query.lower().strip()
    words = [w.strip("?!.,;:") for w in query_lower.split() if len(w.strip("?!.,;:")) > 2]

    # Static synonym expansion tables
    SYNONYMS = {
        # Dentists / medical
        "стоматолог": ["стоматолог", "зубной", "зубна", "дантист", "zahnarzt", "dentist",
                       "стоматология", "стоматологическая", "лечение зубов"],
        "зубной": ["зубной", "зубна", "стоматолог", "дантист", "zahnarzt", "dentist"],
        "стоматология": ["стоматолог", "стоматология", "zahnarzt", "зубной"],
        "zahnarzt": ["zahnarzt", "zahnärzte", "zahnbehandlung", "dentist", "стоматолог"],
        "врач": ["врач", "врачи", "доктор", "лікар", "arzt", "ärzte", "доктор", "медик"],
        "доктор": ["доктор", "врач", "врачи", "лікар", "arzt", "arztpraxis"],
        # Hair / beauty
        "парикмахер": ["парикмахер", "парикмахерская", "перукар", "friseur", "friseurin",
                       "барбер", "barber", "стрижка", "стрижки", "стригу", "стригут"],
        "стрижка": ["стрижка", "стрижки", "стригу", "прическа", "укладка", "зачіска", "friseur"],
        # Services / repairs
        "ремонт": ["ремонт", "ремонту", "отремонтировать", "ремонтирование", "reparieren", "reparatur",
                   "муж на час", "чоловік на годину", "бытовой мастер", "побутовий майстер"],
        "муж": ["муж на час", "чоловік на годину", "мастер", "майстер", "ремонт", "сборка мебели",
                "збірка меблів", "установка кухни", "встановлення кухні", "подключение техники"],
        "мебел": ["мебель", "мебели", "меблів", "сборка мебели", "збірка меблів", "кухня", "кухні"],
        "кухн": ["кухня", "кухни", "кухні", "установка кухни", "встановлення кухні", "мебель", "меблів"],
        "подключ": ["подключение", "подключить", "підключення", "підключити", "электроприбор", "електроприлад"],
        "перевозк": ["перевозк", "перевезен", "транспорт", "transport", "umzug"],
        "уборк": ["уборк", "убираю", "клининг", "reinigung", "putzen"],
        # Education / language
        "мов": ["мов", "язык", "sprache", "language", "німецьк", "deutsch", "english"],
        "курс": ["курс", "курси", "навчання", "обучение", "unterricht", "тренинг"],
        # Documents / legal
        "документ": ["документ", "документи", "документы", "unterlagen", "dokument"],
        "виза": ["виза", "віза", "visum", "aufenthalt"],
    }

    # Find matching synonyms and also include original query words
    result = set()
    for w in words:
        result.add(w)
        # Check exact match
        if w in SYNONYMS:
            for s in SYNONYMS[w]:
                result.add(s)
        # Check substring match (e.g. "стоматолог" matches "стоматологи")
        for key, syns in SYNONYMS.items():
            if key in w or w in key:
                for s in syns:
                    result.add(s)

    # Add keyword variants with city name (common search pattern)
    has_city = any(c in query_lower for c in ["потсдам", "potsdam", "берлин", "berlin"])
    if has_city:
        result_copy = list(result)
        for kw in result_copy:
            result.add(f"{kw} потсдам")
            result.add(f"{kw} potsdam")

    final = [kw for kw in result if kw]
    logger.info(f"Local keyword expansion: {len(final)} keywords")
    return final if final else query.split()


def _rerank(query: str, messages: List[Dict], top_k: int = 25) -> List[Dict]:
    """Select the most relevant messages from the candidates pool."""
    if len(messages) <= top_k:
        return messages

    # Limit input to avoid oversized prompts (>150 msgs × 200 chars ≈ ~10k tokens)
    candidates = messages[:150]
    numbered = "\n".join(
        f"{i}: [{m['date']}] @{m['user']}: {m['text'][:200]}"
        for i, m in enumerate(candidates)
    )
    prompt = (
        f"Запит користувача: \"{query}\"\n\n"
        f"Нижче {len(candidates)} повідомлень з чату Потсдама.\n"
        f"Вибери індекси {top_k} найрелевантніших для відповіді на запит.\n\n"
        f"ПРАВИЛА РАНЖУВАННЯ (важливо!):\n"
        f"1. ПОВИДОМЛЕННЯ ДЕ ХТОСЬ ПРОПОНУЄ ПОСЛУГУ — НАЙВИЩИЙ ПРІОРИТЕТ "
        f"(напр. 'роблю стрижки', 'можу постригти', 'приймаю запис', 'є вільний час', 'пишіть в ЛС')\n"
        f"2. ПОВИДОМЛЕННЯ З РЕКОМЕНДАЦІЄЮ — високий пріоритет "
        f"(напр. 'рекомендую @username', 'зверніться до @username', 'у @username чудово робить')\n"
        f"3. ПОВИДОМЛЕННЯ З КОНКРЕТНИМИ ФАКТАМИ/КОНТАКТАМИ — середній пріоритет\n"
        f"4. ЗАПИТИ/ПИТАННЯ ВІД ІНШИХ КОРИСТУВАЧІВ ('хто робить?', 'порадьте', 'шукаю') — НИЗЬКИЙ ПРІОРИТЕТ, "
        f"включай ТІЛЬКИ якщо є відповідь у ланцюжку повідомлень\n"
        f"5. Загальні розмови без конкретики — НАЙНИЖЧИЙ ПРІОРИТЕТ\n\n"
        f"Свіжіші повідомлення мають перевагу над старими.\n"
        f"Повернути ТІЛЬКИ JSON масив індексів, наприклад: [0, 3, 7, 12]\n\n"
        f"{numbered}"
    )
    try:
        raw = _call_ai(prompt, max_tokens=1500, timeout=30)
        if not raw or raw == "RATE_LIMIT":
            raise RuntimeError("rerank AI call returned empty")
        match = re.search(r'\[[\d,\s]+\]', raw)
        if match:
            indices = json.loads(match.group())
            selected = [candidates[i] for i in indices if 0 <= i < len(candidates)]
            if selected:
                logger.info(f"Re-ranked: {len(messages)} → {len(selected)} messages")
                return selected
    except Exception as e:
        logger.warning(f"Re-ranking failed: {e}")
    return candidates[:top_k]


# ── Output ────────────────────────────────────────────────────────────────

def _send_answer(message, text: str) -> None:
    text = text.replace("**", "").replace("*", "\u2022")
    MAX = 3900
    if len(text) <= MAX:
        message.reply_text(text, parse_mode=None)
        return
    parts = text.split("\n\n")
    chunk = ""
    for part in parts:
        if len(chunk) + len(part) + 2 > MAX:
            if chunk:
                message.reply_text(chunk.strip(), parse_mode=None)
            chunk = part
        else:
            chunk = chunk + "\n\n" + part if chunk else part
    if chunk:
        message.reply_text(chunk.strip(), parse_mode=None)


# ── Main handler ──────────────────────────────────────────────────────────

def handle_ai_query(update: Update, context: CallbackContext) -> None:
    """Main handler: FAQ lookup first, then deep chat history search."""
    if not update.message or not update.message.text:
        return
    query = _extract_query(update.message.text)
    if query is None:
        return

    try:
        context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    except Exception:
        pass

    # ── 0. Normalize long queries ─────────────────────────────────────────
    # Distill verbose/conversational messages into a concise search intent
    # before any further processing (FAQ lookup, keyword expansion, rerank).
    if len(query) > 120:
        query = _normalize_query(query)

    # ── 1. FAQ fast path ──────────────────────────────────────────────────
    # Only use FAQ for short, direct topic questions.
    # Skip it when:
    #   • query contains chat-history intent ("що писали в чаті", "знайди", "сводку", …)
    #   • query is temporal ("на выходные", "сегодня", "ближайшие") — needs live DB data
    #   • query is long/complex (> 120 chars — clearly not a simple lookup)
    faq = _load_faq()
    _use_faq = (
        not _is_chat_history_query(query)
        and not _is_temporal_query(query)
        and len(query) <= 120
    )
    faq_answer = _search_faq(query, faq) if _use_faq else None

    if faq_answer:
        logger.info(f"FAQ hit for query: {query!r}")
        faq_context = f"\n\n--- ДОВІДКОВА ІНФОРМАЦІЯ (FAQ) ---\n{faq_answer}\n--- КІНЕЦЬ FAQ ---\n"
    else:
        faq_context = ""

    # ── 2. Deep chat history search ───────────────────────────────────────
    session = DBSession()
    try:
        chat_ids = [c.id for c in session.query(Chat).filter(Chat.enable == 1).all()]
        if not chat_ids:
            update.message.reply_text("Немає активних чатів для пошуку.")
            return

        is_temporal = _is_temporal_query(query)
        is_provider_query = _is_service_provider_query(query)
        anchor_words = _get_anchor_words(query)

        logger.info(f"Query: {query!r} | temporal: {is_temporal} | provider: {is_provider_query} | anchor: {anchor_words}")

        # ── Step A: Vector search (primary) ───────────────────────────────
        col = _get_chroma()
        vector_ready = col is not None and _chroma_count > 0  # use cached count, never call col.count() again

        if vector_ready:
            if is_temporal:
                # Temporal: first try last 14 days, fallback to 90 days
                vec_msgs = _vector_search(query, n_results=50, since_days=14)
                if len(vec_msgs) < 5:
                    logger.info("Too few temporal results in 14d, expanding to 90d")
                    vec_msgs = _vector_search(query, n_results=50, since_days=90)
            else:
                # Non-temporal: last year first, fallback all time
                vec_msgs = _vector_search(query, n_results=50, since_days=365)
                if len(vec_msgs) < 5:
                    logger.info("Too few results in 365d, searching all time")
                    vec_msgs = _vector_search(query, n_results=50)
        else:
            logger.warning("ChromaDB not ready, falling back to keyword search")
            vec_msgs = []

        # ── Step B: Keyword fallback / supplement ────────────────────────
        if not vector_ready:
            # ChromaDB not available — full keyword search
            keywords = _expand_keywords(query)
            if _is_hair_query(query):
                keywords += _HAIR_SERVICE_KEYWORDS
            if is_temporal:
                keywords += _get_upcoming_date_patterns(14)
            keyword_msgs = _search_keywords_with_fallback(
                session, chat_ids, keywords, anchor_words=anchor_words,
                provider_query=is_provider_query,
            )
        else:
            # ChromaDB available — use keywords only for anchor words (high precision)
            keyword_msgs = []
            if anchor_words:
                keyword_msgs = _search_keywords_with_fallback(
                    session, chat_ids, [], anchor_words=anchor_words,
                    provider_query=is_provider_query,
                )

        # Provider names and @usernames live in the user table, not in the
        # message body. Always add their complete posting history as an
        # independent retrieval channel.
        author_msgs = []
        if is_provider_query:
            author_terms = _provider_author_terms(query)
            author_rows = _search_provider_authors(
                session, chat_ids, author_terms, limit=40,
            )
            author_msgs = _rows_to_dicts(author_rows)
            logger.info(
                f"Provider author search: {len(author_msgs)} messages for {author_terms}"
            )

        # ── Step C: Recent posts for temporal queries ────────────────────
        if is_temporal:
            recent_msgs = _search_recently_posted(session, chat_ids, days=14, limit=60)
            logger.info(f"Recent 14d posts: {len(recent_msgs)}")
        elif is_provider_query:
            # For provider/contact searches, random recent chatter pollutes the answer.
            recent_msgs = []
        else:
            recent_msgs = _search_recent(session, chat_ids, limit=10)

        ranked_sources = [items for items in (vec_msgs, keyword_msgs, author_msgs) if items]
        if len(ranked_sources) > 1:
            fused = _rrf_merge(ranked_sources)
        else:
            fused = ranked_sources[0] if ranked_sources else []
        all_candidates = fused + recent_msgs
        if is_provider_query:
            before_filter = len(all_candidates)
            # Soft boost for provider-like messages, but don't drop seekers
            scored = sorted(all_candidates,
                key=lambda m: (_provider_signal_score(m), m.get("date", "")),
                reverse=True)
            all_candidates = _group_provider_history(scored)
            logger.info(f"Provider boost: {before_filter} candidates sorted by signal score")
        if not all_candidates:
            update.message.reply_text("В базі немає повідомлень для відповіді.")
            return

        logger.info(
            f"Candidates: {len(vec_msgs)} vector + {len(keyword_msgs)} keyword + "
            f"{len(author_msgs)} author + {len(recent_msgs)} recent = {len(all_candidates)}"
        )

        # ── Step D: Re-rank top-25 ───────────────────────────────────────
        top_msgs = _rerank(query, all_candidates, top_k=25)

        # Step F: build context string
        ctx = _build_context(top_msgs)
        today = datetime.utcnow().strftime("%Y-%m-%d")

        prompt = (
            f"{SYSTEM_PROMPT_CHAT.format(today=today)}\n\n"
            f"--- КОНТЕКСТ З ЧАТУ ---\n{ctx}\n"
            f"--- КІНЕЦЬ КОНТЕКСТУ ---\n"
            f"{faq_context}"
            f"Питання: {query}"
        )

        # Step G: main answer
        answer = _call_ai(prompt, max_tokens=8192, timeout=120)

        if answer == "RATE_LIMIT":
            update.message.reply_text("Перевищено ліміт запитів до AI. Спробуйте через хвилину.")
            return
        if not answer:
            update.message.reply_text("AI не зміг сформувати відповідь. Спробуйте пізніше.")
            return
        _send_answer(update.message, answer)

    except Exception as e:
        logger.error(f"AI handler error: {e}", exc_info=True)
        try:
            update.message.reply_text("Виникла помилка. Спробуйте пізніше.")
        except Exception:
            pass
    finally:
        session.close()


# Handler registration
handler = MessageHandler(
    Filters.regex(
        r"(?i)(?<!\w)(?:" + "|".join(
            re.escape(alias) for alias in sorted(TRIGGER_ALIASES, key=len, reverse=True)
        ) + r")(?!\w)"
    ) & (~Filters.command),
    handle_ai_query,
)
