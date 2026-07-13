"""
Merge faq_generated.json into /app/config/faq.json.
Skips entries whose keywords already exist in the current FAQ.

Usage (inside container):
    python3 /app/tools/merge_faq.py [--dry-run]
"""

import json
import sys
from pathlib import Path

GENERATED = Path("/app/tools/faq_generated.json")
TARGET = Path("/app/config/faq.json")
DRY_RUN = "--dry-run" in sys.argv

with open(TARGET, encoding="utf-8") as f:
    current = json.load(f)

with open(GENERATED, encoding="utf-8") as f:
    generated = json.load(f)

# Collect all keywords already in the FAQ (lowercase)
existing_kw = set()
for entry in current:
    for kw in entry.get("keywords", []):
        existing_kw.add(kw.lower())

added = 0
skipped = 0

for entry in generated:
    # Remove metadata field before merging
    entry.pop("_category", None)

    # Check if any keyword already exists
    new_kw = [kw.lower() for kw in entry.get("keywords", [])]
    overlap = [kw for kw in new_kw if kw in existing_kw]

    if overlap:
        print(f"SKIP (already has keywords {overlap}): {entry.get('answer','')[:60]}")
        skipped += 1
        continue

    if DRY_RUN:
        print(f"WOULD ADD: keywords={entry['keywords']} | {entry.get('answer','')[:80]}")
    else:
        current.append(entry)
        for kw in new_kw:
            existing_kw.add(kw)
        print(f"ADDED: keywords={entry['keywords']} | {entry.get('answer','')[:80]}")
    added += 1

print(f"\n{'DRY RUN — ' if DRY_RUN else ''}Added: {added}, Skipped: {skipped}")

if not DRY_RUN and added > 0:
    with open(TARGET, "w", encoding="utf-8") as f:
        json.dump(current, f, ensure_ascii=False, indent=2)
    print(f"Saved to {TARGET}. Total entries: {len(current)}")
