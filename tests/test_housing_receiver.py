import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from telegram.error import NetworkError

from database import Base, ImmoweltListing
from user_handlers import housing_receiver


class FakeBot:
    def __init__(self, error=None):
        self.messages = []
        self.error = error

    def send_message(self, **kwargs):
        if self.error is not None:
            raise self.error
        self.messages.append(kwargs)


def _payload(listing_id="abc", user_id=544675510):
    return {
        "source": "immowelt",
        "user_id": user_id,
        "filter_title": "Пошук Каті",
        "listing": {
            "listing_id": listing_id,
            "url": "https://www.immowelt.de/expose/%s" % listing_id,
            "title": "Wohnung zur Miete",
            "price": "1.119 €",
            "rooms": "3 Zimmer",
            "address": "Brunnenallee 3 a, Waldstadt I, Potsdam (14478)",
        },
    }


class HousingReceiverTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            'sqlite://',
            connect_args={'check_same_thread': False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self._original_session = housing_receiver.DBSession
        housing_receiver.DBSession = sessionmaker(bind=self.engine)

    def tearDown(self):
        housing_receiver.DBSession = self._original_session
        self.engine.dispose()
    def test_immowelt_payload_is_sent_to_filter_owner(self):
        bot = FakeBot()
        payload = {
            "source": "immowelt",
            "user_id": 544675510,
            "filter_title": "Пошук Каті",
            "listing": {
                "listing_id": "abc",
                "url": "https://www.immowelt.de/expose/abc",
                "title": "Wohnung zur Miete",
                "price": "1.119 €",
                "rooms": "3 Zimmer",
                "area": "75,7 m²",
                "floor": "1. Geschoss",
                "availability": "frei ab 02.08.2026",
                "address": "Brunnenallee 3 a, Waldstadt I, Potsdam (14478)",
            },
        }

        result = housing_receiver.handle_immowelt_result(bot, payload)

        self.assertEqual(result, {"ok": True})
        self.assertEqual(bot.messages[0]["chat_id"], 544675510)
        self.assertIn("Нове житло на Immowelt", bot.messages[0]["text"])
        self.assertIn("Пошук Каті", bot.messages[0]["text"])
        self.assertEqual(
            bot.messages[0]["reply_markup"].inline_keyboard[0][0].url,
            "https://www.immowelt.de/expose/abc",
        )

    def test_immowelt_listing_is_recorded_with_parsed_numbers_for_stats(self):
        bot = FakeBot()

        housing_receiver.handle_immowelt_result(bot, {
            "source": "immowelt",
            "user_id": 544675510,
            "filter_title": "Пошук Каті",
            "listing": {
                "listing_id": "abc",
                "url": "https://www.immowelt.de/expose/abc",
                "title": "Wohnung zur Miete",
                "price": "1.119 €",
                "rooms": "3 Zimmer",
                "area": "75,7 m²",
                "address": "Brunnenallee 3 a, Waldstadt I, Potsdam (14478)",
            },
        })

        session = housing_receiver.DBSession()
        try:
            row = session.query(ImmoweltListing).get("abc")
        finally:
            session.close()
        self.assertIsNotNone(row)
        self.assertEqual(row.rooms, 3.0)
        self.assertEqual(row.area_m2, 75.7)
        self.assertEqual(row.price_eur, 1119.0)

    def test_immowelt_listing_is_recorded_once_across_duplicate_deliveries(self):
        bot = FakeBot()

        housing_receiver.handle_immowelt_result(bot, _payload())
        housing_receiver.handle_immowelt_result(bot, _payload())

        session = housing_receiver.DBSession()
        try:
            count = session.query(ImmoweltListing).count()
        finally:
            session.close()
        self.assertEqual(count, 1)

    def test_parse_number_handles_missing_and_unparseable_values(self):
        self.assertIsNone(housing_receiver._parse_number(None))
        self.assertIsNone(housing_receiver._parse_number(""))
        self.assertIsNone(housing_receiver._parse_number("keine Angabe"))
        self.assertEqual(housing_receiver._parse_number("3,5 Zimmer"), 3.5)

    def test_immowelt_payload_rejects_non_immowelt_url(self):
        bot = FakeBot()
        payload = {
            "source": "immowelt",
            "user_id": 544675510,
            "filter_title": "Пошук Каті",
            "listing": {
                "listing_id": "abc",
                "url": "https://example.com/abc",
                "title": "Wohnung zur Miete",
                "address": "Potsdam",
            },
        }

        with self.assertRaisesRegex(ValueError, "Immowelt"):
            housing_receiver.handle_immowelt_result(bot, payload)

        self.assertEqual(bot.messages, [])

    def test_same_listing_is_not_sent_to_the_same_person_twice(self):
        bot = FakeBot()

        first = housing_receiver.handle_immowelt_result(bot, _payload())
        second = housing_receiver.handle_immowelt_result(bot, _payload())

        self.assertEqual(first, {"ok": True})
        self.assertEqual(second, {"ok": True, "duplicate": True})
        self.assertEqual(len(bot.messages), 1)

    def test_other_person_still_gets_the_same_listing(self):
        bot = FakeBot()

        housing_receiver.handle_immowelt_result(bot, _payload(user_id=1))
        housing_receiver.handle_immowelt_result(bot, _payload(user_id=2))

        self.assertEqual([m["chat_id"] for m in bot.messages], [1, 2])

    def test_lost_response_counts_as_delivered_and_blocks_the_retry(self):
        # Telegram доставляет сообщение и обрывает ответ: отправитель повторит,
        # и без этой ветки человек получил бы вторую копию.
        broken = FakeBot(error=NetworkError(
            "urllib3 HTTPError ('Connection aborted.', "
            "RemoteDisconnected('Remote end closed connection without response'))"
        ))

        result = housing_receiver.handle_immowelt_result(broken, _payload())

        self.assertEqual(result, {"ok": True, "assumed_delivered": True})

        retry = FakeBot()
        self.assertEqual(
            housing_receiver.handle_immowelt_result(retry, _payload()),
            {"ok": True, "duplicate": True},
        )
        self.assertEqual(retry.messages, [])

    def test_connect_timeout_stays_retryable(self):
        # Соединение не открылось — сообщения человек не видел, повтор обязан
        # состояться, иначе объявление потеряется совсем.
        broken = FakeBot(error=NetworkError(
            "urllib3 HTTPError HTTPSConnectionPool(host='api.telegram.org', port=443): "
            "Max retries exceeded (Caused by ConnectTimeoutError("
            "'Connection to api.telegram.org timed out. (connect timeout=5.0)'))"
        ))

        with self.assertRaises(NetworkError):
            housing_receiver.handle_immowelt_result(broken, _payload())

        retry = FakeBot()
        self.assertEqual(housing_receiver.handle_immowelt_result(retry, _payload()), {"ok": True})
        self.assertEqual(len(retry.messages), 1)

    def test_system_message_is_sent_as_html(self):
        bot = FakeBot()

        result = housing_receiver.handle_system_message(bot, {"user_id": 312029534, "text": "🟡 <b>Тест</b>"})

        self.assertEqual(result, {"ok": True})
        self.assertEqual(bot.messages[0]["chat_id"], 312029534)
        self.assertEqual(bot.messages[0]["text"], "🟡 <b>Тест</b>")
        self.assertEqual(bot.messages[0]["parse_mode"], "HTML")

    def test_system_message_requires_user_id_and_text(self):
        bot = FakeBot()

        with self.assertRaisesRegex(ValueError, "user_id"):
            housing_receiver.handle_system_message(bot, {"text": "hi"})
        with self.assertRaisesRegex(ValueError, "text"):
            housing_receiver.handle_system_message(bot, {"user_id": 1})
        self.assertEqual(bot.messages, [])

    def test_system_message_lost_response_counts_as_delivered(self):
        broken = FakeBot(error=NetworkError(
            "urllib3 HTTPError ('Connection aborted.', "
            "RemoteDisconnected('Remote end closed connection without response'))"
        ))

        result = housing_receiver.handle_system_message(broken, {"user_id": 1, "text": "hi"})

        self.assertEqual(result, {"ok": True, "assumed_delivered": True})

    def test_system_message_connect_timeout_stays_retryable(self):
        broken = FakeBot(error=NetworkError(
            "urllib3 HTTPError HTTPSConnectionPool(host='api.telegram.org', port=443): "
            "Max retries exceeded (Caused by ConnectTimeoutError("
            "'Connection to api.telegram.org timed out. (connect timeout=5.0)'))"
        ))

        with self.assertRaises(NetworkError):
            housing_receiver.handle_system_message(broken, {"user_id": 1, "text": "hi"})


if __name__ == "__main__":
    unittest.main()
