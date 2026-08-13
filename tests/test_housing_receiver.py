import unittest

from user_handlers import housing_receiver


class FakeBot:
    def __init__(self):
        self.messages = []

    def send_message(self, **kwargs):
        self.messages.append(kwargs)


class HousingReceiverTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
