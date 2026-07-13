"""
Weekly job: scan ALL topic categories for new FAQ entries.
For each of 53 categories, fetches recent messages from the DB,
extracts practical facts via Gemini, deduplicates, and auto-saves to faq.json.
Admin gets a summary. No manual intervention needed.
"""

import json
import logging
import os
import time
from datetime import datetime, timedelta
from typing import List, Dict

import requests
from telegram.ext import CallbackContext

from database import Chat, DBSession, Message, User

logger = logging.getLogger(__name__)

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
AI_MODEL           = os.getenv("AI_MODEL", "moonshotai/kimi-k2")
ADMIN_ID           = int(os.getenv("ADMIN_ID", "312029534"))
FAQ_PATH           = os.getenv("FAQ_PATH", "/app/config/faq.json")
LEARN_DAYS         = int(os.getenv("FAQ_LEARN_DAYS", "30"))    # look back N days
MSGS_PER_CAT       = int(os.getenv("FAQ_MSGS_PER_CAT", "100")) # messages fetched per category
SLEEP_BETWEEN      = float(os.getenv("FAQ_SLEEP", "1.5"))       # seconds between AI calls

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# ── All topic categories (53 topics) ─────────────────────────────────────────
CATEGORIES = [
    # ── Medical ──────────────────────────────────────────────────────────────
    {"name": "Терапевт / Hausarzt",
     "search": ["терапевт", "hausarzt", "семейный врач", "сімейний лікар", "116117"]},
    {"name": "Педіатр / Kinderarzt",
     "search": ["педіатр", "педиатр", "kinderarzt", "дитячий лікар", "детский врач"]},
    {"name": "Стоматолог / Zahnarzt",
     "search": ["стоматолог", "zahnarzt", "зубной", "зубний"]},
    {"name": "Гінеколог / Frauenarzt",
     "search": ["гінеколог", "гинеколог", "gynäkologe", "frauenarzt"]},
    {"name": "Ортопед / Травматолог",
     "search": ["ортопед", "травматолог", "orthopäde"]},
    {"name": "Невролог / Психолог / Психіатр",
     "search": ["невролог", "психолог", "психіатр", "psychiater", "neurologe"]},
    {"name": "Офтальмолог / Augenarzt",
     "search": ["офтальмолог", "окулист", "augenarzt", "окуліст"]},
    {"name": "Дерматолог / Hautarzt",
     "search": ["дерматолог", "hautarzt"]},
    {"name": "Онколог / Кардіолог / Уролог / Ендокринолог",
     "search": ["онколог", "кардіолог", "кардиолог", "уролог", "ендокринолог"]},
    {"name": "Логопед / Ерготерапевт",
     "search": ["логопед", "ergo", "ерготерапевт", "мовлення"]},
    {"name": "Медичний перекладач",
     "search": ["перекладач лікар", "переводчик врач", "dolmetscher arzt"]},
    {"name": "Krankenversicherung / Страховка",
     "search": ["krankenversicherung", "страховка", "страхування", "AOK", "TK", "Barmer"]},
    {"name": "Ліки / Рецепт / Аптека",
     "search": ["рецепт", "rezept", "ліки", "лекарства", "аптека", "apotheke"]},
    # ── Documents / Government ────────────────────────────────────────────────
    {"name": "Ausländerbehörde / ВНЖ",
     "search": ["ausländerbehörde", "aufenthaltstitel", "внж", "fiktionsbescheinigung"]},
    {"name": "Jobcenter / Bürgergeld",
     "search": ["jobcenter", "bürgergeld", "ausbildung", "beralter", "берате"]},
    {"name": "Sozialamt",
     "search": ["sozialamt", "соціальний", "социальный"]},
    {"name": "Familienkasse / Kindergeld",
     "search": ["familienkasse", "kindergeld", "kinderzuschlag", "кіндергельт"]},
    {"name": "Паспорт / Посольство / Консульство",
     "search": ["паспорт", "посольство", "консульство", "закордонний паспорт", "embassy"]},
    {"name": "Anmeldung / Реєстрація",
     "search": ["anmeldung", "прописка", "реєстрація", "meldeadresse"]},
    {"name": "BAMF / Інтеграційний курс / Zuweisung",
     "search": ["bamf", "zuweisung", "цувайзунг", "integrationskurs", "infopoint"]},
    {"name": "Wohngeld",
     "search": ["wohngeld", "вонгельд", "субсидія на житло"]},
    {"name": "Steuer / Finanzamt / Податки",
     "search": ["steuer", "steuernummer", "finanzamt", "податок", "налог"]},
    # ── Housing ───────────────────────────────────────────────────────────────
    {"name": "Пошук квартири / WBS",
     "search": ["wohnung", "квартира", "житло", "wbs", "wohnungssuche"]},
    {"name": "Kaution / Залог",
     "search": ["kaution", "залог", "завдаток", "kautionshilfe"]},
    {"name": "Меблі / Kleinanzeigen / Безкоштовно",
     "search": ["kleinanzeigen", "меблі безкоштовно", "мебель бесплатно", "меблі б/в"]},
    {"name": "Ремонт / Handwerker / Майстер",
     "search": ["handwerker", "майстер", "ремонт", "сантехнік", "електрик"]},
    # ── Transport ─────────────────────────────────────────────────────────────
    {"name": "VBB / Deutschlandticket / Проїзний",
     "search": ["vbb", "deutschlandticket", "mobilitätsticket", "проїзний"]},
    {"name": "Авто / Führerschein / TÜV",
     "search": ["führerschein", "водійські права", "tüv", "kfz", "haftpflicht"]},
    # ── Work & Education ──────────────────────────────────────────────────────
    {"name": "Робота / Minijob / Arbeit",
     "search": ["arbeit", "minijob", "робота", "работа", "teilzeit"]},
    {"name": "Курси німецької / Sprachkurs",
     "search": ["sprachkurs", "deutschkurs", "курс мови", "vhs", "volkshochschule"]},
    {"name": "Диплом / Визнання освіти / Anabin",
     "search": ["anabin", "диплом", "визнання", "studium", "anerkennung"]},
    {"name": "Репетитор / Nachhilfe",
     "search": ["репетитор", "nachhilfe", "навчання дітей"]},
    # ── Financial & Legal ─────────────────────────────────────────────────────
    {"name": "Банк / Konto / Sparkasse",
     "search": ["konto", "банк", "рахунок", "sparkasse", "volksbank"]},
    {"name": "Юрист / Rechtsberatung / Anwalt",
     "search": ["rechtsberatung", "anwalt", "юрист", "правова допомога", "адвокат"]},
    {"name": "Переклади / Присяжний перекладач",
     "search": ["beglaubigte übersetzung", "присяжний перекладач", "переклад документів"]},
    # ── Food & Social ─────────────────────────────────────────────────────────
    {"name": "Tafel / Їжа безкоштовно / DRK",
     "search": ["tafel", "lebensmittel", "їжа безкоштовно", "гуманитарка", "drk"]},
    {"name": "AWO / Caritas / Консультації організацій",
     "search": ["awo", "caritas", "diakonie", "migrationsberatung", "kindermut",
                "schillertreff", "neuendorfer", "sellostrasse"]},
    {"name": "Психологічна допомога / Криза / Trauma",
     "search": ["психолог безкоштовно", "психолог бесплатно", "травма", "криза", "krisenchat"]},
    {"name": "Домашнє насилля / Frauenhaus",
     "search": ["домашнє насилля", "frauenhaus", "насилля", "насилие"]},
    # ── Children & Education ──────────────────────────────────────────────────
    {"name": "Школа / Schule",
     "search": ["schule", "schulamt", "школа", "willkommensklasse"]},
    {"name": "Kita / Дитячий садок",
     "search": ["kita", "krippe", "kindergarten", "дитячий садок", "kita platz"]},
    {"name": "Дитячі гуртки / Sportverein",
     "search": ["sportverein", "спортивна секція", "гурток", "кружок", "kinder sport"]},
    {"name": "Літній табір / Sommercamp",
     "search": ["sommercamp", "feriencamp", "літній табір", "летний лагерь"]},
    # ── Recreation & Culture ──────────────────────────────────────────────────
    {"name": "Заходи / Events для українців",
     "search": ["захід для українців", "концерт", "veranstaltung", "захід"]},
    {"name": "Басейн / Fitnessstudio / Schwimmbad",
     "search": ["schwimmbad", "басейн", "fitnessstudio", "gym", "wellenbad"]},
    {"name": "Бібліотека / Stadtbibliothek",
     "search": ["bibliothek", "stadtbibliothek", "бібліотека"]},
    {"name": "Музеї / Культура",
     "search": ["museum", "музей", "ausstellung", "kostenlos museum"]},
    # ── Practical everyday ────────────────────────────────────────────────────
    {"name": "Інтернет / SIM / Провайдер",
     "search": ["sim karte", "провайдер", "mobilfunk", "internet zuhause"]},
    {"name": "Розпечатати / Відсканувати / Copy-Shop",
     "search": ["drucken", "розпечатати", "copy shop", "сканувати"]},
    {"name": "Перукар / Friseur",
     "search": ["friseur", "перукар", "стрижка", "парикмахер"]},
    {"name": "Швея / Ательє / Ремонт одягу",
     "search": ["швея", "ательє", "ремонт одягу", "schneiderin"]},
    {"name": "Слюсар / Schlüsseldienst",
     "search": ["schlüsseldienst", "слюсар", "замок", "schlüssel"]},
    {"name": "Посилки / Nova Poshta / Перевезення Україна-Германія",
     "search": ["нова пошта", "nova poshta", "посилка", "перевезення"]},
]

EXTRACT_PROMPT = """\
Ти аналітик чату жителів Потсдама. Тема: "{category}".

Знайди до 3 найкорисніших практичних фактів із повідомлень нижче.
Хороші факти: конкретна адреса, телефон, посилання, ім'я фахівця, графік роботи, ціна, порада.
Пропускай: суперечки, скарги, запитання без відповіді, флуд, рекламу.

Формат СТРОГО:
ENTRY_START
KEYWORDS: слово1, слово2, слово3
ANSWER: конкретна практична відповідь (до 400 символів)
DATE: РРРР-ММ-ДД
ENTRY_END

Якщо корисних фактів немає — виведи тільки: NONE

Повідомлення:
{messages}"""


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except Exception as e:
        logger.error(f"Failed to load {path}: {e}")
        return default


def _save_json(path, data) -> bool:
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        logger.error(f"Failed to save {path}: {e}")
        return False


def _call_ai(prompt: str) -> str:
    if not OPENROUTER_API_KEY:
        return ""
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": AI_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 2048,
        "temperature": 0.0,
    }
    try:
        resp = requests.post(OPENROUTER_URL, headers=headers, json=payload, timeout=30)
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        logger.error(f"OpenRouter error: {e}")
        return ""


def _parse_response(raw: str, chunk_date: str) -> List[Dict]:
    if not raw or raw.strip().upper() == "NONE":
        return []
    entries = []
    for block in raw.split("ENTRY_START"):
        block = block.strip()
        if "ENTRY_END" not in block:
            continue
        block = block.split("ENTRY_END")[0].strip()
        fields = {}
        for line in block.splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                fields[k.strip().upper()] = v.strip()
        kw_raw = fields.get("KEYWORDS", "")
        answer = fields.get("ANSWER", "").strip()[:400]
        date = fields.get("DATE", chunk_date)
        keywords = [k.strip() for k in kw_raw.split(",") if k.strip()]
        if keywords and len(answer) >= 15:
            if not answer.endswith((".", "!", "?")):
                answer = answer.rstrip(",; ") + "."
            entries.append({"keywords": keywords, "answer": answer, "source_date": date})
    return entries


def _is_duplicate(new_entry: Dict, existing: List[Dict]) -> bool:
    new_kw = set(k.lower() for k in new_entry.get("keywords", []))
    for entry in existing:
        ex_kw = set(k.lower() for k in entry.get("keywords", []))
        if len(new_kw & ex_kw) >= 2:
            return True
    return False


def _fetch_category_messages(session, chat_ids, keywords, days, limit) -> List[str]:
    """Fetch recent messages matching any of the category keywords."""
    cutoff = datetime.utcnow() - timedelta(days=days)
    seen = set()
    msgs = []
    for kw in keywords:
        rows = (
            session.query(Message, User)
            .outerjoin(User, Message.from_id == User.id)
            .filter(Message.from_chat.in_(chat_ids))
            .filter(Message.text.isnot(None))
            .filter(Message.text != "")
            .filter(Message.date >= cutoff)
            .filter(Message.text.ilike(f"%{kw}%"))
            .filter(~Message.text.ilike("потсдамбот%"))
            .filter(~Message.text.ilike("потбот%"))
            .order_by(Message.date.desc())
            .limit(limit)
            .all()
        )
        for msg, user in rows:
            if msg._id in seen:
                continue
            seen.add(msg._id)
            uname = (user.username or user.fullname or "?") if user else "?"
            date_str = msg.date.strftime("%Y-%m-%d") if hasattr(msg.date, "strftime") else ""
            text = (msg.text or "").strip().replace("\n", " ")[:300]
            msgs.append(f"[{date_str}] @{uname}: {text}")
        if len(msgs) >= limit:
            break
    return msgs[:limit]


# ── Main job ──────────────────────────────────────────────────────────────────

def run_faq_learn(context: CallbackContext) -> None:
    """Scheduler entry point — immediately hands off to a daemon thread
    so the job-queue thread is freed and the bot stays responsive."""
    import threading
    t = threading.Thread(target=_faq_learn_worker, args=(context,), daemon=True, name="faq-learn")
    t.start()


def _faq_learn_worker(context: CallbackContext) -> None:
    """Weekly job: scan all topic categories and extract new FAQ entries."""
    logger.info("FAQ category-learn job started")

    session = DBSession()
    try:
        chat_ids = [c.id for c in session.query(Chat).filter(Chat.enable == 1).all()]
    finally:
        session.close()

    if not chat_ids:
        logger.info("No active chats, skipping FAQ learn")
        return

    existing_faq = _load_json(FAQ_PATH, [])
    all_entries = list(existing_faq)  # working copy for dedup
    new_entries = []

    total_cats = len(CATEGORIES)
    for idx, cat in enumerate(CATEGORIES, 1):
        name = cat["name"]
        keywords = cat["search"]
        logger.info(f"\n[{idx}/{total_cats}] {name}")

        session = DBSession()
        try:
            msgs = _fetch_category_messages(session, chat_ids, keywords, LEARN_DAYS, MSGS_PER_CAT)
        finally:
            session.close()

        if not msgs:
            logger.info("  0 messages found, skipping")
            continue

        logger.info(f"  {len(msgs)} messages found")

        # Process in chunks of 50 messages
        chunk_size = 50
        for i in range(0, len(msgs), chunk_size):
            chunk = msgs[i:i + chunk_size]
            chunk_date = chunk[0][1:11] if chunk else ""
            prompt = EXTRACT_PROMPT.format(category=name, messages="\n".join(chunk))

            raw = _call_ai(prompt)
            if not raw:
                logger.info("  AI returned empty, skipping chunk")
                time.sleep(SLEEP_BETWEEN)
                continue

            entries = _parse_response(raw, chunk_date)
            for entry in entries:
                if _is_duplicate(entry, all_entries):
                    logger.info(f"  Duplicate: {entry['keywords'][:3]}")
                    continue
                kw3 = ", ".join(entry["keywords"][:3])
                ans80 = entry["answer"][:80]
                logger.info(f"  + [{kw3}]: {ans80}...")
                new_entries.append(entry)
                all_entries.append(entry)

            time.sleep(SLEEP_BETWEEN)

    if not new_entries:
        logger.info(f"No new FAQ entries found this week ({total_cats} categories checked)")
        try:
            context.bot.send_message(
                chat_id=ADMIN_ID,
                text=(
                    f"🤖 FAQ авто-навчання: нічого нового за {LEARN_DAYS} днів\n"
                    f"({total_cats} категорій перевірено, дублікатів не рахується)\n"
                    f"Всього в FAQ: {len(existing_faq)}"
                )
            )
        except Exception:
            pass
        return

    # Save
    updated_faq = existing_faq + new_entries
    if not _save_json(FAQ_PATH, updated_faq):
        logger.error("Failed to save updated faq.json")
        return

    logger.info(f"\n{'='*60}")
    logger.info(f"Done! Added {len(new_entries)} new entries. FAQ total: {len(updated_faq)}")

    # Summary to admin (grouped by category hint from keywords)
    summary_lines = []
    for e in new_entries:
        kw = ", ".join(e.get("keywords", [])[:3])
        ans = e.get("answer", "")[:80]
        summary_lines.append(f"• [{kw}]: {ans}")

    # Send in chunks (Telegram 4096 char limit)
    header = (
        f"🤖 FAQ авто-навчання: +{len(new_entries)} нових записів\n"
        f"({total_cats} категорій • {LEARN_DAYS} днів • всього в FAQ: {len(updated_faq)})\n\n"
    )
    body = "\n".join(summary_lines)
    full_text = header + body
    chunk_size_tg = 3800
    for i in range(0, len(full_text), chunk_size_tg):
        try:
            context.bot.send_message(chat_id=ADMIN_ID, text=full_text[i:i + chunk_size_tg])
        except Exception as e:
            logger.error(f"Failed to notify admin: {e}")
