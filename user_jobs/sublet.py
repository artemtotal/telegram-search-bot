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


# Житло лише для студентів або під студентську WG (аудит 01.11.2026: «4 Zimmerwohnung …
# WG Studenten», «1 Zimmer … nur für Studenten») — сім'ї, що шукають квартиру, його не
# отримають, тож воно йде тим самим людям, що й суборенда. «WG-geeignet» (можна й WG)
# сюди не входить: таку квартиру здають і родині.
STUDENT_WG_RE = re.compile(
    r"nur\s+f(?:ü|ue|u)r\s+studen"
    r"|studenten[\s-]*wg"
    r"|\bwg[\s-]+(?:f(?:ü|ue|u)r\s+)?studen"
    r"|f(?:ü|ue|u)r\s+(?:eine\s+)?(?:\d\s*er[\s-]*)?(?:studenten[\s-]*)?wg\b",
    re.IGNORECASE,
)


# Кімната в WG, а не квартира (самоперевірка 02.11.2026: «WG-Zimmer zur Miete» з Immowelt
# пішла 16 людям без вибору «Кімнати в WG», «1 Zimmer in einer 4-Zimmer-Wohnung / Zweck-WG»
# на 11 м² — 43). Лише явна кімната: «4-Zimmer-Wohnung, geeignet für Familien oder WG»,
# «WG gesucht für 3-Zimmer-Wohnung» — це ціла квартира, сюди не входить.
WG_ROOM_RE = re.compile(
    r"\bwg[\s-]*(?:zimmer|room)"
    r"|\bzimmer\b[^|]{0,80}?\bin\s+(?:(?:einer|der|unserer|meiner)\s+)?(?:\d+\s*-?\s*er[\s-]*)?wg\b"
    r"|\bzimmer\s+in\s+(?:einer|der|unserer|meiner|einem)\s+(?:\S+\s+){0,2}?\S*wohnung\b"
    r"|\bzweck[\s!-]*wg\b"
    r"|\broom\s+in\s+(?:a\s+)?shared\s+(?:flat|apartment)",
    re.IGNORECASE,
)


def is_sublet(title: Any) -> bool:
    """Суборенда, тимчасова оренда, кімната в WG чи житло лише для студентів / під WG."""
    text = str(title or "")
    return bool(SUBLET_RE.search(text) or STUDENT_WG_RE.search(text) or WG_ROOM_RE.search(text))


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
