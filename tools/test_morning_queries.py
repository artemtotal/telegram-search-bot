#!/usr/bin/env python3
"""
Test the 11 morning queries through the full pipeline (mirrors handle_ai_query).
Run inside the container: python tools/test_morning_queries.py
"""

import json
import logging
import os
import sys
import textwrap
from datetime import datetime

sys.path.insert(0, "/app")
logging.basicConfig(level=logging.WARNING)

from database import Chat, DBSession
from user_handlers.msg_ai import (
    _is_chat_history_query,
    _is_temporal_query,
    _get_anchor_words,
    _get_upcoming_date_patterns,
    _load_faq,
    _search_faq,
    _get_chroma,
    _expand_keywords_via_gemini,
    _search_keywords_with_fallback,
    _search_recently_posted,
    _search_recent,
    _rerank_via_gemini,
    _build_context,
    _call_gemini,
    SYSTEM_PROMPT_CHAT,
    SYSTEM_PROMPT_FAQ,
    _chroma_count,
)

QUERIES = [
    "Сделай сводку за сегодня только по практической помощи беженцам из Украины: документы, жильё, Jobcenter, школа, медицина, работа, курсы немецкого, переводы, мероприятия. Укажи, какие вопросы остались без ответа.",
    "Сделай сводку за последние сутки только по практической помощи беженцам из Украины: документы, жильё, Jobcenter, школа, медицина, работа, курсы немецкого, переводы, мероприятия. Укажи, какие вопросы остались без ответа.",
    "Проанализируй переписку за последние 7 дней. Покажи: 10 самых частых тем за неделю. Самые полезные контакты и ссылки. Вопросы, которые так и не получили ответ.",
    "Что в чате уже писали про Wohngeld?",
    "Какие советы в чате давали по поиску квартиры?",
    "Какие контакты врачей, переводчиков или консультаций упоминались в чате?",
    "Найди в чате все полезные контакты по теме медицина: врачи, клиники, горячие линии, консультации, переводчики.",
    "Найди все ссылки, которые давали по теме Jobcenter, Bürgergeld и Sozialamt.",
    "Собери контакты организаций, которые помогают украинцам: консультации, одежда, мебель, еда, документы, психологическая помощь.",
    "Подготовь новое закреплённое сообщение для чата на основе самых частых вопросов за последние 2 недели.",
    "Я только приехал/приехала из Украины в Германию. По информации из этого чата составь мне пошаговый список: регистрация, жильё, выплаты, медицина, школа/садик, немецкий язык, работа.",
]

SEP = "=" * 70


def run_query(query: str, faq, chat_ids, query_num: int) -> str:
    print(f"\n{SEP}")
    print(f"QUERY #{query_num}:")
    print(textwrap.fill(query, 80))
    print(SEP)

    is_chat_history = _is_chat_history_query(query)
    use_faq = not is_chat_history and len(query) <= 120
    is_temporal = _is_temporal_query(query)
    anchor_words = _get_anchor_words(query)

    print(f"  is_chat_history={is_chat_history}  use_faq={use_faq}  is_temporal={is_temporal}")
    print(f"  anchor_words: {anchor_words[:8]}")

    # FAQ fast path
    faq_answer = _search_faq(query, faq) if use_faq else None
    if faq_answer:
        print(f"  ROUTE: FAQ hit")
        prompt = (
            f"{SYSTEM_PROMPT_FAQ}\n\n"
            f"--- ДОВІДКОВА ІНФОРМАЦІЯ ---\n{faq_answer}\n"
            f"--- КІНЕЦЬ ---\n\n"
            f"Питання: {query}"
        )
        answer = _call_gemini(prompt, max_tokens=4096, thinking=False)
        print(f"\n--- FAQ ANSWER ---")
        print(answer or "(empty)")
        return "FAQ"

    # Deep search
    print(f"  ROUTE: Deep keyword search")

    session = DBSession()
    try:
        col = _get_chroma()
        vector_ready = col is not None and _chroma_count > 0
        vec_msgs = []

        if not vector_ready:
            print(f"  ChromaDB: disabled → keyword search only")
            keywords = _expand_keywords_via_gemini(query)
            if is_temporal:
                keywords += _get_upcoming_date_patterns(14)
            print(f"  keywords ({len(keywords)}): {keywords[:10]}")
            keyword_msgs = _search_keywords_with_fallback(
                session, chat_ids, keywords, anchor_words=anchor_words
            )
        else:
            keyword_msgs = []
            if anchor_words:
                keyword_msgs = _search_keywords_with_fallback(
                    session, chat_ids, [], anchor_words=anchor_words
                )

        if is_temporal:
            recent_msgs = _search_recently_posted(session, chat_ids, days=14, limit=60)
        else:
            recent_msgs = _search_recent(session, chat_ids, limit=10)

        all_candidates = vec_msgs + keyword_msgs + recent_msgs
        print(f"  candidates: {len(vec_msgs)} vector + {len(keyword_msgs)} keyword + {len(recent_msgs)} recent = {len(all_candidates)}")

        if not all_candidates:
            print("\n  (no messages found)")
            return "EMPTY"

        top_msgs = _rerank_via_gemini(query, all_candidates, top_k=25)
        print(f"  after rerank: {len(top_msgs)} messages")

        ctx = _build_context(top_msgs)
        today = datetime.utcnow().strftime("%Y-%m-%d")
        print(f"  context: {len(ctx)} chars")

        prompt = (
            f"{SYSTEM_PROMPT_CHAT.format(today=today)}\n\n"
            f"--- КОНТЕКСТ З ЧАТУ ---\n{ctx}\n"
            f"--- КІНЕЦЬ КОНТЕКСТУ ---\n\n"
            f"Питання: {query}"
        )

        print(f"  calling Gemini ({len(prompt)} chars)...")
        answer = _call_gemini(prompt, max_tokens=2048, thinking=False, timeout=90)

        print(f"\n--- ANSWER ---")
        if answer:
            print(answer)
        else:
            print("(empty response)")
        return "GEMINI" if answer else "EMPTY"

    finally:
        session.close()


def main():
    print("Loading FAQ...")
    faq = _load_faq()
    print(f"FAQ: {len(faq)} entries")

    session = DBSession()
    try:
        chat_ids = [c.id for c in session.query(Chat).filter(Chat.enable == 1).all()]
    finally:
        session.close()
    print(f"Active chats: {chat_ids}")

    results = []
    for i, query in enumerate(QUERIES, 1):
        route = run_query(query, faq, chat_ids, i)
        results.append((i, route, query[:60]))

    print(f"\n\n{SEP}")
    print("SUMMARY")
    print(SEP)
    for num, route, q in results:
        print(f"  #{num:2d} [{route:6s}] {q}")


if __name__ == "__main__":
    main()
