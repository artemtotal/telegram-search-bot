import os
os.environ["FAQ_LEARN_DAYS"] = "30"
os.environ["FAQ_LEARN_BATCH"] = "120"
from user_jobs.faq_learn import _get_recent_messages, _call_gemini, _parse_response, _is_duplicate, _load_json, EXTRACT_PROMPT, FAQ_PATH, FAQ_PENDING_PATH

existing_faq = _load_json(FAQ_PATH, [])
pending = _load_json(FAQ_PENDING_PATH, [])
existing_all = existing_faq + pending
print("Existing FAQ:", len(existing_faq), "Pending:", len(pending))

msgs = _get_recent_messages(days=30, limit=120)
chunk_size = 40
for i in range(0, len(msgs), chunk_size):
    chunk = msgs[i:i+chunk_size]
    chunk_date = chunk[0][1:11] if chunk else ""
    raw = _call_gemini(EXTRACT_PROMPT.format(messages=chr(10).join(chunk)))
    entry = _parse_response(raw, chunk_date)
    if entry:
        dup = _is_duplicate(entry, existing_all)
        print("Chunk", i//chunk_size, "keywords:", entry["keywords"], "duplicate:", dup)
    else:
        print("Chunk", i//chunk_size, "no entry parsed")
