"""Пошуки, створені на паузі й жодного разу не запущені (25.11.2026).

Із 27.10 новий пошук із картки створювався на паузі й стартував лише після «▶️ Почати пошук».
Хто кнопку не натиснув - сидів без квартир (навіть із оплаченою підпискою). Відтепер пошук
створюється активним (housing_card), а цей модуль знаходить і вмикає старі такі пошуки.

«Ні разу не запускався» у базі прямого признака не має, тож судимо за слідами. Пошук - NEVER,
лише якщо ВСЕ разом:
  1. усі його записи на паузі (Immowelt - за станом приймача, як у картці);
  2. жодної відправки з журналу housing_send_log по його записах;
  3. жодної події «фільтр збережено/запущено» людини після створення пошуку (запуск кнопкою
     пише подію edited=False; будь-яка правка на сайті - edited=True);
  4. створений після появи картки (27.10) - раніше пошуки створював майстер одразу активними,
     тож пауза там - це чиясь пауза;
  5. записи площадок створені разом із пошуком (пізніший запис = пошук правили: невідомо, що було).
Ознаки 2 і 3 порушені - це «був у роботі» (пауза людини або системи: зайві пошуки безкоштовних
02.11, «я знайшов» тощо). Порушені 4, 5 або є лише правки на сайті - «сумнівний»: не вмикаємо,
виводимо списком.

Чого модуль не знає: чи запускали пошук кнопкою на сайті «Відновити» (подію вона не пише) і
чи не заводився пошук без жодного слова події (адмін). Такий випадок потрапить у «сумнівні»
(є правки) або в NEVER (їх вмикати нешкідливо: людина хотіла працюючий пошук).
"""

import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from sqlalchemy import text

from database import DBSession, HousingFilterEvent, HousingSendLog
from user_handlers import housing_monitor as hm
from user_jobs import housing_search_store, housing_tier

logger = logging.getLogger(__name__)

CARD_DATE = datetime(2026, 10, 27)
MEMBER_GRACE = timedelta(minutes=10)
EVENT_GRACE = timedelta(minutes=1)

NEVER = "never"
WORKED = "worked"
DOUBTFUL = "doubtful"
ACTIVE = "active"


def _sends(user_id: int, members) -> int:
    session = DBSession()
    try:
        total = 0
        for source, filter_id in members:
            total += (session.query(HousingSendLog)
                      .filter(HousingSendLog.user_id == int(user_id), HousingSendLog.source == str(source),
                              HousingSendLog.filter_id == int(filter_id)).count())
        return total
    finally:
        session.close()


def _events_after(user_id: int, since: datetime) -> Tuple[int, int]:
    """(запусків, правок) людини після `since`."""
    session = DBSession()
    try:
        rows = (session.query(HousingFilterEvent.edited)
                .filter(HousingFilterEvent.user_id == int(user_id),
                        HousingFilterEvent.created_at >= since - EVENT_GRACE).all())
        return sum(1 for row in rows if not row[0]), sum(1 for row in rows if row[0])
    finally:
        session.close()


def _member_created(members) -> Optional[datetime]:
    """Найпізніше створення запису площадки (локальні таблиці; Immowelt тут не видно)."""
    session = DBSession()
    try:
        latest = None
        for source, filter_id in members:
            if source not in hm._LOCAL_SOURCE_MODULES:
                continue
            row = session.execute(
                text(f"SELECT created_at FROM {source}_filter WHERE filter_id = :f"), {"f": int(filter_id)},
            ).first()
            if row and row[0]:
                value = row[0] if isinstance(row[0], datetime) else datetime.fromisoformat(str(row[0])[:26])
                latest = value if latest is None or value > latest else latest
        return latest
    finally:
        session.close()


def classify(stored: Dict, view: Dict) -> Tuple[str, str]:
    """(NEVER|WORKED|DOUBTFUL|ACTIVE, причина) для одного пошуку.
    stored - housing_search_store.get(...), view - елемент hm.user_searches (зі станом записів)."""
    if not hm._search_paused(view):
        return ACTIVE, "працює"
    user_id = int(stored["user_id"])
    members = [(source, filter_id) for source, filter_id in stored["members"]]
    sends = _sends(user_id, members)
    if sends:
        return WORKED, f"було відправок: {sends}"
    created = stored["created_at"]
    starts, edits = _events_after(user_id, created)
    if starts:
        return WORKED, "є подія запуску/збереження фільтра"
    if created < CARD_DATE:
        return DOUBTFUL, "створений до картки (27.10): пауза могла бути чиєюсь"
    member_created = _member_created(members)
    if member_created is not None and member_created - created > MEMBER_GRACE:
        return DOUBTFUL, "записи площадок створені пізніше за пошук (його правили)"
    if edits:
        return DOUBTFUL, f"є правки на сайті ({edits}), а запуску не видно"
    return NEVER, "на паузі від створення, відправок і подій немає"


def inspect_user(user_id: int, views: Optional[List[Dict]] = None) -> List[Dict]:
    """[{search_id, created_at, status, reason, view}] по всіх пошуках людини."""
    views = hm.user_searches(int(user_id)) if views is None else views
    result = []
    for view in views:
        stored = housing_search_store.get(view["search_id"], int(user_id))
        if stored is None:
            continue
        status, reason = classify(stored, view)
        result.append({"search_id": int(view["search_id"]), "created_at": stored["created_at"],
                       "status": status, "reason": reason, "view": view})
    return sorted(result, key=lambda item: (item["created_at"], item["search_id"]))


def pick(user_id: int, inspected: List[Dict]) -> Tuple[List[Dict], str]:
    """Які NEVER-пошуки вмикати. Платній людині - усі; безкоштовній - один (найстаріший),
    і жодного, якщо в неї вже є активний пошук (ліміт «один активний»)."""
    never = [item for item in inspected if item["status"] == NEVER]
    if not never:
        return [], ""
    if housing_tier.is_premium(int(user_id)):
        return never, ""
    if any(item["status"] == ACTIVE for item in inspected):
        return [], "безкоштовний, активний пошук уже є"
    return never[:1], ("безкоштовний: вмикаємо лише найстаріший" if len(never) > 1 else "")


def rebaseline(user_id: int, view: Dict) -> int:
    """Тиха база перед запуском: усе, що вже висить на ринку під умови запису, - не нове.
    Без цього запис, що пролежав на паузі, при вмиканні віддав би людині весь накопичений ринок.
    (Immowelt веде приймач; тут його не чіпаємо.) Повертає, скільки пар позначено."""
    marked = 0
    for item in view["items"]:
        source, filter_id = hm.housing_search_grouping.item_key(item)
        if source not in hm._LOCAL_SOURCE_MODULES:
            continue
        store, matching = hm._LOCAL_SOURCE_MODULES[source]
        filt = next((f for f in store.list_filters() if int(f["filter_id"]) == int(filter_id)), None)
        if filt is None:
            continue
        delivered = store.delivered_pairs()
        session = DBSession()
        try:
            rows = session.query(hm._LISTING_MODELS[source]).all()
            listings = [store.listing_to_dict(row) for row in rows]
        finally:
            session.close()
        for listing in listings:
            key = str(listing.get("listing_key") or "")
            if key and (int(filter_id), key) not in delivered and matching.matches_filter(listing, filt):
                store.mark_delivered(int(filter_id), key)
                marked += 1
    return marked


def activate(user_id: int, view: Dict) -> int:
    """Перебазувати й увімкнути всі записи пошуку. Повертає, скільки пар позначено базою."""
    marked = rebaseline(user_id, view)
    for item in view["items"]:
        source, filter_id = hm.housing_search_grouping.item_key(item)
        hm._set_one_filter_active(source, filter_id, True, int(user_id))
    return marked
