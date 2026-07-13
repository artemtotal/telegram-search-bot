"""
Bulk FAQ builder — scans 2 years of chat history by topic categories,
extracts up to 3 practical FAQ entries per category via Gemini, and
merges them directly into /app/config/faq.json.

Usage inside container:
    docker exec tgbot python3 /app/tools/build_faq_bulk.py

Safe to re-run: skips categories already covered in existing FAQ (by keyword match).
Progress is logged; interrupt and resume any time.
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
from sqlalchemy import or_

SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR.parent))
from database import DBSession, Message, User, Chat

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL   = os.getenv("GEMINI_MODEL", "gemini-2.5-pro")
FAQ_PATH       = os.getenv("FAQ_PATH", "/app/config/faq.json")
MSGS_PER_CAT   = 200   # messages fetched per category
DAYS_BACK      = 730   # look back 2 years
SLEEP_BETWEEN  = 1.5   # seconds between Gemini calls (rate-limit safety)

GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "{model}:generateContent?key={api_key}"
)

# ── Categories ────────────────────────────────────────────────────────────────
# Each entry: name, search keywords (DB ILIKE), faq_keywords (for matcher)
CATEGORIES = [
    # ── Medical specialists ───────────────────────────────────────────────────
    {
        "name": "Терапевт / Сімейний лікар / Hausarzt",
        "search": ["терапевт", "hausarzt", "семейный врач", "сімейний лікар", "allgemeinarzt", "116117"],
        "faq_keywords": ["терапевт", "hausarzt", "сімейний лікар", "семейный врач", "116117"],
    },
    {
        "name": "Педіатр / Дитячий лікар / Kinderarzt",
        "search": ["педіатр", "педиатр", "kinderarzt", "kindearzt", "дитячий лікар", "детский врач"],
        "faq_keywords": ["педіатр", "педиатр", "kinderarzt", "дитячий лікар"],
    },
    {
        "name": "Стоматолог / Zahnarzt",
        "search": ["стоматолог", "zahnarzt", "зубной", "зубний", "dentist", "зубной врач"],
        "faq_keywords": ["стоматолог", "zahnarzt", "зубний", "зубной"],
    },
    {
        "name": "Гінеколог / Frauenarzt",
        "search": ["гінеколог", "гинеколог", "gynäkologe", "frauenarzt", "гінекол"],
        "faq_keywords": ["гінеколог", "гинеколог", "gynäkologe", "frauenarzt"],
    },
    {
        "name": "Ортопед / Травматолог",
        "search": ["ортопед", "травматолог", "orthopäde", "orthopäd", "ортопед"],
        "faq_keywords": ["ортопед", "травматолог", "orthopäde"],
    },
    {
        "name": "Невролог / Психолог / Психіатр",
        "search": ["невролог", "психолог", "психіатр", "psychiater", "neurologe", "нейролог", "нарколог"],
        "faq_keywords": ["невролог", "психолог", "психіатр", "psychiater", "neurologe"],
    },
    {
        "name": "Офтальмолог / Окуліст / Augenarzt",
        "search": ["офтальмолог", "окулист", "augenarzt", "окуліст", "зір", "зрение"],
        "faq_keywords": ["офтальмолог", "окулист", "augenarzt", "окуліст"],
    },
    {
        "name": "Дерматолог / Hautarzt",
        "search": ["дерматолог", "hautarzt", "шкіра", "кожа", "дерматит", "акне"],
        "faq_keywords": ["дерматолог", "hautarzt"],
    },
    {
        "name": "Онколог / Кардіолог / Уролог / Ендокринолог",
        "search": ["онколог", "кардіолог", "кардиолог", "уролог", "ендокринолог", "ендокринол", "hämato"],
        "faq_keywords": ["онколог", "кардіолог", "уролог", "ендокринолог"],
    },
    {
        "name": "Логопед / Ерготерапевт",
        "search": ["логопед", "логопедист", "ergo", "ерготерапевт", "мовлення", "мова розвиток дитини"],
        "faq_keywords": ["логопед", "ergo", "ерготерапевт"],
    },
    {
        "name": "Медичний перекладач / Dolmetscher Arzt",
        "search": ["перекладач лікар", "переводчик врач", "dolmetscher arzt", "перекладач медич",
                   "перевод на приеме", "перевод к врачу"],
        "faq_keywords": ["перекладач", "медичний перекладач", "dolmetscher arzt"],
    },
    {
        "name": "Страховка / Krankenversicherung / AOK / TK / Barmer",
        "search": ["krankenversicherung", "страховка", "страхування", " TK ", "AOK", "Barmer",
                   "versicherungskarte", "страховий"],
        "faq_keywords": ["krankenversicherung", "страховка", "страхування", "AOK", "TK", "Barmer"],
    },
    {
        "name": "Ліки / Рецепт / Аптека / Rezept",
        "search": ["рецепт", "rezept", "ліки", "лекарства", "аптека", "apotheke", "таблетки"],
        "faq_keywords": ["ліки", "рецепт", "rezept", "аптека", "apotheke"],
    },
    # ── Documents / Government ────────────────────────────────────────────────
    {
        "name": "Ausländerbehörde / ВНЖ / Aufenthaltstitel",
        "search": ["ausländerbehörde", "aufenthaltstitel", "внж", "fiktionsbescheinigung",
                   "auslanderbehor", "aufenthaltserlaubnis"],
        "faq_keywords": ["ausländerbehörde", "aufenthaltstitel", "внж", "fiktionsbescheinigung"],
    },
    {
        "name": "Jobcenter / Bürgergeld / Ausbildung",
        "search": ["jobcenter", "bürgergeld", "alg ii", "ausbildung", "leistungsportal",
                   "беральтер", "beralter", "берате"],
        "faq_keywords": ["jobcenter", "bürgergeld", "ausbildung", "leistungsportal"],
    },
    {
        "name": "Sozialamt / Соціальний відділ",
        "search": ["sozialamt", "соціальний", "социальный", "соцамт", "soziale hilfe"],
        "faq_keywords": ["sozialamt", "соціальний", "социальный"],
    },
    {
        "name": "Familienkasse / Kindergeld / Kinderzuschlag",
        "search": ["familienkasse", "kindergeld", "kinderzuschlag", "кіндергельт", "дитяча допомога"],
        "faq_keywords": ["familienkasse", "kindergeld", "kinderzuschlag", "кіндергельт"],
    },
    {
        "name": "Паспорт / Посольство / Консульство",
        "search": ["паспорт", "посольство", "консульство", "загранпаспорт", "закордонний паспорт",
                   "embassy", "консульськ"],
        "faq_keywords": ["паспорт", "посольство", "консульство", "закордонний паспорт"],
    },
    {
        "name": "Реєстрація / Прописка / Anmeldung",
        "search": ["anmeldung", "прописка", "реєстрація", "зареєструватися", "регистрация",
                   "meldeadresse", "meldebescheinigung"],
        "faq_keywords": ["anmeldung", "прописка", "реєстрація", "meldebescheinigung"],
    },
    {
        "name": "BAMF / Інтеграційний офіс / Цувайзунг / Zuweisung",
        "search": ["bamf", "zuweisung", "цувайзунг", "infopoint", "інфопойнт",
                   "integration", "integrationskurs"],
        "faq_keywords": ["bamf", "zuweisung", "цувайзунг", "infopoint"],
    },
    {
        "name": "Wohngeld / Жилищна субсидія",
        "search": ["wohngeld", "вонгельд", "wongeld", "жилищная субсид", "субсидія на житло"],
        "faq_keywords": ["wohngeld", "вонгельд", "субсидія на житло"],
    },
    {
        "name": "Податки / Steuer / Steuernummer / Finanzamt",
        "search": ["steuer", "steuernummer", "finanzamt", "податок", "налог", "steueridentifikationsnummer"],
        "faq_keywords": ["steuer", "steuernummer", "finanzamt", "податок"],
    },
    # ── Housing ───────────────────────────────────────────────────────────────
    {
        "name": "Пошук квартири / Wohnung / WBS",
        "search": ["wohnung", "wohnheim", "квартира", "житло", "mietwohnung", "wbs",
                   "wohnungssuche", "квартиру шукаю"],
        "faq_keywords": ["wohnung", "квартира", "житло", "wbs", "wohnungssuche"],
    },
    {
        "name": "Залог / Kaution / Завдаток",
        "search": ["kaution", "залог", "завдаток", "депозит", "kautionshilfe"],
        "faq_keywords": ["kaution", "залог", "завдаток"],
    },
    {
        "name": "Меблі / Kleinanzeigen / Ebay / Безкоштовно",
        "search": ["kleinanzeigen", "ebay kleinanzeigen", "меблі безкоштовно", "мебель бесплатно",
                   "меблі б/в", "мебель б/у"],
        "faq_keywords": ["kleinanzeigen", "меблі", "мебель", "безкоштовно"],
    },
    {
        "name": "Ремонт квартири / Майстер / Handwerker",
        "search": ["ремонт квартири", "handwerker", "майстер", "мастер ремонт", "сантехнік",
                   "електрик", "renovierung"],
        "faq_keywords": ["ремонт", "handwerker", "майстер", "сантехнік", "електрик"],
    },
    # ── Transport ─────────────────────────────────────────────────────────────
    {
        "name": "VBB / Deutschlandticket / Mobilitätsticket / Проїзний",
        "search": ["vbb", "deutschlandticket", "mobilitätsticket", "kvp", "проїзний",
                   "semester ticket", "schülerticket"],
        "faq_keywords": ["vbb", "deutschlandticket", "mobilitätsticket", "проїзний"],
    },
    {
        "name": "Авто / Водійські права / TÜV / Страховка авто",
        "search": ["führerschein", "водійські права", "права", "автострахов", "kfz",
                   "tüv", "haftpflicht", "fahrzeug"],
        "faq_keywords": ["führerschein", "водійські права", "tüv", "автострахов"],
    },
    # ── Work & Education ──────────────────────────────────────────────────────
    {
        "name": "Робота / Arbeit / Minijob / Частична зайнятість",
        "search": ["arbeit", "minijob", "mini-job", "робота", "работа", "частичная занятость",
                   "teilzeit", "vollzeit"],
        "faq_keywords": ["arbeit", "minijob", "робота", "teilzeit"],
    },
    {
        "name": "Курси німецької / Sprachkurs / B1 / B2",
        "search": ["sprachkurs", "deutschkurs", "курс мови", "курси мови", "b1 kurs", "b2 kurs",
                   "vhs", "volkshochschule", "немецкий язык курс"],
        "faq_keywords": ["sprachkurs", "deutschkurs", "курси мови", "vhs", "volkshochschule"],
    },
    {
        "name": "Диплом / Визнання освіти / Studium / Anabin",
        "search": ["anabin", "диплом", "визнання", "nostrifikation", "studium", "hochschule",
                   "підтвердження диплому", "anerkennung"],
        "faq_keywords": ["anabin", "диплом", "визнання", "studium", "anerkennung"],
    },
    {
        "name": "Репетитор / Навчання дітей / Nachhilfe",
        "search": ["репетитор", "nachhilfe", "навчання дітей", "підготовка до школи",
                   "підтягнути математику", "нахільфе"],
        "faq_keywords": ["репетитор", "nachhilfe", "навчання дітей"],
    },
    # ── Financial & Legal ─────────────────────────────────────────────────────
    {
        "name": "Банк / Konto / Ощадна каса / Sparkasse",
        "search": ["konto", "банк", "рахунок", "sparkasse", "volksbank", "girokonto",
                   "банківський рахунок", "счёт"],
        "faq_keywords": ["konto", "банк", "рахунок", "sparkasse", "volksbank"],
    },
    {
        "name": "Юрист / Rechtsberatung / Anwalt / Правова допомога",
        "search": ["rechtsberatung", "anwalt", "юрист", "правова допомога", "миграційний юрист",
                   "адвокат", "recht"],
        "faq_keywords": ["rechtsberatung", "anwalt", "юрист", "правова допомога"],
    },
    {
        "name": "Переклади / Присяжний перекладач / Beglaubigte Übersetzung",
        "search": ["beglaubigte übersetzung", "присяжний перекладач", "перевод документов",
                   "переклад документів", "notariell", "перекладач документів"],
        "faq_keywords": ["beglaubigte übersetzung", "присяжний перекладач", "переклад документів"],
    },
    # ── Food & Social ─────────────────────────────────────────────────────────
    {
        "name": "Tafel / Їжа безкоштовно / Гуманітарна допомога",
        "search": ["tafel", "lebensmittel", "їжа безкоштовно", "гуманитарка", "гумдопомога",
                   "drk", "essen kostenlos"],
        "faq_keywords": ["tafel", "їжа безкоштовно", "гуманитарка", "drk"],
    },
    {
        "name": "AWO / Caritas / Консультації / Допомога організацій",
        "search": ["awo", "caritas", "diakonie", "міграційна консультація",
                   "migrationsberatung", "соціальна консультація"],
        "faq_keywords": ["awo", "caritas", "diakonie", "migrationsberatung"],
    },
    {
        "name": "Психологічна допомога / Криза / Trauma",
        "search": ["психолог безкоштовно", "психолог бесплатно", "травма", "криза",
                   "кризовий центр", "психологічна допомога"],
        "faq_keywords": ["психолог безкоштовно", "травма", "кризовий центр"],
    },
    {
        "name": "Домашнє насилля / Захист / Frauenhaus",
        "search": ["домашнє насилля", "домашнее насилие", "frauenhaus", "жіночий дім",
                   "насилля", "насилие"],
        "faq_keywords": ["домашнє насилля", "frauenhaus"],
    },
    # ── Children & Education ──────────────────────────────────────────────────
    {
        "name": "Школа / Schule / Записатися до школи",
        "search": ["schule", "schulamt", "школа", "записати дитину до школи",
                   "willkommensklasse", "integrationsklass"],
        "faq_keywords": ["schule", "schulamt", "школа", "willkommensklasse"],
    },
    {
        "name": "Дитячий садок / Kita / Krippe",
        "search": ["kita", "krippe", "kindergarten", "дитячий садок", "садик", "садок",
                   "детский сад", "kita platz"],
        "faq_keywords": ["kita", "krippe", "kindergarten", "дитячий садок"],
    },
    {
        "name": "Дитячі гуртки / Секції / Sportverein / Спорт для дітей",
        "search": ["sportverein", "спортивна секція", "детская секция", "kinder sport",
                   "гурток", "кружок", "дитячий гурток"],
        "faq_keywords": ["sportverein", "спортивна секція", "гурток", "kinder sport"],
    },
    {
        "name": "Літній табір / Sommercamp / Лагер для дітей",
        "search": ["sommercamp", "feriencamp", "літній табір", "летний лагерь",
                   "фериїн", "ferien"],
        "faq_keywords": ["sommercamp", "літній табір", "feriencamp"],
    },
    # ── Recreation & Culture ──────────────────────────────────────────────────
    {
        "name": "Заходи / Events / Концерти для українців",
        "search": ["захід для українців", "event für ukrainer", "концерт", "захід",
                   "урочистість", "veranstaltung"],
        "faq_keywords": ["захід для українців", "event", "концерт"],
    },
    {
        "name": "Басейн / Спортзал / Gym / Schwimmbad",
        "search": ["schwimmbad", "басейн", "fitnessstudio", "gym", "спортзал",
                   "спортивний зал", "wellenbad"],
        "faq_keywords": ["schwimmbad", "басейн", "fitnessstudio", "gym"],
    },
    {
        "name": "Бібліотека / Stadtbibliothek",
        "search": ["bibliothek", "stadtbibliothek", "бібліотека", "библиотека",
                   "читальня", "bücherei"],
        "faq_keywords": ["bibliothek", "stadtbibliothek", "бібліотека"],
    },
    {
        "name": "Музеї / Культура / Безкоштовний вхід",
        "search": ["museum", "музей", "kostenlos museum", "безкоштовний вхід",
                   "ausstellung", "kunstraum"],
        "faq_keywords": ["museum", "музей", "kostenlos"],
    },
    # ── Practical everyday ────────────────────────────────────────────────────
    {
        "name": "Інтернет / Провайдери / Sim-картка",
        "search": ["internet zuhause", "провайдер", "sim karte", "сімка",
                   "мобільний інтернет", "mobilfunk"],
        "faq_keywords": ["провайдер", "sim karte", "сімка", "internet"],
    },
    {
        "name": "Розпечатати / Відсканувати / Copy-Shop",
        "search": ["drucken", "сканувати", "розпечатати", "роздрукувати",
                   "copy shop", "копі шоп"],
        "faq_keywords": ["drucken", "розпечатати", "copy shop"],
    },
    {
        "name": "Перукар / Перукарня / Friseur",
        "search": ["friseur", "перукар", "перукарня", "стрижка", "парикмахер"],
        "faq_keywords": ["friseur", "перукар", "стрижка"],
    },
    {
        "name": "Швея / Пошиття / Ательє / Ремонт одягу",
        "search": ["швея", "швачка", "ательє", "ремонт одягу", "кравець",
                   "änderungsschneiderei", "schneiderin"],
        "faq_keywords": ["швея", "ательє", "ремонт одягу", "schneiderin"],
    },
    {
        "name": "Слюсар / Замок / Schlüsseldienst",
        "search": ["schlüsseldienst", "слюсар", "замок", "захлопнулась дверь",
                   "ключ", "schlüssel"],
        "faq_keywords": ["schlüsseldienst", "слюсар", "замок"],
    },
    {
        "name": "Посилки / Nova Poshta / Перевезення Україна-Германія",
        "search": ["нова пошта", "nova poshta", "посилка", "перевезення",
                   "доставка в украину", "доставка в україну"],
        "faq_keywords": ["нова пошта", "посилка", "перевезення"],
    },
]


# ── Multi-entry extraction prompt ─────────────────────────────────────────────

EXTRACT_PROMPT = """\
Ти аналітик чату жителів Потсдама (Україна/Росія/Німеччина). Тема: "{category}".

Знайди до 3 найкорисніших практичних фактів із повідомлень нижче.
Хороші факти: конкретна адреса, телефон, посилання, ім'я фахівця, графік роботи, ціна, практична порада.
Пропускай: суперечки, скарги, запитання без відповіді, флуд, привітання, рекламу.

Для КОЖНОГО знайденого факту виведи РІВНО ЦЕЙ ФОРМАТ (без змін):
ENTRY_START
KEYWORDS: слово1, слово2, слово3
ANSWER: конкретна практична відповідь (до 500 символів)
DATE: РРРР-ММ-ДД
ENTRY_END

Якщо корисних фактів немає — виведи тільки: NONE

Повідомлення з чату:
{messages}
"""


# ── Gemini call ────────────────────────────────────────────────────────────────

def _call_gemini(prompt: str) -> str:
    url = GEMINI_URL.format(model=GEMINI_MODEL, api_key=GEMINI_API_KEY)
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            # gemini-2.5-pro: thinking tokens count against maxOutputTokens.
            "maxOutputTokens": 3000,
            "temperature": 0.1,
            "thinkingConfig": {"thinkingBudget": 1024},
        },
    }
    for attempt in range(3):
        try:
            resp = requests.post(url, json=payload, timeout=60)
            if resp.status_code == 429:
                log.warning("Rate limit — sleeping 60s")
                time.sleep(60)
                continue
            resp.raise_for_status()
            data = resp.json()
            parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
            # filter out thinking parts
            return "".join(p.get("text", "") for p in parts if p.get("thought") is not True).strip()
        except Exception as e:
            log.warning(f"Gemini attempt {attempt+1} failed: {e}")
            time.sleep(5)
    return ""


# ── Response parser ────────────────────────────────────────────────────────────

def _parse_entries(raw: str, default_date: str) -> List[Dict]:
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
        kw_raw  = fields.get("KEYWORDS", "")
        answer  = fields.get("ANSWER", "").strip()
        date    = fields.get("DATE", default_date)
        keywords = [k.strip() for k in kw_raw.split(",") if len(k.strip()) >= 2]
        if keywords and len(answer) >= 20:
            if not answer.endswith((".", "!", "?")):
                answer = answer.rstrip(",; ") + "."
            entries.append({"keywords": keywords, "answer": answer, "source_date": date})
    return entries


# ── Deduplication ──────────────────────────────────────────────────────────────

def _is_duplicate(new_entry: Dict, existing: List[Dict], threshold: int = 2) -> bool:
    new_kw = set(k.lower() for k in new_entry.get("keywords", []))
    for e in existing:
        exist_kw = set(k.lower() for k in e.get("keywords", []))
        if len(new_kw & exist_kw) >= threshold:
            return True
    return False


# ── Message fetching ───────────────────────────────────────────────────────────

def _fetch_messages(session, chat_ids: List[int],
                    keywords: List[str], limit: int, days_back: int) -> List[str]:
    cutoff = datetime.utcnow() - timedelta(days=days_back)
    rows = (
        session.query(Message, User)
        .outerjoin(User, Message.from_id == User.id)
        .filter(Message.from_chat.in_(chat_ids))
        .filter(Message.text.isnot(None))
        .filter(Message.text != "")
        .filter(Message.date >= cutoff)
        .filter(or_(*[Message.text.ilike(f"%{kw}%") for kw in keywords]))
        .order_by(Message.date.desc())
        .limit(limit)
        .all()
    )
    result = []
    for msg, user in rows:
        username = (user.username or user.fullname or "?") if user else "?"
        date_str = msg.date.strftime("%Y-%m-%d") if hasattr(msg.date, "strftime") else "?"
        text = (msg.text or "").strip().replace("\n", " ")[:400]
        result.append(f"[{date_str}] @{username}: {text}")
    return result


# ── FAQ I/O ────────────────────────────────────────────────────────────────────

def _load_faq() -> List[Dict]:
    try:
        with open(FAQ_PATH, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return []
    except Exception as e:
        log.error(f"Cannot load FAQ: {e}")
        return []


def _save_faq(data: List[Dict]) -> None:
    with open(FAQ_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    if not GEMINI_API_KEY:
        log.error("GEMINI_API_KEY not set!")
        sys.exit(1)

    session = DBSession()
    chat_ids = [c.id for c in session.query(Chat).filter(Chat.enable == 1).all()]
    log.info(f"Active chat IDs: {chat_ids}")
    log.info(f"Categories to process: {len(CATEGORIES)}")

    faq = _load_faq()
    log.info(f"Existing FAQ entries: {len(faq)}")
    all_entries = list(faq)   # track all (existing + new) for dedup
    added_total = 0

    for i, cat in enumerate(CATEGORIES, 1):
        name = cat["name"]
        log.info(f"\n[{i}/{len(CATEGORIES)}] {name}")

        msgs = _fetch_messages(session, chat_ids, cat["search"], MSGS_PER_CAT, DAYS_BACK)
        if len(msgs) < 3:
            log.info(f"  Only {len(msgs)} messages found, skipping")
            continue
        log.info(f"  {len(msgs)} messages found")

        # Latest message date for fallback DATE field
        try:
            default_date = msgs[0][1:11]  # "[2026-05-01]" → "2026-05-01"
        except Exception:
            default_date = datetime.utcnow().strftime("%Y-%m-%d")

        prompt = EXTRACT_PROMPT.format(
            category=name,
            messages="\n".join(msgs[:100])  # cap at 100 to keep prompt size reasonable
        )
        raw = _call_gemini(prompt)
        if not raw:
            log.info("  Gemini returned empty, skipping")
            time.sleep(SLEEP_BETWEEN)
            continue

        new_entries = _parse_entries(raw, default_date)
        log.info(f"  Parsed {len(new_entries)} entries from Gemini")

        added_this_cat = 0
        for entry in new_entries:
            if _is_duplicate(entry, all_entries):
                log.info(f"  Duplicate: {entry['keywords'][:3]}")
                continue
            faq.append(entry)
            all_entries.append(entry)
            added_this_cat += 1
            added_total += 1
            log.info(f"  + [{', '.join(entry['keywords'][:3])}]: {entry['answer'][:60]}...")

        if added_this_cat > 0:
            _save_faq(faq)  # save after each category (safe to interrupt)
            log.info(f"  Saved. FAQ total: {len(faq)}")

        time.sleep(SLEEP_BETWEEN)

    session.close()
    log.info(f"\n{'='*60}")
    log.info(f"Done! Added {added_total} new entries. FAQ total: {len(faq)}")


if __name__ == "__main__":
    main()
