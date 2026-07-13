"""
Convert "Дюся" bot CSV export to our faq.json format and merge.

Usage:
    python3 tools/import_dyusya_csv.py <path_to_csv>

Reads the CSV, cleans answers, extracts keywords, deduplicates against
existing FAQ, and appends new entries to /app/config/faq.json.
"""

import csv
import json
import re
import sys
from pathlib import Path

CSV_PATH = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/app/tools/dyusya.csv")
FAQ_PATH = Path("/app/tools/faq_from_dyusya.json")

# ── clean answer text ────────────────────────────────────────────────────────
STRIP_PREFIXES = [
    "Потбот знає таке:",
    "Потбот знає таке :",
]
STRIP_SUFFIXES = [
    "Дякую що звернулися до мене! 💛💙 🇺🇦 🇩🇪",
    "Дякую що звернулися до мене!  💛💙 🇺🇦 🇩🇪",
    "Дякую що звернулися до мене! 💛💙 🇺🇦 🇩🇪",
]

# Category-only rows (skip them)
CATEGORY_MARKERS = {
    "РАБОТА", "МЕДИЦИНА", "ГУМПОМОЩЬ", "БЫТОВЫЕ ВОПРОСЫ",
    "ЖИЛЬЕ", "ЖИЗНЬ В ГЕРМАНИИ", "ЮРИСТ, АДВОКАТ, ПЕРЕВОДЧИК",
    "наука", "медперсонал", "жизнь в Германии", "фото", "склероз", "диабет",
}

def clean_answer(text: str) -> str:
    text = text.strip()
    # Remove known prefixes
    for p in STRIP_PREFIXES:
        if text.startswith(p):
            text = text[len(p):].strip()
    # Remove known suffixes
    for s in STRIP_SUFFIXES:
        if text.endswith(s):
            text = text[:-len(s)].strip()
    # Remove trailing/leading blank lines
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


def parse_keywords(raw: str) -> list:
    if not raw or not raw.strip():
        return []
    parts = [k.strip().lower() for k in raw.split(",")]
    # Filter out empty, very short, or pure-number tokens
    return [k for k in parts if len(k) > 1 and not k.isdigit()]


def is_category_row(answer: str, kw: str) -> bool:
    """Detect rows that are just section headers."""
    stripped = answer.strip()
    if stripped in CATEGORY_MARKERS or not stripped:
        return True
    if not kw.strip() and len(stripped) < 50 and "\n" not in stripped:
        return True
    return False


# ── parse CSV ────────────────────────────────────────────────────────────────
entries = []

with open(CSV_PATH, encoding="utf-8-sig", newline="") as f:
    reader = csv.reader(f)
    for row in reader:
        if not row:
            continue
        answer_raw = row[0] if len(row) > 0 else ""
        kw_raw     = row[1] if len(row) > 1 else ""

        if is_category_row(answer_raw, kw_raw):
            continue

        answer = clean_answer(answer_raw)
        keywords = parse_keywords(kw_raw)

        if not answer or len(answer) < 20:
            continue
        if not keywords:
            continue

        entries.append({
            "keywords": keywords,
            "answer": answer,
            "source": "dyusya_bot_import",
        })

print(f"Parsed {len(entries)} entries from CSV")

# ── load current FAQ (local copy) for dedup check ───────────────────────────
LOCAL_FAQ = Path("/app/config/faq.json")
existing_kw: set = set()

if LOCAL_FAQ.exists():
    with open(LOCAL_FAQ, encoding="utf-8") as f:
        current = json.load(f)
    for e in current:
        for kw in e.get("keywords", []):
            existing_kw.add(kw.lower())
    print(f"Existing local FAQ: {len(current)} entries, {len(existing_kw)} keywords")

# ── deduplicate and filter ───────────────────────────────────────────────────
new_entries = []
skipped = 0

for e in entries:
    overlap = [kw for kw in e["keywords"] if kw in existing_kw]
    if overlap:
        skipped += 1
        continue
    # Mark keywords as seen so we don't add duplicates within the import itself
    for kw in e["keywords"]:
        existing_kw.add(kw)
    new_entries.append(e)

print(f"New entries to add: {len(new_entries)}, skipped duplicates: {skipped}")

# ── write output ─────────────────────────────────────────────────────────────
with open(FAQ_PATH, "w", encoding="utf-8") as f:
    json.dump(new_entries, f, ensure_ascii=False, indent=2)

print(f"\nSaved to: {FAQ_PATH}")
print("Next steps:")
print("  1. Review faq_from_dyusya.json")
print("  2. docker cp faq_from_dyusya.json tgbot:/app/tools/faq_from_dyusya.json")
print("  3. docker exec tgbot bash -c \"python3 /app/tools/merge_dyusya.py\"")
