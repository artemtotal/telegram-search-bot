import importlib
import json
import os
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock


def load(bg=False, **env):
    """Свежий модуль с нужными настройками (они читаются при импорте).
    Фоновая загрузка каталога в тестах выключена (в сеть не ходим); bg=True оставляет её настоящей."""
    base = {"MASTERS_URL": "https://example.org/m/", "MASTERS_HINT": "on", "MASTERS_HINT_CARDS": ""}
    base.update(env)
    with mock.patch.dict(os.environ, base, clear=False):
        import user_handlers.masters_hint as mh
        mh = importlib.reload(mh)
    if not bg:
        mh.warm = lambda: None
    return mh


class FakeMessage:
    def __init__(self, text, chat_id=-100, message_id=1, reply=None, is_topic=False, thread_id=None):
        self.text = text
        self.chat_id = chat_id
        self.message_id = message_id
        self.reply_to_message = reply
        self.is_topic_message = is_topic
        self.message_thread_id = thread_id
        self.sent = []

    def reply_text(self, text, **kwargs):
        self.sent.append((text, kwargs))


class MatchTest(unittest.TestCase):
    def setUp(self):
        self.mh = load()

    def test_requests_for_masters(self):
        cases = {
            "Добрый день. Посоветуйте русскоговорящего гинеколога.": "health",
            "Здравствуйте,подскажите пожалуйста нотариуса": "docs",
            "Привіт. Порадьте, будь ласка, перекладача, щоб піти на прийом до окуліста.": "docs",
            "Хто робить перевезення по Потсдаму?": "movers",
            "Добрый день. Подскажите пожалуйста перевозчика на Украину (перевозка посылок)": "ukraine",
            "шукаю хорошого перукаря з чоловічих стрижок, якщо у когось є рекомендації, напишіть будь ласка": "beauty",
            "Добрый день\nКто может подсказать контакты СТО?": "auto",
            "Нужен электрик с документами, кто проверит проводку и составит схему.": "home",
            "Э хтось хто робить торти? Треба весільний торт": "food",
            "Доброго дня! Підкажіть, будь ласка, хороші ремонтні сервіси для Макбуків в Берліні чи поблизу": "tech",
            "Ищу мастера, который разбирается в принтерах Epson, писать в личку": "tech",
            "Доброго дня. Підкажіть будь ласка контакти майстра з татуювання у Потсдамі": "",
        }
        for text, cat in cases.items():
            self.assertEqual(self.mh.match(text), cat, text)

    def test_not_a_request(self):
        for text in [
            "Потсдам сегодня красивый, правда?",                                    # «сдам» внутри слова, нет услуги
            "Пропоную манікюр і педикюр у Потсдамі, запис відкрито",                 # предложение, не просьба
            "Шукаю роботу перукарем, є досвід",                                      # ищет работу
            "Шукаю професійного жіночого перукаря в салон. Якщо ви зацікавлені, напишіть",   # наём
            "Шукаю 2-х малярів і 2-х сантехніків в Берліні +49 176 61461546",        # объявление с телефоном
            "Якщо просто психолог потрібен, то і в Потсдамі є. В Альбатрос не зверталися?",   # совет в ответ
            "Друзі, де можна знайти недороге приміщення для кабінету майстра манікюру?",      # ищут помещение
            "Кто знает, где можно в Потсдаме купить коробку для торта?",             # покупка, не мастер
            "Підкажіть, чи потрібен WBS, якщо платить джобцентр?",                   # вопрос не про услугу
            "Шукаю квартиру в Потсдамі, 2 кімнати, порадьте маклера чи сайт?",       # жильё
            "ок",
        ]:
            self.assertIsNone(self.mh.match(text), text)

    def test_text_is_short_ukrainian_with_link(self):
        self.assertEqual(self.mh.text_for(""), "🧰 Увесь каталог майстрів і послуг нашого чату тут: https://example.org/m/?s=hint")
        self.assertIn("https://example.org/m/?s=hint#cat=beauty", self.mh.text_for("beauty"))
        self.assertEqual(self.mh.link("bot"), "https://example.org/m/?s=bot")
        self.assertIn("«Краса»", self.mh.text_for("beauty"))
        self.assertLess(len(self.mh.text_for("docs")), 160)


class ReplyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = os.path.join(self.tmp.name, "hint.json")
        self.user = SimpleNamespace(id=7, is_bot=False)

    def tearDown(self):
        self.tmp.cleanup()

    def test_on_sends_once_per_user_and_respects_gap(self):
        mh = load(MASTERS_HINT_STATE=self.state)
        ask = "Порадьте, будь ласка, стоматолога в Потсдамі"
        first = FakeMessage(ask)
        self.assertTrue(mh.maybe_reply(first, self.user, ask))
        self.assertEqual(len(first.sent), 1)
        self.assertIn("#cat=health", first.sent[0][0])
        again = FakeMessage(ask, message_id=2)
        self.assertFalse(mh.maybe_reply(again, self.user, ask))          # тому же человеку не повторяем
        other = FakeMessage(ask, message_id=3)
        self.assertFalse(mh.maybe_reply(other, SimpleNamespace(id=8, is_bot=False), ask))   # пауза в чате
        self.assertEqual(again.sent + other.sent, [])

    def test_gap_and_daily_limit(self):
        mh = load(MASTERS_HINT_STATE=self.state, MASTERS_HINT_GAP_MIN="20", MASTERS_HINT_MAX_PER_DAY="2")
        t0 = 1_000_000.0
        self.assertTrue(mh._reserve(-100, 1, now=t0))
        self.assertFalse(mh._reserve(-100, 2, now=t0 + 60))               # 1 минута: рано
        self.assertTrue(mh._reserve(-100, 2, now=t0 + 21 * 60))           # через 21 минуту можно
        self.assertFalse(mh._reserve(-100, 3, now=t0 + 3 * 3600))         # третья за сутки: нельзя
        self.assertTrue(mh._reserve(-100, 3, now=t0 + 25 * 3600))         # на следующий день можно
        self.assertFalse(mh._reserve(-100, 1, now=t0 + 6 * 86400))        # тому же человеку раньше недели нельзя
        self.assertTrue(mh._reserve(-100, 1, now=t0 + 8 * 86400))

    def test_log_mode_and_off_do_not_write_to_chat(self):
        ask = "Порадьте, будь ласка, стоматолога в Потсдамі"
        for mode in ("log", "off"):
            mh = load(MASTERS_HINT=mode, MASTERS_HINT_STATE=os.path.join(self.tmp.name, mode + ".json"))
            msg = FakeMessage(ask)
            self.assertFalse(mh.maybe_reply(msg, self.user, ask))
            self.assertEqual(msg.sent, [])
        mh = load(MASTERS_URL="", MASTERS_HINT_STATE=self.state)           # без ссылки выключено
        self.assertFalse(mh.enabled())

    def test_settings_from_config_file_and_env_wins(self):
        import json
        cfg = os.path.join(self.tmp.name, "masters.json")
        with open(cfg, "w", encoding="utf-8") as fh:
            json.dump({"url": "https://file.example/", "hint": "on", "gap_min": 5, "max_per_day": 3}, fh)
        clean = {k: "" for k in ("MASTERS_URL", "MASTERS_HINT", "MASTERS_HINT_GAP_MIN", "MASTERS_HINT_MAX_PER_DAY")}
        mh = load(MASTERS_CONFIG=cfg, MASTERS_HINT_STATE=self.state, **clean)
        self.assertEqual((mh.MASTERS_URL, mh.MODE, mh.GAP_MIN, mh.MAX_PER_DAY), ("https://file.example/", "on", 5.0, 3))
        self.assertTrue(mh.enabled())
        mh = load(MASTERS_CONFIG=cfg, MASTERS_HINT_STATE=self.state, MASTERS_URL="https://env.example/", MASTERS_HINT="off")
        self.assertEqual((mh.MASTERS_URL, mh.MODE), ("https://env.example/", "off"))
        mh = load(MASTERS_CONFIG=os.path.join(self.tmp.name, "nope.json"), MASTERS_HINT_STATE=self.state, **clean)
        self.assertEqual((mh.MASTERS_URL, mh.MODE), ("", "off"))

    def test_reply_failure_is_swallowed(self):
        mh = load(MASTERS_HINT_STATE=self.state)
        msg = FakeMessage("Порадьте стоматолога в Потсдамі, будь ласка?")
        msg.reply_text = mock.Mock(side_effect=RuntimeError("boom"))
        self.assertFalse(mh.maybe_reply(msg, self.user, msg.text))


class GroupHandlerTest(unittest.TestCase):
    """kb_search.on_group_message: подсказка уходит, когда ответа из справочника в чат не было."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.mh = load(MASTERS_HINT_STATE=os.path.join(self.tmp.name, "hint.json"))
        import user_handlers.kb_search as kb_search
        self.kb = importlib.reload(kb_search)
        self.user = SimpleNamespace(id=42, is_bot=False)

    def tearDown(self):
        self.tmp.cleanup()

    def _update(self, message):
        message.from_user = self.user
        return SimpleNamespace(effective_message=message)

    def test_hint_when_kb_not_ready(self):
        msg = FakeMessage("Добрый день. Посоветуйте русскоговорящего гинеколога.")
        with mock.patch.object(self.kb, "ready", return_value=False):
            self.kb.on_group_message(self._update(msg), SimpleNamespace(bot=SimpleNamespace(username="Potsdambot")))
        self.assertEqual(len(msg.sent), 1)
        self.assertIn("каталог", msg.sent[0][0])

    def test_topic_message_is_not_a_reply_but_real_reply_is_skipped(self):
        topic_root = SimpleNamespace(message_id=500, forum_topic_created=SimpleNamespace(name="Послуги"))
        in_topic = FakeMessage("Порадьте, будь ласка, перукаря в Потсдамі", reply=topic_root, is_topic=True, thread_id=500)
        with mock.patch.object(self.kb, "ready", return_value=False):
            self.kb.on_group_message(self._update(in_topic), SimpleNamespace(bot=SimpleNamespace(username="Potsdambot")))
        self.assertEqual(len(in_topic.sent), 1)
        other = SimpleNamespace(message_id=777, forum_topic_created=None)
        answer = FakeMessage("Порадьте, будь ласка, перукаря в Потсдамі", message_id=9, reply=other, is_topic=True, thread_id=500)
        self.user = SimpleNamespace(id=43, is_bot=False)
        with mock.patch.object(self.kb, "ready", return_value=False):
            self.kb.on_group_message(self._update(answer), SimpleNamespace(bot=SimpleNamespace(username="Potsdambot")))
        self.assertEqual(answer.sent, [])

    def test_bot_and_potsdambot_queries_are_ignored(self):
        msg = FakeMessage("потсдамбот порадь стоматолога?")
        with mock.patch.object(self.kb, "ready", return_value=False):
            self.kb.on_group_message(self._update(msg), SimpleNamespace(bot=SimpleNamespace(username="Potsdambot")))
        self.assertEqual(msg.sent, [])


# ── Ответ конкретными мастерами из каталога (26.11.2026) ────────────────────────────────────────────────────

def card(card_id, name, cat, sub, kw=(), **extra):
    """Карточка подставного каталога в том же виде, что в site/data.json (люди и телефоны выдуманные)."""
    row = {"id": card_id, "name": name, "cat": cat, "sub": sub, "desc": "", "kw": list(kw), "area": "", "addr": "", "mode": [],
           "c": [{"t": "tel", "v": "+49 331 5550100", "h": "tel:+493315550100", "wa": "", "l": ""}],
           "rec": 0, "ads": 0, "adc": False, "first": "2024", "last": "2025-06"}
    row.update(extra)
    return row


CATALOG = {"built": "2026-11-26", "items": [
    card("den1", "Стоматолог Олена Зразкова", "health", "стоматолог", ["стоматолог", "зубной врач", "Zahnarzt"], area="Potsdam", rec=1),
    card("den2", "Zahnarztpraxis Muster", "health", "стоматологія", ["зубной врач", "Zahnarzt"], area="Berlin", rec=3),
    card("den3", "Стоматологія для дітей «Усмішка»", "health", "дитячий стоматолог", ["детский стоматолог"], rec=5, last="2022-08"),
    card("ort1", "Ортодонт Dr. Beispiel", "health", "ортодонт", ["брекеты", "Zahnspange"], area="Potsdam", rec=8),
    card("mas1", "Масаж, Тарас", "health", "масаж", ["массаж", "массажист"], area="Potsdam", rec=9),
    card("gyn1", "Praxis Beispiel", "health", "гінеколог", ["Frauenarzt"], area="Potsdam"),
    card("ped1", "Педіатр Ірина Зразкова", "health", "педіатр", ["детский врач"], area="Potsdam", rec=4),
    card("ped2", "Kinderarztpraxis Muster", "health", "педіатр", ["педиатр", "Kinderarzt"], area="Berlin"),
    card("tr1", "Бюро «Зразок»", "docs", "усний переклад, супровід", ["переводчик", "устный перевод", "Dolmetscher"], rec=2),
    card("tr2", "Присяжна перекладачка Наталія Зразкова", "docs", "присяжні переклади", ["присяжный переводчик"], area="Potsdam", rec=6),
    card("tr3", "Перекладачка Natalya Zrazkova", "docs", "переклад документів", ["переводчик", "перевод документов"], area="Potsdam", rec=1),
    card("not1", "Notare Muster", "docs", "нотаріус", ["нотариус", "Beglaubigung"], area="Potsdam", rec=9),
    card("mov1", "Перевезення меблів, Тест", "movers", "перевезення меблів", ["переезд", "грузчики"], mode=["visit"]),
    card("ua1", "Рейси в Україну, Тест", "ukraine", "посилки, пасажири", ["посылки", "автобус в Украину"], area="Ukraine–Deutschland", rec=3),
    card("ua2", "Остап, рейси в Україну", "ukraine", "речі та пасажири", ["перевозчик", "посылки"], area="Ukraine–Deutschland", lst=1),
    card("ua3", "Перевізник Тарас, Потсдам–Україна", "ukraine", "посилки", ["посылки", "передачи"], area="Ukraine–Deutschland"),
    card("ua4", "Перевезення Україна–Німеччина, Тест", "ukraine", "пасажирські перевезення", ["автобус"], area="Ukraine–Deutschland"),
    card("cle1", "Чищення диванів Muster", "cleaning", "хімчистка меблів", ["химчистка", "диван"]),
    card("hair1", "Friseur Muster", "beauty", "стрижки", ["парикмахер"], rec=1),
    card("hair2", "Салон «Зразок»", "beauty", "перукарня", []),
    card("oth1", "Копіцентр Muster", "other", "друк", ["печать"]),
]}

NAILS = [
    card("n_rec1", "Манікюр Б", "beauty", "манікюр", ["маникюр"], rec=1, last="2026-01"),
    card("n_new", "Манікюр Д", "beauty", "манікюр", ["маникюр"], last="2026-06"),
    card("n_old", "Манікюр Є", "beauty", "манікюр", ["маникюр"], rec=7, last="2023-12"),
    card("n_adc", "Манікюр Г", "beauty", "манікюр", ["маникюр"], adc=True, last="2024-02"),
    card("n_rec2", "Манікюр А", "beauty", "манікюр", ["маникюр"], rec=2, last="2024-05"),
    card("n_mid", "Манікюр Е", "beauty", "манікюр", ["маникюр"], last="2025-01"),
    card("n_lst", "Манікюр В", "beauty", "манікюр", ["маникюр"], lst=1, last="2024-01"),
    card("hair", "Перукар Ж", "beauty", "перукар", ["парикмахер"], rec=50),
]


class CardsTest(unittest.TestCase):
    """Бот называет до трёх мастеров из каталога; каталог подставной, сети нет."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.mh = load(MASTERS_HINT_STATE=os.path.join(self.tmp.name, "hint.json"))
        self.user = SimpleNamespace(id=7, is_bot=False)

    def tearDown(self):
        self.tmp.cleanup()

    def fill(self, data=CATALOG, mh=None):
        """Каталог попадает в память тем же путём, что в боте, только вместо сети — готовые байты."""
        mh = mh or self.mh
        raw = data if isinstance(data, bytes) else json.dumps(data).encode("utf-8")
        with mock.patch.object(mh, "_download", return_value=raw):
            return mh._refresh()

    def ids(self, text, cat=None):
        return self.mh.answer(cat if cat is not None else self.mh.match(text), text)[1]

    def test_cards_match_the_word_of_the_request(self):
        self.assertTrue(self.fill())
        ask = "Порадьте, будь ласка, стоматолога"
        body, shown = self.mh.answer(self.mh.match(ask), ask)
        self.assertEqual(shown, ["den2", "den1", "den3"])            # чаще советовали; свежая; давняя (2022) последней
        self.assertNotIn("mas1", shown)                               # массажиста советовали чаще всех, но спросили не его
        self.assertNotIn("ort1", shown)                               # Zahnspange у ортодонта не делает его стоматологом
        lines = body.split("\n")
        self.assertEqual(lines[0], "🧰 З каталогу майстрів і послуг нашого чату:")
        self.assertEqual(lines[1:3], ["• Zahnarztpraxis Muster: стоматологія, Berlin", "https://example.org/m/?s=hint#id=den2"])
        self.assertEqual(lines[3], "• Стоматолог Олена Зразкова: стоматолог, Potsdam")
        self.assertEqual(lines[5], "• Стоматологія для дітей «Усмішка»: дитячий стоматолог")      # района нет: без запятой
        self.assertEqual(lines[-1], "Увесь розділ «Здоров’я»: https://example.org/m/?s=hint#cat=health")
        self.assertEqual(len(lines), 8)
        for secret in ("5550100", "+49", "tel:"):                     # телефоны в чат не выводим
            self.assertNotIn(secret, body)

    def test_ukrainian_and_russian_forms(self):
        self.fill()
        self.assertEqual(self.ids("Добрый день. Посоветуйте русскоговорящего гинеколога."), ["gyn1"])    # в карточке только «гінеколог»
        self.assertEqual(self.ids("Здравствуйте, подскажите пожалуйста нотариуса"), ["not1"])
        # спросили по-украински, а в карточке слово только русское или немецкое, и наоборот
        self.assertEqual(self.ids("Шукаю хорошого перукаря, порадьте когось"), ["hair1", "hair2"])
        self.assertEqual(self.ids("Подскажите, пожалуйста, хорошего парикмахера"), ["hair1", "hair2"])
        self.assertEqual(self.ids("Порадьте, будь ласка, педіатра для малюка"), ["ped1", "ped2"])
        self.assertIn("tr1", self.ids("Потрібен перекладач"))
        self.assertEqual(self.ids("Хто робить перевезення по Потсдаму?"), ["mov1"])                     # в карточке «переезд, грузчики»

    def test_narrow_topic_beats_wide_one(self):
        self.fill()
        self.assertEqual(self.ids("Порадьте присяжного перекладача, треба перекласти диплом"), ["tr2"])     # вторая карточка того же человека не нужна
        self.assertEqual(self.ids("Доброго ранку. Потрібен перекладач на 23 вересня на 11:45 до лікаря."), ["tr1"])
        self.assertEqual(self.ids("Потрібен перекладач"), ["tr2", "tr1"])                               # без уточнений: все переводчики, без дубля
        self.assertNotIn("not1", self.ids("Подскажите пожалуйста контакты немецкого присяжного. Спасибо"))

    def test_order(self):
        cards = self.mh._parse({"items": NAILS})
        with mock.patch.object(self.mh, "MAX_CARDS", 10):
            got = [c["id"] for c in self.mh.pick("beauty", "Порадьте майстра манікюру", cards, "2026-11-26")]
        # рекомендации из чата; список проверенных; рекламодатель; свежесть; давняя (до 2024) в конце, хоть её и советовали чаще всех
        self.assertEqual(got, ["n_rec2", "n_rec1", "n_lst", "n_adc", "n_new", "n_mid", "n_old"])
        self.assertEqual([c["id"] for c in self.mh.pick("beauty", "Порадьте майстра манікюру", cards, "2026-11-26")],
                         ["n_rec2", "n_rec1", "n_lst"])                                               # в ответ идут первые три

    def test_pinned_card_is_first_and_marked(self):
        data = {"items": CATALOG["items"] + [
            card("pin1", "Стоматолог Реклама", "health", "стоматолог", ["Zahnarzt"], area="Berlin", pin=1, last="2026-11"),
            card("pin2", "Масаж Реклама", "health", "масаж", ["массаж"], pin=1, last="2026-11"),
        ]}
        self.fill(data)
        ask = "Порадьте, будь ласка, стоматолога в Потсдамі"
        body, shown = self.mh.answer("health", ask)
        self.assertEqual(shown, ["pin1", "den1", "den2"])             # закреп первым, хотя он в Берлине и его не советовали
        self.assertNotIn("pin2", shown)                               # закреп другого мастера (массаж) сюда не попадает
        lines = body.split("\n")
        self.assertEqual(lines[1], "• Стоматолог Реклама (реклама): стоматолог, Berlin")
        self.assertEqual(body.count("(реклама)"), 1)

    def test_city_child_name_and_exception(self):
        self.fill()
        self.assertEqual(self.ids("Порадьте, будь ласка, стоматолога в Потсдамі"), ["den1", "den2", "den3"])    # Потсдам выше Берлина
        self.assertEqual(self.ids("Порадьте дитячого стоматолога, будь ласка")[0], "den3")                    # «для детей» важнее давности
        self.assertEqual(self.ids("Підкажіть, будь ласка, де можна відправити посилку в Україну?"), ["ua1", "ua2", "ua3"])  # советовали; из списка; прочие
        self.assertEqual(self.ids("Хто знає, чи їздить ще перевізник Тарас з посилками в Україну?"), ["ua3", "ua1", "ua2"])   # имя из запроса
        self.assertEqual(self.ids("Порадьте педіатра в Потсдамі. Крім Зразкової, будь ласка"), ["ped2"])       # «крім …» карточку убирает
        # страна и город в запросе не имя мастера: порядок прежний (советовали; из списка; прочие)
        self.assertEqual(self.ids("Порадьте будь ласка перевізника, нам треба виїхати з Німеччини до України"), ["ua1", "ua2", "ua4"])

    def test_common_capitalized_word_is_not_a_name(self):
        rows = [card("d%d" % n, "Переклад, Документи %d" % n, "docs", "переклад документів", ["переводчик"], rec=n) for n in range(1, 5)]
        cards = self.mh._parse({"items": rows})
        ask = "Потрібен переклад свідоцтва для ДП Документ, порадьте когось"
        self.assertEqual([c["id"] for c in self.mh.pick("docs", ask, cards, "2026-11-26")], ["d4", "d3", "d2"])    # слово есть у всех: это не имя
        cards = self.mh._parse({"items": rows[:2] + [card("d9", "Переклад, Остап", "docs", "переклад документів", ["переводчик"])]})
        ask = "Порадьте, будь ласка, перекладача Остапа, треба переклад свідоцтва"
        self.assertEqual([c["id"] for c in self.mh.pick("docs", ask, cards, "2026-11-26")], ["d9", "d2", "d1"])    # а это имя: одна карточка

    def test_city_named_to_avoid_goes_down(self):
        self.fill()
        self.assertEqual(self.ids("Порадьте, будь ласка, стоматолога"), ["den2", "den1", "den3"])
        self.assertEqual(self.ids("Порадьте, будь ласка, стоматолога, тільки не в Берліні"), ["den1", "den2", "den3"])
        self.assertEqual(self.ids("Порадьте стоматолога? Берлін не підходить"), ["den1", "den2", "den3"])
        # «якщо не в Потсдамі, то в Берліні» — не відмова від Потсдама
        self.assertEqual(self.mh._cities("Порадьте стоматолога, якщо не в Потсдамі, то в Берліні"), (["potsdam", "berlin"], []))

    def test_legal_and_document_topics(self):
        data = {"items": CATALOG["items"] + [
            card("tax1", "Steuerberatung Muster", "legal", "податковий консультант", ["налоговый консультант", "Steuerberater"], rec=2),
            card("buh1", "Бухгалтер Тест", "legal", "бухгалтерія", ["бухгалтер"], rec=5),
            card("ins1", "Versicherung Muster", "legal", "страхування", ["страховка", "Versicherung"], rec=7),
        ]}
        self.fill(data)
        self.assertEqual(self.ids("Порадьте, будь ласка, податкового консультанта"), ["tax1"])     # бухгалтер і страховик — інше
        self.assertEqual(self.ids("Підкажіть хорошого бухгалтера, будь ласка"), ["buh1"])
        # апостиль на свідоцтво: це не переклад, а апостилю в каталозі немає — одне посилання
        ask = "Підкажіть, де зробити апостиль на свідоцтво про народження?"
        self.assertEqual(self.mh.match(ask), "docs")
        self.assertEqual(self.ids(ask), [])
        self.assertEqual(self.ids("Порадьте присяжного, треба перекласти свідоцтво про народження"), ["tr2"])

    def test_reply_is_never_longer_than_600(self):
        long_name = "Дуже довга назва салону краси з усіма послугами одразу для всіх"
        rows = [card("x" * 39 + str(n), long_name + " " + str(n), "beauty", "манікюр, педикюр, нарощування вій, брови, ламінування, епіляція",
                     ["маникюр"], area="Berlin, Potsdam, Brandenburg, Magdeburg, Leipzig") for n in range(5)]
        self.fill({"items": rows})
        body, shown = self.mh.answer("beauty", "Порадьте майстра манікюру")
        self.assertLessEqual(len(body), 600)
        self.assertGreaterEqual(len(shown), 1)
        self.assertEqual(body.count("#id="), len(shown))
        self.assertTrue(body.endswith("#cat=beauty"))
        self.assertIn("…", body)                                       # длинное сокращено, а не обрезано посреди ссылки
        self.fill()
        for ask in ("Порадьте, будь ласка, стоматолога", "Потрібен перекладач", "Хто робить перевезення по Потсдаму?"):
            self.assertLessEqual(len(self.mh.answer(self.mh.match(ask), ask)[0]), 600)

    def test_network_failure_gives_plain_link(self):
        ask = "Порадьте, будь ласка, стоматолога"
        with mock.patch.object(self.mh, "_download", side_effect=OSError("no network")):
            self.assertFalse(self.mh._refresh())
        self.assertEqual(self.mh.answer("health", ask), (self.mh.text_for("health"), []))
        self.fill()                                                   # сайт ответил: карточки есть
        self.assertTrue(self.mh.answer("health", ask)[1])
        with mock.patch.object(self.mh, "_download", side_effect=OSError("no network")):
            self.assertFalse(self.mh._refresh())                      # короткий сбой: прежняя копия ещё годится
        self.assertTrue(self.mh.answer("health", ask)[1])
        self.mh._catalog["at"] = time.time() - self.mh.STALE_SEC - 1  # сайт молчит дольше 45 минут: снова одна ссылка
        self.assertEqual(self.mh.answer("health", ask), (self.mh.text_for("health"), []))

    def test_empty_or_broken_catalog_gives_plain_link(self):
        ask = "Порадьте, будь ласка, стоматолога"
        for raw in ({"built": "2026-11-26", "items": []}, {"oops": 1}, b"<html>502 Bad Gateway</html>", b"", b"[1, 2, 3]"):
            self.assertFalse(self.fill(raw), raw)
            self.assertEqual(self.mh.answer("health", ask), (self.mh.text_for("health"), []), raw)
        self.fill()
        self.assertTrue(self.mh.answer("health", ask)[1])
        self.assertFalse(self.fill({"items": []}))                    # каталог опустел: прежние карточки больше не показываем
        self.assertEqual(self.mh.answer("health", ask), (self.mh.text_for("health"), []))

    def test_no_suitable_cards_or_no_section_gives_plain_link(self):
        self.fill()
        for ask, cat in {
            "Добрый день. Подскажите пожалуйста эндокринолога русскоговорящего": "health",       # такого врача в каталоге нет
            "Добрий день, підкажіть клініку у Потсдамі, де лікар дасть лікарняний?": "health",     # просто «лікар»: раздел есть, темы нет
            "Шукаю помічницю з прибирання будинку, порадьте когось?": "cleaning",                # в разделе только химчистка мебели
            "А какого размера фотография нужна? Подскажите": "events",                           # фотография, а не фотограф
            "Потрібен номер перевізника, який би від’їжджав сьогодні або завтра": "movers",        # куда едет, неясно
            "Доброго дня. Підкажіть будь ласка контакти майстра з татуювання у Потсдамі": "",    # раздел не определён
        }.items():
            self.assertEqual(self.mh.match(ask), cat, ask)
            self.assertEqual(self.mh.answer(cat, ask), (self.mh.text_for(cat), []), ask)
        self.assertEqual(self.mh.answer("", "Порадьте майстра")[0], "🧰 Увесь каталог майстрів і послуг нашого чату тут: https://example.org/m/?s=hint")

    def test_switch_off(self):
        ask = "Порадьте, будь ласка, стоматолога"
        self.assertTrue(self.mh.CARDS)                                # по умолчанию включено
        mh = load(MASTERS_HINT_CARDS="off")
        self.fill(mh=mh)
        self.assertEqual(mh.answer("health", ask), (mh.text_for("health"), []))
        cfg = os.path.join(self.tmp.name, "masters.json")
        for value, expected in ((False, False), ("off", False), (0, False), (True, True), ("on", True)):
            with open(cfg, "w", encoding="utf-8") as fh:
                json.dump({"url": "https://file.example/", "hint": "on", "cards": value}, fh)
            mh = load(MASTERS_CONFIG=cfg, MASTERS_URL="", MASTERS_HINT="")
            self.assertEqual(mh.CARDS, expected, value)
            self.fill(mh=mh)
            self.assertEqual(bool(mh.answer("health", ask)[1]), expected, value)

    def test_phones_and_bad_rows_never_reach_the_chat(self):
        rows = [
            card("ok1", "Стоматолог Перший", "health", "стоматолог, запис +49 176 12345678", ["стоматолог"], area="Potsdam, 0331 5550100"),
            card("ph1", "Стоматолог 0176 1234 5678", "health", "стоматолог", ["стоматолог"], rec=9),
            card("bad id!", "Стоматолог Третій", "health", "стоматолог", ["стоматолог"], rec=9),
            card("x" * 41, "Стоматолог Четвертий", "health", "стоматолог", ["стоматолог"], rec=9),
            card("noname", "", "health", "стоматолог", ["стоматолог"], rec=9),
            "не карточка", None, {"id": "half"},
            {"id": "min1", "name": "Стоматолог Мінімум", "cat": "health", "kw": "не список", "mode": None, "rec": "багато", "last": None},
        ]
        self.assertTrue(self.fill({"items": rows}))
        body, shown = self.mh.answer("health", "Порадьте, будь ласка, стоматолога")
        self.assertEqual(sorted(shown), ["min1", "ok1"])
        self.assertIn("• Стоматолог Перший\n", body)                  # специализация и район с телефоном отброшены целиком
        self.assertNotRegex(body.replace("https://example.org/m/?s=hint#", ""), r"\d{4}")

    def test_links_and_data_url(self):
        self.assertEqual(self.mh.link("hint", card="den1"), "https://example.org/m/?s=hint#id=den1")
        self.assertEqual(self.mh.link("hint", "health"), "https://example.org/m/?s=hint#cat=health")
        self.assertEqual(self.mh._data_url(), "https://example.org/m/data.json")
        mh = load(MASTERS_URL="https://example.org/m?x=1#top")
        self.assertEqual(mh._data_url(), "https://example.org/m/data.json")
        mh = load(MASTERS_URL="file:///etc/passwd")
        self.assertFalse(mh._refresh())                               # только http(s)

    def test_requests_about_ukraine_carriers_and_offers(self):
        for text, cat in {
            "Доброго дня . Порадьте будь ласка перевізника 🙏🏻 нам треба виїхати з Німеччини до України": "ukraine",
            "Добрый день. Подскажите перевозчика, который едет в Украину в среду или четверг.": "ukraine",
            "Подскажите перевозчика посылок нормального пожалуйста": "ukraine",
            "Добрый день! Подскажите контакты проверенного перевозчика Потсдам/ Берлин - Киев.": "ukraine",
            "Привет. У кого есть контакты человека чтоб вещи перевезти? Нужен для переезда": "movers",
        }.items():
            self.assertEqual(self.mh.match(text), cat, text)
        for text in (
            "Привіт! Потрібні модельки на манікюр (однотонне покриття). Безкоштовно, пишіть у приват",   # мастер ищет клиентов
            "Доброго дня! Можливо комусь потрібен вчитель англійської для школяра? Пишіть.",
            "Привіт! Є хтось, хто хотів би підтягнути мовлення дитини з логопедом онлайн?",
        ):
            self.assertIsNone(self.mh.match(text), text)

    def test_maybe_reply_sends_cards_as_plain_text(self):
        self.fill()
        ask = "Порадьте, будь ласка, стоматолога в Потсдамі"
        msg = FakeMessage(ask)
        self.assertTrue(self.mh.maybe_reply(msg, self.user, ask))
        text, kwargs = msg.sent[0]
        self.assertIn("https://example.org/m/?s=hint#id=den1", text)
        self.assertTrue(text.endswith("https://example.org/m/?s=hint#cat=health"))
        self.assertEqual(kwargs, {"parse_mode": None, "disable_web_page_preview": True})
        generic = FakeMessage("Доброго дня. Підкажіть будь ласка контакти майстра з татуювання у Потсдамі", chat_id=-200)
        self.assertTrue(self.mh.maybe_reply(generic, SimpleNamespace(id=8, is_bot=False), generic.text))
        self.assertEqual(generic.sent[0][0], self.mh.text_for(""))    # раздел не определён: общая ссылка, как раньше


class BackgroundCatalogTest(unittest.TestCase):
    """Каталог грузит фоновый поток: обработчик сообщений не ждёт сеть."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.mh = load(bg=True, MASTERS_HINT_STATE=os.path.join(self.tmp.name, "hint.json"))
        self.gate = threading.Event()
        self.user = SimpleNamespace(id=7, is_bot=False)
        self.ask = "Порадьте, будь ласка, стоматолога в Потсдамі"

    def tearDown(self):
        self.gate.set()
        self.mh._stop.set()
        if self.mh._loader is not None:
            self.mh._loader.join(5)
            self.assertFalse(self.mh._loader.is_alive())
        self.tmp.cleanup()

    def test_slow_site_does_not_hold_the_handler(self):
        calls = []

        def hang():
            calls.append(1)
            self.gate.wait(30)                                        # сайт «висит»
            raise OSError("timed out")

        self.mh._download = hang
        self.mh.COLD_WAIT = 0.3
        msg = FakeMessage(self.ask)
        started = time.monotonic()
        self.assertTrue(self.mh.maybe_reply(msg, self.user, self.ask))
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(msg.sent[0][0], self.mh.text_for("health"))  # ответили как раньше, одной ссылкой
        self.assertEqual(calls, [1])
        self.mh.warm()                                                # второй поток не заводится
        self.assertEqual(sum(1 for t in threading.enumerate() if t.name == "masters-catalog"), 1)

    def test_background_load_fills_the_cache(self):
        self.mh._download = lambda: json.dumps(CATALOG).encode("utf-8")
        msg = FakeMessage(self.ask)
        self.assertTrue(self.mh.maybe_reply(msg, self.user, self.ask))  # первая подсказка после старта: дождались загрузки
        self.assertIn("#id=den1", msg.sent[0][0])
        self.assertEqual(self.mh._loader.name, "masters-catalog")
        self.assertTrue(self.mh._loader.daemon)

    def test_no_background_thread_when_switched_off(self):
        for env in ({"MASTERS_HINT_CARDS": "off"}, {"MASTERS_HINT": "off"}, {"MASTERS_URL": ""}):
            mh = load(bg=True, MASTERS_HINT_STATE=os.path.join(self.tmp.name, "off.json"), **env)
            mh.warm()
            self.assertIsNone(mh._loader, env)
        self.mh = load(bg=True, MASTERS_HINT_STATE=os.path.join(self.tmp.name, "hint.json"))


if __name__ == "__main__":
    unittest.main()
