"""Who gets new flats instantly and who a couple of hours later.

Housing monitoring is open to everyone. The free tier gets every flat its
filters catch, but only once the listing is FREE_DELAY old; a paid
subscription (an active `housing_access_user` row, which the admin grants)
gets it on the very scan that finds it. In a market where a good flat can
be gone within hours, that head start is what people pay for.

Also the one place every real send is written down (`log_sent`), whatever
the source: the per-source `*_delivery` tables mix real sends with the
silent baseline taken at filter creation and die with the filter, so they
could never say how many flats a person actually got.
"""

import html
import logging
import os
import time
from datetime import datetime, timedelta
from typing import Dict, Optional, Set, Tuple

import pytz
import requests

import i18n
from database import DBSession, HousingAccessUser, HousingHeldMatch, HousingSendLog
from user_jobs import sublet

logger = logging.getLogger(__name__)

ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
FREE_DELAY = timedelta(hours=float(os.getenv("HOUSING_FREE_DELAY_HOURS", "2") or 2))
PREMIUM_PRICE_EUR = 5

# Тиха ніч за берлінським часом. Вікно те саме, що й у приймача check-Wohnung
# (HOUSING_QUIET_HOURS_*): там воно стримує Immowelt, тут - решту площадок.
BERLIN = pytz.timezone("Europe/Berlin")
QUIET_START_HOUR = int(os.getenv("HOUSING_QUIET_HOURS_START", "23") or 23) % 24
QUIET_END_HOUR = int(os.getenv("HOUSING_QUIET_HOURS_END", "8") or 8) % 24
# Безкоштовний тариф уночі мовчить завжди, не питаючи налаштування: квартири й
# так приходять із затримкою, а сповіщення о другій ночі - найчастіша причина
# «незручно» в опитуванні (25.10.2026). Вмикається в docker-compose; без
# змінної вимкнено, щоб тести не залежали від години запуску.
FREE_QUIET_NIGHT = os.getenv("HOUSING_FREE_QUIET_NIGHT", "0") == "1"
CHECK_WOHNUNG_BASE_URL = os.getenv("CHECK_WOHNUNG_BASE_URL", "http://host.docker.internal:18765")
_QUIET_PREF_TTL = 600
_quiet_pref_cache: Dict[int, Tuple[float, bool]] = {}


def utc_now() -> datetime:
    return datetime.utcnow()


def premium_user_ids() -> Set[int]:
    session = DBSession()
    try:
        ids = {
            int(row.user_id)
            for row in session.query(HousingAccessUser.user_id).filter(HousingAccessUser.active.is_(True))
        }
    finally:
        session.close()
    if ADMIN_ID:
        ids.add(ADMIN_ID)
    return ids


def is_premium(user_id: int) -> bool:
    return int(user_id) in premium_user_ids()


def is_quiet_hours(now: Optional[datetime] = None) -> bool:
    """`now` - naive UTC, як усюди в базі бота. Вікно 23-8 переходить через
    північ, тому просте `start <= h < end` для нього не годиться."""
    if QUIET_START_HOUR == QUIET_END_HOUR:
        return False
    hour = pytz.utc.localize(now or utc_now()).astimezone(BERLIN).hour
    if QUIET_START_HOUR < QUIET_END_HOUR:
        return QUIET_START_HOUR <= hour < QUIET_END_HOUR
    return hour >= QUIET_START_HOUR or hour < QUIET_END_HOUR


def quiet_hours_label() -> str:
    return f"{QUIET_START_HOUR:02d}:00–{QUIET_END_HOUR:02d}:00"


def wants_quiet_night(user_id: int) -> bool:
    """Чи ввімкнула людина «🌙 Тиху ніч». Налаштування живе в приймачі
    check-Wohnung; запит іде лише вночі й кешується на 10 хвилин, щоб обхід
    десяти площадок не смикав приймач на кожну квартиру. Приймач недоступний -
    вважаємо, що ні: краще прийде вночі, ніж не прийде."""
    user_id = int(user_id)
    cached = _quiet_pref_cache.get(user_id)
    if cached and time.monotonic() - cached[0] < _QUIET_PREF_TTL:
        return cached[1]
    try:
        response = requests.get(
            f"{CHECK_WOHNUNG_BASE_URL}/api/housing/notification-prefs",
            params={"user_id": user_id}, timeout=3,
        )
        response.raise_for_status()
        wants = bool(response.json().get("quiet_hours_enabled"))
    except Exception as exc:
        logger.warning("Could not read quiet-night setting of %s: %s", user_id, exc)
        wants = False
    _quiet_pref_cache[user_id] = (time.monotonic(), wants)
    return wants


def forget_quiet_pref(user_id: int) -> None:
    """Після перемикання в налаштуваннях - щоб нове значення діяло одразу."""
    _quiet_pref_cache.pop(int(user_id), None)


def free_quiet_always(user_id: int) -> bool:
    return FREE_QUIET_NIGHT and not is_premium(user_id)


def night_hold(user_id: int, premium: bool, now: Optional[datetime] = None) -> bool:
    """Притримати квартиру до ранку: зараз тиха ніч, і людина або на
    безкоштовному тарифі (FREE_QUIET_NIGHT), або сама ввімкнула тиху ніч.
    Квартира не позначається доставленою - її підбере перший ранковий обхід."""
    if not is_quiet_hours(now):
        return False
    if not premium and FREE_QUIET_NIGHT:
        return True
    return wants_quiet_night(user_id)


def free_delay_hours() -> str:
    return f"{FREE_DELAY.total_seconds() / 3600:g}"


def free_footer(user_id: int) -> str:
    """The line under every flat a free-tier person gets."""
    lang = i18n.get_lang(int(user_id))
    return "\n\n" + i18n.t(
        "housing.free.footer", lang, hours=free_delay_hours(), price=PREMIUM_PRICE_EUR,
    )


def log_sent(
    user_id: int, source: str, listing_key: str, filter_id: Optional[int] = None, delayed: bool = False,
) -> None:
    """Never lets a logging problem get in the way of the send itself."""
    session = DBSession()
    try:
        session.add(HousingSendLog(
            user_id=int(user_id),
            source=str(source),
            listing_key=str(listing_key),
            filter_id=int(filter_id) if filter_id is not None else None,
            delayed=bool(delayed),
            sent_at=utc_now(),
        ))
        session.commit()
    except Exception:
        session.rollback()
        logger.exception("Could not log a %s send to %s", source, user_id)
    finally:
        session.close()


# Відповіді Telegram, після яких цій людині не дійде вже ніщо.
_GONE_MARKERS = (
    "bot was blocked", "user_is_blocked", "user is deactivated", "chat not found",
    "bot was kicked", "peer_id_invalid",
)


def recipient_gone(exc: BaseException) -> bool:
    """Чи означає помилка відправки, що людина недосяжна назавжди."""
    from telegram.error import BadRequest, Unauthorized

    if isinstance(exc, Unauthorized):
        return True
    return isinstance(exc, BadRequest) and any(marker in str(exc).lower() for marker in _GONE_MARKERS)


class DeliveryGate:
    """One per scan of a source: decides whether a match may go out now.

    A free-tier match that isn't old enough yet is simply skipped - not
    marked delivered - so the same match comes up again on a later scan and
    goes out once it is. A listing taken down before then is never sent to
    the free tier - instead it is written down as held (HousingHeldMatch) and
    housing_missed tells the person it was taken down while they waited.
    """

    def __init__(
        self, source: str, listing_model=None, now: Optional[datetime] = None, quiet_hours: bool = True,
    ) -> None:
        self.source = source
        self.now = now or utc_now()
        self.premium = premium_user_ids()
        self._listing_model = listing_model
        self._first_seen: Optional[Dict[str, datetime]] = None
        self._wants_rooms: Dict[int, bool] = {}
        # quiet_hours=False - для ручного «надіслати за останню годину»: там
        # людина сама попросила й чекає відповіді просто зараз.
        self._quiet_hours = quiet_hours
        self._night_hold: Dict[int, bool] = {}
        self._held: Optional[Set[Tuple[int, str]]] = None

    def _held_for_night(self, user_id: int) -> bool:
        if not self._quiet_hours:
            return False
        user_id = int(user_id)
        if user_id not in self._night_hold:
            self._night_hold[user_id] = night_hold(user_id, self.is_premium(user_id), self.now)
        return self._night_hold[user_id]

    def is_premium(self, user_id: int) -> bool:
        return int(user_id) in self.premium

    def _first_seen_at(self, listing: Dict) -> Optional[datetime]:
        value = listing.get("first_seen_at")
        if isinstance(value, datetime):
            return value
        if self._listing_model is None:
            return None
        if self._first_seen is None:
            session = DBSession()
            try:
                model = self._listing_model
                self._first_seen = {
                    str(key): seen for key, seen in session.query(model.listing_key, model.first_seen_at)
                }
            finally:
                session.close()
        return self._first_seen.get(str(listing.get("listing_key") or ""))

    def _sublet_unwanted(self, user_id: int, listing: Dict) -> bool:
        if not sublet.is_sublet(listing.get("title")):
            return False
        user_id = int(user_id)
        if user_id not in self._wants_rooms:
            self._wants_rooms[user_id] = sublet.wants_rooms(user_id)
        return not self._wants_rooms[user_id]

    def due(self, user_id: int, listing: Dict, remember: bool = True) -> bool:
        """`remember=False` - for flats that don't really match the filter
        (Kleinanzeigen near-misses): no "it was taken down while you waited"
        notice for those."""
        # Суборенда — лише тим, хто шукає кімнату (див. user_jobs/sublet.py).
        # Не позначається доставленою: якщо людина потім обере кімнати, вона
        # отримає й це оголошення.
        if self._sublet_unwanted(user_id, listing):
            return False
        premium = self.is_premium(user_id)
        if self._held_for_night(user_id):
            if remember and not premium:
                self._remember_held(user_id, listing)
            return False
        if premium:
            return True
        first_seen = self._first_seen_at(listing)
        if first_seen is None or self.now - first_seen >= FREE_DELAY:
            return True
        if remember:
            self._remember_held(user_id, listing)
        return False

    def _remember_held(self, user_id: int, listing: Dict) -> None:
        """Записує, що квартира чекає на безкоштовну людину: якщо її знімуть
        раніше, housing_missed скаже про це (а не мовчки викине)."""
        key = str(listing.get("listing_key") or "")
        if not key:
            return
        session = DBSession()
        try:
            if self._held is None:
                self._held = {
                    (int(uid), str(k))
                    for uid, k in session.query(HousingHeldMatch.user_id, HousingHeldMatch.listing_key)
                    .filter(HousingHeldMatch.source == self.source)
                }
            if (int(user_id), key) in self._held:
                return
            session.add(HousingHeldMatch(
                user_id=int(user_id), source=self.source, listing_key=key,
                summary=listing_summary(listing), held_at=self.now,
            ))
            session.commit()
            self._held.add((int(user_id), key))
        except Exception:
            session.rollback()
            logger.exception("Could not remember a held %s flat for %s", self.source, user_id)
        finally:
            session.close()

    def footer(self, user_id: int) -> str:
        return "" if self.is_premium(user_id) else free_footer(user_id)

    def sent(self, user_id: int, listing_key: str, filter_id: Optional[int] = None) -> None:
        log_sent(user_id, self.source, listing_key, filter_id, delayed=not self.is_premium(user_id))
        forget_held(user_id, self.source, listing_key)


def listing_summary(listing: Dict) -> str:
    """«Wohnung · Babelsberg · 3 кімн. · 75 м² · 850 €» без мови: цифри й
    позначки, які зрозумілі українською, російською й німецькою."""
    def num(value):
        try:
            return f"{float(value):g}"
        except (TypeError, ValueError):
            return ""
    parts = [str(listing.get("title") or "").strip()[:80], str(listing.get("address") or "").strip()[:60]]
    if num(listing.get("rooms")):
        parts.append(f"🚪 {num(listing.get('rooms'))}")
    if num(listing.get("area_m2")):
        parts.append(f"{num(listing.get('area_m2'))} m²")
    if num(listing.get("price_eur")):
        parts.append(f"{num(listing.get('price_eur'))} €")
    return " · ".join(part for part in parts if part)


def forget_held(user_id: int, source: str, listing_key: str) -> None:
    session = DBSession()
    try:
        session.query(HousingHeldMatch).filter(
            HousingHeldMatch.user_id == int(user_id),
            HousingHeldMatch.source == str(source),
            HousingHeldMatch.listing_key == str(listing_key),
        ).delete(synchronize_session=False)
        session.commit()
    except Exception:
        session.rollback()
        logger.exception("Could not forget a held %s flat for %s", source, user_id)
    finally:
        session.close()


def missed_notice(user_id: int, source_label: str, summary: str):
    """(text, keyboard) of "this flat was taken down before the free tier
    could send it" - the same for polled sources and Immowelt."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    lang = i18n.get_lang(int(user_id))
    text = i18n.t(
        "housing.missed.notice", lang, source=html.escape(source_label), summary=html.escape(summary or "—"),
        hours=free_delay_hours(), price=PREMIUM_PRICE_EUR,
    )
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(
        i18n.t("housing.btn.request_access", lang), callback_data="housing:access_request",
    )]])
    return text, keyboard
