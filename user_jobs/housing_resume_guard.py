"""Поиск, долго пролежавший на паузе, при включении не отдаёт накопленный рынок (25.11.2026).

Опасность нашёл поток C: обычное включение фильтра (`set_filter_active`) тихую
базу не берёт, и поиск, лежавший на паузе две недели, при включении присылал
человеку всё, что за это время висело на рынке под его фильтр (у одного
человека ~948 квартир). Это касается кнопки возобновления в карточке,
`grant_paid_months(…, resume_filters=True)` после оплаты, снятия паузы после
«🏠 Я знайшов(ла) житло», возобновления на сайте подписчика.

Решение диспетчера: пауза дольше 24 часов - при включении перебазировать
(накопленное не слать), короче - как раньше (человек получит то, что пропустил
за час-другой паузы).

Как это работает:
- `note_paused(source, filter_id)` - когда активный фильтр ставят на паузу,
  запоминаем время (таблица `housing_filter_pause`);
- `before_activate(source, filter_id, user_id)` - перед включением: если фильтр
  сейчас на паузе и пауза дольше `HOUSING_RESUME_REBASE_HOURS` (24) или её начало
  неизвестно (пауза случилась до 25.11.2026) - всё, что сейчас подходит под
  фильтр, помечается как уже отправленное. Запись о паузе удаляется.
- Immowelt (приёмник check-Wohnung на ПК) здесь не перебазируется: бот не ведёт его
  журнал отправок, а PATCH `/active` приёмника базу не берёт. Это ограничение
  записано в отчёте 25.11.2026.

Выключатель: `HOUSING_RESUME_REBASE=0` - как до 25.11.2026 (без перебазирования).
Никогда не бросает исключений наружу: ошибка здесь не должна мешать включению.
"""

import logging
import os
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import DATETIME, INTEGER, TEXT, Column

from database import Base, DBSession, engine

logger = logging.getLogger(__name__)


class HousingFilterPause(Base):
    """Когда активный фильтр (источник + id) поставили на паузу."""

    __tablename__ = 'housing_filter_pause'

    source = Column(TEXT, primary_key=True)
    filter_id = Column(INTEGER, primary_key=True)
    user_id = Column(INTEGER, index=True)
    paused_at = Column(DATETIME, nullable=False)


Base.metadata.create_all(engine, tables=[HousingFilterPause.__table__])


def utc_now() -> datetime:
    return datetime.utcnow()


def enabled() -> bool:
    return str(os.getenv("HOUSING_RESUME_REBASE", "1") or "").strip().lower() not in ("", "0", "false", "no", "off")


def rebase_after() -> timedelta:
    try:
        hours = float(os.getenv("HOUSING_RESUME_REBASE_HOURS", "24") or 24)
    except ValueError:
        hours = 24.0
    return timedelta(hours=max(0.0, hours))


def _local(source: str):
    """(store, matching, listing model) для площадок, которые бот обходит сам; иначе None."""
    from user_handlers import housing_monitor as hm

    pair = hm._LOCAL_SOURCE_MODULES.get(str(source))
    model = hm._LISTING_MODELS.get(str(source))
    if pair is None or model is None:
        return None
    return pair[0], pair[1], model


def _filter(store, filter_id: int) -> Optional[dict]:
    return next((f for f in store.list_filters() if int(f["filter_id"]) == int(filter_id)), None)


def note_paused(source: str, filter_id: int, user_id: Optional[int] = None) -> None:
    """Активный фильтр уходит на паузу: запомнить время (уже записанную паузу не сдвигать)."""
    try:
        local = _local(source)
        if local is None:
            return
        filt = _filter(local[0], filter_id)
        if filt is None or not filt.get("active", True):
            return
        session = DBSession()
        try:
            if session.query(HousingFilterPause).get((str(source), int(filter_id))) is None:
                session.add(HousingFilterPause(
                    source=str(source), filter_id=int(filter_id),
                    user_id=int(user_id) if user_id is not None else None, paused_at=utc_now(),
                ))
                session.commit()
        finally:
            session.close()
    except Exception:
        logger.exception("Could not note the pause of %s filter %s", source, filter_id)


def rebase_filter(source: str, filter_id: int) -> int:
    """Всё, что сейчас подходит под фильтр, - уже отправлено. Возвращает, сколько пар помечено."""
    local = _local(source)
    if local is None:
        return 0
    store, matching, model = local
    filt = _filter(store, filter_id)
    if filt is None:
        return 0
    delivered = store.delivered_pairs()
    session = DBSession()
    try:
        listings = [store.listing_to_dict(row) for row in session.query(model).filter(model.is_active.is_(True)).all()]
    finally:
        session.close()
    marked = 0
    for listing in listings:
        key = str(listing.get("listing_key") or "")
        if key and (int(filter_id), key) not in delivered and matching.matches_filter(listing, filt):
            store.mark_delivered(int(filter_id), key)
            marked += 1
    return marked


def before_activate(source: str, filter_id: int, user_id: Optional[int] = None) -> int:
    """Перед включением фильтра. Возвращает, сколько пар помечено базой (0 - не перебазировали)."""
    try:
        local = _local(source)
        if local is None:
            return 0
        filt = _filter(local[0], filter_id)
        if filt is None or filt.get("active", True):
            return 0
        session = DBSession()
        try:
            row = session.query(HousingFilterPause).get((str(source), int(filter_id)))
            paused_at = row.paused_at if row is not None else None
            if row is not None:
                session.delete(row)
                session.commit()
        finally:
            session.close()
        if not enabled():
            return 0
        if paused_at is not None and utc_now() - paused_at <= rebase_after():
            return 0
        marked = rebase_filter(source, filter_id)
        logger.info(
            "Resume of %s filter %s (user %s) after a long/unknown pause: %s waiting flats marked as sent",
            source, filter_id, user_id, marked,
        )
        return marked
    except Exception:
        logger.exception("Could not rebase %s filter %s before resuming", source, filter_id)
        return 0
