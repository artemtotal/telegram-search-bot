"""Підказки про платні функції під квартирою для безкоштовних (25.11.2026).

Раніше під кожною квартирою безкоштовна людина бачила одну й ту саму строку
«квартира прийшла із затримкою…». Тепер це по колу різні короткі підказки,
кожна про одну функцію підписки, яку бот справді має:

 1 speed        - затримка ~2 год, з підпискою одразу;
 2 letter       - лист німецькою під цю квартиру одним дотиком;
 3 mappe        - «Папка заявника», один PDF;
 4 applications - «Мої заявки»: весь список і нагадування про перегляд;
 5 quiet        - «Тиха ніч»;
 6 matches      - «Підходять зараз»: список, а не лише кількість;
 7 searches     - кілька пошуків одразу (безкоштовно один);
 8 missed       - справжня цифра: скільки квартир за 7 днів зняли раніше, ніж
                  безкоштовна версія встигла їх надіслати (0 - підказка пропускається);
 9 packages     - пакети (ціни з housing_packages);
10 referral     - «Запроси друга»: +1 місяць, коли друг оформить підписку.

Кожна підказка закінчується посиланням «⚡ Підписка від N €» - це глибоке
посилання t.me/<бот>?start=sub, яке відкриває вибір пакета оплати.

Лічильник по колу - свій у кожної людини, у таблиці housing_free_hint_state
(створюється при імпорті модуля, старі дані не чіпаються). Кожен виклик
build() бере наступну підказку, тож кілька повідомлень за один обхід монітора
отримують різні. Якщо підказка цієї людини зараз не підходить (немає цифри
«упущено»), береться наступна по колу.

Вимкнути: HOUSING_FREE_HINTS=0 (повертається стара строка housing.free.footer).
Будь-яка помилка тут - теж стара строка: див. housing_tier.free_footer.
"""

import logging
import os
import threading
from datetime import datetime, timedelta
from typing import Dict, Optional

from sqlalchemy import DATETIME, INTEGER, Column

import i18n
from database import Base, DBSession, engine
from user_jobs import housing_packages

logger = logging.getLogger(__name__)

# Порядок кола. Перша - швидкість, як просив власник.
ORDER = (
    "speed", "letter", "mappe", "applications", "quiet",
    "matches", "searches", "missed", "packages", "referral",
)
MISSED_WINDOW = timedelta(days=7)
START_PARAM = "sub"


class HousingFreeHintState(Base):
    """Яка підказка наступна в людини (індекс у ORDER)."""

    __tablename__ = 'housing_free_hint_state'

    user_id = Column(INTEGER, primary_key=True)
    next_index = Column(INTEGER, nullable=False, default=0)
    updated_at = Column(DATETIME)


Base.metadata.create_all(engine, tables=[HousingFreeHintState.__table__])

_lock = threading.Lock()


def enabled() -> bool:
    return (os.getenv("HOUSING_FREE_HINTS", "1") or "1").strip() != "0"


def utc_now() -> datetime:
    return datetime.utcnow()


# ---------- умови й числа ----------

def missed_week(user_id: int) -> int:
    """Різні квартири, про які людині за 7 днів написали «вже зняли»."""
    from user_jobs import housing_pay_store

    now = utc_now()
    return int(housing_pay_store.missed_count(int(user_id), now - MISSED_WINDOW, now + timedelta(minutes=1)))


def _flats_form(lang: str, n: int) -> str:
    """Форма слова «квартира» для числа (uk/ru - три форми, de - дві)."""
    if lang == "de":
        return i18n.t("hints.flats.1" if n == 1 else "hints.flats.5", lang)
    last, last2 = n % 10, n % 100
    if last == 1 and last2 != 11:
        key = "hints.flats.1"
    elif 2 <= last <= 4 and not 12 <= last2 <= 14:
        key = "hints.flats.2"
    else:
        key = "hints.flats.5"
    return i18n.t(key, lang)


def _params(name: str, user_id: int, lang: str) -> Optional[Dict[str, object]]:
    """Параметри тексту підказки або None, якщо підказка зараз не підходить."""
    from user_jobs import housing_tier

    if name == "speed":
        return {"hours": housing_tier.free_delay_hours()}
    if name == "mappe":
        from user_handlers import housing_mappe

        return {} if housing_mappe.ENABLED else None
    if name == "applications":
        from user_handlers import housing_tracker

        return {"n": housing_tracker.FREE_LIMIT} if housing_tracker.enabled() else None
    if name == "quiet":
        return {"night": housing_tier.quiet_hours_label()} if housing_tier.QUIET_START_HOUR != housing_tier.QUIET_END_HOUR else None
    if name == "missed":
        n = missed_week(user_id)
        return {"n": n, "flats": _flats_form(lang, n)} if n > 0 else None
    if name == "packages":
        if not housing_packages.enabled():
            return None
        return {
            "p3": housing_packages.price_eur(housing_packages.M3),
            "pf": housing_packages.price_eur(housing_packages.UNTIL_FOUND),
        }
    if name == "referral":
        from user_handlers import housing_found, housing_referral

        if not housing_referral.enabled():
            return None
        # У «знайшов житло» на місці кнопки запрошення стоїть «Відновити пошуки».
        if housing_found.free_resume_buttons(user_id, lang, False):
            return None
        return {}
    return {}   # letter, matches, searches


def sub_link(lang: str) -> str:
    """«⚡ Підписка від N €» - посилання /start=sub; без імені бота - просто текст."""
    from user_jobs import housing_letter_store, housing_tier

    label = i18n.t("hints.sub_link", lang, price=housing_tier.PREMIUM_PRICE_EUR)
    username = getattr(housing_letter_store, "_bot_username", None)
    if not username:
        return label
    return f'<a href="https://t.me/{username}?start={START_PARAM}">{label}</a>'


def render(name: str, user_id: int, lang: str) -> Optional[str]:
    params = _params(name, user_id, lang)
    if params is None:
        return None
    return i18n.t(f"hints.{name}", lang, **params) + "\n" + sub_link(lang)


def build(user_id: int) -> Optional[str]:
    """Наступна підказка людини (з «\\n\\n» попереду) або None - тоді виклик
    лишає стару строку. Лічильник посувається тільки тут."""
    uid = int(user_id)
    lang = i18n.get_lang(uid)
    with _lock:
        session = DBSession()
        try:
            row = session.query(HousingFreeHintState).get(uid)
            start = (int(row.next_index or 0) if row is not None else 0) % len(ORDER)
        finally:
            session.close()
        chosen = None
        for step in range(len(ORDER)):
            index = (start + step) % len(ORDER)
            text = render(ORDER[index], uid, lang)
            if text:
                chosen = index
                break
        if chosen is None:
            return None
        session = DBSession()
        try:
            row = session.query(HousingFreeHintState).get(uid)
            if row is None:
                row = HousingFreeHintState(user_id=uid)
                session.add(row)
            row.next_index = (chosen + 1) % len(ORDER)
            row.updated_at = utc_now()
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
    return "\n\n" + text


def start_param_payment(update, context) -> bool:
    """/start sub: показує вибір пакета оплати. False - оплата вимкнена,
    тоді виклик показує звичайний «дім»."""
    from user_handlers import housing_payment

    if not housing_payment.enabled():
        return False
    housing_payment.show_months(update, context)
    return True
