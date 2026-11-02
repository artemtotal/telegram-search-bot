"""Сайт підписника — етап 10 «Берлін і Бранденбург» (02.11.2026).

Те саме, що й у боті, але на великому екрані: свої пошуки (створити, змінити
місце, ціни, кімнати, площу, WBS, площадки, пауза, видалити), статистика
«скільки й звідки прийшло» і історія — останні надіслані квартири та листи
орендодавцям, які склав бот.

* Підписники (housing_tier.is_premium) і адмін бачать усе. З 02.11.2026 зайти
  може й безкоштовний користувач моніторингу (housing_monitor.is_allowed): пошуки
  повністю (як і в боті), статистика за 7 днів, 10 останніх квартир; листів
  орендодавцям немає (це функція підписки), вгорі — нагадування про підписку.
* Вхід — одноразове посилання з бота («🏘 Житло» → «🌐 Сайт»): живе 10 хвилин,
  у базі лише SHA-256 токена (HousingWebToken). Далі — підписана кука на 30 днів
  (HMAC секретом із файла HOUSING_WEB_SECRET_FILE, створюється сам).
* Зміни йдуть тими самими функціями, що й картка фільтра (housing_card.
  change_place / change_sources / apply_criteria) — бот і сайт не розходяться.
* Сервер — стандартний ThreadingHTTPServer, як у приймачів бота; назовні його
  відкриває Tailscale Funnel (HOUSING_WEB_URL — публічна адреса для посилань).
* Кожна форма несе CSRF-токен; кука HttpOnly, Secure, SameSite=Lax.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import logging
import os
import secrets
import threading
import time
from collections import Counter
from datetime import datetime, timedelta
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import i18n
from database import DBSession, HousingSendLog, HousingWebToken
from user_jobs import housing_access_store, housing_area, housing_letter_store, housing_search_store, housing_tier, housing_wbs

logger = logging.getLogger(__name__)

HOST = os.getenv("HOUSING_WEB_HOST", "0.0.0.0")
PORT = int(os.getenv("HOUSING_WEB_PORT", "5013") or 5013)
PUBLIC_URL = os.getenv("HOUSING_WEB_URL", "").rstrip("/")
SECRET_FILE = os.getenv("HOUSING_WEB_SECRET_FILE", "/app/config/housing_web_secret")
LINK_TTL = timedelta(minutes=10)
SESSION_TTL = timedelta(days=30)
COOKIE = "hw_session"
NUMBER_FIELDS = ("max_price_eur", "max_price_warm_eur", "min_rooms", "max_rooms", "min_area_m2", "max_area_m2")
PRICE_MIN_OF = {"max_price_eur": "min_price_eur", "max_price_warm_eur": "min_price_warm_eur"}

_secret_cache: Optional[bytes] = None


def _t(key: str, lang: str, **kwargs) -> str:
    return i18n.t(f"web.{key}", lang, **kwargs)


def _secret() -> bytes:
    global _secret_cache
    if _secret_cache is None:
        try:
            with open(SECRET_FILE, "rb") as handle:
                _secret_cache = handle.read().strip()
        except FileNotFoundError:
            _secret_cache = secrets.token_hex(32).encode()
            with open(SECRET_FILE, "wb") as handle:
                handle.write(_secret_cache)
    return _secret_cache


def _sign(text: str) -> str:
    return hmac.new(_secret(), text.encode(), hashlib.sha256).hexdigest()


def utc_now() -> datetime:
    return datetime.utcnow()


# --- доступ -----------------------------------------------------------------------------

FREE_STATS_DAYS = 7
FREE_HISTORY_LIMIT = 10


def full_access(user_id: int) -> bool:
    """Усе на сайті: адмін і підписник."""
    from user_handlers import housing_monitor as hm
    return int(user_id) == hm.ADMIN_ID or housing_tier.is_premium(int(user_id))


def allowed(user_id: int) -> bool:
    """Хто взагалі може зайти: підписник або безкоштовний користувач моніторингу."""
    from user_handlers import housing_monitor as hm
    return full_access(user_id) or hm.is_allowed(int(user_id))


def create_login_link(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    session = DBSession()
    try:
        now = utc_now()
        session.add(HousingWebToken(
            token_hash=hashlib.sha256(token.encode()).hexdigest(), user_id=int(user_id),
            created_at=now, expires_at=now + LINK_TTL,
        ))
        session.commit()
    finally:
        session.close()
    return f"{PUBLIC_URL}/login?t={token}"


def redeem_token(token: str) -> Optional[int]:
    """Користувач за одноразовим токеном або None (чужий, протух, уже використаний)."""
    if not token:
        return None
    session = DBSession()
    try:
        row = session.query(HousingWebToken).filter(
            HousingWebToken.token_hash == hashlib.sha256(token.encode()).hexdigest()
        ).first()
        if row is None or row.used_at is not None or row.expires_at < utc_now():
            return None
        row.used_at = utc_now()
        session.commit()
        return int(row.user_id)
    finally:
        session.close()


def session_cookie(user_id: int) -> str:
    expires = int(time.time() + SESSION_TTL.total_seconds())
    value = f"{int(user_id)}.{expires}"
    return f"{value}.{_sign(value)}"


def session_user(cookie_value: str) -> Optional[int]:
    try:
        user, expires, signature = cookie_value.split(".")
    except (AttributeError, ValueError):
        return None
    if not hmac.compare_digest(signature, _sign(f"{user}.{expires}")):
        return None
    if int(expires) < time.time():
        return None
    return int(user)


def csrf_token(user_id: int) -> str:
    return _sign(f"csrf:{int(user_id)}")[:32]


# --- дані -------------------------------------------------------------------------------

def _searches(user_id: int) -> List[Dict]:
    from user_handlers import housing_monitor as hm
    return hm.user_searches(int(user_id))


def _search(user_id: int, search_id: int) -> Tuple[Optional[int], Optional[Dict]]:
    for number, search in enumerate(_searches(user_id), 1):
        if search["search_id"] == int(search_id):
            return number, search
    return None, None


def _subscription_until(user_id: int) -> Optional[datetime]:
    for row in housing_access_store.list_users(active_only=True):
        if int(row["user_id"]) == int(user_id):
            return row.get("expires_at")
    return None


def _sends(user_id: int, days: int = 30, limit: Optional[int] = None) -> List[Dict]:
    session = DBSession()
    try:
        query = session.query(HousingSendLog).filter(
            HousingSendLog.user_id == int(user_id), HousingSendLog.sent_at >= utc_now() - timedelta(days=days),
        ).order_by(HousingSendLog.sent_at.desc())
        if limit:
            query = query.limit(limit)
        return [{"source": row.source, "listing_key": row.listing_key, "sent_at": row.sent_at,
                 "snapshot": row.snapshot} for row in query.all()]
    finally:
        session.close()


def _listing_info(source: str, key: str) -> Dict:
    """Назва, адреса й посилання надісланої квартири (з бази площадки)."""
    from user_handlers import housing_monitor as hm
    if source == "immowelt":
        return {"title": "Immowelt", "url": f"https://www.immowelt.de/expose/{key}"}
    model = hm._LISTING_MODELS.get(source)
    modules = hm._LOCAL_SOURCE_MODULES.get(source)
    if model is None or not modules:
        return {}
    session = DBSession()
    try:
        row = session.query(model).filter(model.listing_key == str(key)).first()
        if row is None:
            return {}
        listing = modules[0].listing_to_dict(row)
        return {"title": listing.get("title"), "address": listing.get("address"), "url": listing.get("detail_url")}
    finally:
        session.close()


# --- зміни (ті самі функції, що й у картці) ---------------------------------------------------

def _number(raw: str) -> Optional[float]:
    text = (raw or "").strip().replace("€", "").replace("m²", "").replace(" ", "").replace(",", ".")
    if not text:
        return None
    value = float(text)
    if value <= 0 or value > 100000:
        raise ValueError(text)
    return int(value) if value.is_integer() else round(value, 2)


def save_search(user_id: int, search_id: int, form: Dict[str, List[str]], lang: str) -> Optional[str]:
    """Зберігає форму пошуку. Повертає текст помилки або None."""
    from user_handlers import housing_card

    def field(name: str) -> str:
        return (form.get(name) or [""])[0]

    _number_, search = _search(user_id, search_id)
    if search is None:
        return _t("not_found", lang)
    # 1. Місце.
    current = search["criteria"].get("area")
    text = field("place").strip()
    try:
        radius = int(field("radius") or 0)
    except ValueError:
        radius = 0
    if not text or text == housing_area.label(dict(current, radius_km=0) if current else None, lang).split(" (")[0]:
        base = current or housing_area.find(housing_area.POTSDAM)[0]
    else:
        options = housing_area.find(text)
        if not options:
            return _t("place_not_found", lang, text=html.escape(text[:60]))
        base = options[0]
    place = housing_area.make(base, radius)
    if place and current and current.get("bezirke") and housing_area.is_berlin_city(place) and housing_area.is_berlin_city(current):
        place["bezirke"] = list(current["bezirke"])
    failed: List[str] = []
    if place != current:
        failed += housing_card.change_place(search, place)
        _n, search = _search(user_id, search_id)
    # 2. Площадки й кооперативи Берліна.
    wanted = form.get("src") or []
    if not wanted:
        return _t("pick_a_portal", lang)
    failed += housing_card.change_sources(
        search, wanted, coops=search.get("coops") or [],
        ptypes=search["criteria"].get("property_types") or [], berlin_coops=bool(field("berlin_coops")),
    )
    _n, search = _search(user_id, search_id)
    # 3. Ціни, кімнати, площа, WBS.
    criteria = dict(search["criteria"])
    try:
        for name in NUMBER_FIELDS:
            value = _number(field(name))
            if name in PRICE_MIN_OF and value != criteria.get(name):
                criteria[PRICE_MIN_OF[name]] = None
            criteria[name] = value
    except ValueError:
        return _t("bad_number", lang)
    for low, high in (("min_rooms", "max_rooms"), ("min_area_m2", "max_area_m2")):
        if criteria.get(low) is not None and criteria.get(high) is not None and criteria[low] > criteria[high]:
            return _t("min_over_max", lang)
    criteria["wbs"] = housing_wbs.normalize(field("wbs"))
    failed += housing_card.apply_criteria(search, criteria)
    if failed:
        return _t("save_failed", lang, sources=", ".join(sorted(set(failed))))
    return None


def set_paused(user_id: int, search_id: int, paused: bool) -> bool:
    from user_handlers import housing_monitor as hm
    _n, search = _search(user_id, search_id)
    if search is None:
        return False
    for item in search["items"]:
        source, filter_id = hm.housing_search_grouping.item_key(item)
        hm._set_one_filter_active(source, filter_id, not paused, int(user_id))
    return True


def delete_search(user_id: int, search_id: int) -> bool:
    from user_handlers import housing_monitor as hm
    _n, search = _search(user_id, search_id)
    if search is None:
        return False
    for source, filter_id in search["members"]:
        hm._delete_one_filter(source, filter_id, int(user_id))
    housing_search_store.delete(search_id, int(user_id))
    hm._sync_coop_subscriptions(int(user_id))
    return True


def new_search(user_id: int) -> Optional[int]:
    """Як «➕ Додати фільтр» без питань: Потсдам, на паузі — далі форма."""
    from user_handlers import housing_card, housing_monitor as hm
    if not housing_card.can_add_search(int(user_id)):
        return None
    criteria: Dict = {"districts": [], "property_types": []}
    return housing_card.create_search(
        int(user_id), criteria, housing_card.place_sources(criteria), list(hm.COOP_KEYS), active=False,
    )


# --- сторінки ------------------------------------------------------------------------------

CSS = """
:root{--bg:#f6f7f4;--card:#fff;--fg:#1f2421;--muted:#66706a;--line:#dfe3dc;--accent:#2f6f4f;--warn:#a5441d}
@media (prefers-color-scheme:dark){:root{--bg:#141815;--card:#1d221e;--fg:#e8ece8;--muted:#9aa49d;--line:#2f3631;--accent:#7cc49b;--warn:#f0956c}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}
header{display:flex;flex-wrap:wrap;gap:12px;align-items:center;padding:14px 16px;border-bottom:1px solid var(--line);background:var(--card)}
header b{margin-right:auto}nav a{margin-right:14px;color:var(--accent);text-decoration:none}main{max-width:900px;margin:0 auto;padding:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin:12px 0}
.promo{border-left:4px solid var(--accent)}
.muted{color:var(--muted)}.warn{color:var(--warn)}.row{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
button,.btn{background:var(--accent);color:var(--bg);border:0;border-radius:8px;padding:7px 12px;font:inherit;cursor:pointer;text-decoration:none}
button.ghost{background:transparent;color:var(--accent);border:1px solid var(--accent)}
label{display:block;margin:8px 0 2px;font-size:13px;color:var(--muted)}input,select{width:100%;padding:7px 9px;border:1px solid var(--line);border-radius:7px;background:var(--bg);color:var(--fg);font:inherit}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}.checks label{display:inline-flex;gap:6px;margin:4px 14px 4px 0;color:var(--fg)}
.checks input{width:auto}table{width:100%;border-collapse:collapse}td,th{padding:6px 4px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top;font-variant-numeric:tabular-nums}
details pre{white-space:pre-wrap;font:13px/1.45 ui-monospace,monospace}form.inline{display:inline}
"""


def _page(lang: str, title: str, body: str, user_id: Optional[int] = None) -> str:
    nav = banner = ""
    if user_id and not full_access(user_id):
        banner = f'<div class="card promo">{_t("free_banner", lang)}</div>'
    if user_id:
        nav = (f'<nav><a href="/">{_t("nav.searches", lang)}</a><a href="/stats">{_t("nav.stats", lang)}</a>'
               f'<a href="/history">{_t("nav.history", lang)}</a><a href="/logout">{_t("nav.logout", lang)}</a></nav>')
    return (f'<!doctype html><html lang="{lang}"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title>'
            f'<style>{CSS}</style></head><body><header><b>🏠 {_t("brand", lang)}</b>{nav}</header>'
            f'<main>{banner}{body}</main></body></html>')


def _value(value) -> str:
    if value is None:
        return ""
    return str(int(value)) if float(value).is_integer() else str(value)


def _criteria_summary(criteria: Dict, lang: str) -> str:
    from user_handlers import housing_card
    parts = [housing_area.label(criteria.get("area"), lang)]
    for low, high, unit in (("min_price_eur", "max_price_eur", "€ Kalt"), ("min_price_warm_eur", "max_price_warm_eur", "€ Warm"),
                            ("min_rooms", "max_rooms", _t("rooms_unit", lang)), ("min_area_m2", "max_area_m2", "m²")):
        lo, hi = criteria.get(low), criteria.get(high)
        if lo is not None or hi is not None:
            parts.append(f"{_value(lo) or '…'}–{_value(hi) or '…'} {unit}")
    if criteria.get("wbs"):
        parts.append("WBS: " + housing_card.wbs_label(criteria["wbs"], lang))
    return " · ".join(html.escape(part) for part in parts)


def page_searches(user_id: int, lang: str, note: str = "") -> str:
    from user_handlers import housing_monitor as hm
    until = _subscription_until(user_id)
    csrf = csrf_token(user_id)
    blocks = [f'<p class="muted">{_t("until", lang, date=until.strftime("%d.%m.%Y"))}</p>' if until else ""]
    if note:
        blocks.append(f'<p class="warn">{note}</p>')
    searches = _searches(user_id)
    if not searches:
        blocks.append(f'<div class="card">{_t("no_searches", lang)}</div>')
    for number, search in enumerate(searches, 1):
        paused = hm._search_paused(search)
        sources = ", ".join(html.escape(hm.SOURCE_LABEL.get(s, s)) for s in hm._search_sources(search))
        status = _t("status_off", lang) if paused else _t("status_on", lang)
        sid = search["search_id"]
        blocks.append(
            f'<div class="card"><div class="row"><b>#{number}</b><span>{status}</span></div>'
            f'<p>{_criteria_summary(search["criteria"], lang)}</p><p class="muted">{sources}</p>'
            f'<div class="row"><a class="btn" href="/search/{sid}">{_t("edit", lang)}</a>'
            f'<form class="inline" method="post" action="/search/{sid}/{"resume" if paused else "pause"}">'
            f'<input type="hidden" name="csrf" value="{csrf}"><button class="ghost">{_t("resume" if paused else "pause", lang)}</button></form>'
            f'<form class="inline" method="post" action="/search/{sid}/delete" onsubmit="return confirm(this.dataset.q)" '
            f'data-q="{html.escape(_t("delete_confirm", lang))}"><input type="hidden" name="csrf" value="{csrf}">'
            f'<button class="ghost">{_t("delete", lang)}</button></form></div></div>'
        )
    from user_handlers import housing_card
    if housing_card.can_add_search(user_id):
        blocks.append(f'<form method="post" action="/search/new"><input type="hidden" name="csrf" value="{csrf}">'
                      f'<button>{_t("new_search", lang)}</button></form>')
    else:
        blocks.append(f'<p class="muted">{_t("second_paid", lang)}</p>')
    return _page(lang, _t("nav.searches", lang), "".join(blocks), user_id)


def page_edit(user_id: int, search_id: int, lang: str, note: str = "") -> Optional[str]:
    from user_handlers import housing_card, housing_monitor as hm
    number, search = _search(user_id, search_id)
    if search is None:
        return None
    criteria = search["criteria"]
    place = criteria.get("area")
    place_text = housing_area.label(dict(place, radius_km=0) if place else None, lang).split(" (")[0]
    radius = int((place or {}).get("radius_km") or 0)
    radius_options = "".join(
        f'<option value="{r}"{" selected" if r == radius else ""}>'
        f'{_t("radius_city", lang) if not r else f"+ {r} km"}</option>' for r in housing_area.RADII
    )
    numbers = "".join(
        f'<div><label>{_t("field." + name, lang)}</label><input name="{name}" inputmode="decimal" value="{_value(criteria.get(name))}"></div>'
        for name in NUMBER_FIELDS
    )
    wbs = housing_wbs.normalize(criteria.get("wbs")) or ""
    wbs_options = "".join(
        f'<option value="{value}"{" selected" if value == wbs else ""}>{html.escape(housing_card.wbs_label(value or None, lang))}</option>'
        for value in ("", *housing_wbs.CHOICES)
    )
    present = set(hm._search_sources(search))
    checks = "".join(
        f'<label><input type="checkbox" name="src" value="{source}"{" checked" if source in present else ""}> '
        f'{html.escape(hm.SOURCE_LABEL.get(source, source))}</label>'
        for source in housing_card.covering_sources(place)
    )
    coops = ""
    if housing_area.touches_berlin(place):
        on = criteria.get("berlin_coops") is not False
        coops = (f'<label><input type="checkbox" name="berlin_coops" value="1"{" checked" if on else ""}> '
                 f'{html.escape(i18n.t("housing.card.btn.berlin_coops", lang, n=housing_card.coop_offers.count()))}</label>')
    body = (
        f'<h2>#{number}</h2>' + (f'<p class="warn">{note}</p>' if note else "") +
        f'<form method="post" action="/search/{search_id}" class="card"><input type="hidden" name="csrf" value="{csrf_token(user_id)}">'
        f'<div class="grid"><div><label>{_t("field.place", lang)}</label><input name="place" value="{html.escape(place_text)}"></div>'
        f'<div><label>{_t("field.radius", lang)}</label><select name="radius">{radius_options}</select></div></div>'
        f'<p class="muted">{_t("place_hint", lang)}</p>'
        f'<div class="grid">{numbers}<div><label>WBS</label><select name="wbs">{wbs_options}</select></div></div>'
        f'<label>{_t("field.portals", lang)}</label><div class="checks">{checks}{coops}</div>'
        f'<p><button>{_t("save", lang)}</button> <a href="/">{_t("back", lang)}</a></p></form>'
    )
    return _page(lang, f"#{number}", body, user_id)


def page_stats(user_id: int, lang: str) -> str:
    from user_handlers import housing_monitor as hm
    days_back = 30 if full_access(user_id) else FREE_STATS_DAYS
    sends = _sends(user_id, days=days_back)
    if not sends:
        return _page(lang, _t("nav.stats", lang), f'<div class="card">{_t("no_sends", lang)}</div>', user_id)
    by_source = Counter(row["source"] for row in sends)
    by_day = Counter(row["sent_at"].date() for row in sends)
    days = [(utc_now() - timedelta(days=offset)).date() for offset in range(days_back - 1, -1, -1)]
    peak = max(by_day.values()) or 1
    width, height, bar = 600, 140, 600 / len(days)
    bars = "".join(
        f'<rect x="{index * bar + 1:.1f}" y="{height - 20 - (by_day.get(day, 0) / peak) * (height - 30):.1f}" '
        f'width="{bar - 2:.1f}" height="{(by_day.get(day, 0) / peak) * (height - 30):.1f}" fill="var(--accent)">'
        f'<title>{day.strftime("%d.%m")}: {by_day.get(day, 0)}</title></rect>'
        for index, day in enumerate(days)
    )
    labels = (f'<text x="0" y="{height - 4}" font-size="11" fill="var(--muted)">{days[0].strftime("%d.%m")}</text>'
              f'<text x="{width}" y="{height - 4}" font-size="11" fill="var(--muted)" text-anchor="end">{days[-1].strftime("%d.%m")}</text>')
    rows = "".join(
        f'<tr><td>{html.escape(hm.SOURCE_LABEL.get(source, source))}</td><td>{count}</td></tr>'
        for source, count in by_source.most_common()
    )
    body = (f'<div class="card"><b>{_t("sent_days", lang, days=days_back, n=len(sends))}</b>'
            f'<svg viewBox="0 0 {width} {height}" width="100%" role="img" aria-label="{_t("by_day", lang)}">{bars}{labels}</svg></div>'
            f'<div class="card"><b>{_t("by_source", lang)}</b><table>{rows}</table></div>')
    return _page(lang, _t("nav.stats", lang), body, user_id)


def page_history(user_id: int, lang: str) -> str:
    from user_handlers import housing_monitor as hm
    full = full_access(user_id)
    rows = []
    for row in _sends(user_id, days=60, limit=100 if full else FREE_HISTORY_LIMIT):
        info = _listing_info(row["source"], row["listing_key"])
        snapshot = {}
        try:
            snapshot = json.loads(row["snapshot"] or "{}")
        except ValueError:
            pass
        facts = " · ".join(part for part in (
            f'{_value(snapshot.get("rooms"))} {_t("rooms_unit", lang)}' if snapshot.get("rooms") else "",
            f'{_value(snapshot.get("area_m2"))} m²' if snapshot.get("area_m2") else "",
            f'{_value(snapshot.get("price_eur"))} € Kalt' if snapshot.get("price_eur") else "",
            f'{_value(snapshot.get("price_warm_eur"))} € Warm' if snapshot.get("price_warm_eur") else "",
        ) if part)
        title = html.escape(str(info.get("title") or row["listing_key"]))
        url = str(info.get("url") or "")
        link = f'<a href="{html.escape(url)}" target="_blank" rel="noopener">{title}</a>' if url.startswith("https://") else title
        rows.append(f'<tr><td>{row["sent_at"].strftime("%d.%m %H:%M")}</td><td>{link}<br><span class="muted">'
                    f'{html.escape(hm.SOURCE_LABEL.get(row["source"], row["source"]))} · {html.escape(str(info.get("address") or ""))} {html.escape(facts)}</span></td></tr>')
    letters = []
    for item in housing_letter_store.list_letters(user_id) if full else []:
        url = str(item.get("url") or "")
        title = html.escape(str(item.get("title") or item["listing_key"]))
        link = f'<a href="{html.escape(url)}" target="_blank" rel="noopener">{title}</a>' if url.startswith("https://") else title
        letters.append(f'<div class="card"><div class="muted">{item["created_at"].strftime("%d.%m.%Y %H:%M")}</div>{link}'
                       f'<details><summary>{_t("show_letter", lang)}</summary><pre>{html.escape(item.get("letter") or "")}</pre></details></div>')
    no_letters = _t("no_letters" if full else "letters_paid", lang)
    body = (f'<h2>{_t("letters", lang)}</h2>' + ("".join(letters) or f'<p class="muted">{no_letters}</p>') +
            f'<h2>{_t("sent", lang)}</h2>' +
            (f'<div class="card"><table>{"".join(rows)}</table></div>' if rows else f'<p class="muted">{_t("no_sends", lang)}</p>'))
    return _page(lang, _t("nav.history", lang), body, user_id)


# --- HTTP ---------------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "PotsdamBotWeb/1.0"

    def log_message(self, fmt, *args):  # noqa: N802 — без шуму в журналі бота
        logger.debug("web %s", fmt % args)

    def _user(self) -> Optional[int]:
        cookie = SimpleCookie(self.headers.get("Cookie") or "")
        user_id = session_user(cookie[COOKIE].value) if COOKIE in cookie else None
        return user_id if user_id is not None and allowed(user_id) else None

    def _send(self, status: int, body: str = "", headers: Optional[Dict[str, str]] = None) -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, location: str, cookie: Optional[str] = None) -> None:
        headers = {"Location": location}
        if cookie is not None:
            headers["Set-Cookie"] = cookie
        self._send(303, "", headers)

    def _anonymous(self) -> None:
        lang = "uk"
        self._send(401, _page(lang, _t("brand", lang), f'<div class="card">{_t("login_needed", lang)}</div>'))

    def do_GET(self):  # noqa: N802
        try:
            self._get()
        except Exception:
            logger.exception("Web GET %s failed", self.path)
            self._send(500, "Error")

    def do_POST(self):  # noqa: N802
        try:
            self._post()
        except Exception:
            logger.exception("Web POST %s failed", self.path)
            self._send(500, "Error")

    def _get(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/login":
            user_id = redeem_token((parse_qs(parsed.query).get("t") or [""])[0])
            if user_id is None or not allowed(user_id):
                lang = i18n.get_lang(user_id) if user_id else "uk"
                self._send(403, _page(lang, _t("brand", lang), f'<div class="card">{_t("login_bad", lang)}</div>'))
                return
            cookie = (f"{COOKIE}={session_cookie(user_id)}; Max-Age={int(SESSION_TTL.total_seconds())}; "
                      "Path=/; HttpOnly; Secure; SameSite=Lax")
            self._redirect("/", cookie)
            return
        if parsed.path == "/logout":
            self._redirect("/", f"{COOKIE}=; Max-Age=0; Path=/; HttpOnly; Secure; SameSite=Lax")
            return
        if parsed.path == "/health":
            self._send(200, "ok")
            return
        user_id = self._user()
        if user_id is None:
            self._anonymous()
            return
        lang = i18n.get_lang(user_id)
        if parsed.path == "/":
            self._send(200, page_searches(user_id, lang))
        elif parsed.path == "/stats":
            self._send(200, page_stats(user_id, lang))
        elif parsed.path == "/history":
            self._send(200, page_history(user_id, lang))
        elif parsed.path.startswith("/search/"):
            try:
                search_id = int(parsed.path.split("/")[2])
            except (IndexError, ValueError):
                self._send(404, "Not found")
                return
            page = page_edit(user_id, search_id, lang)
            self._send(200, page) if page else self._send(404, "Not found")
        else:
            self._send(404, "Not found")

    def _post(self) -> None:
        user_id = self._user()
        if user_id is None:
            self._anonymous()
            return
        length = min(int(self.headers.get("Content-Length") or 0), 64 * 1024)
        form = parse_qs(self.rfile.read(length).decode("utf-8", "replace"), keep_blank_values=True)
        if not hmac.compare_digest((form.get("csrf") or [""])[0], csrf_token(user_id)):
            self._send(403, "Bad form token")
            return
        lang = i18n.get_lang(user_id)
        parts = urlparse(self.path).path.strip("/").split("/")
        if parts == ["search", "new"]:
            search_id = new_search(user_id)
            self._redirect(f"/search/{search_id}" if search_id else "/")
            return
        if len(parts) >= 2 and parts[0] == "search":
            try:
                search_id = int(parts[1])
            except ValueError:
                self._send(404, "Not found")
                return
            action = parts[2] if len(parts) > 2 else "save"
            if action in ("pause", "resume"):
                set_paused(user_id, search_id, action == "pause")
                self._redirect("/")
            elif action == "delete":
                delete_search(user_id, search_id)
                self._redirect("/")
            elif action == "save":
                error = save_search(user_id, search_id, form, lang)
                if error:
                    self._send(400, page_edit(user_id, search_id, lang, note=error) or "Not found")
                else:
                    self._redirect("/")
            else:
                self._send(404, "Not found")
            return
        self._send(404, "Not found")


def start() -> Optional[ThreadingHTTPServer]:
    if not PUBLIC_URL:
        logger.info("Housing web is off: HOUSING_WEB_URL is not set")
        return None
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    threading.Thread(target=server.serve_forever, name="housing-web", daemon=True).start()
    logger.info("Housing web started at http://%s:%s (public %s)", HOST, PORT, PUBLIC_URL)
    return server


# --- кнопка в боті ------------------------------------------------------------------------------

def send_login_link(update, context) -> None:
    query = update.callback_query
    user_id = int(update.effective_user.id)
    lang = i18n.get_lang(user_id)
    if not PUBLIC_URL:
        query.answer(_t("not_configured", lang), show_alert=True)
        return
    if not allowed(user_id):
        query.answer(_t("paid_only", lang), show_alert=True)
        return
    query.answer()
    link = create_login_link(user_id)
    context.bot.send_message(
        chat_id=user_id, text=_t("link_text", lang), parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(_t("open", lang), url=link)]]),
    )
