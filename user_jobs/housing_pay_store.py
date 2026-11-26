"""Сховище для функцій підписки (25.11.2026): журнал «квартиру вже зняли»,
повернення ушедших, подарункові дні, «приведи друга», «знайшов житло»,
розгорнуті відповіді опитування.

Таблиці створюються самі при імпорті модуля (як і решта проєкту), старі
дані не чіпаються. Модуль імпортується з housing_payment, housing_missed і
housing_receiver - тож таблиці є ще до першого запису.
"""

import logging
import secrets
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

from sqlalchemy import DATETIME, INTEGER, TEXT, Column, UniqueConstraint, func

from database import (
    AnonymousUser,
    Base,
    DBSession,
    HousingAccessUser,
    HousingPayment,
    HousingSearch,
    HousingSendLog,
    HousingUserJourney,
    UserSettings,
    engine,
)

logger = logging.getLogger(__name__)


def utc_now() -> datetime:
    return datetime.utcnow()


class HousingMissedLog(Base):
    """Кожне повідомлення «⏳ Цю квартиру вже зняли»: кому, яке оголошення, коли.
    Раніше воно йшло людині, але в базу не писалось."""

    __tablename__ = 'housing_missed_log'

    id = Column(INTEGER, primary_key=True)
    user_id = Column(INTEGER, nullable=False, index=True)
    source = Column(TEXT, nullable=False)
    listing_key = Column(TEXT, nullable=False)
    created_at = Column(DATETIME, nullable=False, index=True)


class HousingWinback(Base):
    """Одне повідомлення «повернення» на одне закінчення підписки
    (ended_at - дата закінчення, яка стояла в доступі людини)."""

    __tablename__ = 'housing_winback'
    __table_args__ = (UniqueConstraint('user_id', 'ended_at', name='uq_housing_winback_end'),)

    id = Column(INTEGER, primary_key=True)
    user_id = Column(INTEGER, nullable=False, index=True)
    ended_at = Column(DATETIME, nullable=False)
    sent_at = Column(DATETIME, nullable=False)


class HousingPayNudge(Base):
    """Одне нагадування «ви почали оплату, але чек не надійшов» на людину
    (26.11.2026, housing_pay_nudge). user_id - ключ: повторно не пишемо ніколи."""

    __tablename__ = 'housing_pay_nudge'

    user_id = Column(INTEGER, primary_key=True)
    payment_id = Column(INTEGER, nullable=False)
    sent_at = Column(DATETIME, nullable=False)


class HousingGift(Base):
    """«🎁 2 дні миттєвих»: натиснута людиною, не частіше разу на 90 днів."""

    __tablename__ = 'housing_gift'

    id = Column(INTEGER, primary_key=True)
    user_id = Column(INTEGER, nullable=False, index=True)
    started_at = Column(DATETIME, nullable=False)
    until = Column(DATETIME, nullable=False)
    # Коли надіслано «подарунок закінчився» (або коли від нього відмовились
    # через те, що людина вже заплатила).
    end_handled_at = Column(DATETIME)


class HousingReferralCode(Base):
    __tablename__ = 'housing_referral_code'

    user_id = Column(INTEGER, primary_key=True)
    code = Column(TEXT, nullable=False, unique=True)
    created_at = Column(DATETIME, nullable=False)


class HousingReferral(Base):
    """Хто кого привів: одна людина - один запрошувач (invited_id - ключ)."""

    __tablename__ = 'housing_referral'

    invited_id = Column(INTEGER, primary_key=True)
    inviter_id = Column(INTEGER, nullable=False, index=True)
    created_at = Column(DATETIME, nullable=False)
    rewarded_at = Column(DATETIME)
    # month - додано місяць; thanks - подяка без продовження (пакет «поки не знайду» або безстроковий доступ).
    reward_kind = Column(TEXT)


class HousingFoundState(Base):
    """«🏠 Я знайшов(ла) житло»: пошуки на паузі. Підписку не забираємо."""

    __tablename__ = 'housing_found_state'

    user_id = Column(INTEGER, primary_key=True)
    found_at = Column(DATETIME, nullable=False)
    resumed_at = Column(DATETIME)


class HousingSurveyMore(Base):
    """Розгорнута відповідь опитування («✍️ Написати докладніше»)."""

    __tablename__ = 'housing_survey_more'

    id = Column(INTEGER, primary_key=True)
    user_id = Column(INTEGER, nullable=False, index=True)
    survey_answer = Column(TEXT)
    text = Column(TEXT, nullable=False)
    created_at = Column(DATETIME, nullable=False)


_TABLES = [
    HousingMissedLog.__table__, HousingWinback.__table__, HousingGift.__table__,
    HousingReferralCode.__table__, HousingReferral.__table__, HousingFoundState.__table__,
    HousingSurveyMore.__table__, HousingPayNudge.__table__,
]
Base.metadata.create_all(engine, tables=_TABLES)


# --- журнал «квартиру вже зняли» -------------------------------------------------

def log_missed(user_id: int, source: str, listing_key: str) -> None:
    """Не заважає відправці: помилка запису лише в лог."""
    session = DBSession()
    try:
        session.add(HousingMissedLog(
            user_id=int(user_id), source=str(source), listing_key=str(listing_key or ""), created_at=utc_now(),
        ))
        session.commit()
    except Exception:
        session.rollback()
        logger.exception("Could not log a missed flat of %s", user_id)
    finally:
        session.close()


def missed_count(user_id: int, start: datetime, end: datetime) -> int:
    """Скільки різних квартир людині написали «вже зняли» у проміжку."""
    session = DBSession()
    try:
        rows = (
            session.query(HousingMissedLog.source, HousingMissedLog.listing_key)
            .filter(HousingMissedLog.user_id == int(user_id))
            .filter(HousingMissedLog.created_at >= start, HousingMissedLog.created_at < end)
            .distinct().all()
        )
        return len(rows)
    finally:
        session.close()


# --- подарункові дні -------------------------------------------------------------

def last_gift(user_id: int) -> Optional[HousingGift]:
    session = DBSession()
    try:
        row = (
            session.query(HousingGift).filter(HousingGift.user_id == int(user_id))
            .order_by(HousingGift.started_at.desc()).first()
        )
        if row is not None:
            session.expunge(row)
        return row
    finally:
        session.close()


def add_gift(user_id: int, started_at: datetime, until: datetime) -> int:
    session = DBSession()
    try:
        row = HousingGift(user_id=int(user_id), started_at=started_at, until=until)
        session.add(row)
        session.commit()
        return int(row.id)
    finally:
        session.close()


def gifts_to_close(now: datetime) -> List[HousingGift]:
    """Подарунки, що минули і про які ще не сказано."""
    session = DBSession()
    try:
        rows = (
            session.query(HousingGift).filter(HousingGift.end_handled_at.is_(None))
            .filter(HousingGift.until <= now).all()
        )
        for row in rows:
            session.expunge(row)
        return rows
    finally:
        session.close()


def mark_gift_handled(gift_id: int) -> None:
    session = DBSession()
    try:
        row = session.query(HousingGift).get(int(gift_id))
        if row is not None and row.end_handled_at is None:
            row.end_handled_at = utc_now()
            session.commit()
    finally:
        session.close()


def gift_matching(user_id: int, expires_at: Optional[datetime]) -> Optional[HousingGift]:
    """Подарунок, що відповідає цій даті закінчення доступу (±1 хв)."""
    if expires_at is None:
        return None
    session = DBSession()
    try:
        for row in session.query(HousingGift).filter(HousingGift.user_id == int(user_id)):
            if abs(row.until - expires_at) < timedelta(minutes=1):
                session.expunge(row)
                return row
        return None
    finally:
        session.close()


def gift_running(user_id: int, now: Optional[datetime] = None) -> Optional[HousingGift]:
    """Чи діє зараз саме подарунок (а не підписка, видана пізніше)."""
    now = now or utc_now()
    session = DBSession()
    try:
        access = session.query(HousingAccessUser).get(int(user_id))
        if access is None or not access.active or not access.expires_at or access.expires_at <= now:
            return None
    finally:
        session.close()
    gift = gift_matching(user_id, access.expires_at)
    if gift is not None and gift.end_handled_at is None:
        return gift
    return None


# --- повернення -------------------------------------------------------------------

def winback_sent(user_id: int, ended_at: datetime) -> bool:
    session = DBSession()
    try:
        return (
            session.query(HousingWinback.id)
            .filter(HousingWinback.user_id == int(user_id), HousingWinback.ended_at == ended_at).first()
        ) is not None
    finally:
        session.close()


def mark_winback_sent(user_id: int, ended_at: datetime) -> bool:
    session = DBSession()
    try:
        session.add(HousingWinback(user_id=int(user_id), ended_at=ended_at, sent_at=utc_now()))
        session.commit()
        return True
    except Exception:
        session.rollback()
        return False
    finally:
        session.close()


def pay_nudge_sent(user_id: int) -> bool:
    session = DBSession()
    try:
        return session.query(HousingPayNudge.user_id).filter(HousingPayNudge.user_id == int(user_id)).first() is not None
    finally:
        session.close()


def mark_pay_nudge_sent(user_id: int, payment_id: int) -> bool:
    session = DBSession()
    try:
        session.add(HousingPayNudge(user_id=int(user_id), payment_id=int(payment_id), sent_at=utc_now()))
        session.commit()
        return True
    except Exception:
        session.rollback()
        return False
    finally:
        session.close()


# --- «приведи друга» ---------------------------------------------------------------

def referral_code(user_id: int) -> str:
    session = DBSession()
    try:
        row = session.query(HousingReferralCode).get(int(user_id))
        if row is not None:
            return row.code
        for _attempt in range(5):
            code = secrets.token_hex(4)
            if session.query(HousingReferralCode.user_id).filter(HousingReferralCode.code == code).first():
                continue
            session.add(HousingReferralCode(user_id=int(user_id), code=code, created_at=utc_now()))
            session.commit()
            return code
        raise RuntimeError("could not make a referral code")
    finally:
        session.close()


def inviter_by_code(code: str) -> Optional[int]:
    session = DBSession()
    try:
        row = session.query(HousingReferralCode).filter(HousingReferralCode.code == str(code)).first()
        return int(row.user_id) if row is not None else None
    finally:
        session.close()


def is_new_user(user_id: int) -> bool:
    """Бот про людину ще нічого не знає: жодного запису користувача чи пошуку
    житла. UserSettings створюється на першому ж екрані в особистих, тож
    «щойно вперше написав» = немає жодного з цих записів."""
    uid = int(user_id)
    session = DBSession()
    try:
        checks = (
            (UserSettings, UserSettings.user_id), (AnonymousUser, AnonymousUser.user_id),
            (HousingAccessUser, HousingAccessUser.user_id), (HousingUserJourney, HousingUserJourney.user_id),
            (HousingSearch, HousingSearch.user_id), (HousingPayment, HousingPayment.user_id),
            (HousingSendLog, HousingSendLog.user_id),
        )
        for _model, column in checks:
            if session.query(column).filter(column == uid).first() is not None:
                return False
        return True
    finally:
        session.close()


def bind_referral(invited_id: int, inviter_id: int) -> bool:
    """Тільки новий для бота, не сам себе, один запрошувач. True - прив'язано."""
    invited_id, inviter_id = int(invited_id), int(inviter_id)
    if invited_id == inviter_id:
        return False
    session = DBSession()
    try:
        if session.query(HousingReferral).get(invited_id) is not None:
            return False
    finally:
        session.close()
    if not is_new_user(invited_id):
        return False
    session = DBSession()
    try:
        session.add(HousingReferral(invited_id=invited_id, inviter_id=inviter_id, created_at=utc_now()))
        session.commit()
        return True
    except Exception:
        session.rollback()
        return False
    finally:
        session.close()


def get_referral(invited_id: int) -> Optional[HousingReferral]:
    session = DBSession()
    try:
        row = session.query(HousingReferral).get(int(invited_id))
        if row is not None:
            session.expunge(row)
        return row
    finally:
        session.close()


def referral_counts(inviter_id: int) -> Tuple[int, int]:
    """(запрошено, з них оплатили): оплата - хоч один схвалений платіж друга."""
    session = DBSession()
    try:
        invited = [int(r.invited_id) for r in session.query(HousingReferral.invited_id)
                   .filter(HousingReferral.inviter_id == int(inviter_id))]
        if not invited:
            return 0, 0
        paid = (
            session.query(func.count(func.distinct(HousingPayment.user_id)))
            .filter(HousingPayment.user_id.in_(invited), HousingPayment.status == "approved").scalar()
        )
        return len(invited), int(paid or 0)
    finally:
        session.close()


def claim_reward(invited_id: int, kind: str) -> Optional[int]:
    """Атомарно: позначити винагороду видану. Повертає inviter_id, якщо саме
    цей виклик її забрав, і None, якщо запрошення нема або вже нагороджено."""
    session = DBSession()
    try:
        row = session.query(HousingReferral).get(int(invited_id))
        if row is None or row.rewarded_at is not None:
            return None
        updated = (
            session.query(HousingReferral)
            .filter(HousingReferral.invited_id == int(invited_id), HousingReferral.rewarded_at.is_(None))
            .update({"rewarded_at": utc_now(), "reward_kind": kind}, synchronize_session=False)
        )
        session.commit()
        return int(row.inviter_id) if updated else None
    finally:
        session.close()


def unclaim_reward(invited_id: int) -> None:
    """Скасувати відмітку, якщо продовження запрошувачу не вдалося."""
    session = DBSession()
    try:
        session.query(HousingReferral).filter(HousingReferral.invited_id == int(invited_id)).update(
            {"rewarded_at": None, "reward_kind": None}, synchronize_session=False,
        )
        session.commit()
    finally:
        session.close()


def approved_payments_except(user_id: int, payment_id: int) -> int:
    session = DBSession()
    try:
        return (
            session.query(HousingPayment.id)
            .filter(HousingPayment.user_id == int(user_id), HousingPayment.status == "approved")
            .filter(HousingPayment.id != int(payment_id)).count()
        )
    finally:
        session.close()


# --- «знайшов житло» --------------------------------------------------------------

def set_found(user_id: int) -> None:
    session = DBSession()
    try:
        row = session.query(HousingFoundState).get(int(user_id))
        now = utc_now()
        if row is None:
            session.add(HousingFoundState(user_id=int(user_id), found_at=now))
        else:
            row.found_at = now
            row.resumed_at = None
        session.commit()
    finally:
        session.close()


def clear_found(user_id: int) -> bool:
    """Зняти позначку (пошуки відновлено). True - позначка була."""
    session = DBSession()
    try:
        row = session.query(HousingFoundState).get(int(user_id))
        if row is None or row.resumed_at is not None:
            return False
        row.resumed_at = utc_now()
        session.commit()
        return True
    finally:
        session.close()


def is_found(user_id: int) -> bool:
    session = DBSession()
    try:
        row = session.query(HousingFoundState).get(int(user_id))
        return bool(row is not None and row.resumed_at is None)
    finally:
        session.close()


# --- опитування -------------------------------------------------------------------

def survey_answer(user_id: int) -> str:
    session = DBSession()
    try:
        row = session.query(HousingUserJourney).get(int(user_id))
        return str(row.survey_answer or "") if row is not None else ""
    finally:
        session.close()


def save_survey_more(user_id: int, answer: str, text: str) -> int:
    session = DBSession()
    try:
        row = HousingSurveyMore(user_id=int(user_id), survey_answer=str(answer or "")[:64], text=text, created_at=utc_now())
        session.add(row)
        session.commit()
        return int(row.id)
    finally:
        session.close()
