import json
import os
import sys
import unittest
from unittest.mock import Mock, patch

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from user_handlers import msg_ai


class AiProviderTests(unittest.TestCase):
    def test_calls_omniroute_without_api_key(self):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "choices": [{"message": {"content": "Працює"}}]
        }

        with patch.object(msg_ai, "OPENROUTER_API_KEY", ""), patch.object(
            msg_ai.requests, "post", return_value=response
        ) as post:
            result = msg_ai._call_ai("Як справи?", max_tokens=50, timeout=10)

        self.assertEqual(result, "Працює")
        headers = post.call_args.kwargs["headers"]
        payload = post.call_args.kwargs["json"]
        self.assertNotIn("Authorization", headers)
        self.assertFalse(payload["stream"])
        self.assertEqual(payload["model"], msg_ai.AI_MODEL)

    def test_returns_empty_when_omniroute_fails(self):
        with patch.object(
            msg_ai.requests, "post", side_effect=msg_ai.requests.RequestException("offline")
        ):
            result = msg_ai._call_ai("Як справи?", max_tokens=50, timeout=10)

        self.assertEqual(result, "")

    def test_provider_author_terms_keep_only_trailing_name(self):
        self.assertEqual(
            msg_ai._provider_author_terms("ремонт стиральных машин Александр"),
            ["александр"],
        )
        self.assertEqual(
            msg_ai._provider_author_terms("нужен хороший электрик Сергей"),
            ["сергей"],
        )

    def test_vacancy_intent_rejects_service_work_and_housing(self):
        non_vacancies = (
            "стоимость работы электрика",
            "ремонт робота-пылесоса",
            "требуется квартира",
            "wir suchen eine Wohnung",
        )
        for query in non_vacancies:
            with self.subTest(query=query):
                self.assertFalse(msg_ai._is_vacancy_query(query))

    def test_provider_intent_recognizes_handyman_queries(self):
        queries = (
            "муж на час Дмитрий",
            "нужен бытовой мастер",
            "кто делает мелкий ремонт",
            "нужна сборка мебели и установка кухни",
            "подключение бытовой техники",
        )

        for query in queries:
            with self.subTest(query=query):
                self.assertTrue(msg_ai._is_service_provider_query(query))

    def test_provider_intent_recognizes_carrier_queries(self):
        queries = (
            "перевозчик в Украину",
            "порадьте перевізника до України",
            "кто перевозит людей и посылки",
            "виїхати з Німеччини до України",
        )

        for query in queries:
            with self.subTest(query=query):
                self.assertTrue(msg_ai._is_service_provider_query(query))
                self.assertTrue(msg_ai._is_carrier_query(query))

    def test_provider_intent_recognizes_all_service_categories(self):
        # Real seeker phrasings pulled from the last 6 months of chat history.
        queries = (
            # медицина
            "порадьте хорошого стоматолога в Потсдамі",
            "подскажите терапевта, который берёт новых пациентов",
            "шукаю дитячого лікаря педіатра",
            "нужен мануальщик или костоправ, спина болит",
            "порекомендуйте масажиста",
            "шукаю логопеда для дитини",
            # красота
            "підкажіть майстра манікюру",
            "ищу мастера по наращиванию ресниц",
            "порадьте грумера для собачки",
            # кондитер / еда
            "хто пече тортики? скиньте контакти",
            "шукаю кондитера на замовлення",
            # флорист
            "може хтось є флорист, зробити букет",
            # юрист / переводчик
            "потрібен присяжний перекладач з англійської",
            "шукаю юриста для консультації",
            "нужен нотариус",
            # няня / репетитор
            "шукаю репетитора з математики",
            "нужна няня для ребёнка",
            # уборка
            "шукаю людину на прибирання квартири",
            # авто
            "підкажіть контакти шиномонтажу",
            "нужен автомастер",
            # фото
            "шукаю фотографа на сімейну зйомку",
        )

        for query in queries:
            with self.subTest(query=query):
                self.assertTrue(msg_ai._is_service_provider_query(query))

    def test_carrier_keywords_include_ads_and_route_terms(self):
        for query in (
            "перевізник до України",
            "виїхати з Німеччини до України",
        ):
            with self.subTest(query=query):
                keywords = msg_ai._expand_keywords(query)
                self.assertIn("перевезення", keywords)
                self.assertIn("перевозчик", keywords)
                self.assertIn("посилки", keywords)

    def test_carrier_candidates_put_contact_answers_before_seeker_questions(self):
        messages = [
            {
                "id": 1,
                "user": "requester",
                "date": "2026-07-19",
                "text": "Порадьте будь ласка перевізника до України",
            },
            {
                "id": 2,
                "user": "Kateryna_Sur",
                "date": "2026-07-19",
                "text": "+380966622986 Dima Hryhorovych +380676746710 Ivan",
            },
            {
                "id": 3,
                "user": "Потбот",
                "date": "2025-12-14",
                "text": (
                    "Перевезення людей та посилок, відправка новою поштою. "
                    "+380679450904 Павло +380979850174 Тетяна"
                ),
            },
        ]

        result = msg_ai._prioritize_provider_candidates(
            messages, "перевізник до України"
        )

        self.assertEqual([item["id"] for item in result[:2]], [3, 2])
        self.assertNotIn(1, [item["id"] for item in result])

    def test_carrier_candidates_do_not_admit_unrelated_phone_chunks(self):
        messages = [
            {
                "id": 1,
                "user": "noise",
                "date": "2026-07-19",
                "text": "Обсуждали интернет PYUR, телефон +491234567890",
            },
            {
                "id": 2,
                "user": "carrier",
                "date": "2026-07-18",
                "text": "Перевезення людей до України +380966622986",
            },
        ]

        result = msg_ai._prioritize_provider_candidates(messages, "перевозчик")

        self.assertEqual([item["id"] for item in result], [2])

    def test_carrier_topic_rejects_unrelated_provider_offer(self):
        self.assertFalse(msg_ai._matches_query_topic(
            "перевозчик в Украину",
            {"text": "Предлагаю услуги парикмахера, пишите в ЛС"},
        ))
        self.assertTrue(msg_ai._matches_query_topic(
            "перевозчик в Украину",
            {"text": "Перевезення людей та посилок до України"},
        ))

    def test_query_topic_keeps_offers_within_service_family(self):
        # Medical query must reject a groomer / carrier and keep a dentist catalog.
        self.assertFalse(msg_ai._matches_query_topic(
            "порекомендуйте стоматолога в Потсдамі",
            {"text": "Пропоную грумерські послуги, працюю з тваринками +49..."},
        ))
        self.assertFalse(msg_ai._matches_query_topic(
            "порекомендуйте стоматолога в Потсдамі",
            {"text": "Пропоную послуги з перевезення по Німеччині +380..."},
        ))
        self.assertFalse(msg_ai._matches_query_topic(
            "порекомендуйте стоматолога в Потсдамі",
            {"text": "Массаж и депиляция в салоне Потсдама"},
        ))
        self.assertFalse(msg_ai._matches_query_topic(
            "порекомендуйте стоматолога в Потсдамі",
            {"text": "Психолог AWO Potsdam, запис у групі"},
        ))
        self.assertFalse(msg_ai._matches_query_topic(
            "порекомендуйте стоматолога в Потсдамі",
            {"text": "Логопед і дефектолог для дітей від 3 років"},
        ))
        self.assertTrue(msg_ai._matches_query_topic(
            "порекомендуйте стоматолога в Потсдамі",
            {"text": "Zahnarzt Potsdam: Dr. Yulia, стоматолог, тел. 0331..."},
        ))
        # Confectioner query must reject a groomer and keep a cake baker.
        self.assertFalse(msg_ai._matches_query_topic(
            "хто пече тортики",
            {"text": "Пропоную грумерські послуги для собак"},
        ))
        self.assertTrue(msg_ai._matches_query_topic(
            "хто пече тортики",
            {"text": "Печу торти на замовлення, бенто, капкейки +49..."},
        ))
        # Florist query keeps a florist, rejects a carrier.
        self.assertTrue(msg_ai._matches_query_topic(
            "може хтось є флорист зробити букет",
            {"text": "Флористка, збираю букети та композиції з квітів"},
        ))
        self.assertFalse(msg_ai._matches_query_topic(
            "може хтось є флорист зробити букет",
            {"text": "Продаю синю коляску в гарному кольорі"},
        ))
        self.assertFalse(msg_ai._matches_query_topic(
            "може хтось є флорист зробити букет",
            {"text": "Розклад поїздок на квітень"},
        ))
        self.assertFalse(msg_ai._matches_query_topic(
            "може хтось є флорист зробити букет",
            {"text": "Кондитер: торти, зефір у вигляді букетів, приймаю замовлення"},
        ))
        self.assertFalse(msg_ai._matches_query_topic(
            "може хтось є флорист зробити букет",
            {"text": "Перевезення посилок до України"},
        ))

    def test_provider_rerank_fallback_keeps_direct_offers_ahead_of_questions(self):
        messages = [
            {
                "id": 1,
                "user": "seeker",
                "date": "2026-07-19",
                "text": "Порадьте стоматолога в Потсдамі?",
            },
            {
                "id": 2,
                "user": "answer",
                "date": "2026-07-18",
                "text": "Рекомендую стоматолога Dr. Yulia, телефон 0331 708335",
            },
        ]

        with patch.object(msg_ai, "_call_ai", return_value=""):
            result = msg_ai._rerank(
                "порадьте стоматолога",
                messages,
                top_k=2,
                preserve_provider_offers=True,
            )

        self.assertEqual([item["id"] for item in result], [2, 1])

    def test_provider_rerank_prefers_concrete_contact_over_high_signal_discussion(self):
        messages = [
            {
                "id": 1,
                "user": "discussion",
                "date": "2026-07-19",
                "text": (
                    "Рекомендую обговорити, хто пече тортики; пропонуйте варіанти "
                    "і пишіть відповіді в чаті"
                ),
            },
            {
                "id": 2,
                "user": "baker",
                "date": "2025-12-01",
                "text": "Кондитер Світлана. Торти на замовлення. Тел. +49 331 708335",
            },
        ]

        with patch.object(msg_ai, "_call_ai", return_value=""):
            result = msg_ai._rerank(
                "хто пече тортики",
                messages,
                top_k=2,
                preserve_provider_offers=True,
            )

        self.assertEqual([item["id"] for item in result], [2, 1])

    def test_qdrant_backend_is_vector_ready_without_chroma(self):
        with patch.dict(os.environ, {"VECTOR_BACKEND": "qdrant"}):
            self.assertTrue(msg_ai._is_vector_ready())

    def test_vacancy_intent_is_detected_separately(self):
        queries = (
            "свежие вакансии в Потсдаме",
            "ищу работу водителем",
            "шукаю роботу",
            "aktuelle Stellenangebote",
        )

        for query in queries:
            with self.subTest(query=query):
                self.assertTrue(msg_ai._is_vacancy_query(query))

        self.assertFalse(msg_ai._is_vacancy_query("кто ремонтирует мебель"))

    def test_vacancy_candidates_are_limited_to_90_days_and_newest_first(self):
        from datetime import datetime, timedelta

        now = datetime.utcnow()
        candidates = [
            {"id": 1, "date_obj": now - timedelta(days=20), "date": "recent",
             "text": "Ищем водителя", "score": 0.7},
            {"id": 2, "date_obj": now - timedelta(days=95), "date": "stale",
             "text": "Вакансия водителя", "score": 0.9},
            {"id": 3, "date_obj": now - timedelta(days=2), "date": "newest",
             "text": "Требуется водитель", "score": 0.6},
            {"id": 3, "date_obj": now - timedelta(days=2), "date": "duplicate",
             "text": "Требуется водитель", "score": 0.6},
            {"id": 4, "date_obj": now - timedelta(days=1), "date": "noise",
             "text": "Обсуждали ремонт машины", "score": 0.95},
        ]

        result = msg_ai._prioritize_vacancy_candidates(candidates, now=now)

        self.assertEqual([item["id"] for item in result], [3, 1])

    def test_wait_indicator_uses_ukrainian_text_and_is_deleted(self):
        message = Mock(text="Потсдамбот знайди сантехніка")
        indicator = Mock()
        message.reply_text.return_value = indicator
        update = Mock(message=message)

        created = msg_ai._create_wait_indicator(update)
        msg_ai._delete_wait_indicator(created)

        message.reply_text.assert_called_once_with(
            "⏳ Запит отримано. Шукаю відповідь, будь ласка, зачекайте…",
            parse_mode=None,
        )
        indicator.delete.assert_called_once_with()

    def test_extract_query_accepts_alias_and_trigger_anywhere(self):
        cases = {
            "посдамбот муж на час": "муж на час",
            "Подскажите, потсдамбот, кто ремонтирует мебель?": "Подскажите, кто ремонтирует мебель?",
            "Потсдам бот найди электрика": "найди электрика",
        }

        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(msg_ai._extract_query(text), expected)

    def test_extract_query_rejects_trigger_inside_another_word(self):
        self.assertIsNone(msg_ai._extract_query("это непотсдамботик"))

    def test_extract_query_does_not_claim_other_potbot(self):
        self.assertIsNone(msg_ai._extract_query("Потбот найди электрика"))

    def test_provider_query_always_supplements_recent_results_with_full_history(self):
        recent = list(range(1, 8))
        historical = [50, 51]
        expected = [{"id": value} for value in recent + historical]

        with patch.object(
            msg_ai, "_search_keyword_ids", side_effect=[recent, historical]
        ) as search, patch.object(msg_ai, "_fetch_chain", return_value=expected):
            result = msg_ai._search_keywords_with_fallback(
                Mock(), [-1001], ["ремонт"], provider_query=True,
            )

        self.assertEqual(search.call_count, 2)
        self.assertEqual(result, expected)
        self.assertIsNotNone(search.call_args_list[1].kwargs["before"])

    def test_author_search_matches_fullname_username_and_alias(self):
        session = Mock()
        user_query = Mock()
        message_query = Mock()
        session.query.side_effect = [user_query, message_query]
        user_query.all.return_value = [
            Mock(id=7, fullname="Dmytro", username="Dmytriii"),
        ]
        message_query.outerjoin.return_value = message_query
        message_query.filter.return_value = message_query
        message_query.order_by.return_value = message_query
        message_query.limit.return_value = message_query
        message_query.all.return_value = [
            (Mock(_id=42), Mock(fullname="Dmytro", username="Dmytriii")),
        ]

        rows = msg_ai._search_provider_authors(
            session, [-1001], ["дмитрий", "dmytriii"], limit=10,
        )

        self.assertEqual(rows[0][0]._id, 42)

    def test_author_search_matches_cyrillic_fullname_case_insensitively(self):
        from database import Base, Chat, Message, User
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool

        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        session = sessionmaker(bind=engine)()
        try:
            session.add(Chat(id=-1001, title="test", enable=True))
            session.add(User(id=7, fullname="Дмитрий", username=""))
            session.add(Message(
                _id=42, id=420, from_id=7, from_chat=-1001,
                text="Ремонтирую мебель", text_lower="ремонтирую мебель",
            ))
            session.commit()

            rows = msg_ai._search_provider_authors(
                session, [-1001], ["дмитр"], limit=10,
            )

            self.assertEqual([row[0]._id for row in rows], [42])
        finally:
            session.close()
            engine.dispose()

    def test_provider_author_terms_extract_arbitrary_name_after_service(self):
        terms = msg_ai._provider_author_terms("муж на час Сергей")

        self.assertIn("сергей", terms)
        self.assertNotIn("муж", terms)
        self.assertNotIn("час", terms)

    def test_provider_author_terms_ignore_service_words(self):
        terms = msg_ai._provider_author_terms("нужен электрик Сергей")

        self.assertEqual(terms, ["сергей"])

    def test_vector_search_subprocess_returns_message_dicts(self):
        fake = type("Result", (), {
            "returncode": 0,
            "stdout": '[{"id": 42, "text": "ремонт", "user": "Dmytriii"}]\n',
            "stderr": "",
        })()

        with patch.dict(os.environ, {"VECTOR_BACKEND": "qdrant"}), patch.object(
            msg_ai.subprocess, "run", return_value=fake
        ) as run:
            result = msg_ai._vector_search("муж на час", n_results=12, since_days=365)

        self.assertEqual(result[0]["id"], 42)
        payload = json.loads(run.call_args.kwargs["input"])
        self.assertEqual(payload["query"], "муж на час")
        self.assertEqual(payload["n_results"], 12)
        self.assertEqual(payload["since_days"], 365)

    def test_vector_search_subprocess_crash_falls_back_without_killing_bot(self):
        fake = type("Result", (), {
            "returncode": 134,
            "stdout": "",
            "stderr": "Fatal glibc error",
        })()

        with patch.object(msg_ai.subprocess, "run", return_value=fake):
            self.assertEqual(msg_ai._vector_search("муж на час"), [])

    def test_provider_author_terms_do_not_treat_connectors_as_names(self):
        self.assertEqual(
            msg_ai._provider_author_terms(
                "муж на час или мастер по ремонту бытовой техники или сантехник есть в чате"
            ),
            [],
        )
        self.assertEqual(
            msg_ai._provider_author_terms(
                "знайди в групі контакти майстрів типу чоловік на годину"
            ),
            [],
        )

    def test_provider_rerank_keeps_direct_offer_omitted_by_ai(self):
        messages = [
            {
                "id": 10,
                "user": "Dmytriii",
                "date": "2026-04-26",
                "text": (
                    "Допоможу з ремонтом оселі, зібрати меблі, підключити "
                    "електроприлади. Телефон +49 160 94878456"
                ),
            },
            {
                "id": 11,
                "user": "requester",
                "date": "2026-07-01",
                "text": "Порадьте майстра для ремонту пральної машини?",
            },
        ] + [
            {
                "id": value,
                "user": f"user{value}",
                "date": "2026-06-01",
                "text": "Загальна розмова без контакту",
            }
            for value in range(12, 40)
        ]

        with patch.object(msg_ai, "_call_ai", return_value="[1, 2, 3]"):
            result = msg_ai._rerank(
                "муж на час или мастер по ремонту бытовой техники",
                messages,
                top_k=3,
                preserve_provider_offers=True,
            )

        self.assertIn(10, [item["id"] for item in result])

    def test_provider_offer_search_finds_older_handyman_despite_newer_questions(self):
        from datetime import datetime, timedelta
        from database import Base, Chat, Message, User
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool

        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        session = sessionmaker(bind=engine)()
        try:
            session.add(Chat(id=-1001, title="test", enable=True))
            session.add_all([
                User(id=7, fullname="Dmytro", username="Dmytriii"),
                User(id=8, fullname="Requester", username="requester"),
            ])
            session.add_all([
                Message(
                    _id=42, id=420, from_id=7, from_chat=-1001,
                    date=datetime(2025, 1, 5),
                    text=(
                        "Допоможу з ремонтом оселі, зібрати меблі, підключити "
                        "побутову техніку. Телефон +49 160 94878456"
                    ),
                    text_lower=(
                        "допоможу з ремонтом оселі, зібрати меблі, підключити "
                        "побутову техніку. телефон +49 160 94878456"
                    ),
                ),
                Message(
                    _id=43, id=430, from_id=7, from_chat=-1001,
                    date=datetime(2025, 2, 5),
                    text="Пропоную ремонт оселі та збирання меблів. Телефон +49 160 94878456",
                    text_lower="пропоную ремонт оселі та збирання меблів. телефон +49 160 94878456",
                ),
            ])
            for offset in range(30):
                text = "Порадьте майстра, потрібен чоловік на годину для ремонту"
                session.add(Message(
                    _id=100 + offset, id=1000 + offset, from_id=8, from_chat=-1001,
                    date=datetime(2026, 7, 1) + timedelta(minutes=offset),
                    text=text, text_lower=text.lower(),
                ))
            session.commit()

            offers = msg_ai._search_provider_offers(
                session,
                [-1001],
                "знайди контакти майстрів типу чоловік на годину",
            )

            self.assertEqual([item["user"] for item in offers], ["Dmytriii"])
            self.assertEqual(offers[0]["id"], 43)
        finally:
            session.close()
            engine.dispose()

    def test_carrier_offer_search_finds_old_contact_catalog(self):
        from datetime import datetime
        from database import Base, Chat, Message, User
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool

        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        session = sessionmaker(bind=engine)()
        try:
            session.add(Chat(id=-1001, title="test", enable=True))
            session.add(User(id=7, fullname="Потбот", username="potbot"))
            session.add(Message(
                _id=42, id=420, from_id=7, from_chat=-1001,
                date=datetime(2024, 1, 5),
                text=(
                    "Потбот знає таке: перевізники в/з України. "
                    "Діма +380966622986, Іван +380676746710"
                ),
                text_lower=(
                    "потбот знає таке: перевізники в/з україни. "
                    "діма +380966622986, іван +380676746710"
                ),
            ))
            session.commit()

            offers = msg_ai._search_provider_offers(
                session, [-1001], "перевізник до України",
            )

            self.assertEqual([item["id"] for item in offers], [42])
        finally:
            session.close()
            engine.dispose()

    def test_provider_candidate_filter_excludes_bot_queries_anywhere(self):
        messages = [
            {"id": 1, "text": "Ремонтирую мебель, пишите в ЛС", "date": "2026-07-19"},
            {"id": 2, "text": "Подскажите, потсдамбот, кто ремонтирует мебель?", "date": "2026-07-19"},
        ]

        result = msg_ai._filter_provider_candidates(messages, "ремонт мебели")

        self.assertEqual([item["id"] for item in result], [1])

    def test_provider_author_search_excludes_bot_queries_anywhere(self):
        from database import Base, Chat, Message, User
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool

        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        session = sessionmaker(bind=engine)()
        try:
            session.add(Chat(id=-1001, title="test", enable=True))
            session.add(User(id=7, fullname="Сергей", username="sergey"))
            session.add_all([
                Message(
                    _id=41, id=410, from_id=7, from_chat=-1001,
                    text="Ремонтирую мебель", text_lower="ремонтирую мебель",
                ),
                Message(
                    _id=42, id=420, from_id=7, from_chat=-1001,
                    text="Подскажите, потсдамбот, кто чинит мебель?",
                    text_lower="подскажите, потсдамбот, кто чинит мебель?",
                ),
            ])
            session.commit()

            rows = msg_ai._search_provider_authors(
                session, [-1001], ["сергей"], limit=10,
            )

            self.assertEqual([row[0]._id for row in rows], [41])
        finally:
            session.close()
            engine.dispose()

    def test_provider_history_keeps_multiple_messages_per_author(self):
        messages = [
            {"id": value, "user": "Dmytriii", "date": f"2026-04-{value:02d}"}
            for value in range(1, 7)
        ] + [{"id": 99, "user": "Other", "date": "2026-05-01"}]

        grouped = msg_ai._group_provider_history(messages)

        self.assertEqual([item["id"] for item in grouped], [1, 2, 3, 4, 99])


if __name__ == "__main__":
    unittest.main()
