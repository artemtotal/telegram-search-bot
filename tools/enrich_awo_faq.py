#!/usr/bin/env python3
"""
Extract comprehensive AWO FAQ entries from chat history and add to faq.json.
Run inside container: python tools/enrich_awo_faq.py
"""

import json
import logging
import os
import sys
import time

sys.path.insert(0, "/app")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

import requests
from database import DBSession, Message, Chat, User

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL   = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
FAQ_PATH       = os.getenv("FAQ_PATH", "/app/config/faq.json")

GEMINI_API_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "{model}:generateContent?key={api_key}"
)

AWO_SUBTOPICS = [
    {
        "name": "AWO загальне / всі сервіси",
        "keywords": ["awo", "консультаційні центри awo", "awo potsdam"],
        "limit": 200,
    },
    {
        "name": "AWO Анастасія / kindermut",
        "keywords": ["kindermut", "анастасія", "anastasia", "awo-potsdam.de"],
        "limit": 100,
    },
    {
        "name": "AWO Сергій / Neuendorfer / соціальні консультації",
        "keywords": ["neuendorfer", "sellostrasse", "shevchuk", "сергій", "awo консультац"],
        "limit": 100,
    },
    {
        "name": "AWO Schillertreff / сніданок",
        "keywords": ["schillertreff", "schillerplatz", "сніданок awo", "завтрак awo"],
        "limit": 80,
    },
    {
        "name": "AWO довготривала підтримка / житло / erlenhof",
        "keywords": ["erlenhof", "довготривал", "подтримк", "awo bezirksverband", "langzeit"],
        "limit": 80,
    },
    {
        "name": "AWO заходи / кафе / театр / erzählcafé",
        "keywords": ["erzählcafé", "theaterlabor", "театральна майстерня", "awo café", "sprachcafé awo"],
        "limit": 80,
    },
]

EXTRACT_PROMPT = """\
Проаналізуй повідомлення з Telegram-чату мешканців Потсдама, присвячені AWO (Arbeiterwohlfahrt) та її сервісам.

Витягни ВСІ корисні практичні факти: адреси, телефони, email, графіки роботи, ім'я спеціаліста, умови отримання допомоги.
Один ENTRY = один сервіс / одна людина / одна адреса. Можна до 5 entries.

Формат СТРОГО:
ENTRY_START
KEYWORDS: слово1, слово2, слово3
ANSWER: конкретна практична інформація (до 400 символів)
DATE: ГГГГ-ММ-ДД
ENTRY_END

Якщо немає нічого корисного — виведи NONE.

Повідомлення:
{messages}"""


def _load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def _save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _call_gemini(prompt: str) -> str:
    url = GEMINI_API_URL.format(model=GEMINI_MODEL, api_key=GEMINI_API_KEY)
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "maxOutputTokens": 3000,
            "temperature": 0.0,
            "thinkingConfig": {"thinkingBudget": 1024},
        },
    }
    resp = requests.post(url, json=payload, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
    return "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()


def _parse_response(raw: str, chunk_date: str):
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


def _is_duplicate(new_entry, existing):
    new_kw = set(k.lower() for k in new_entry.get("keywords", []))
    for entry in existing:
        ex_kw = set(k.lower() for k in entry.get("keywords", []))
        if len(new_kw & ex_kw) >= 2:
            return True
    return False


def _fetch_messages(keywords, limit):
    session = DBSession()
    try:
        chat_ids = [-1001724565311]
        seen = set()
        msgs = []
        for kw in keywords:
            rows = (session.query(Message, User)
                .outerjoin(User, Message.from_id == User.id)
                .filter(Message.from_chat.in_(chat_ids))
                .filter(Message.text.isnot(None))
                .filter(Message.text != "")
                .filter(Message.text.ilike(f"%{kw}%"))
                .filter(~Message.text.ilike("%потсдамбот%"))
                .filter(~Message.text.ilike("%посдамбот%"))
                .filter(~Message.text.ilike("%потсдам бот%"))
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
            if len(msgs) >= limit:
                break
        return msgs
    finally:
        session.close()


def main():
    faq = _load_json(FAQ_PATH, [])
    all_entries = list(faq)
    new_total = 0

    logger.info(f"Starting AWO FAQ enrichment. Current FAQ size: {len(faq)}")

    for subtopic in AWO_SUBTOPICS:
        name = subtopic["name"]
        keywords = subtopic["keywords"]
        limit = subtopic["limit"]

        logger.info(f"\n--- {name} ---")
        msgs = _fetch_messages(keywords, limit)
        logger.info(f"  Found {len(msgs)} messages")

        if not msgs:
            logger.info("  Skipping (no messages)")
            continue

        # Process in chunks of 50
        chunk_size = 50
        added = 0
        for i in range(0, len(msgs), chunk_size):
            chunk = msgs[i:i + chunk_size]
            chunk_date = chunk[0][1:11] if chunk else ""
            prompt = EXTRACT_PROMPT.format(messages="\n".join(chunk))

            try:
                raw = _call_gemini(prompt)
            except Exception as e:
                logger.error(f"  Gemini error: {e}")
                time.sleep(3)
                continue

            entries = _parse_response(raw, chunk_date)
            for entry in entries:
                if _is_duplicate(entry, all_entries):
                    kw3 = entry["keywords"][:3]
                    logger.info(f"  Duplicate: {kw3}")
                    continue
                kw3 = ", ".join(entry["keywords"][:3])
                ans80 = entry["answer"][:80]
                logger.info(f"  + [{kw3}]: {ans80}...")
                all_entries.append(entry)
                faq.append(entry)
                added += 1

            time.sleep(1.5)

        logger.info(f"  Added {added} entries for subtopic '{name}'")
        new_total += added

    # Save
    _save_json(FAQ_PATH, faq)
    logger.info(f"\nDone! Added {new_total} new AWO entries. FAQ total: {len(faq)}")

    # Print summary of all AWO entries now in FAQ
    awo_entries = [e for e in faq if any("awo" in k.lower() for k in e.get("keywords", []))]
    logger.info(f"\nAll AWO entries in FAQ ({len(awo_entries)}):")
    for e in awo_entries:
        logger.info(f"  [{', '.join(e['keywords'][:3])}]: {e['answer'][:100]}")


if __name__ == "__main__":
    main()
