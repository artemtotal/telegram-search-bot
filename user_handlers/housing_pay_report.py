"""Личный отчёт перед концом подписки (25.11.2026).

В предупреждение «подписка скоро кончится» и в сообщение в день окончания
добавляются цифры ЭТОГО человека за оплаченный период - только настоящие,
из базы:
- сколько квартир пришло мгновенно (housing_send_log, delayed = 0);
- сколько писем арендодателям он составил (housing_letter_log);
- сколько из этих квартир исчезли с сайта раньше, чем их получили бы
  бесплатные (HOUSING_FREE_DELAY_HOURS) - только если это считается надёжно
  (опрашиваемые площадки, объявление снято, сканы после этого шли дальше).
  Это нижняя граница: Immowelt так посчитать нельзя, поэтому «щонайменше».

Нулевые строки не показываются. Если квартир за период 0 и это подтверждено
рынком (в базе за 30 дней под фильтр человека ничего не появлялось) -
«за цей час нічого не з'явилось»; кнопки расширения фильтра - те же, что в
housing_relax (housing:relax:<search_id>:<code>), и только с честными цифрами.
Под сообщением - три кнопки пакетов.

Отключить: HOUSING_PAY_REPORT_ENABLED=0 (вернутся прежние тексты).
"""

import logging
import math
import os
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from sqlalchemy import func
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import i18n
from database import DBSession, HousingLetterLog, HousingPayment, HousingSendLog
from user_jobs import housing_missed, housing_tier

logger = logging.getLogger(__name__)

# Платёж, оплаченный не позже чем за столько до конца предыдущего, - продление
# того же «периода».
CHAIN_GAP = timedelta(days=2)
# Объявление считается снятым по-настоящему, если сканы шли дальше:
# последнее, что видели на площадке, позже последнего появления этого объявления.
SCAN_CONTINUED_AFTER = timedelta(minutes=30)
KEY_CHUNK = 400


def enabled() -> bool:
    return (os.getenv("HOUSING_PAY_REPORT_ENABLED", "1") or "1").strip() != "0"


def utc_now() -> datetime:
    return datetime.utcnow()


# --- период -------------------------------------------------------------------------

def _payment_start(row: HousingPayment) -> Optional[datetime]:
    """С чего оплата начала работать: с квитанции, если ждали с временным днём."""
    if row.provisional_until and row.receipt_at:
        return row.receipt_at
    return row.decided_at or row.created_at


def period_start(user_id: int, end: Optional[datetime] = None) -> Optional[datetime]:
    """Начало оплаченного периода по журналу оплат: последняя схваленная оплата
    плюс все продления, шедшие встык. None - оплат нет (ручная выдача)."""
    end = end or utc_now()
    session = DBSession()
    try:
        rows = (
            session.query(HousingPayment)
            .filter(HousingPayment.user_id == int(user_id), HousingPayment.status == "approved")
            .filter(HousingPayment.decided_at.isnot(None), HousingPayment.decided_at <= end)
            .order_by(HousingPayment.decided_at.desc()).all()
        )
        if not rows:
            return None
        start = _payment_start(rows[0])
        for prev in rows[1:]:
            if prev.expires_at is not None and start is not None and prev.expires_at >= start - CHAIN_GAP:
                start = _payment_start(prev) or start
            else:
                break
        return start
    finally:
        session.close()


def last_delayed_at(user_id: int, end: datetime) -> Optional[datetime]:
    """Когда человеку последний раз пришла квартира с задержкой (бесплатный
    уровень) - всё, что мгновенно пришло позже, относится к подписке."""
    session = DBSession()
    try:
        return (
            session.query(func.max(HousingSendLog.sent_at))
            .filter(HousingSendLog.user_id == int(user_id), HousingSendLog.delayed.is_(True))
            .filter(HousingSendLog.sent_at < end).scalar()
        )
    finally:
        session.close()


def subscription_period(user_id: int, end: Optional[datetime] = None) -> Optional[datetime]:
    """Начало периода подписки человека. Для ручных выдач (без оплат) -
    момент после последней доставки с задержкой; None - с самого начала."""
    end = end or utc_now()
    start = period_start(user_id, end)
    if start is not None:
        return start
    return last_delayed_at(user_id, end)


# --- цифры --------------------------------------------------------------------------

def instant_sends(user_id: int, start: Optional[datetime], end: datetime) -> List[Tuple[str, str]]:
    """Различные квартиры, пришедшие мгновенно, как (источник, ключ)."""
    session = DBSession()
    try:
        query = (
            session.query(HousingSendLog.source, HousingSendLog.listing_key)
            .filter(HousingSendLog.user_id == int(user_id), HousingSendLog.delayed.is_(False))
            .filter(HousingSendLog.sent_at < end)
        )
        if start is not None:
            query = query.filter(HousingSendLog.sent_at >= start)
        return [(str(s), str(k)) for s, k in query.distinct().all()]
    finally:
        session.close()


def letters_count(user_id: int, start: Optional[datetime], end: datetime) -> int:
    session = DBSession()
    try:
        query = (
            session.query(func.count(HousingLetterLog.id))
            .filter(HousingLetterLog.user_id == int(user_id), HousingLetterLog.created_at < end)
        )
        if start is not None:
            query = query.filter(HousingLetterLog.created_at >= start)
        return int(query.scalar() or 0)
    finally:
        session.close()


def used_letters_ever(user_id: int) -> bool:
    session = DBSession()
    try:
        return session.query(HousingLetterLog.id).filter(HousingLetterLog.user_id == int(user_id)).first() is not None
    finally:
        session.close()


def vanished_early(sends: List[Tuple[str, str]]) -> int:
    """Сколько из этих квартир сняли с сайта раньше, чем бесплатный уровень
    получил бы их (FREE_DELAY после появления). Считаем только опрашиваемые
    площадки и только те объявления, что точно сняты: is_active = 0, жили
    меньше FREE_DELAY, а сканы площадки шли дальше (иначе это мог быть сбой)."""
    by_source: Dict[str, List[str]] = {}
    for source, key in sends:
        if source in housing_missed.LISTING_MODELS:
            by_source.setdefault(source, []).append(key)
    total = 0
    session = DBSession()
    try:
        for source, keys in by_source.items():
            model = housing_missed.LISTING_MODELS[source]
            newest_seen = session.query(func.max(model.last_seen_at)).scalar()
            if newest_seen is None:
                continue
            for index in range(0, len(keys), KEY_CHUNK):
                rows = (
                    session.query(model.first_seen_at, model.last_seen_at)
                    .filter(model.listing_key.in_(keys[index:index + KEY_CHUNK]))
                    .filter(model.is_active.is_(False)).all()
                )
                for first_seen, last_seen in rows:
                    if first_seen is None or last_seen is None:
                        continue
                    if last_seen - first_seen < housing_tier.FREE_DELAY and newest_seen - last_seen > SCAN_CONTINUED_AFTER:
                        total += 1
    finally:
        session.close()
    return total


# --- расширение фильтра при нулевом результате ----------------------------------------

def relax_for_empty(user_id: int, market=None) -> Tuple[bool, list, str]:
    """(рынок подтвердил «ничего не появлялось», кнопки расширения, текст-строка).
    Любая ошибка (в том числе сеть Immowelt) - «не подтверждено» и без кнопок."""
    try:
        from user_handlers import housing_monitor as hm
        from user_handlers import housing_relax
        from user_jobs import housing_search_store

        market = market or housing_relax.Market()
        for search in housing_search_store.list_for_user(int(user_id)):
            filters = market.member_filters(search["members"])
            if not filters or not housing_relax._active(filters):
                continue
            base, found = housing_relax.offers(market, search["criteria"], filters)
            if base is None:
                return False, [], ""
            lang = i18n.get_lang(user_id)
            rows = housing_relax.offer_rows(search["search_id"], search["criteria"], base, found, lang) if found else []
            return base == 0, rows, i18n.t("pay.report.relax", lang) if rows else ""
        return False, [], ""
    except Exception:
        logger.exception("Could not check the market for the empty-period report of %s", user_id)
        return False, [], ""


def build_block(
    user_id: int, start: Optional[datetime], end: Optional[datetime] = None, lang: Optional[str] = None,
    market=None,
) -> Dict[str, object]:
    """Блок цифр человека за период [start, end). Возвращает словарь:
    text (может быть пустым), relax_rows, sends, letters, vanished."""
    end = end or utc_now()
    lang = lang or i18n.get_lang(user_id)
    sends = instant_sends(user_id, start, end)
    count = len(sends)
    letters = letters_count(user_id, start, end)
    vanished = vanished_early(sends) if count else 0
    lines: List[str] = []
    relax_rows: list = []
    if count:
        lines.append(i18n.t("pay.report.title", lang))
        lines.append(i18n.t("pay.report.instant", lang, n=count))
        if vanished:
            lines.append(i18n.t("pay.report.vanished", lang, n=vanished, hours=housing_tier.free_delay_hours()))
    if letters:
        if not lines:
            lines.append(i18n.t("pay.report.title", lang))
        lines.append(i18n.t("pay.report.letters", lang, n=letters))
    if not count:
        verified, relax_rows, relax_text = relax_for_empty(user_id, market)
        if verified:
            window_days = _window_days()
            if start is not None and (end - start) <= timedelta(days=window_days):
                lines.append(i18n.t("pay.report.empty", lang))
            else:
                lines.append(i18n.t("pay.report.empty_days", lang, days=window_days))
        if relax_text:
            lines.append(relax_text)
    if not used_letters_ever(user_id):
        lines.append(i18n.t("pay.report.letter_hint", lang))
    return {
        "text": "\n".join(lines), "relax_rows": relax_rows, "sends": count, "letters": letters, "vanished": vanished,
    }


def _window_days() -> int:
    try:
        from user_handlers import housing_relax

        return int(housing_relax.RELAX_DAYS)
    except Exception:
        return 30



def _package_rows(lang: str) -> list:
    from user_handlers import housing_payment

    return housing_payment.package_buttons(lang)


def usable() -> bool:
    """Отчёт и кнопки пакетов имеют смысл, только если оплата картой включена."""
    from user_handlers import housing_payment
    from user_jobs import housing_packages

    return enabled() and housing_payment.enabled() and housing_packages.enabled()


# --- готовые сообщения -------------------------------------------------------------

def warning_message(user_id: int, expires_at: datetime, market=None):
    """(текст, клавиатура) предупреждения за 3 дня или None, если отчёт выключен
    или не собрался (тогда уходит прежний текст)."""
    try:
        if not usable():
            return None
        lang = i18n.get_lang(user_id)
        end = utc_now()
        block = build_block(user_id, subscription_period(user_id, end), end, lang, market)
        parts = [i18n.t(
            "pay.report.warn", lang, date=expires_at.strftime("%d.%m.%Y"), hours=housing_tier.free_delay_hours(),
        )]
        if block["text"]:
            parts.append(block["text"])
        parts.append(i18n.t("pay.report.choose", lang))
        rows = list(block["relax_rows"]) + _package_rows(lang)
        return "\n\n".join(parts), InlineKeyboardMarkup(rows)
    except Exception:
        logger.exception("Could not build the expiry warning report for %s", user_id)
        return None


def ended_addition(user_id: int, market=None):
    """(текст-добавка, клавиатура) к сообщению «подписка закончилась» или None."""
    try:
        if not usable():
            return None
        lang = i18n.get_lang(user_id)
        end = utc_now()
        block = build_block(user_id, subscription_period(user_id, end), end, lang, market)
        parts = [block["text"]] if block["text"] else []
        parts.append(i18n.t("pay.report.choose", lang))
        rows = list(block["relax_rows"]) + _package_rows(lang)
        return "\n\n".join(parts), InlineKeyboardMarkup(rows)
    except Exception:
        logger.exception("Could not build the ended-subscription report for %s", user_id)
        return None


def gift_end_message(user_id: int, started_at: datetime, market=None):
    """(текст, клавиатура) «подарочные дни закончились» с тремя пакетами."""
    try:
        lang = i18n.get_lang(user_id)
        end = utc_now()
        block = build_block(user_id, started_at, end, lang, market)
        parts = [i18n.t("pay.gift.end_head", lang)]
        if block["text"]:
            parts.append(block["text"])
        parts.append(i18n.t("pay.report.choose", lang))
        rows = list(block["relax_rows"]) + _package_rows(lang)
        return "\n\n".join(parts), InlineKeyboardMarkup(rows)
    except Exception:
        logger.exception("Could not build the gift-end message for %s", user_id)
        return None
