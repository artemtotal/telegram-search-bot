"""Підказка в чаті: коли хтось шукає майстра чи послугу, бот коротко відповідає посиланням
на каталог «Майстри Потсдама» (окремий сайт, MASTERS_URL).

26.11.2026, прохання Артема: «щоб він автоматично казав, що весь каталог майстрів тут.
Коротким повідомленням українською».

MASTERS_HINT = off | log | on   (типово off; log — нічого не пише в чат, лише лічить у журналі)
Обмеження, щоб не засмічувати чат: одна підказка людині на MASTERS_HINT_USER_DAYS днів,
не частіше ніж раз на MASTERS_HINT_GAP_MIN хвилин у чаті, не більше MASTERS_HINT_MAX_PER_DAY на добу.
Стан — маленький JSON у /app/config, без текстів повідомлень.
"""
import json
import logging
import os
import re
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

# Налаштування: змінна оточення, а якщо її немає — файл /app/config/masters.json
# ({"url": "...", "hint": "on", "user_days": 7, "gap_min": 20, "max_per_day": 10}). Файл лежить у томі з даними,
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
    ("ukraine", r"посилк|посылк|передач\w*\s+(в|з|до|из|на)\s+укра|(в|до|з|из)\s+україн\w*\s+(їде|їдуть|рейс|бус|перевіз)|перевізник\w*\s+(в|до|на)\s+укра"
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
            return cat
    return None


def link(src: str, cat: str = "") -> str:
    """Посилання на каталог із позначкою джерела (?s=bot — кнопка в меню, ?s=hint — підказка в чаті): для статистики сайту."""
    url = MASTERS_URL
    if src:
        url += ("&" if "?" in url else "?") + "s=" + src
    if cat and cat in CAT_NAMES:
        url += "#cat=" + cat
    return url


def text_for(cat: str) -> str:
    if cat and cat in CAT_NAMES:
        return "🧰 Увесь каталог майстрів і послуг нашого чату тут: %s (розділ «%s»)" % (link("hint", cat), CAT_NAMES[cat])
    return "🧰 Увесь каталог майстрів і послуг нашого чату тут: %s" % link("hint")


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
    cat = match(text)
    if cat is None:
        return False
    if not _reserve(message.chat_id, user.id):
        logger.info("masters_hint: limit, message %s", message.message_id)
        return False
    if MODE != "on":
        logger.info("masters_hint: would reply (cat=%s) to message %s", cat or "-", message.message_id)
        return False
    try:
        message.reply_text(text_for(cat), parse_mode=None, disable_web_page_preview=True)
        logger.info("masters_hint: sent (cat=%s) to message %s", cat or "-", message.message_id)
        return True
    except Exception:
        logger.exception("masters_hint: reply failed")
        return False
