"""Підказка в чаті: коли хтось шукає майстра чи послугу, бот коротко відповідає посиланням
на каталог «Майстри Потсдама» (окремий сайт, MASTERS_URL).

26.11.2026, прохання Артема: «щоб він автоматично казав, що весь каталог майстрів тут.
Коротким повідомленням українською». Друга черга того ж дня: коли розділ зрозумілий, бот називає
до трьох конкретних майстрів із каталогу, які підходять під слово прохання (назва, спеціалізація, район
і посилання на картку; телефонів у чат не пишемо).

MASTERS_HINT = off | log | on   (типово off; log — нічого не пише в чат, лише лічить у журналі)
Обмеження, щоб не засмічувати чат: одна підказка людині на MASTERS_HINT_USER_DAYS днів,
не частіше ніж раз на MASTERS_HINT_GAP_MIN хвилин у чаті, не більше MASTERS_HINT_MAX_PER_DAY на добу.
Стан — маленький JSON у /app/config, без текстів повідомлень.

Картки беремо з <MASTERS_URL>/data.json. Їх перечитує фоновий потік раз на 30 хвилин (запит до 5 секунд),
обробник повідомлень сам у мережу не ходить. Каталогу немає (збій, порожня відповідь) або в masters.json
стоїть "cards": false — відповідь, як і раніше, одним посиланням на розділ.
"""
import collections
import html
import json
import logging
import os
import re
import threading
import time
import urllib.request
import zlib
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

# Налаштування: змінна оточення, а якщо її немає — файл /app/config/masters.json
# ({"url": "...", "hint": "on", "cards": true, "user_days": 7, "gap_min": 20, "max_per_day": 10}). Файл лежить у томі з даними,
# тож увімкнути чи вимкнути можна без перестворення контейнера: поправити файл і перезапустити бота.
CONFIG_PATH = os.environ.get("MASTERS_CONFIG", "/app/config/masters.json")


def _file_cfg() -> dict:
    try:
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


_CFG = _file_cfg()


def _setting(env_name: str, key: str, default: str) -> str:
    value = os.environ.get(env_name)
    if value is None or value == "":
        value = _CFG.get(key, default)
    return str(value).strip()


MASTERS_URL = _setting("MASTERS_URL", "url", "")
MODE = _setting("MASTERS_HINT", "hint", "off").lower()          # off | log | on
USER_DAYS = float(_setting("MASTERS_HINT_USER_DAYS", "user_days", "7"))
GAP_MIN = float(_setting("MASTERS_HINT_GAP_MIN", "gap_min", "20"))
MAX_PER_DAY = int(float(_setting("MASTERS_HINT_MAX_PER_DAY", "max_per_day", "10")))
STATE_PATH = os.environ.get("MASTERS_HINT_STATE", "/app/config/masters_hint.json")
# Вимикач карток: "cards": false у masters.json повертає відповідь одним посиланням.
CARDS = _setting("MASTERS_HINT_CARDS", "cards", "on").lower() not in ("0", "false", "off", "no")

# 27.11.2026: сайт каталогу як Mini App. "webapp": "on" у masters.json — кнопка «Меню» і кнопки в особистих чатах відкривають
# сайт всередині Telegram. "app_link" — пряме посилання Mini App з BotFather (https://t.me/Potsdambot/<коротке_ім'я>):
# у групах web_app-кнопок немає, тож там посилання ведуть на нього (з startapp=cat_<розділ> чи id_<картка>), доки порожнє — як раніше.
WEBAPP = _setting("MASTERS_WEBAPP", "webapp", "off").lower() in ("on", "1", "true", "yes")
APP_LINK = _setting("MASTERS_APP_LINK", "app_link", "")

CACHE_SEC = 30 * 60                # як часто фоновий потік перечитує каталог
STALE_SEC = CACHE_SEC + 15 * 60     # старішу копію не показуємо: тоді відповідь одним посиланням
RETRY_SEC = 5 * 60                  # після збою пробуємо частіше
FETCH_TIMEOUT = 5                   # секунд на запит
COLD_WAIT = 3.0                     # стільки чекаємо найперше завантаження після старту бота, і лише коли підказка вже йде в чат
MAX_BYTES = 4 * 1024 * 1024
MAX_CARDS = 3
MAX_LEN = 600                       # довжина відповіді в чаті
FRESH_FROM = "2024"                 # кого в чаті востаннє згадували раніше, ті йдуть після свіжих

_lock = threading.Lock()

# Прохання, а не пропозиція: «шукаю», «порадьте», «хто може», «потрібен» …
_ASK_RE = re.compile(
    r"(підкаж|подскаж|порад|посовет|порекоменд|рекоменд|хто знає|кто знает|хто може|кто может|хто робить|кто делает"
    r"|де знайти|где найти|де можна|где можно|чи є |есть ли|є хтось|есть кто|шукаю|шукаємо|ищу|ищем|нужен|нужна|нужны"
    r"|потрібен|потрібна|потрібні|треба|wer kennt|empfehl|kennt jemand|suche )",
    re.IGNORECASE,
)
_ASK_START_RE = re.compile(
    r"\s*(добрий день|доброго дня|добрый день|привіт|привет|вітаю|здравствуйте|доброго ранку|добрый вечер|добрий вечір)?[\s,.!]*"
    r"(шукаю|шукаємо|ищу|ищем|нужен|нужна|нужны|потрібен|потрібна|потрібні|порадьте|посоветуйте|подскажите|підкажіть|порекомендуйте)",
    re.IGNORECASE,
)
# \b важливе: «Потсдам» містить «сдам».
_OFFER_RE = re.compile(
    r"\b(пропоную|пропонуємо|предлагаю|предлагаем|продам|продаю|продається|сдам|здам|здаю|сдаю|віддам|отдам|вакансі|ваканси"
    r"|требуются|требуется|потрібні працівник|потрібен працівник|ищем сотрудник|шукаємо працівник|запрошуємо|приглашаем"
    r"|надаю послуг|оказываю услуг|виконую|выполняю|запис відкрит|запись открыт|шукаю роботу|ищу работу|шукаю підробіток"
    r"|ищу подработку|шукаю квартиру|ищу квартиру|шукаю житло|ищу жилье|ищу жильё|шукаю кімнату|ищу комнату)",
    re.IGNORECASE,
)

# Не пошук майстра: наймають працівника, шукають приміщення, або це порада у відповідь, або оголошення з телефоном.
_SKIP_RE = re.compile(
    r"(зацікавлен|заинтересован|резюме|зарплат|мініджоб|минижоб|minijob|на роботу|на работу|в команду|у команду|підробіт|подработ"
    r"|оплата\s+(щодня|щоденно|каждый день|ежедневно)|щоденн\w+\s+(о)?плат|ежедневн\w+\s+оплат|графік\w*\s+робот|график\w*\s+работ"
    r"|гнучк\w+\s+графік|гибк\w+\s+график|приміщенн|помещени|оренд[аиу]\s+кабінет|аренд[аыу]\s+кабинет"
    # майстер сам шукає клієнтів: «потрібні модельки на манікюр», «комусь потрібен вчитель?», «хто хотів би …»
    r"|(потрібн|нужн)\w+\s+модел|модел\w*\s+на\s|кому(сь|-то|-нибудь)?\s+(потрібен|потрібна|потрібні|нужен|нужна|нужны)\b"
    r"|хто\s+хотів\s+би|кто\s+хотел\s+бы"
    r"|\b(купити|придбати|купить|приобрести)\b"
    r"|\b(раджу|рекомендую|советую|зверніться|звертайтесь|обратитесь|обращайтесь|спробуйте|попробуйте)\b)",
    re.IGNORECASE,
)
_ANSWER_START_RE = re.compile(r"\s*(якщо|если|наприклад|например|так,|да,|ні,|нет,|@)", re.IGNORECASE)
_PHONE_RE = re.compile(r"(?:\+|\b0)\d[\d\s()\-]{8,}\d")

W = r"(?<![0-9A-Za-zА-Яа-яЁёІіЇїЄєҐґ])"     # початок слова
# (розділ каталогу, основи слів). Порядок важливий: точніші розділи вище.
_CATS = [
    ("tech", r"ремонт\w*\s+(телефон|смартфон|ноутбук|комп|планшет|айфон|iphone|техніки|техники)|майстер\w*\s+по\s+комп|мастер\w*\s+по\s+комп"
             r"|комп.?ютерн\w+\s+майст|компьютерн\w+\s+мастер|handyreparatur|принтер|макбук|macbook|ремонтн\w+\s+сервіс|ремонтн\w+\s+сервис"),
    ("ukraine", r"посилк|посылк|посилок|посылок|передач\w*\s+(в|з|до|из|на)\s+укра|(в|до|з|из)\s+україн\w*\s+(їде|їдуть|рейс|бус|перевіз)"
                r"|перевізник\w*\s+(в|до|на)\s+укра"
                r"|перевозчик\w*\s+(в|на)\s+укра|рейс\w*\s+(в|до|на|з|из)\s+(укра|київ|киев|львів|львов|одес|харк|дніпр|днепр)|нов\w+\s+пошт"),
    ("auto", r"автосервіс|автосервис|шиномонтаж|автомеханік|автомеханик|автоелектрик|автоэлектрик|автомайстер|автомастер|автослюсар|автослесар"
             r"|\bсто\b|werkstatt|тонув|тониров|евакуатор|эвакуатор|детейлінг|детейлинг|хімчистк\w+\s+(авто|салон)|химчистк\w+\s+(авто|салон)"),
    ("beauty", r"манікюр|маникюр|педикюр|перукар|парикмахер|стрижк|фарбуванн\w+\s+волос|окрашиван|бров|ресниц|нарощуванн\w+\s+ві|війки|косметолог"
               r"|епіляц|эпиляц|шугарин|депіляц|депиляц|барбер|зачіск|причёск|прическ|візаж|визаж|макіяж|макияж|нігт|ногт|ламінуванн|ламинирован|friseur|maniküre|kosmetik"),
    # перекладач вище за лікаря: «порадьте перекладача, щоб піти до окуліста» — це переклади
    ("docs", r"перекладач|переводчик|переклад\w*\s+(документ|диплом|свідоцтв|довідк|паспорт)|перевод\w*\s+(документ|диплом|свидетельств|справк|паспорт)"
             r"|присяжн|апостил|нотаріус|нотариус|dolmetscher|übersetzer|übersetzung|супровід\w*\s+(до|в|у)|сопровожден\w*\s+(к|в)"),
    ("health", r"лікар|врач|стоматолог|зубн\w+\s+(лікар|врач)|гінеколог|гинеколог|педіатр|педиатр|терапевт|дерматолог|психолог|психотерап|логопед"
               r"|ортопед|окуліст|окулист|офтальмолог|\bлор\w{0,2}\b|масаж|массаж|остеопат|фізіотерап|физиотерап|ендокринолог|эндокринолог"
               r"|невролог|невропатолог|уролог|кардіолог|кардиолог|хірург|хирург|zahnarzt|hausarzt|frauenarzt|kinderarzt"),
    ("movers", r"перевез|перевіз|перевоз|вантаж|грузчик|грузоперевоз|вантажник|переїзд|переезд|трансфер|\bбус\w{0,3}\b|вивез|вывез|вивіз|вывоз"
               r"|umzug|transporter"),
    ("cleaning", r"прибиранн|прибиральниц|уборк|уборщиц|клінінг|клининг|хімчистк|химчистк|reinigung|putzfrau"),
    ("home", r"сантехнік|сантехник|електрик|электрик|плиточник|плитк|маляр|шпаклюв|шпаклев|штукатур|ремонт\w*\s+(квартир|кімнат|комнат|ванн|кухн|дах|крыш)"
             r"|зібрати\s+мебл|збирання\s+мебл|сборк\w+\s+мебел|собрать\s+мебел|майстер\w*\s+на\s+годину|мастер\w*\s+на\s+час|муж\w*\s+на\s+час"
             r"|чоловік\w*\s+на\s+годину|handwerker|покласти\s+(плитк|ламінат)|положить\s+(плитк|ламинат)|поклеїти\s+шпалер|поклеить\s+обо"
             r"|повісити|повесить|підключити\s+(пральн|посудомий|плит)|подключить\s+(стирал|посудомо|плит)|ремонтник|будівельник|строител"),
    ("legal", r"адвокат|юрист|страхов\w+\s+(агент|брокер|консультант)|страховик|податков\w+\s+консультант|налогов\w+\s+консультант|steuerberater"
              r"|бухгалтер|anwalt|rechtsanwalt"),
    ("pets", r"ветеринар|грумінг|груминг|грумер|перетримк|передержк|tierarzt|вигул\w*\s+собак|выгул\w*\s+собак"),
    ("food", r"торт|кондитер|випічк\w*\s+на\s+замовл|выпечк\w*\s+на\s+заказ|кейтеринг|капкейк|вареник\w*\s+на\s+замовл|пельмен\w*\s+на\s+заказ"),
    ("events", r"фотограф|відеограф|видеограф|ведуч|ведущ|аніматор|аниматор|тамада|декор\w*\s+(свят|праздник)|фотосесі|фотосесси"),
    ("kids", r"нян[яюіиь]|няню|бебісіт|бебисит|babysitter|tagesmutter|дитяч\w+\s+(гурток|центр|студі)|детск\w+\s+(кружок|центр|студи)"),
    ("edu", r"репетитор|вчител|учител\w+\s+(німец|немец|англ|матем|музик|музык)|викладач|преподавател|курс\w*\s+(німец|немец|англ)|автошкол"
            r"|fahrschule|nachhilfe|урок\w*\s+(німец|немец|англ|матем|фортеп|гітар|гитар|вокал)"),
    ("sport", r"тренер|фітнес\s*тренер|фитнес\s*тренер|йог[аиуі]\b|пілатес|пилатес|танц\w+\s+(для|студі|студи|школ)"),
    ("", r"майстер|майстра|майстрів|мастер[ауов]?\b|спеціаліст|специалист"),
]
_CAT_RE = [(cat, re.compile(W + "(?:" + rx + ")", re.IGNORECASE)) for cat, rx in _CATS]

CAT_NAMES = {
    "beauty": "Краса", "health": "Здоров’я", "movers": "Перевезення", "ukraine": "Україна ↔ Німеччина", "home": "Ремонт і дім",
    "cleaning": "Прибирання", "auto": "Авто", "docs": "Документи й переклади", "legal": "Юристи, страхування", "kids": "Діти",
    "edu": "Навчання", "food": "Їжа на замовлення", "events": "Свята і фото", "pets": "Тварини", "tech": "Техніка та IT",
    "sport": "Спорт і хобі",
}

# «Перевізник» разом з Україною: це рейси в Україну, а не переїзди містом.
_UA_ROUTE_RE = re.compile(
    W + r"(?:україн|украин|київ|києв|киев|львів|львов|одес|харків|харков|харьков|дніпр|днепр|житомир|вінниц|винниц|чернівц|черновц"
        r"|тернопіл|тернопол|полтав|запоріж|запорож|черкас|черніг|черниг|ужгород|хмельниц|луцьк|луцк|миколаїв|миколаєв|николаев|херсон)",
    re.IGNORECASE,
)

# ── Які картки каталогу підходять під прохання ────────────────────────────────────────────────────────────────
# Тема всередині розділу: основи слів у проханні і основи слів у картці (назва, спеціалізація sub, слова пошуку kw).
# Основи українські, російські й німецькі разом: у картках спеціалізація українською, слова пошуку російською та німецькою.
# wide — загальна тема («перекладач», «репетитор»): вона сама відбирає картки, лише коли точнішої теми в проханні немає.
Topic = collections.namedtuple("Topic", "ask card wide")


def _topic(ask: str, card: str = "", wide: bool = False) -> Topic:
    return Topic(re.compile(W + "(?:" + ask + ")", re.IGNORECASE),
                 re.compile(W + "(?:" + (card or ask) + ")", re.IGNORECASE), wide)


_TOPICS = {
    "beauty": [
        _topic(r"манікюр|маникюр|нігт|ногт|гель.?лак|maniküre", r"манікюр|маникюр|нігт|ногт|maniküre|nagel|nägel"),
        _topic(r"педикюр|подолог", r"педикюр|подолог|pediküre|fußpflege"),
        _topic(r"вій\b|вії|війк|війок|ресниц", r"вій\b|вії|війк|ресниц|wimper"),
        _topic(r"бров", r"бров|augenbrau"),
        _topic(r"перукар|парикмахер|стрижк|стрижок|підстриг|постриг|подстри|зачіск|зачісок|причёск|прическ|причесок"
               r"|фарбуванн\w*\s+волос|окрашиван|friseur",
               r"перукар|парикмахер|стрижк|зачіск|причёск|прическ|фарбуванн\w*\s+волос|окрашиван|колорист|friseur|барбер|barber"),
        _topic(r"барбер|чоловіч\w+\s+(?:стриж|зачіс)|мужск\w+\s+(?:стриж|прич)|бород", r"барбер|barber|чоловіч|мужск|бород|herren"),
        _topic(r"косметолог|kosmetik", r"косметолог"),          # Kosmetikstudio пишуть і перукарні
        _topic(r"епіляц|эпиляц|шугарин|депіляц|депиляц", r"епіляц|эпиляц|шугарин|депіляц|депиляц|sugaring|waxing|haarentfernung"),
        _topic(r"візаж|визаж|макіяж|макияж", r"візаж|визаж|макіяж|макияж|мейкап|visagist"),
        _topic(r"ламінуванн|ламинирован"),
    ],
    "health": [
        _topic(r"стоматолог|зубн|zahnarzt", r"стоматолог|зубн|zahn[aä]rzt"),      # не Zahnspange: це ортодонт
        _topic(r"гінеколог|гинеколог|frauenarzt", r"гінеколог|гинеколог|frauen[aä]rzt|gynäkolog"),
        _topic(r"педіатр|педиатр|kinderarzt", r"педіатр|педиатр|kinder[aä]rzt|детск\w+\s+врач|дитяч\w+\s+лікар"),
        _topic(r"терапевт|сімейн\w+\s+лікар|семейн\w+\s+врач|hausarzt", r"терапевт|сімейн|семейн|haus[aä]rzt|allgemeinmedizin"),
        _topic(r"дерматолог", r"дерматолог|haut[aä]rzt|dermatolog"),
        _topic(r"психолог|психотерап", r"психолог|психотерап|psycholog|psychotherap"),
        _topic(r"логопед", r"логопед|logopäd|sprachtherap"),
        _topic(r"ортопед", r"ортопед|orthopäd"),
        _topic(r"ортодонт|брекет", r"ортодонт|брекет|kieferorthop"),
        _topic(r"окуліст|окулист|офтальмолог", r"окуліст|окулист|офтальмолог|augen[aä]rzt"),
        _topic(r"лор\w{0,2}\b|hno\b", r"лор\b|отоларинголог|hno"),
        _topic(r"масаж|массаж", r"масаж|массаж|massage"),
        _topic(r"остеопат", r"остеопат|osteopath"),
        _topic(r"фізіотерап|физиотерап", r"фізіотерап|физиотерап|physiotherap"),
        _topic(r"ендокринолог|эндокринолог|діабет|диабет", r"ендокринолог|эндокринолог|діабет|диабет|endokrinolog|diabet"),
        _topic(r"невролог|невропатолог", r"невролог|невропатолог|neurolog"),
        _topic(r"уролог", r"уролог|urolog"),
        _topic(r"кардіолог|кардиолог", r"кардіолог|кардиолог|kardiolog"),
        _topic(r"хірург|хирург", r"хірург|хирург|chirurg"),
    ],
    "docs": [
        _topic(r"перекладач|переводчик|переклад|перевод|dolmetscher|übersetz", r"перекладач|переводчик|переклад|перевод|dolmetsch|übersetz", True),
        # переклад документів: саме слово «свідоцтво» чи «диплом» ще не означає, що шукають перекладача (може, апостиль)
        _topic(r"присяжн|письмов\w*\s+переклад|письмен\w*\s+перевод"
               r"|(?:переклад|перевод|перекласти|перевести)\w*\s+(?:\w+\s+){0,2}(?:документ|диплом|свідоцтв|свидетельств|довідк|справк|паспорт|атестат|аттестат)",
               r"присяжн|завірен|заверен|письмов|письмен"      # без Beglaubigung: так пише й нотаріус
               r"|(?:переклад|перевод)\w*\s+(?:документ|диплом|свідоцтв|свидетельств|атестат|аттестат)"),
        # усний переклад і супровід: до лікаря, в установу, на конкретний день і годину
        _topic(r"усн|устн|супров|сопровожд|лікар|врач|прийом|при[её]м|термін|термин|termin|збор|собрани|jobcenter|джобцентр|банк"
               r"|standesamt|б[юу]ргерамт|амт\b|бех[ьо]рд|behörde|лікарн|больниц|клінік|клиник|окуліст|окулист|офтальмолог|зубн|гімназ|гимназ|школ"
               r"|завтра|сьогодні|сегодня|\d{1,2}[.:]\d{2}",
               r"усн|устн|супров|сопровожд|dolmetsch"),
        _topic(r"нотаріус|нотариус|notar|довірен|доверенност", r"нотаріус|нотариус|notar|довірен|доверенност"),
        _topic(r"апостил|apostill|легаліз|легализ"),
    ],
    "movers": [
        _topic(r"переїзд|переезд|вантаж|груз|мебл|мебел|umzug|речі|речей|вещи|вещей|холодильник|диван|шаф[аиу]|шкаф|пральн|стирал"
               r"|бус\w{0,3}\b|спринтер|transporter",
               r"переїзд|переезд|вантаж|груз|мебл|мебел|umzug|möbel"),
        _topic(r"трансфер|аеропорт|аэропорт|пасажир|пассажир|таксі|такси|легков", r"трансфер|аеропорт|аэропорт|пасажир|пассажир|таксі|такси|flughafen"),
        # «перевізник» без уточнень сюди не входить: так у чаті зазвичай питають про рейси в Україну
        _topic(r"перевезенн|перевезт|перевест|перевозк|перевозит|перевозят|вивез|вывез|вивіз|вывоз",
               r"перевез|перевіз|перевоз|вантаж|груз|transport|umzug", True),
    ],
    "ukraine": [
        _topic(r"посилк|посилок|посылк|посылок|передач|передат|переслат|відправит|отправит|сумк|речі|вещи|paket",
               r"посилк|посилок|посылк|посылок|передач|речі|paket"),
        _topic(r"пасажир|пассажир|автобус|їхати|поїхати|виїхати|доїхати|їде\b|їдуть|едет|едут|ехать|уехать|выехать|доехать"
               r"|місц[еья]\b|мест[оа]\b|рейс|квит[ок]|билет",
               r"пасажир|пассажир|автобус|bus\b|рейс|людей"),
        _topic(r"нов\w+\s+пошт"),
        _topic(r"перевез|перевіз|перевоз", r"посилк|посилок|посылк|посылок|передач|речі|paket|пасажир|пассажир|автобус|bus\b|рейс|людей", True),
    ],
    "cleaning": [
        _topic(r"прибиранн|прибиральниц|уборк|уборщиц|клінінг|клининг|putzfrau", r"прибиранн|прибиральниц|уборк|уборщиц|клінінг|клининг|putz"),
        _topic(r"хімчистк|химчистк|reinigung", r"хімчистк|химчистк|чищенн|polsterreinigung|teppichreinigung"),
    ],
    "home": [
        _topic(r"електрик|электрик|розетк|проводк|(?:підключ|подключ)\w*\s+(?:плит|духов|вароч|варильн)", r"електрик|электрик|elektrik"),
        _topic(r"сантехнік|сантехник|(?:підключ|подключ)\w*\s+(?:пральн|посудом|стирал)", r"сантехнік|сантехник|klempner|sanitär"),
        _topic(r"плиточник|плитк", r"плиточник|плитк|fliesen"),
        _topic(r"маляр|шпаклюв|шпаклев|шпакл[её]в|штукатур|шпалер|обо[иїе]\b",
               r"маляр|шпаклюв|шпаклев|шпакл[её]в|штукатур|шпалер|обои|maler|фарбуванн\w*\s+стін|покраск"),
        _topic(r"мебл|мебел", r"мебл|мебел|möbel"),
        _topic(r"на\s+годину|на\s+час\b|handwerker|повісити|повесить", r"на\s+годину|на\s+час\b|handwerker"),
        _topic(r"ремонт\w*\s+(?:дах|крыш)", r"дах|крыш|покрівл|кровл"),
        _topic(r"ламінат|ламинат"),
        _topic(r"ремонт\w*\s+(?:квартир|кімнат|комнат|ванн|кухн)|ремонтник|будівельник|строител", r"ремонт|будівельн|строит|кладк", True),
    ],
    "auto": [
        _topic(r"автосервіс|автосервис|сто\b|автомайстер|автомастер|автомеханік|автомеханик|автослюсар|автослесар|werkstatt|майстерн|мастерск",
               r"автосервіс|автосервис|сто\b|автомайстер|автомастер|автомеханік|автомеханик|ремонт\w*\s+авто|werkstatt"),
        _topic(r"шиномонтаж|перевзу|переобу", r"шиномонтаж|reifenservice|reifenwechsel|переобу"),
        _topic(r"автоелектрик|автоэлектрик", r"автоелектрик|автоэлектрик|autoelektrik"),
        _topic(r"евакуатор|эвакуатор", r"евакуатор|эвакуатор|лафет|abschlepp"),
        _topic(r"тонув|тониров"),
        _topic(r"детейлінг|детейлинг|хімчистк|химчистк", r"детейлінг|детейлинг|хімчистк|химчистк|autoaufbereitung"),
    ],
    "legal": [
        _topic(r"адвокат|anwalt|rechtsanw"),
        _topic(r"юрист", r"юрист|адвокат"),
        _topic(r"міграц|миграц", r"міграц|миграц|migration"),
        _topic(r"трудов", r"трудов|arbeitsrecht"),
        _topic(r"страхов", r"страхуванн|страховк|страховани|страховик|versicherung"),      # «страховой спор» — це вже адвокат
        _topic(r"бухгалтер", r"бухгалтер|buchhalt"),
        _topic(r"податков|налогов|steuer", r"податк|налог|steuer|деклараці|деклараци"),
    ],
    "pets": [
        _topic(r"ветеринар|tierarzt", r"ветеринар|ветлікар|ветврач|ветклін|ветклин|tier[aä]rzt|tierklinik"),
        _topic(r"грумінг|груминг|грумер", r"грумінг|груминг|грумер|hundefriseur"),
        _topic(r"перетримк|передержк"),
        _topic(r"вигул|выгул"),
    ],
    "food": [
        _topic(r"торт|кондитер|випічк|выпечк|капкейк|тістечк|пирожн|десерт", r"торт|кондитер|випічк|выпечк|капкейк|тістечк|пирожн|десерт|konditor|kuchen"),
        _topic(r"кондитерськ|кондитерск|кав.?ярн|кофейн|кафе", r"кондитерськ|кондитерск|konditorei|кав.?ярн|кофейн|кафе"),
        _topic(r"вареник|пельмен"),
        _topic(r"кейтеринг|catering"),
    ],
    "events": [
        # «фотографія на паспорт» — не пошук фотографа
        _topic(r"фотограф(?![іи][яюїєией])|фотосес", r"фотограф|фотосес|fotograf"),
        _topic(r"відеограф|видеограф", r"відеограф|видеограф|videograf"),
        _topic(r"ведуч|ведущ|тамада", r"ведуч|ведущ|тамада|moderat"),
        _topic(r"аніматор|аниматор", r"аніматор|аниматор|animat"),
        _topic(r"декор", r"декор|оформленн|оформлени|dekor"),
    ],
    "kids": [
        _topic(r"нян|бебісіт|бебисит|babysitter|tagesmutter"),
        _topic(r"гурток|гуртк|кружок|кружк|студі|студи|центр", r"гурток|гуртк|кружок|кружк|секці|секци|студі|студи|центр|розвит|развит"),
    ],
    "edu": [
        _topic(r"німец|немец|deutsch"),
        _topic(r"англ|englisch"),
        _topic(r"матем|mathe"),
        _topic(r"музик|музык|фортеп|гітар|гитар|вокал", r"музик|музык|фортеп|піаніно|пианино|гітар|гитар|вокал|klavier|gitarr|musik"),
        _topic(r"автошкол|fahrschule"),
        _topic(r"репетитор|вчител|учител|викладач|преподавател|nachhilfe", r"репетитор|вчител|учител|викладач|преподавател|nachhilfe|lehrer", True),
        _topic(r"курс", r"курс|kurs|школ|schule", True),
    ],
    "sport": [
        # «тренер» без виду спорту теми не дає: у каталозі тренери з різного
        _topic(r"фітнес|фитнес|fitness", r"фітнес|фитнес|fitness|ems\b|trx\b"),
        _topic(r"йог", r"йог|yoga"),
        _topic(r"пілатес|пилатес", r"пілатес|пилатес|pilates"),
        _topic(r"танц|танец|tanz", r"танц|танец|tanz|балет|хіп.?хоп|хип.?хоп"),
    ],
    "tech": [
        _topic(r"ремонт\w*\s+(?:телефон|смартфон|айфон|iphone)|handyreparatur", r"телефон|смартфон|айфон|iphone|handy"),
        _topic(r"ноутбук|комп.?ютер|компьютер|комп[аиу]?\b|макбук|macbook|laptop", r"ноутбук|комп.?ютер|компьютер|макбук|macbook|laptop|notebook"),
        _topic(r"планшет", r"планшет|tablet"),
        _topic(r"принтер", r"принтер|drucker"),
        _topic(r"ремонт\w*\s+(?:техніки|техники)|ремонтн\w+\s+серв[іи]с", r"ремонт|reparatur|repair", True),
    ],
}


def _word(rx: str) -> "re.Pattern":
    return re.compile(W + "(?:" + rx + ")", re.IGNORECASE)


# Уточнення з прохання. Вони не відбирають картки, а лише ставлять відповідні вище.
# Місто: основи в проханні і рядок, який шукаємо в районі картки (area).
_CITIES = [(_word(ask), needle) for ask, needle in (
    (r"потсдам|постдам|потздам|potsdam", "potsdam"), (r"берлін|берлин|berlin", "berlin"),
    (r"бранденбург|brandenburg", "brandenburg"), (r"бабельсберг|babelsberg", "babelsberg"),
    (r"тельтов|teltow", "teltow"), (r"вердер|werder", "werder"), (r"міхендорф|михендорф|michendorf", "michendorf"),
    (r"кляйнмахнов|клайнмахнов|kleinmachnow", "kleinmachnow"), (r"штансдорф|stahnsdorf", "stahnsdorf"),
)]
# Сильне побажання «для дитини»: такі картки йдуть першими, навіть давні. Шукаємо в назві, спеціалізації, словах пошуку, описі.
_KIDS = (_word(r"дитяч|дитин|дітей|дітям|діти\b|детск|детей|детям|дети\b|реб[её]н|малюк|малыш|школяр|школьник|дошкільн|дошкольн"),
         _word(r"дитяч|дитин|дітей|дітям|діти\b|детск|детей|детям|дети\b|kinder|малюк|малыш|школяр|школьник"))
# М'які побажання: мова, виїзд додому, онлайн.
_LANG = (_word(r"російськ|русск|рос\b|україномовн|українськомовн|українською|украиноязычн|укр\b"),
         _word(r"російськомовн|російською|русскоязычн|русскоговорящ|україномовн|українськомовн|українською|украиноязычн|двома\s+мовами"))
_VISIT_RE = _word(r"на\s+дому|на\s+дом\b|додому|виїзд|выезд")      # картки з mode: visit
_ONLINE_RE = _word(r"онлайн|online|дистанц")                        # картки з mode: online
_BUT_RE = re.compile(r"(?:крім|окрім|кроме)\s+([^\W\d_]{5,})", re.IGNORECASE)      # «крім Манукової»: цю картку не радимо
# Місто назвали, щоб його відкинути: «тільки не в Берліні», «Берлін не підходить» (але «якщо не в Потсдамі, то …» — не відмова).
_NOT_BEFORE_RE = re.compile(r"(?<!якщо )(?<!если )\b(?:не|крім|окрім|кроме)\s+(?:в|у|з|із|из)?\s*$", re.IGNORECASE)
_NOT_AFTER_RE = re.compile(r"[^\W\d_]*\s+не\s+(?:підход|подход|підійд|подойд|потрібн|нужн|треба|надо|хоч)", re.IGNORECASE)
_NOT_A_NAME = _word(r"studio|salon|praxis|zentrum|center|centre|gmbh|schule|school|academy|service|klinik|[aä]rzt")
_GERMANY_RE = _word(r"німеч|герман|deutsch|german")

_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,40}\Z")
_DIGITS_RE = re.compile(r"\d(?:[\s()./-]*\d){6,}")                  # схоже на телефон: такого в чат не пишемо
_LETTERS_RE = re.compile(r"[^\W\d_]+")
_LATIN = dict(zip("абвгґдеєжзиіїйклмнопрстуфхцчшщьюяыэёъ",
                  ["a", "b", "v", "h", "g", "d", "e", "ie", "zh", "z", "y", "i", "i", "i", "k", "l", "m", "n", "o", "p", "r", "s", "t", "u", "f",
                   "kh", "ts", "ch", "sh", "shch", "", "iu", "ia", "y", "e", "e", ""]))


def enabled() -> bool:
    return bool(MASTERS_URL) and MODE in ("log", "on")


def match(text: str) -> Optional[str]:
    """None — це не пошук майстра; "" — шукають майстра без зрозумілого розділу; інакше id розділу каталогу."""
    text = (text or "").strip()
    if len(text) < 12 or len(text) > 400:
        return None
    if _OFFER_RE.search(text) or _SKIP_RE.search(text) or _ANSWER_START_RE.match(text) or _PHONE_RE.search(text):
        return None
    if not _ASK_RE.search(text):
        return None
    if "?" not in text and not _ASK_START_RE.match(text):
        return None
    for cat, rx in _CAT_RE:
        if rx.search(text):
            if cat == "movers" and _UA_ROUTE_RE.search(text):
                return "ukraine"
            return cat
    return None


def link(src: str, cat: str = "", card: str = "") -> str:
    """Посилання на каталог із позначкою джерела (?s=bot — кнопка в меню, ?s=hint — підказка в чаті): для статистики сайту.
    card — посилання на одну картку (#id=…), cat — на розділ (#cat=…).
    Для групи (src "hint") і заданого app_link — пряме посилання Mini App з startapp=id_<картка> / cat_<розділ>."""
    if APP_LINK and src == "hint":
        param = ("id_" + card) if card else ("cat_" + cat) if cat and cat in CAT_NAMES else ""
        return APP_LINK + ((("&" if "?" in APP_LINK else "?") + "startapp=" + param) if param else "")
    url = MASTERS_URL
    if src:
        url += ("&" if "?" in url else "?") + "s=" + src
    if card:
        url += "#id=" + card
    elif cat and cat in CAT_NAMES:
        url += "#cat=" + cat
    return url


def webapp_enabled() -> bool:
    return WEBAPP and MASTERS_URL.lower().startswith("https://")


def webapp_info(tab: str = ""):
    """WebAppInfo сайту (джерело ?s=bot, tab — вкладка: #tab=masters) чи None, якщо Mini App вимкнено або бібліотека його не знає."""
    if not webapp_enabled():
        return None
    try:
        from telegram import WebAppInfo
    except ImportError:
        return None
    # вкладка и в query (?tab=), и во фрагменте: Telegram дописывает к фрагменту свои параметры, query сохраняет надёжнее
    url = link("bot")
    if tab:
        url += ("&" if "?" in url else "?") + "tab=" + tab + "#tab=" + tab
    return WebAppInfo(url=url)


def private_button(text: str, src: str = "bot", tab: str = ""):
    """Кнопка сайту для ОСОБИСТОГО чату: web_app (сайт всередині Telegram), а без неї — звичайне посилання."""
    from telegram import InlineKeyboardButton
    info = webapp_info(tab)
    if info is not None:
        return InlineKeyboardButton(text, web_app=info)
    return InlineKeyboardButton(text, url=link(src) + ("#tab=" + tab if tab else ""))


def text_for(cat: str) -> str:
    if cat and cat in CAT_NAMES:
        return "🧰 Увесь каталог майстрів і послуг нашого чату тут: %s (розділ «%s»)" % (link("hint", cat), CAT_NAMES[cat])
    return "🧰 Увесь каталог майстрів і послуг нашого чату тут: %s" % link("hint")


# ── Каталог у пам'яті ─────────────────────────────────────────────────────────────────────────────────────────

_cat_lock = threading.Lock()
_catalog = {"items": [], "at": 0.0, "failed": False}
_first = threading.Event()          # перша спроба завантаження вже була (вдала чи ні)
_stop = threading.Event()
_loader = None                      # фоновий потік, один на процес


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1].rstrip(" ,.;:") + "…"


def _clean(value, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return "" if _DIGITS_RE.search(text) else _clip(text, limit)


def _number(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _months(last: str) -> int:
    found = re.match(r"(\d{4})(?:-(\d{1,2}))?", last)
    return int(found.group(1)) * 12 + int(found.group(2) or 0) if found else 0


def _stems(text: str) -> frozenset:
    """Основи слів: перші п'ять літер кожного слова від п'яти літер."""
    return frozenset(word[:5].lower() for word in _LETTERS_RE.findall(text) if len(word) >= 5)


def _who(name: str) -> str:
    """Прізвище наприкінці назви латинкою («… Наталія Просуленко» і «… Natalya Prosulenko» дають одне й те саме):
    так упізнаємо дві картки однієї людини. "" — назва закінчується не прізвищем."""
    words = _LETTERS_RE.findall(re.sub(r"\([^)]*\)", " ", name))
    if len(words) < 3 or len(words[-1]) < 5 or not (words[-1][0].isupper() and words[-2][0].isupper()):
        return ""
    last = words[-1]
    if _NOT_A_NAME.match(last) or _UA_ROUTE_RE.match(last) or any(ask.match(last) for ask, _ in _CITIES):
        return ""
    return "".join(_LATIN.get(letter, letter) for letter in last.lower())


def _parse(data) -> List[dict]:
    """Картки з data.json у вигляді, зручному для відбору. Контактів у пам'яті не тримаємо."""
    rows = data.get("items") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise ValueError("no items in catalog")
    cards = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        card_id, name = str(row.get("id") or ""), _clean(row.get("name"), 60)
        if not _ID_RE.match(card_id) or not name:
            continue
        sub, last = _clean(row.get("sub"), 60), str(row.get("last") or "")
        words = [word for word in row.get("kw") if isinstance(word, str)] if isinstance(row.get("kw"), list) else []
        modes = tuple(mode for mode in row.get("mode") if isinstance(mode, str)) if isinstance(row.get("mode"), list) else ()
        hay = " ; ".join([name, sub] + words)
        cards.append({
            "id": card_id, "cat": str(row.get("cat") or ""), "name": name, "sub": sub, "area": _clean(row.get("area"), 40),
            "mode": modes, "pin": 1 if row.get("pin") else 0, "rec": _number(row.get("rec")), "lst": 1 if row.get("lst") else 0,
            "adc": 1 if row.get("adc") else 0, "old": 1 if last < FRESH_FROM else 0, "when": _months(last),
            "hay": hay, "hay2": hay + " ; " + re.sub(r"\s+", " ", str(row.get("desc") or "")),
            "who": _who(name), "names": _stems(name), "stems": _stems(hay),
        })
    return cards


def _data_url() -> str:
    return MASTERS_URL.split("#", 1)[0].split("?", 1)[0].rstrip("/") + "/data.json"


def _download() -> bytes:
    url = _data_url()
    if not url.lower().startswith(("https://", "http://")):
        raise ValueError("catalog url is not http")
    request = urllib.request.Request(url, headers={"User-Agent": "masters-hint/1.0", "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT) as response:
        raw = response.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("catalog is too big")
    return raw


def _refresh() -> bool:
    """Один похід по каталог. True — картки є. Збій не кидає далі: попередня копія живе, доки не застаріє."""
    try:
        cards = _parse(json.loads(_download().decode("utf-8")))
    except Exception as exc:
        with _cat_lock:
            again, _catalog["failed"] = _catalog["failed"], True
        if not again:
            logger.warning("masters_hint: catalog not loaded: %s", exc)
        _first.set()
        return False
    with _cat_lock:
        news = _catalog["failed"] or len(cards) != len(_catalog["items"]) or not _catalog["at"]
        _catalog.update(items=cards, at=time.time(), failed=False)
    if news:
        logger.info("masters_hint: catalog loaded, %d cards", len(cards))
    _first.set()
    return bool(cards)


def _loop(stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            good = _refresh()
        except Exception:
            logger.exception("masters_hint: catalog refresh failed")
            good = False
        stop.wait(CACHE_SEC if good else RETRY_SEC)


def warm() -> None:
    """Запускає фонове оновлення каталогу, якщо воно ще не працює. Нічого не чекає."""
    global _loader
    if not (CARDS and enabled()):
        return
    with _cat_lock:
        if _loader is not None and _loader.is_alive():
            return
        _loader = threading.Thread(target=_loop, args=(_stop,), name="masters-catalog", daemon=True)
        _loader.start()


def catalog(wait: float = 0.0) -> List[dict]:
    """Картки з пам'яті. [] — каталогу немає або копія застаріла: тоді відповідаємо одним посиланням.
    wait — скільки секунд можна почекати найперше завантаження після старту (у мережу цей виклик не ходить)."""
    warm()
    loader = _loader
    if wait > 0 and not _first.is_set() and loader is not None and loader.is_alive():
        _first.wait(wait)
    with _cat_lock:
        if time.time() - _catalog["at"] > STALE_SEC:
            return []
        return _catalog["items"]


# ── Відбір карток і текст відповіді ───────────────────────────────────────────────────────────────────────────

def _own_names(text: str) -> List[str]:
    """Власні назви з прохання («перевізник Дмитрий», «у Jobcenter»): перші п'ять літер.
    Слово на початку речення, міста й Україну не беремо."""
    stems = []
    for found in _LETTERS_RE.finditer(text):
        word, before = found.group(), text[:found.start()].rstrip()
        if len(word) < 5 or not word[0].isupper() or not before or not (before[-1].isalnum() or before[-1] in ",:;«\"'–—-"):
            continue
        if _UA_ROUTE_RE.match(word) or _GERMANY_RE.match(word) or any(ask.match(word) for ask, _ in _CITIES):
            continue
        stems.append(word[:5].lower())
    return stems


def _cities(text: str) -> Tuple[List[str], List[str]]:
    """Міста з прохання: де шукають і які відкинули («тільки не в Берліні», «Берлін не підходить»)."""
    want, avoid = [], []
    for ask, needle in _CITIES:
        yes = no = False
        for found in ask.finditer(text):
            if _NOT_BEFORE_RE.search(text[:found.start()]) or _NOT_AFTER_RE.match(text, found.start()):
                no = True
            else:
                yes = True
        if yes:
            want.append(needle)
        elif no:
            avoid.append(needle)
    return want, avoid


def pick(cat: str, text: str, cards: List[dict], day: str = "") -> List[dict]:
    """До трьох карток розділу, які підходять під слова прохання. [] — підхожих немає.

    Беремо лише картки, де є тема прохання (стоматолог, манікюр, переїзд …). Порядок серед них:
    закріплені (реклама); ті, де є ще й «для дитини» чи назване в проханні ім'я; свіжі перед давніми (до 2024 року);
    названо місто — картки з цього міста, потім без адреси, потім з інших міст, разом із мовою, виїздом, онлайном;
    збіг із більшою кількістю тем; кого частіше радили в чаті; список перевірених перевізників; рекламодавці; свіжість.
    Цілком рівних щодня чергуємо. Дві картки однієї людини поруч не показуємо; «крім Іваненка» цю картку прибирає,
    «тільки не в Берліні» ставить берлінські картки останніми."""
    fired = [topic for topic in _TOPICS.get(cat, ()) if topic.ask.search(text)]
    need = [topic for topic in fired if not topic.wide] or fired
    if not need:
        return []
    section = [card for card in cards if card["cat"] == cat]
    banned = [found.group(1)[:5].lower() for found in _BUT_RE.finditer(text)]
    # ім'я з прохання рахуємо, лише коли воно вказує на одну-дві картки розділу: «Дмитрий» — так, «Документ» — ні
    named = [stem for stem in _own_names(text)
             if stem not in banned and 0 < sum(1 for card in section if stem in card["stems"]) <= 2]
    cities, far = _cities(text)
    kids, lang = bool(_KIDS[0].search(text)), bool(_LANG[0].search(text))
    visit, online = bool(_VISIT_RE.search(text)), bool(_ONLINE_RE.search(text))
    day = day or time.strftime("%Y-%m-%d")
    ranked = []
    for card in section:
        if not any(topic.card.search(card["hay"]) for topic in need):
            continue
        if any(stem in card["names"] for stem in banned):
            continue
        strong = (1 if kids and _KIDS[1].search(card["hay2"]) else 0) + (1 if any(stem in card["stems"] for stem in named) else 0)
        area = card["area"].lower()
        if any(needle in area for needle in cities):
            near = 2
        elif any(needle in area for needle in far):
            near = -1
        elif cities:    # без адреси, «вся Німеччина» чи наш Потсдам — можливо, підходить; інше місто — ні
            near = 1 if not area or "potsdam" in area or "deutschland" in area or "ukraine" in area else 0
        else:
            near = 0
        soft = near + (1 if lang and _LANG[1].search(card["hay2"]) else 0)
        soft += (1 if visit and "visit" in card["mode"] else 0) + (1 if online and "online" in card["mode"] else 0)
        topics = sum(1 for topic in fired if topic.card.search(card["hay"]))
        turn = zlib.crc32((card["id"] + day).encode("utf-8"))
        ranked.append(((-card["pin"], -strong, card["old"], -soft, -topics, -card["rec"], -card["lst"], -card["adc"], -card["when"], turn), card))
    ranked.sort(key=lambda pair: pair[0])
    chosen, people = [], set()
    for _, card in ranked:
        if card["who"] and card["who"] in people:
            continue
        people.add(card["who"])
        chosen.append(card)
        if len(chosen) == MAX_CARDS:
            break
    return chosen


def _card_line(card: dict, name_len: int, sub_len: int, with_area: bool) -> str:
    details = [part for part in (_clip(card["sub"], sub_len) if sub_len else "", card["area"] if with_area else "") if part]
    line = "• <b>" + _esc(_clip(card["name"], name_len)) + "</b>" + (" (реклама)" if card["pin"] else "")
    if details:
        line += ": " + _esc(", ".join(details))
    return line


def _esc(text: str) -> str:
    return html.escape(text, quote=False)


def _compose(cat: str, cards: List[dict]) -> Tuple[str, int]:
    """28.11.2026: вступ і до трьох рядків «• Назва: спеціалізація, район» (HTML, назва жирним) БЕЗ посилань:
    на картки й розділ ведуть кнопки під повідомленням. Не довше MAX_LEN (видимого тексту): спершу коротшають
    спеціалізація й назва, потім зникає район, потім остання картка.
    Повертає текст і скільки карток у нього вмістилося ("" і 0 — не вмістилася жодна)."""
    for count in range(len(cards), 0, -1):
        for name_len, sub_len, with_area in ((60, 60, True), (60, 30, True), (40, 30, True), (40, 0, True), (40, 0, False)):
            lines = ["🧰 З каталогу майстрів і послуг нашого чату:"]
            lines += [_card_line(card, name_len, sub_len, with_area) for card in cards[:count]]
            body = "\n".join(lines)
            if len(html.unescape(re.sub(r"<[^>]+>", "", body))) <= MAX_LEN:
                return body, count
    return "", 0


def plain_text(cat: str) -> str:
    """Підказка без карток: без посилання в тексті, до каталогу веде кнопка."""
    if cat and cat in CAT_NAMES:
        return "🧰 Увесь каталог майстрів і послуг нашого чату — у кнопці нижче (розділ «%s»)." % _esc(CAT_NAMES[cat])
    return "🧰 Увесь каталог майстрів і послуг нашого чату — у кнопці нижче."


BTN_NAME_LEN = 28


def _buttons(cat: str, cards: List[dict]) -> List[Tuple[str, str]]:
    """Підписи й посилання кнопок під підказкою: по одній на майстра, знизу розділ; без карток — одна «Відкрити каталог»."""
    if not cards:
        return [("🧰 Відкрити каталог", link("hint", cat))]
    rows = [(_clip(card["name"], BTN_NAME_LEN), link("hint", card=card["id"])) for card in cards]
    if cat and cat in CAT_NAMES:
        rows.append(("🧰 Усі майстри розділу", link("hint", cat)))
    else:
        rows.append(("🧰 Відкрити каталог", link("hint")))
    return rows


def answer_full(cat: str, text: str, wait: float = 0.0) -> Tuple[str, List[str], List[Tuple[str, str]]]:
    """HTML-текст підказки, id названих карток і кнопки (підпис, url). Без карток (розділ не визначено, підхожих немає,
    каталог не завантажився, вимикач) — коротка фраза й одна кнопка на каталог."""
    if CARDS and cat in CAT_NAMES:
        try:
            if any(topic.ask.search(text) for topic in _TOPICS.get(cat, ())):      # без теми каталог не потрібен
                cards = pick(cat, text, catalog(wait))
                body, count = _compose(cat, cards) if cards else ("", 0)
                if body:
                    return body, [card["id"] for card in cards[:count]], _buttons(cat, cards[:count])
        except Exception:
            logger.exception("masters_hint: cards failed, plain text instead")
    return plain_text(cat), [], _buttons(cat, [])


def answer(cat: str, text: str, wait: float = 0.0) -> Tuple[str, List[str]]:
    """Текст підказки і id названих карток (кнопки — в answer_full)."""
    body, ids, _ = answer_full(cat, text, wait)
    return body, ids


def markup(buttons: List[Tuple[str, str]]):
    """URL-кнопки по одній у рядок (у групі web_app-кнопок немає)."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, url=url)] for label, url in buttons])


def _load() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(data.get("sent"), list):
            return data
    except (OSError, ValueError):
        pass
    return {"sent": []}


def _save(data: dict) -> None:
    tmp = STATE_PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, STATE_PATH)
    except OSError:
        logger.exception("masters_hint: state not saved")


def _reserve(chat_id: int, user_id: int, now: Optional[float] = None) -> bool:
    """Перевіряє обмеження і одразу займає місце (щоб два потоки не відповіли разом)."""
    now = time.time() if now is None else now
    with _lock:
        data = _load()
        sent = [row for row in data["sent"] if now - row[0] < max(USER_DAYS, 1) * 86400]
        if sum(1 for row in sent if now - row[0] < 86400) >= MAX_PER_DAY:
            return False
        if any(row[1] == chat_id and now - row[0] < GAP_MIN * 60 for row in sent):
            return False
        if any(row[2] == user_id and now - row[0] < USER_DAYS * 86400 for row in sent):
            return False
        sent.append([now, chat_id, user_id])
        data["sent"] = sent
        _save(data)
        return True


def note_sent(chat_id: int, user_id: int) -> None:
    """Інша відповідь бота вже містила посилання на каталог: рахуємо її, щоб не повторюватись."""
    with _lock:
        data = _load()
        data["sent"].append([time.time(), chat_id, user_id])
        _save(data)


def maybe_reply(message, user, text: str) -> bool:
    """Повертає True, якщо підказку надіслано в чат."""
    if not enabled():
        return False
    warm()          # каталог підтягується у фоні з першого ж повідомлення в чаті
    cat = match(text)
    if cat is None:
        return False
    if not _reserve(message.chat_id, user.id):
        logger.info("masters_hint: limit, message %s", message.message_id)
        return False
    body, shown, buttons = answer_full(cat, text, COLD_WAIT)
    cards = ",".join(shown) or "-"
    if MODE != "on":
        logger.info("masters_hint: would reply (cat=%s, cards=%s) to message %s", cat or "-", cards, message.message_id)
        return False
    try:
        message.reply_text(body, parse_mode="HTML", reply_markup=markup(buttons), disable_web_page_preview=True)
        logger.info("masters_hint: sent (cat=%s, cards=%s) to message %s", cat or "-", cards, message.message_id)
        return True
    except Exception:
        logger.exception("masters_hint: reply failed")
        return False


# ── Команда /masters (27.11.2026) ─────────────────────────────────────────────────────────────────────────────

CMD_GROUP_GAP_SEC = 180             # у групі не частіше одного разу на 3 хвилини на чат: без спаму
_cmd_last = {}


def masters_command(update, context) -> None:
    """/masters, /maistry, /mastera: коротка відповідь мовою людини з кнопкою-посиланням на каталог."""
    message = update.effective_message
    chat = update.effective_chat
    if message is None or chat is None or not MASTERS_URL:
        return
    private = chat.type == "private"
    if not private:
        now = time.time()
        if now - _cmd_last.get(chat.id, 0.0) < CMD_GROUP_GAP_SEC:
            return
        _cmd_last[chat.id] = now
    import i18n
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    user = update.effective_user
    lang = i18n.get_lang(user.id) if user else "uk"
    label = i18n.t("masters.cmd.btn", lang)
    # В особистому чаті — web_app (відкриває сайт у Telegram); у групі web_app заборонено: посилання (app_link, якщо задано).
    button = private_button(label) if private else InlineKeyboardButton(label, url=link("hint"))
    markup = InlineKeyboardMarkup([[button]])
    try:
        message.reply_text(i18n.t("masters.cmd.text", lang), reply_markup=markup, disable_web_page_preview=True)
    except Exception:
        logger.exception("masters_hint: /masters reply failed")


from telegram.ext import CommandHandler  # noqa: E402

command_handler = CommandHandler(["masters", "maistry", "mastera"], masters_command)
