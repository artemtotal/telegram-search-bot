"""Суборенда й тимчасова оренда — лише тим, хто шукає кімнату.

Рішення Артема 13.10.2026: людям, які шукають постійне житло (часто через
Jobcenter), «Untermiete für ein Jahr» чи «Zwischenmiete bis März» — це «не ті
квартири». Тож такі оголошення йдуть лише тому, хто в майстрі явно обрав
«Кімнати в WG». Типовий вибір «усе» (усі три позначки, як вони стоять від
початку) кімнатою не вважається.

Розпізнаємо за заголовком — інших даних більшість площадок не дає:
Untermiete / Untervermietung, Zwischenmiete, befristet (але не «unbefristet»),
«auf Zeit», «für ein Jahr», проміжок дат «01.09.2026 – 14.09.2026».
«Möbliert» саме по собі суборенда не є: на ImmoScout24 багато звичайних
мебльованих квартир на необмежений строк.
"""

import json
import re
from typing import Any

from database import DBSession, HousingSearch

SUBLET_RE = re.compile(
    r"unter[\s-]*miet"
    r"|untervermiet"
    r"|zwischen[\s-]*miet"
    r"|(?<!un)befrist"
    r"|\bauf\s+zeit\b"
    r"|zeitlich\s+begrenzt"
    r"|f(?:ü|ue|u)r\s+(?:ein|1)\s+jahr"
    r"|\b\d{1,2}\.\d{1,2}\.(?:\d{2,4})?\s*[-–—]\s*\d{1,2}\.\d{1,2}\.",
    re.IGNORECASE,
)


def is_sublet(title: Any) -> bool:
    return bool(SUBLET_RE.search(str(title or "")))


def wants_rooms(user_id: int) -> bool:
    """Чи обрала людина «Кімнати в WG» явно хоча б в одному своєму пошуку."""
    session = DBSession()
    try:
        rows = session.query(HousingSearch.criteria).filter(HousingSearch.user_id == int(user_id)).all()
    finally:
        session.close()
    for (raw,) in rows:
        try:
            chosen = json.loads(raw or "{}").get("property_types") or []
        except (TypeError, ValueError):
            continue
        if "room" in chosen:
            return True
    return False
