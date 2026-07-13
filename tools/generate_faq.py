"""
One-time FAQ generator from chat history.

Scans the DB by topic categories, sends message batches to Gemini,
extracts structured FAQ entries, saves to faq_generated.json for review.

Usage (inside container or locally with DB access):
    python3 tools/generate_faq.py

Output: tools/faq_generated.json  — review and merge into /app/config/faq.json
"""

import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Optional

import requests
from sqlalchemy import or_, create_engine
from sqlalchemy.orm import sessionmaker

# ── paths ──────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent
REPO_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(REPO_ROOT))

from database import Message, User, Chat, Base

DB_PATH = os.getenv("DB_PATH", "/app/config/bot.db")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
OUTPUT_FILE = SCRIPT_DIR / "faq_generated.json"
EXISTING_FAQ = os.getenv("FAQ_PATH", "/app/config/faq.json")

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "{model}:generateContent?key={api_key}"
)

# ── topic categories ────────────────────────────────────────────────────────
# Each category: name, keywords for DB search, faq_keywords (for matcher)
CATEGORIES = [
    {
        "name": "Ортопеди / Травматологи",
        "search": ["ортопед", "травматолог", "orthopäde", "orthopäd"],
        "faq_keywords": ["ортопед", "ортопеди", "ортопедів", "ортопеды", "травматолог", "orthopäde"],
    },
    {
        "name": "Стоматологи",
        "search": ["стоматолог", "zahnarzt", "зубной", "зубний", "dentist"],
        "faq_keywords": ["стоматолог", "стоматолога", "зубний", "зубной", "zahnarzt", "dentist"],
    },
    {
        "name": "Терапевти / Сімейні лікарі",
        "search": ["терапевт", "hausarzt", "семейный врач", "сімейний лікар", "allgemeinmedizin"],
        "faq_keywords": ["терапевт", "hausarzt", "сімейний лікар", "семейный врач"],
    },
    {
        "name": "Педіатри / Дитячі лікарі",
        "search": ["педіатр", "педиатр", "kindearzt", "kinderarzt", "дитячий лікар"],
        "faq_keywords": ["педіатр", "педиатр", "kinderarzt", "дитячий лікар"],
    },
    {
        "name": "Гінекологи",
        "search": ["гінеколог", "гинеколог", "gynäkologe", "frauenarzt"],
        "faq_keywords": ["гінеколог", "гинеколог", "gynäkologe", "frauenarzt"],
    },
    {
        "name": "Неврологи / Психологи / Психіатри",
        "search": ["невролог", "психолог", "психіатр", "psychiater", "neurologe", "нейролог"],
        "faq_keywords": ["невролог", "психолог", "психіатр", "psychiater", "neurologe"],
    },
    {
        "name": "Офтальмологи / Окулісти",
        "search": ["офтальмолог", "окулист", "augearzt", "augenarzt"],
        "faq_keywords": ["офтальмолог", "окулист", "augenarzt"],
    },
    {
        "name": "Дерматологи",
        "search": ["дерматолог", "hautarzt"],
        "faq_keywords": ["дерматолог", "hautarzt"],
    },
    {
        "name": "Jobcenter / Робота / Ausbildung",
        "search": ["jobcenter", "job center", "ausbildung", "биржа труда", "біржа праці"],
        "faq_keywords": ["jobcenter", "ausbildung", "біржа праці", "биржа труда"],
    },
    {
        "name": "Школи / Kita / Садки",
        "search": ["kita", "kindergarten", "schule", "школа", "садок", "садик"],
        "faq_keywords": ["kita", "kindergarten", "schule", "школа", "садок"],
    },
    {
        "name": "Ausländerbehörde / Амт / ВНЖ / Aufenthaltstitel",
        "search": ["ausländerbehörde", "aufenthaltstitel", "auslanderbehor", "амт", "amt"],
        "faq_keywords": ["ausländerbehörde", "aufenthaltstitel", "амт"],
    },
    {
        "name": "Курси мови / Інтеграція",
        "search": ["sprachkurs", "integrationskurs", "курс мови", "курсы немецкого", "deutsch"],
        "faq_keywords": ["sprachkurs", "integrationskurs", "курс мови", "курсы немецкого"],
    },
    {
        "name": "Tafel / Їжа / Гуманітарка",
        "search": ["tafel", "lebensmittel", "їжа безкоштовно", "гуманитарка", "гумдопомога"],
        "faq_keywords": ["tafel", "їжа", "гуманитарка", "гумдопомога", "lebensmittel"],
    },
    {
        "name": "Житло / Квартири / Wohnung",
        "search": ["wohnung", "wohnheim", "квартира", "житло", "mietwohnung"],
        "faq_keywords": ["wohnung", "wohnheim", "квартира", "житло"],
    },
    {
        "name": "Банки / Фінанси / Рахунок",
        "search": ["konto", "банк", "банка", "рахунок", "счёт", "sparkasse", "volksbank"],
        "faq_keywords": ["konto", "банк", "рахунок", "sparkasse", "volksbank"],
    },
    {
        "name": "Страховка / Krankenversicherung",
        "search": ["krankenversicherung", "versicherung", "страховка", "страхування", "TK ", "AOK ", "Barmer"],
        "faq_keywords": ["krankenversicherung", "versicherung", "страховка", "страхування"],
    },
    {
        "name": "Юридична допомога / Rechtsberatung",
        "search": ["rechtsberatung", "anwalt", "юрист", "правова допомога", "правовая помощь"],
        "faq_keywords": ["rechtsberatung", "anwalt", "юрист", "правова допомога"],
    },
    {
        "name": "Транспорт / Проїзд / VBB",
        "search": ["vbb", "deutschlandticket", "ticket", "квиток", "проїзний", "проездной"],
        "faq_keywords": ["vbb", "deutschlandticket", "квиток", "проїзний"],
    },
    {
        "name": "Волонтери / Допомога",
        "search": ["волонтер", "доброволец", "допомога", "помощь", "freiwillig"],
        "faq_keywords": ["волонтер", "доброволец", "допомога", "freiwillig"],
    },
    {
        "name": "Дитячі гуртки / Klubs / Ивенти для дітей",
        "search": ["детский клуб", "дитячий клуб", "детский кружок", "kinder", "для детей", "для дітей"],
        "faq_keywords": ["дитячий клуб", "детский клуб", "kinder", "для детей"],
    },
]

# ── DB setup ────────────────────────────────────────────────────────────────

def get_session():
    engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})
    Session = sessionmaker(bind=engine)
    return Session()


# ── message fetching ────────────────────────────────────────────────────────

def fetch_messages_for_category(session, chat_ids: List[int],
                                  search_keywords: List[str],
                                  limit: int = 60,
                                  days_back: int = 730) -> List[Dict]:
    """Fetch messages matching any search keyword, ordered newest first."""
    cutoff = datetime.utcnow() - timedelta(days=days_back)
    rows = (
        session.query(Message, User)
        .outerjoin(User, Message.from_id == User.id)
        .filter(Message.from_chat.in_(chat_ids))
        .filter(Message.text.isnot(None))
        .filter(Message.text != "")
        .filter(Message.date >= cutoff)
        .filter(or_(*[Message.text.ilike(f"%{kw}%") for kw in search_keywords]))
        .order_by(Message.date.desc())
        .limit(limit)
        .all()
    )
    result = []
    for msg, user in rows:
        username = (user.username or user.fullname or "?") if user else "?"
        date_str = msg.date.strftime("%Y-%m-%d") if hasattr(msg.date, "strftime") else "?"
        result.append({
            "text": (msg.text or "").strip(),
            "user": username,
            "date": date_str,
        })
    return result


# ── Gemini call ─────────────────────────────────────────────────────────────

EXTRACT_PROMPT = """
Ти аналітик чату жителів Потсдама (Німеччина). Нижче — повідомлення з чату по темі: "{category}".

Твоє завдання: якщо в повідомленнях є конкретна корисна інформація (контакти лікарів, адреси, телефони, ціни, посилання, практичні поради) — витягни її у вигляді одного FAQ-запису.

Правила:
- Відповідай ТІЛЬКИ JSON або null.
- Якщо корисної конкретної інформації немає — поверни: null
- Якщо є — поверни JSON об'єкт:
  {{
    "answer": "Конкретна корисна інформація на українській мові. Структуровано, чітко. Включай імена, адреси, телефони, посилання якщо є.",
    "source_date": "YYYY-MM-DD"
  }}
- answer — ТІЛЬКИ конкретні факти, не загальні слова. Максимум 800 символів.
- source_date — дата найсвіжішого корисного повідомлення.
- НЕ включай рекламу, флуд, жарти, питання без відповідей.

Повідомлення з чату:
{messages}

Відповідь (тільки JSON або null, без пояснень):
"""


def call_gemini_extract(category_name: str, messages: List[Dict]) -> Optional[Dict]:
    """Ask Gemini to extract a FAQ entry from messages. Returns dict or None."""
    if not GEMINI_API_KEY or not messages:
        return None

    msg_text = "\n---\n".join(
        f"[{m['date']}] @{m['user']}: {m['text'][:400]}" for m in messages
    )
    prompt = EXTRACT_PROMPT.format(category=category_name, messages=msg_text)

    url = GEMINI_URL.format(model=GEMINI_MODEL, api_key=GEMINI_API_KEY)
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "maxOutputTokens": 1024,
            "temperature": 0.1,
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
    try:
        resp = requests.post(url, json=payload, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
        raw = "".join(p.get("text", "") for p in parts).strip()

        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        raw = raw.strip()

        if raw.lower() == "null" or not raw:
            return None
        return json.loads(raw)
    except json.JSONDecodeError as e:
        log.warning(f"JSON parse error for {category_name!r}: {e} | raw: {raw[:100]}")
        return None
    except Exception as e:
        log.warning(f"Gemini error for {category_name!r}: {e}")
        return None


# ── existing FAQ loader ──────────────────────────────────────────────────────

def load_existing_faq() -> List[Dict]:
    try:
        with open(EXISTING_FAQ, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


# ── main ─────────────────────────────────────────────────────────────────────

def main():
    if not GEMINI_API_KEY:
        log.error("GEMINI_API_KEY not set. Export it before running.")
        sys.exit(1)

    session = get_session()
    chat_ids = [c.id for c in session.query(Chat).filter(Chat.enable == 1).all()]
    if not chat_ids:
        log.error("No enabled chats found in DB.")
        sys.exit(1)
    log.info(f"Active chat IDs: {chat_ids}")

    existing_faq = load_existing_faq()
    log.info(f"Existing FAQ entries: {len(existing_faq)}")

    generated = []
    skipped = 0

    for cat in CATEGORIES:
        name = cat["name"]
        log.info(f"\n{'='*50}\nProcessing: {name}")

        msgs = fetch_messages_for_category(session, chat_ids, cat["search"], limit=60)
        log.info(f"  Found {len(msgs)} messages in DB")

        if len(msgs) < 2:
            log.info("  Too few messages, skipping")
            skipped += 1
            continue

        entry = call_gemini_extract(name, msgs)

        if entry is None:
            log.info("  Gemini: no useful info found")
            skipped += 1
        else:
            answer = entry.get("answer", "").strip()
            if not answer or len(answer) < 30:
                log.info("  Gemini returned empty/short answer, skipping")
                skipped += 1
                continue

            faq_entry = {
                "keywords": cat["faq_keywords"],
                "answer": answer,
                "source_date": entry.get("source_date", ""),
                "_category": name,   # metadata for review, remove before merge
            }
            generated.append(faq_entry)
            log.info(f"  Extracted: {answer[:80]}...")

        # Rate limit: ~2 req/sec to stay safe
        time.sleep(0.6)

    session.close()

    # Save results
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(generated, f, ensure_ascii=False, indent=2)

    log.info(f"\n{'='*50}")
    log.info(f"Done. Generated: {len(generated)}, Skipped: {skipped}")
    log.info(f"Output: {OUTPUT_FILE}")
    log.info("Review the file, remove '_category' fields, then merge into faq.json")


if __name__ == "__main__":
    main()
