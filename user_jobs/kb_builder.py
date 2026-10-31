"""Builds the chat knowledge base: the LLM reads the chat window by window.

Run as a subprocess by robot.py every 15 minutes; it only works on weekdays
during the day (KB_WINDOW, default Mon–Fri 09:00–17:00 Europe/Berlin — Artem's
wish, 31.10.2026: nobody is at home then) and exits otherwise. Progress is a
cursor over message._id, so it resumes where it stopped and later keeps up
with new messages.

    python -m user_jobs.kb_builder --once                 # respects the window
    python -m user_jobs.kb_builder --once --force --max-batches 3
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, "/app")

from user_jobs import kb_common as kb  # noqa: E402

log = logging.getLogger("kb_builder")

TZ = ZoneInfo(os.getenv("KB_TZ", "Europe/Berlin"))
# "days hours": weekdays 0=Mon..6=Sun
KB_WINDOW = os.getenv("KB_WINDOW", "0-4 09:00-17:00")
BATCH_CHARS = int(os.getenv("KB_BATCH_CHARS", "12000"))
BATCH_MAX_MESSAGES = int(os.getenv("KB_BATCH_MAX_MESSAGES", "180"))
MIN_TEXT = 15
LOCK_PATH = os.getenv("KB_LOCK_PATH", "/app/config/kb_builder.lock")

EXTRACT_PROMPT = """
Ти читаєш фрагмент історії групового Telegram-чату українців у Потсдамі (Німеччина).
Повідомлення написані українською, російською, німецькою. Формат рядка:
#<id> [<дата>] <автор>: <текст>   («↩#N» — відповідь на повідомлення N).

Завдання: виписати КОРИСНІ ЗНАННЯ, які згодяться іншим людям пізніше, у вигляді карток.
Що є знанням:
- recommendation — когось/щось порадили (лікар, майстер, юрист, магазин, школа, сервіс), з думкою людей;
- offer — людина сама пропонує послугу/товар/перевезення (є контакт або назва);
- howto — як щось зробити (документи, Jobcenter, Anmeldung, страховка, банк, транспорт…);
- warning — попередження, погані відгуки, шахраї;
- info — корисний факт (адреса, години роботи, ціни, правила);
- event — подія з датою.

Правила:
- Ігноруй балачки, привітання, питання без відповіді, політику, суперечки.
- Об'єднуй в одну картку все, що в цьому фрагменті сказано про одну й ту саму людину/сервіс/тему.
- Контакти (телефони, @логіни, сайти, адреси, e-mail) копіюй ДОСЛІВНО з тексту. Нічого не вигадуй.
- Якщо рекомендують автора повідомлення як виконавця — його ім'я в "name".
- "details": 1–3 речення українською: що саме, що казали (добре/погано), ціни, мова лікаря тощо.
- "topic": 1–4 слова українською (напр. "стоматолог", "перевезення меблів", "Anmeldung", "посилки в Україну").
- "title": коротка назва картки українською.
- "opinion": "+" якщо хвалять, "-" якщо скаржаться, "0" інакше.
- "msg_ids": id повідомлень-джерел (з #id), щонайменше одне.

Поверни ТІЛЬКИ JSON: {"cards": [{"kind","topic","title","name","contacts","details","opinion","msg_ids"}]}
Якщо корисного немає — {"cards": []}.

Фрагмент:
""".strip()


def in_window(now: datetime) -> bool:
    days, hours = KB_WINDOW.split()
    d0, d1 = (int(x) for x in days.split("-"))
    h0, h1 = hours.split("-")
    start = datetime.strptime(h0, "%H:%M").time()
    end = datetime.strptime(h1, "%H:%M").time()
    return d0 <= now.weekday() <= d1 and start <= now.time() < end


def _hours_ago(hours: int) -> str:
    """UTC 'YYYY-MM-DD HH:MM' — message.date is stored in UTC."""
    from datetime import timedelta, timezone
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M")


def take_lock() -> bool:
    try:
        if os.path.exists(LOCK_PATH):
            pid = int(open(LOCK_PATH).read().strip() or 0)
            if pid and os.path.exists(f"/proc/{pid}"):
                return False
        with open(LOCK_PATH, "w") as lock:
            lock.write(str(os.getpid()))
        return True
    except Exception:
        return False


def next_batch(main, cursor: int):
    rows = main.execute(
        """
        SELECT m._id, m.id, m.link, m.date, m.text, m.reply_to_msg_id,
               COALESCE(NULLIF(u.username, ''), u.fullname, '') AS author
        FROM message m LEFT JOIN user u ON u.id = m.from_id
        WHERE m._id > ? ORDER BY m._id LIMIT ?
        """,
        (cursor, BATCH_MAX_MESSAGES * 3),
    ).fetchall()
    batch, chars, last_rowid = [], 0, cursor
    for row in rows:
        last_rowid = row["_id"]
        text = re.sub(r"\s+", " ", (row["text"] or "")).strip()
        if len(text) < MIN_TEXT:
            continue
        line = f"#{row['id']} [{str(row['date'])[:10]}] {row['author'] or 'Учасник'}: "
        if row["reply_to_msg_id"]:
            line += f"↩#{row['reply_to_msg_id']} "
        line += text[:1500]
        batch.append((row, line))
        chars += len(line)
        if chars >= BATCH_CHARS or len(batch) >= BATCH_MAX_MESSAGES:
            break
    return batch, last_rowid


def build_cards(batch, reply: dict):
    by_id = {row["id"]: row for row, _ in batch}
    cards = []
    for raw in (reply or {}).get("cards", []) or []:
        if not isinstance(raw, dict):
            continue
        ids = []
        for value in raw.get("msg_ids") or []:
            try:
                value = int(str(value).lstrip("#"))
            except ValueError:
                continue
            if value in by_id and value not in ids:
                ids.append(value)
        title = str(raw.get("title") or "").strip()
        if not ids or not title:
            continue
        dates = sorted(str(by_id[i]["date"])[:10] for i in ids)
        contacts = raw.get("contacts") or ""
        if isinstance(contacts, list):
            contacts = ", ".join(str(c) for c in contacts)
        cards.append({
            "kind": str(raw.get("kind") or "info")[:20],
            "topic": str(raw.get("topic") or "")[:80],
            "title": title[:200],
            "name": str(raw.get("name") or "")[:200],
            "contacts": str(contacts)[:500],
            "details": str(raw.get("details") or "")[:1200],
            "opinion": str(raw.get("opinion") or "0")[:1],
            "first_date": dates[0],
            "last_date": dates[-1],
            "msg_ids": json.dumps(ids),
            "links": json.dumps([by_id[i]["link"] for i in ids if by_id[i]["link"]]),
        })
    return cards


def embed_pending(store, limit: int = 500) -> int:
    rows = store.execute(
        "SELECT * FROM cards WHERE embedded = 0 ORDER BY id LIMIT ?", (limit,)
    ).fetchall()
    if not rows:
        return 0
    vectors = kb.embed([kb.card_text(r) for r in rows])
    kb.upsert(
        {"id": r["id"], "vector": v, "payload": {"year": str(r["last_date"])[:4], "kind": r["kind"]}}
        for r, v in zip(rows, vectors)
    )
    store.executemany("UPDATE cards SET embedded = 1 WHERE id = ?", [(r["id"],) for r in rows])
    store.commit()
    return len(rows)


def run(force: bool, max_batches: int) -> None:
    if not force and not in_window(datetime.now(TZ)):
        return
    if not take_lock():
        log.info("Another builder is running")
        return
    store = kb.connect()
    main = kb.main_db()
    kb.ensure_collection()
    done = 0
    try:
        embed_pending(store)
        while max_batches <= 0 or done < max_batches:
            if not force and not in_window(datetime.now(TZ)):
                log.info("Out of the working window, stopping")
                break
            cursor = int(kb.get_meta(store, "cursor", "0"))
            batch, last_rowid = next_batch(main, cursor)
            if last_rowid == cursor:
                kb.set_meta(store, "caught_up", datetime.now(TZ).isoformat(timespec="minutes"))
                store.commit()
                log.info("Caught up with the chat")
                break
            if (kb.get_meta(store, "caught_up") and not force and len(batch) < 40
                    and batch and str(batch[0][0]["date"])[:16] > _hours_ago(3)):
                # Live tail: wait until a fuller window has gathered.
                log.info("Only %d fresh messages, waiting for more", len(batch))
                break
            cards, model, seconds = [], "", 0.0
            if batch:
                started = time.time()
                text, model = kb.chat(
                    EXTRACT_PROMPT + "\n" + "\n".join(line for _, line in batch),
                    kb.FAST_MODELS, timeout=120,
                )
                seconds = time.time() - started
                reply = kb.parse_json(text)
                if reply is None:
                    log.warning("Batch after %s: no usable LLM reply, retry later", cursor)
                    time.sleep(60)
                    if not text:
                        break  # every model failed: stop this run, keep the cursor
                    reply = {"cards": []}  # garbage reply: skip the window rather than loop on it
                cards = build_cards(batch, reply)
            store.executemany(
                "INSERT INTO cards(kind, topic, title, name, contacts, details, opinion, first_date,"
                " last_date, msg_ids, links) VALUES (:kind, :topic, :title, :name, :contacts, :details,"
                " :opinion, :first_date, :last_date, :msg_ids, :links)",
                cards,
            )
            store.execute(
                "INSERT INTO batches(first_rowid, last_rowid, messages, cards, model, seconds)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (cursor + 1, last_rowid, len(batch), len(cards), model, round(seconds, 1)),
            )
            kb.set_meta(store, "cursor", last_rowid)
            store.commit()
            embed_pending(store)
            done += 1
            log.info("Batch %d: rows %d..%d, %d messages -> %d cards, %s, %.0f s",
                     done, cursor + 1, last_rowid, len(batch), len(cards), model, seconds)
    finally:
        store.close()
        main.close()
        try:
            os.remove(LOCK_PATH)
        except OSError:
            pass


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--force", action="store_true", help="ignore the working window")
    parser.add_argument("--max-batches", type=int, default=0)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    for noisy in ("urllib3", "requests"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    run(args.force, args.max_batches)


if __name__ == "__main__":
    main()
