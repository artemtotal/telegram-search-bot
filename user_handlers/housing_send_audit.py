"""Вечірня самоперевірка: чи не пішло людям за добу щось явно не те.

Аудит 13.10.2026 знайшов помилки, які тижнями ніхто не помічав, доки люди не
почали скаржитись в опитуванні: Immowelt читав лічильник фото «1 / 60» як
ціну (284 відправки з 27.09 по 11.10), квартира за 1790 € warm ішла фільтрам
«до 562 € холодна», Karl Marx не обмежував ціну людям із лише холодною межею.
Кожну з них видно одразу, якщо подивитись на числа відправленого: 60 € за
85 м², повтор квартири, яка під фільтр уже не підходить.

Тож щовечора бот перевіряє всі відправки за добу і, якщо щось підозріле є,
пише адміну одним повідомленням (квартира — один рядок, скільком пішла). Тихий
день — без повідомлення.
"""

import html
import logging
import re
from collections import OrderedDict
from datetime import datetime, time as dtime, timedelta
from typing import Dict, List, Optional

import pytz
from telegram.ext import CallbackContext

from database import DBSession, HousingSendLog, ImmoweltListing
from user_handlers import housing_monitor as hm
from user_jobs import rent_bounds

logger = logging.getLogger(__name__)

# Перед вечірнім зведенням по фільтрах (21:00), щоб не змішувати.
AUDIT_TIME = dtime(20, 45, tzinfo=pytz.timezone("Europe/Berlin"))
AUDIT_WINDOW = timedelta(days=1)
LISTINGS_SHOWN = 15

# Межі правдоподібного для Потсдама. Найдешевша соціальна квартира — близько
# 5 €/м² холодної, найдорожчі новобуди — до 30 €/м²; запас в обидва боки.
MIN_EUR_PER_M2 = 4.0
MAX_EUR_PER_M2 = 40.0
MIN_AREA_M2 = 10.0
MAX_AREA_M2 = 300.0
MIN_M2_PER_ROOM = 9.0
# Не квартира взагалі: кімната в WG, обмін, пошук житла.
NOT_A_FLAT_RE = re.compile(r"\bwg\b|wg-zimmer|tausch|swap|\bsuche\b|gesucht wird", re.IGNORECASE)


def _num(value) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _row_dict(row) -> Dict:
    return {column.name: getattr(row, column.name) for column in row.__table__.columns}


def listing_problems(listing: Dict) -> List[str]:
    """Що в самих числах оголошення виглядає неправдоподібно."""
    problems = []
    rooms = _num(listing.get("rooms"))
    area = _num(listing.get("area_m2"))
    for key, label in (("price_eur", "холодна"), ("price_warm_eur", "повна"), ("total_rent_eur", "повна")):
        price = _num(listing.get(key))
        if price is not None and area is not None:
            per_m2 = price / area
            if not MIN_EUR_PER_M2 <= per_m2 <= MAX_EUR_PER_M2:
                problems.append(f"{label} {price:g} € за {area:g} м² = {per_m2:.1f} €/м²")
    cold, warm = _num(listing.get("price_eur")), _num(listing.get("price_warm_eur"))
    if cold is not None and warm is not None and warm < cold:
        problems.append(f"повна {warm:g} € менша за холодну {cold:g} €")
    if area is not None and not MIN_AREA_M2 <= area <= MAX_AREA_M2:
        problems.append(f"площа {area:g} м²")
    if rooms is not None and area is not None and rooms >= 2 and area / rooms < MIN_M2_PER_ROOM:
        problems.append(f"{rooms:g} кімн. на {area:g} м²")
    if NOT_A_FLAT_RE.search(str(listing.get("title") or "")):
        problems.append("за заголовком — не квартира")
    return problems


def _load_listing(session, source: str, key: str) -> Optional[Dict]:
    model = ImmoweltListing if source == "immowelt" else hm._LISTING_MODELS.get(source)
    if model is None:
        return None
    row = session.query(model).get(key)
    return _row_dict(row) if row is not None else None


def _filter_by_id(source: str, filter_id, cache: Dict) -> Optional[Dict]:
    modules = hm._LOCAL_SOURCE_MODULES.get(source)
    if modules is None or filter_id is None:
        return None
    if source not in cache:
        cache[source] = {int(f["filter_id"]): f for f in modules[0].list_filters()}
    return cache[source].get(int(filter_id))


def _fits_some_immowelt_filter(listing: Dict, filters: List[Dict]) -> bool:
    """Чи вкладається Immowelt-квартира хоч в один фільтр людини.

    Тут ціна — з тексту картки, без позначки холодна/тепла, тож міряємо
    найпоблажливіше: вистачить, щоб ціна не перевищила ні холодну межу, ні
    повну (чи оцінку повної з холодної). Саме так видно помилку розбору
    в приймачі: 2950 € пішли фільтру «до 680 €», бо він прочитав 14 €.
    """
    rooms, area, price = _num(listing.get("rooms")), _num(listing.get("area_m2")), _num(listing.get("price_eur"))

    def within(value, low, high):
        return value is None or ((low is None or value >= low) and (high is None or value <= high))

    for filt in filters:
        if not within(rooms, filt.get("min_rooms"), filt.get("max_rooms")):
            continue
        if not within(area, filt.get("min_area_m2"), filt.get("max_area_m2")):
            continue
        ceilings = [c for c in (filt.get("max_price_eur"), filt.get("max_price_warm_eur")) if c is not None]
        if filt.get("max_price_eur") is not None and filt.get("max_price_warm_eur") is None:
            ceilings.append(rent_bounds.warm_ceiling(float(filt["max_price_eur"]), area))
        if price is None or not ceilings or price <= max(ceilings):
            return True
    return False


def collect_findings(since: datetime, until: datetime) -> "OrderedDict[tuple, Dict]":
    """{(джерело, ключ): {listing, problems, users}} для підозрілих відправок."""
    session = DBSession()
    try:
        sends = (
            session.query(HousingSendLog)
            .filter(HousingSendLog.sent_at >= since, HousingSendLog.sent_at < until)
            .order_by(HousingSendLog.id)
            .all()
        )
        sends = [(s.user_id, s.source, s.listing_key, s.filter_id) for s in sends]
        listings = {}
        for _user, source, key, _fid in sends:
            if (source, key) not in listings:
                listings[(source, key)] = _load_listing(session, source, key)
    finally:
        session.close()

    filters: Dict = {}
    immowelt_by_user: Optional[Dict[int, List[Dict]]] = None
    if any(source == "immowelt" for _u, source, _k, _f in sends):
        try:
            immowelt_by_user = {}
            for filt in hm._all_immowelt_filters():
                if filt.get("active", True):
                    immowelt_by_user.setdefault(int(filt.get("user_id") or 0), []).append(filt)
        except Exception:
            logger.exception("Audit could not read Immowelt filters")
            immowelt_by_user = None
    findings: "OrderedDict[tuple, Dict]" = OrderedDict()
    for user_id, source, key, filter_id in sends:
        listing = listings.get((source, key))
        if listing is None:
            continue
        problems = listing_problems(listing)
        own = (immowelt_by_user or {}).get(int(user_id))
        if source == "immowelt" and own and not _fits_some_immowelt_filter(listing, own):
            problems.append("не підходить під жоден Immowelt-фільтр людини")
        filt = _filter_by_id(source, filter_id, filters)
        if filt is not None:
            matching = hm._LOCAL_SOURCE_MODULES[source][1]
            try:
                if not matching.matches_filter(listing, filt):
                    problems.append(f"не підходить під фільтр #{filter_id}, з якого пішла")
            except Exception:
                logger.exception("Audit could not re-match %s %s", source, key)
        if not problems:
            continue
        entry = findings.setdefault((source, key), {"listing": listing, "problems": [], "users": set()})
        for problem in problems:
            if problem not in entry["problems"]:
                entry["problems"].append(problem)
        entry["users"].add(int(user_id))
    return findings


def audit_text(findings: "OrderedDict[tuple, Dict]") -> Optional[str]:
    if not findings:
        return None
    sends = sum(len(entry["users"]) for entry in findings.values())
    lines = [
        "🩺 <b>Самоперевірка відправок за добу</b>",
        f"Підозрілих квартир: <b>{len(findings)}</b>, відправок: <b>{sends}</b>.",
        "Найчастіше це помилка розбору площадки — варто глянути код джерела.",
        "",
    ]
    for (source, key), entry in list(findings.items())[:LISTINGS_SHOWN]:
        listing = entry["listing"]
        title = html.escape(str(listing.get("title") or key))[:70]
        url = str(listing.get("detail_url") or "").strip()
        name = f'<a href="{html.escape(url)}">{title}</a>' if url.startswith("https://") else title
        lines.append(
            f"• {hm.SOURCE_LABEL.get(source, source)}: {name} — "
            + html.escape("; ".join(entry["problems"]))
            + f" (людей: {len(entry['users'])})"
        )
    if len(findings) > LISTINGS_SHOWN:
        lines.append(f"…і ще {len(findings) - LISTINGS_SHOWN}.")
    return "\n".join(lines)


def daily_send_audit_job(context: CallbackContext) -> None:
    if not hm.ADMIN_ID:
        return
    until = datetime.utcnow()
    try:
        text = audit_text(collect_findings(until - AUDIT_WINDOW, until))
    except Exception:
        logger.exception("Housing send audit failed")
        return
    if not text:
        return
    try:
        context.bot.send_message(chat_id=hm.ADMIN_ID, text=text, parse_mode="HTML", disable_web_page_preview=True)
    except Exception:
        logger.exception("Could not send the housing send audit")
