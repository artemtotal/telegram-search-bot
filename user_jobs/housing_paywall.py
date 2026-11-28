"""Данные для замков и «продающих» подсказок сайта «Потсдам» (28.11.2026, docs/paywall-contract.md в каталоге сайта).

Только чтение и только то, что бот уже хранит: журнал отправок (housing_send_log), журнал «квартиру вже зняли» (housing_missed_log), настройки тарифа.
Новых обходов площадок и запросов к ним нет. Чего бот не знает, отдаётся как None, и сайт подсказку не показывает.

  block(user_id, lang)            -> поле `paywall` операции housing.me
  early_hours(user_id, rows)      -> {(source, key): часов} «на сколько часов раньше квартиру получили подписчики» (только честные разницы)
"""

import logging
from datetime import datetime, timedelta
from typing import Dict, Iterable, Optional, Tuple

from sqlalchemy import func

import i18n
from database import DBSession, HousingSendLog

logger = logging.getLogger(__name__)

LOSS_DAYS = 7
MAX_EARLY_H = 24
# Ключи features закреплены контрактом; платные функции жилья, у которых бесплатный видит замок. feed_full в боте НЕ ограничен
# (лента отправленных есть только в мосте и отдаётся всем одинаково), поэтому остаётся открытым.
LOCKED_FOR_FREE = ("instant", "matches", "quiet_night", "more_searches", "stats_full", "mappe", "apps_full")
OPEN_FOR_FREE = ("feed_full",)


def utc_now() -> datetime:
    return datetime.utcnow()


def _number(value) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(number) if number == int(number) else round(number, 2)


def delay_h() -> Optional[float]:
    """Задержка бесплатного тарифа в часах числом (HOUSING_FREE_DELAY_HOURS)."""
    from user_jobs import housing_tier

    return _number(housing_tier.FREE_DELAY.total_seconds() / 3600)


def price_from(lang: str) -> Optional[str]:
    """Готовая строка «5 €/міс»: цена самого короткого пакета (та же, что «від N €» во всех текстах бота)."""
    try:
        from user_jobs import housing_tier

        text = i18n.t("paywall.price_from", lang, price=housing_tier.PREMIUM_PRICE_EUR)
        return text if text and text != "paywall.price_from" else None
    except Exception:
        logger.exception("paywall: price_from")
        return None


def features(user_id: int, premium: bool) -> dict:
    from user_jobs import housing_letter_store

    out = {name: {"locked": not premium} for name in LOCKED_FOR_FREE}
    out.update({name: {"locked": False} for name in OPEN_FOR_FREE})
    out["letter"] = {"locked": not premium, "free_left": int(housing_letter_store.free_left(int(user_id)))}
    return out


def delivered_and_gone(user_id: int, now: Optional[datetime] = None) -> Optional[Tuple[int, int]]:
    """(квартир, к которым бесплатный тариф опоздал за LOSS_DAYS дней; из них уже снятых) или None, если считать нечем.

    Опоздал = пришла с задержкой (housing_send_log.delayed, различные квартиры) + бот написал «вже зняли», потому что она пропала раньше отправки
    (housing_missed_log). Снятые = ТОЛЬКО эти точные «вже зняли» (решение 28.11: людям показываем лишь то, в чём уверены; пришла-и-уже-снята не считается).
    Нижняя граница: журнал «вже зняли» ведётся с 25.11.2026 (полные 7 дней с 02.12); пуст за окно: gone = 0."""
    from user_jobs import housing_pay_store

    now = now or utc_now()
    start = now - timedelta(days=LOSS_DAYS)
    session = DBSession()
    try:
        rows = (session.query(HousingSendLog.source, HousingSendLog.listing_key)
                .filter(HousingSendLog.user_id == int(user_id), HousingSendLog.delayed.is_(True))
                .filter(HousingSendLog.sent_at >= start, HousingSendLog.sent_at < now + timedelta(minutes=1))
                .distinct().all())
    finally:
        session.close()
    pairs = [(str(s), str(k)) for s, k in rows]
    told_gone = int(housing_pay_store.missed_count(int(user_id), start, now + timedelta(minutes=1)))
    return len(pairs) + told_gone, told_gone


def loss(user_id: int) -> Optional[dict]:
    """{"days": 7, "delayed": N, "gone": M}; весь None, если за неделю ни одной квартиры с задержкой; gone None, если не посчиталось."""
    try:
        counted = delivered_and_gone(user_id)
    except Exception:
        logger.exception("paywall: loss of %s", user_id)
        return None
    if counted is None or counted[0] <= 0:
        return None
    return {"days": LOSS_DAYS, "delayed": counted[0], "gone": counted[1]}


def block(user_id: int, lang: str) -> dict:
    from user_jobs import housing_tier

    premium = bool(housing_tier.is_premium(int(user_id)))
    return {
        "premium": premium,
        "price_from": price_from(lang),
        "delay_h": delay_h(),
        "features": features(user_id, premium),
        "loss": None if premium else loss(user_id),
    }


def early_hours(user_id: int, rows: Iterable[dict]) -> Dict[Tuple[str, str], Optional[float]]:
    """rows: [{"source", "key", "sent_at", "delayed"}] отправки ЭТОГО человека. Для квартир, пришедших с задержкой, часы между первой отправкой
    подписчикам этой же квартиры (housing_links.social_numbers: instant_at = min sent_at при delayed=0) и отправкой человеку. Нет подписчика,
    получившего квартиру, или разница не положительная: None (задержку-константу вместо факта не подставляем)."""
    wanted = {}
    for row in rows:
        if row.get("delayed") and isinstance(row.get("sent_at"), datetime):
            wanted[(str(row["source"]), str(row["key"]))] = row["sent_at"]
    out = {pair: None for pair in wanted}
    if not wanted:
        return out
    keys = sorted({key for _source, key in wanted})
    session = DBSession()
    try:
        found = (session.query(HousingSendLog.source, HousingSendLog.listing_key, func.min(HousingSendLog.sent_at))
                 .filter(HousingSendLog.listing_key.in_(keys), HousingSendLog.delayed.is_(False))
                 .group_by(HousingSendLog.source, HousingSendLog.listing_key).all())
    finally:
        session.close()
    for source, key, first in found:
        pair = (str(source), str(key))
        if pair in wanted and first is not None:
            hours = (wanted[pair] - first).total_seconds() / 3600
            # меньше 0,1 ч не показываем; больше суток это уже не волна «подписчики сразу, бесплатный через задержку» (повторная отправка, поздно
            # созданный фильтр), такая разница вводила бы в заблуждение
            if round(hours, 1) >= 0.1 and hours <= MAX_EARLY_H:
                out[pair] = round(hours, 1)
    return out
