"""Мост: мониторинг жилья на сайте «Потсдам» (операции пакета H1, 28.11.2026). Файл принадлежит пакету H1; остальные сюда не пишут.

Всё, что умеет меню жилья бота и старый кабинет (housing_web), на сайте внутри Telegram и без перехода в бота. Здесь нет своей логики прав,
лимитов и цен: каждая операция вызывает СУЩЕСТВУЮЩИЕ функции бота (housing_card, housing_web, housing_tracker, housing_found, housing_tier,
housing_monitor, хранилища). Чужого видеть и менять нельзя: все поиски берутся из housing_monitor.user_searches(user_id), заявки и объявления
сверяются с user_id, user_id приходит только от сайта (проверенная подпись Telegram), params["user_id"] не читается.

Операции (POST /int/housing/<имя>):
  me            прежняя проверка контракта H0 (не менять: на неё есть общий тест)
  status        лёгкий статус для главного экрана (без похода к приёмнику Immowelt)
  feed          лента «что прислал бот» (housing_send_log) страницами, лёгкая
  listing       карточка квартиры по номеру (ref, как в ссылках бота), только то, что человек уже получил (или подписчику)
  searches      мои поиски (фильтры)
  place_find    поиск места по названию/индексу (как «📍 Де» в боте)
  search_get    один поиск + варианты формы для его места
  search_options варианты формы для места (площадки, районы, ограничения Jobcenter, WBS, радиусы)
  search_save   создать (нет id) или изменить поиск: место, цены, комнаты, площадь, WBS, районы, площадки, кооперативы, типы жилья
  search_pause  пауза / возобновить; search_delete  удалить
  monitoring_set  включить/выключить мониторинг целиком; found_set  «я нашёл жильё» / вернуть поиск
  matches       «квартиры, что подходят сейчас» (подписчику список, бесплатному число, как в боте)
  apps, apply, app_status, app_viewing, app_found, app_remove   «Подався» и заявки
  stats, market статистика (мои отправки по дням/площадкам; рынок за неделю/месяц)
  notify_get, notify_set   тихая ночь, режим доставки, новости
  letters       мои письма арендодателям (только чтение; сами письма H4)
"""

from __future__ import annotations

import html
import logging
import math
import re
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Dict, List, Optional, Tuple

import pytz

import i18n
from site_bridge import Paywall, Refuse, bot, op

logger = logging.getLogger(__name__)

BERLIN_TZ = pytz.timezone("Europe/Berlin")
FEED_MAX = 40
MATCHES_SHOWN = 50
TEASERS_SHOWN = 5                     # картинок-подсказок бесплатному в «Підходящі зараз» (только img)
DEADLINE_S = 6.5                       # мост отвечает человеку за 8 с: тяжёлые вызовы режем раньше
_TAGS = re.compile(r"<[^>]+>")
_URL = re.compile(r"https://[^\s\"'<>]+$")
_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="h1-bridge")
_LOCKS: Dict[int, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()
NUMBER_FIELDS = ("max_price_eur", "max_price_warm_eur", "min_rooms", "max_rooms", "min_area_m2", "max_area_m2")
APP_STATUS_BY_SITE = ("replied", "rejected")      # как кнопки «💬 Відповіли» и «✖ Відмова» в боте; «перегляд» и «квартира є» отдельными операциями


# ---------------------------------------------------------------------------------------------------------------- общее

def _hm():
    from user_handlers import housing_monitor
    return housing_monitor


def _card():
    from user_handlers import housing_card
    return housing_card


def _plain(text) -> str:
    return html.unescape(_TAGS.sub("", str(text or ""))).strip()


def _t(key: str, lang: str, **kwargs) -> str:
    """Текст бота без разметки Telegram."""
    return _plain(i18n.t(key, lang, **kwargs))


def _lock(user_id: int) -> threading.RLock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(int(user_id), threading.RLock())


def _bad(lang: str, what: str = "") -> Refuse:
    return Refuse("bad_request", i18n.t("hsite.bad_request", lang))


def _need_access(user_id: int, lang: str) -> None:
    if not _hm().is_allowed(int(user_id)):
        raise Refuse("no_access", i18n.t("hsite.no_access", lang))


def _full(user_id: int) -> bool:
    """Подписчик или админ: всё, как в housing_web.full_access."""
    from user_jobs import housing_tier
    return int(user_id) == _hm().ADMIN_ID or housing_tier.is_premium(int(user_id))


def _iso(moment) -> Optional[str]:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ") if isinstance(moment, datetime) else None


def _https(url) -> str:
    url = str(url or "").strip()
    return url[:2000] if _URL.match(url) else ""


def _num(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return int(number) if number == int(number) else round(number, 2)


def _int_param(params: dict, name: str, lang: str, low: int = 1, high: int = 2 ** 31 - 1) -> int:
    value = params.get(name)
    if isinstance(value, bool) or value is None:
        raise _bad(lang)
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise _bad(lang)
    if number < low or number > high:
        raise _bad(lang)
    return number


def _bool_param(params: dict, name: str, lang: str) -> bool:
    value = params.get(name)
    if not isinstance(value, bool):
        raise _bad(lang)
    return value


def _str_list(value, lang: str, allowed=None, limit: int = 60) -> List[str]:
    if not isinstance(value, list) or len(value) > limit:
        raise _bad(lang)
    out = []
    for item in value:
        if not isinstance(item, str) or len(item) > 80:
            raise _bad(lang)
        if allowed is not None and item not in allowed:
            raise _bad(lang)
        if item not in out:
            out.append(item)
    return out


def _deadline(fn, lang: str):
    """Тяжёлое (ходит к приёмнику Immowelt и считает подборки) с границей по времени: на сайте человек получит понятный отказ, а не «сервис недоступен»."""
    future = _POOL.submit(fn)
    try:
        return future.result(timeout=DEADLINE_S)
    except FutureTimeout:
        raise Refuse("slow", i18n.t("hsite.slow", lang))


# --------------------------------------------------------------------------------------------------------- «me» (контракт H0)

@op("housing", "me")
def me(user_id, lang, params):
    """Состояние мониторинга человека: подписка и свои поиски. Только чтение, только существующие функции бота. Оставлено как есть: на форму ответа есть общий тест H0;
    для сайта H1 есть лёгкий `status`."""
    from user_handlers import housing_found, housing_monitor as hm
    from user_jobs import housing_access_store, housing_tier

    searches = hm.user_searches(user_id)
    until, trial = None, False
    premium = housing_tier.is_premium(user_id)
    for row in housing_access_store.list_users(active_only=True):
        if int(row["user_id"]) == int(user_id):
            until = row["expires_at"].strftime("%Y-%m-%d") if row.get("expires_at") else None
            trial = bool(row.get("is_trial"))
            break
    filters = [{"id": s["search_id"], "on": not hm._search_paused(s), "sources": hm._search_sources(s)} for s in searches]
    try:        # замки и подсказки (docs/paywall-contract.md): сбой подсчёта не должен ломать проверку контракта
        from user_jobs import housing_paywall
        paywall = housing_paywall.block(user_id, lang)
    except Exception:
        logger.exception("paywall block failed for %s", user_id)
        paywall = None
    return {
        "paywall": paywall,
        "allowed": bool(hm.is_allowed(user_id)),
        "tier": "premium" if premium else "free",
        "until": until,
        "trial": trial,
        "delay_h": housing_tier.free_delay_hours(),
        "found": bool(housing_found.is_found(user_id)),
        "monitoring": any(f["on"] for f in filters),
        "filters": filters,
    }


# --------------------------------------------------------------------------------------------------------- статус и лента

def _local_active(user_id: int) -> Dict[Tuple[str, int], bool]:
    """{(площадка, filter_id): включён} по площадкам с записями в базе бота (приёмник Immowelt не опрашиваем)."""
    hm = _hm()
    out = {}
    for source, (store, _matching) in hm._LOCAL_SOURCE_MODULES.items():
        try:
            for item in store.list_filters(user_id=int(user_id)):
                out[(source, int(item["filter_id"]))] = bool(item.get("active", True))
        except Exception:
            logger.exception("h1: could not read %s filters", source)
    return out


def _light_searches(user_id: int) -> List[dict]:
    """Поиски человека без похода к приёмнику: housing_search_store + флаги «включён» записей в базе бота."""
    from user_jobs import housing_search_store
    active = _local_active(user_id)
    out = []
    for search in housing_search_store.list_for_user(int(user_id)):
        flags = [active[(source, int(fid))] for source, fid in search["members"] if (source, int(fid)) in active]
        out.append({"id": search["search_id"], "on": (any(flags) if flags else True), "members": len(search["members"])})
    return out


def _status(user_id: int, lang: str) -> dict:
    from database import DBSession, HousingApplication, HousingSendLog
    from user_handlers import housing_card, housing_found
    from user_jobs import housing_access_store, housing_tier

    hm = _hm()
    uid = int(user_id)
    allowed = bool(hm.is_allowed(uid))
    premium = _full(uid)
    until, trial = None, False
    if premium:
        for row in housing_access_store.list_users(active_only=True):
            if int(row["user_id"]) == uid:
                until = row["expires_at"].strftime("%Y-%m-%d") if row.get("expires_at") else None
                trial = bool(row.get("is_trial"))
                break
    searches = _light_searches(uid)
    now = datetime.utcnow()
    session = DBSession()
    try:
        n7 = session.query(HousingSendLog).filter(HousingSendLog.user_id == uid, HousingSendLog.sent_at >= now - timedelta(days=7)).count()
        n_all = session.query(HousingSendLog).filter(HousingSendLog.user_id == uid).count()
        applied = session.query(HousingApplication).filter(HousingApplication.user_id == uid).count()
    finally:
        session.close()
    return {
        "allowed": allowed,
        "tier": "premium" if premium else "free",
        "until": until,
        "trial": trial,
        "delay_h": housing_tier.free_delay_hours(),
        "found": bool(housing_found.is_found(uid)),
        "monitoring": any(s["on"] for s in searches),
        "searches": len(searches),
        "can_add": bool(allowed and housing_card.can_add_search(uid)),
        "n7": n7,
        "n_all": n_all,
        "applied": applied,
        "tracker": _tracker_on(),
        "admin": uid == hm.ADMIN_ID,
        **_extra_flags(),
    }


def _extra_flags() -> dict:
    """Какие пункты меню жилья включены в боте (пакет H6): папка заявника, «запроси друга». Только чтение переключателей."""
    from user_handlers import housing_mappe, housing_referral
    return {"mappe": bool(housing_mappe.ENABLED), "invite": bool(housing_referral.enabled())}


def _tracker_on() -> bool:
    from user_handlers import housing_tracker
    return bool(housing_tracker.enabled())


@op("housing", "status")
def status(user_id, lang, params):
    return _status(user_id, lang)


def _listing_models() -> dict:
    from database import ImmoweltListing
    models = dict(_hm()._LISTING_MODELS)
    models.setdefault("immowelt", ImmoweltListing)
    return models


def _load_listings(pairs) -> Dict[Tuple[str, str], dict]:
    """{(источник, ключ): поля объявления} одним запросом на источник. Поля, которых нет в таблице источника, остаются None."""
    from database import DBSession
    models = _listing_models()
    by_source: Dict[str, set] = {}
    for source, key in pairs:
        by_source.setdefault(source, set()).add(str(key))
    names = ("title", "address", "district", "rooms", "area_m2", "price_eur", "price_warm_eur", "total_rent_eur", "detail_url",
             "cover_image_url", "image_url", "gallery_urls", "first_seen_at", "available_from")
    out = {}
    session = DBSession()
    try:
        for source, keys in by_source.items():
            model = models.get(source)
            if model is None:
                continue
            for row in session.query(model).filter(model.listing_key.in_(sorted(keys))).all():
                out[(source, str(row.listing_key))] = {name: getattr(row, name, None) for name in names}
    finally:
        session.close()
    return out


def _snapshot(raw) -> dict:
    import json
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _first(*values):
    for value in values:
        if value not in (None, ""):
            return value
    return None


def _image(row: dict, snap: Optional[dict] = None) -> str:
    """Адрес фото для ленты и карточки. Рекламный баннер портала (/CAMP-) фото не считается; у Immowelt своей таблицы
    с фото нет, его первое фото лежит в снимке отправки (поле img)."""
    from user_jobs import housing_photo_fallback as hpf
    row = row or {}
    candidates = [row.get("cover_image_url"), row.get("image_url")]
    for field in ("gallery_urls", "images"):
        value = row.get(field)
        candidates += list(value) if isinstance(value, (list, tuple)) else str(value or "").split("\n")
    candidates.append((snap or {}).get("img"))
    for candidate in candidates:
        url = _https(candidate)
        if url and not hpf.is_placeholder(url):
            return url
    return ""


def _direct_url(source: str, key: str, row: dict) -> str:
    url = _https((row or {}).get("detail_url"))
    if not url and source == "immowelt" and re.fullmatch(r"[\w-]{4,60}", key or ""):
        url = "https://www.immowelt.de/expose/%s" % key
    return url


def _tokens_and_refs(user_id: int, pairs) -> Tuple[dict, dict, dict]:
    """Только чтение: свои ссылки перехода (/o/<токен>), номера для карточки и заявки по этим квартирам."""
    from database import DBSession, HousingApplication, HousingLetterRef, HousingLinkRef
    from user_jobs import housing_links
    keys = sorted({str(key) for _s, key in pairs})
    tokens, refs, apps = {}, {}, {}
    if not keys:
        return tokens, refs, apps
    session = DBSession()
    try:
        for row in session.query(HousingLinkRef).filter(HousingLinkRef.user_id == int(user_id), HousingLinkRef.listing_key.in_(keys)).all():
            tokens[(row.source, row.listing_key)] = row.token
        for row in session.query(HousingLetterRef).filter(HousingLetterRef.listing_key.in_(keys)).all():
            refs[(row.source, row.listing_key)] = int(row.id)
        for row in session.query(HousingApplication).filter(HousingApplication.user_id == int(user_id), HousingApplication.listing_key.in_(keys)).all():
            apps[(row.source, row.listing_key)] = {"id": int(row.id), "status": row.status, "hidden": bool(row.hidden)}
    finally:
        session.close()
    base = housing_links.public_url()
    return {k: ("%s/o/%s" % (base, v) if base and re.fullmatch(r"[\w-]{4,40}", v or "") else None) for k, v in tokens.items()}, refs, apps


def _source_label(source: str) -> str:
    return _hm().SOURCE_LABEL.get(source, source)


def _early(user_id: int, sends) -> dict:
    """{(источник, ключ): часов} «подписчики получили раньше» для квартир, пришедших человеку с задержкой; нет честной отметки: нет ключа/None."""
    try:
        from user_jobs import housing_paywall
        rows = [{"source": s["source"], "key": s["key"], "sent_at": s["sent_at"], "delayed": s["delayed"]} for s in sends]
        return housing_paywall.early_hours(int(user_id), rows)
    except Exception:
        logger.exception("early_h failed for %s", user_id)
        return {}


@op("housing", "feed")
def feed(user_id, lang, params):
    from database import DBSession, HousingSendLog
    _need_access(user_id, lang)
    n = min(max(_int_param(params, "n", lang, 1, 10 ** 6) if "n" in params else 20, 1), FEED_MAX)
    before = _int_param(params, "before", lang, 1) if params.get("before") else 0
    session = DBSession()
    try:
        query = session.query(HousingSendLog).filter(HousingSendLog.user_id == int(user_id))
        if before:
            query = query.filter(HousingSendLog.id < before)
        rows = query.order_by(HousingSendLog.id.desc()).limit(n + 1).all()
        sends = [{"id": int(r.id), "source": r.source, "key": str(r.listing_key), "delayed": bool(r.delayed), "sent_at": r.sent_at, "snapshot": r.snapshot} for r in rows]
    finally:
        session.close()
    more = len(sends) > n
    sends = sends[:n]
    pairs = [(s["source"], s["key"]) for s in sends]
    listings = _load_listings(pairs)
    tokens, refs, apps = _tokens_and_refs(user_id, pairs)
    early = _early(user_id, sends)
    items = []
    for s in sends:
        pair = (s["source"], s["key"])
        row = listings.get(pair) or {}
        snap = _snapshot(s["snapshot"])
        cold = _num(_first(snap.get("price_eur"), row.get("price_eur")))
        warm = _num(_first(snap.get("price_warm_eur"), row.get("price_warm_eur"), snap.get("total_rent_eur"), row.get("total_rent_eur")))
        label = _source_label(s["source"])
        app = apps.get(pair)
        items.append({
            "id": s["id"], "src": s["source"], "srcl": label, "ref": refs.get(pair),
            "t": str(row.get("title") or label)[:160], "a": str(row.get("address") or "")[:160],
            "d": str(_first(snap.get("district"), row.get("district")) or "")[:60],
            "r": _num(_first(snap.get("rooms"), row.get("rooms"))), "m2": _num(_first(snap.get("area_m2"), row.get("area_m2"))),
            "p": cold, "pw": warm, "img": _image(row, snap), "url": tokens.get(pair) or _direct_url(s["source"], s["key"], row),
            "ts": _iso(s["sent_at"]), "dl": s["delayed"], "early_h": early.get(pair), "ap": (app["status"] if app and not app["hidden"] else None),
        })
    return {"items": items, "next": sends[-1]["id"] if (more and sends) else None}


def _received(user_id: int, source: str, key: str) -> Optional[dict]:
    from database import DBSession, HousingSendLog
    session = DBSession()
    try:
        row = (session.query(HousingSendLog).filter(HousingSendLog.user_id == int(user_id), HousingSendLog.source == source, HousingSendLog.listing_key == key)
               .order_by(HousingSendLog.id.desc()).first())
        return {"sent_at": row.sent_at, "delayed": bool(row.delayed), "snapshot": row.snapshot} if row is not None else None
    finally:
        session.close()


def _resolve_listing(user_id: int, lang: str, ref) -> Tuple[str, str, Optional[dict]]:
    """(источник, ключ, запись отправки) по номеру. Чужое и не полученное (до конца задержки бесплатного) не отдаём: «не найдено»."""
    from user_jobs import housing_letter_store
    if isinstance(ref, bool) or not isinstance(ref, (int, str)):
        raise _bad(lang)
    try:
        number = int(ref)
    except ValueError:
        raise _bad(lang)
    if number < 1 or number > 2 ** 31 - 1:
        raise _bad(lang)
    found = housing_letter_store.resolve_ref(number)
    gone = Refuse("not_found", _t("housing.track.unknown", lang))
    if not found:
        raise gone
    sent = _received(user_id, found["source"], found["listing_key"])
    if sent is None and not _full(user_id):
        raise gone
    return found["source"], found["listing_key"], sent


@op("housing", "listing")
def listing(user_id, lang, params):
    _need_access(user_id, lang)
    source, key, sent = _resolve_listing(user_id, lang, params.get("ref"))
    row = _load_listings([(source, key)]).get((source, key))
    if row is None and sent is None:
        raise Refuse("not_found", _t("housing.track.unknown", lang))
    row = row or {}
    snap = _snapshot((sent or {}).get("snapshot"))
    tokens, refs, apps = _tokens_and_refs(user_id, [(source, key)])
    app = apps.get((source, key))
    from user_jobs import housing_photo_fallback as hpf
    early = _early(user_id, [{"source": source, "key": key, "sent_at": sent["sent_at"], "delayed": sent["delayed"]}] if sent else []).get((source, key))
    gallery =[u for u in (_https(x) for x in str(row.get("gallery_urls") or "").split("\n")) if u and not hpf.is_placeholder(u)][:6]
    cover = _image(row, snap)
    if cover and cover not in gallery:
        gallery.insert(0, cover)
    return {
        "ref": refs.get((source, key)), "src": source, "srcl": _source_label(source),
        "t": str(row.get("title") or _source_label(source))[:200], "a": str(row.get("address") or "")[:200],
        "d": str(_first(snap.get("district"), row.get("district")) or "")[:60],
        "r": _num(_first(snap.get("rooms"), row.get("rooms"))), "m2": _num(_first(snap.get("area_m2"), row.get("area_m2"))),
        "p": _num(_first(snap.get("price_eur"), row.get("price_eur"))),
        "pw": _num(_first(snap.get("price_warm_eur"), row.get("price_warm_eur"), snap.get("total_rent_eur"), row.get("total_rent_eur"))),
        "from": str(row.get("available_from") or "")[:40], "imgs": gallery[:6],
        "url": tokens.get((source, key)) or _direct_url(source, key, row),
        "ts": _iso((sent or {}).get("sent_at")), "dl": bool((sent or {}).get("delayed")), "early_h": early,
        "ap": ({"id": app["id"], "status": app["status"]} if app and not app["hidden"] else None),
        "full": _full(user_id), "tracker": _tracker_on(), "mappe": _extra_flags()["mappe"],
    }


# ------------------------------------------------------------------------------------------------------------------ поиски

def _searches(user_id: int) -> list:
    return _hm().user_searches(int(user_id))


def _find(user_id: int, search_id: int, lang: str) -> Tuple[int, dict]:
    for number, search in enumerate(_searches(user_id), 1):
        if search["search_id"] == int(search_id):
            return number, search
    raise Refuse("not_found", _t("web.not_found", lang))


def _kind(area) -> str:
    from user_jobs import housing_area
    if housing_area.is_default(area):
        return "potsdam"
    if housing_area.is_berlin_city(area):
        return "berlin"
    return "other"


def _ptypes(criteria: dict) -> List[str]:
    hm = _hm()
    types = hm.normalize_property_types(criteria.get("property_types"))
    return list(types) if types else list(hm.PROPERTY_TYPE_KEYS)


def _ser(number: int, search: dict, lang: str) -> dict:
    from user_jobs import housing_area, housing_wbs
    hm = _hm()
    criteria = search["criteria"]
    area = criteria.get("area")
    present = set(hm._search_sources(search))
    return {
        "id": int(search["search_id"]), "n": number, "on": not hm._search_paused(search),
        "area": housing_area.label(area, lang), "kind": _kind(area), "radius": int((area or {}).get("radius_km") or 0),
        "c": {name: _num(criteria.get(name)) for name in NUMBER_FIELDS + ("min_price_eur", "min_price_warm_eur")},
        "wbs": housing_wbs.normalize(criteria.get("wbs")), "districts": [str(x) for x in (criteria.get("districts") or [])],
        "bezirke": [str(x) for x in ((area or {}).get("bezirke") or [])], "berlin_coops": criteria.get("berlin_coops") is not False,
        "ptypes": _ptypes(criteria),
        "sources": [s for s in hm.AVAILABLE_SOURCE_KEYS if s in present],
        "sources_l": [hm.SOURCE_LABEL.get(s, s) for s in hm.AVAILABLE_SOURCE_KEYS if s in present],
        "coops": [str(x) for x in (search.get("coops") or [])],
    }


def _limits(user_id: int) -> dict:
    from user_handlers import housing_card
    return {"can_add": bool(housing_card.can_add_search(int(user_id))), "free_limit": housing_card.FREE_SEARCH_LIMIT, "full": _full(user_id)}


@op("housing", "searches")
def searches(user_id, lang, params):
    _need_access(user_id, lang)
    items = [_ser(n, s, lang) for n, s in enumerate(_searches(user_id), 1)]
    limits = _limits(user_id)
    return {"items": items, **limits, "paid_note": "" if limits["can_add"] else _t("web.second_paid", lang)}


def _place_label(option: dict) -> str:
    name = option["name"] if option.get("kind") == "city" else "%s %s" % (option.get("plz"), option["name"])
    kreis = option.get("kreis") or ""
    return name + (" (%s)" % kreis if kreis and kreis != option["name"] else "")


@op("housing", "place_find")
def place_find(user_id, lang, params):
    from user_jobs import housing_area
    _need_access(user_id, lang)
    text = params.get("text")
    if not isinstance(text, str) or not text.strip() or len(text) > 60:
        raise _bad(lang)
    options = housing_area.find(text.strip())
    return {"options": [{"pick": i, "label": _place_label(o)} for i, o in enumerate(options)], "text": text.strip()}


def _resolve_place(spec, lang: str, current=None):
    """Место из формы: быстрые «Potsdam»/«Berlin», текст + номер варианта из place_find или {"keep": true} (то же место, другой радиус);
    радиус из housing_area.RADII. None = «лишь Потсдам»."""
    from user_handlers import housing_card
    from user_jobs import housing_area
    if not isinstance(spec, dict):
        raise _bad(lang)
    radius = spec.get("radius", 0)
    if isinstance(radius, bool) or radius not in housing_area.RADII:
        raise _bad(lang)
    quick = spec.get("quick")
    if spec.get("keep") is True:
        base = {k: current[k] for k in ("kind", "name", "code", "kreis", "plz") if current and current.get(k)} if current else None
        if not base:
            base = housing_area.find(housing_card.QUICK_PLACES["potsdam"])[0]
        return housing_area.make(base, radius)
    if quick is not None:
        if quick not in housing_card.QUICK_PLACES:
            raise _bad(lang)
        options, text = housing_area.find(housing_card.QUICK_PLACES[quick]), quick
    else:
        text = spec.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > 60:
            raise _bad(lang)
        options = housing_area.find(text.strip())
    if not options:
        raise Refuse("place_not_found", _t("web.place_not_found", lang, text=str(text).strip()[:60]))
    pick = spec.get("pick", 0)
    if isinstance(pick, bool) or not isinstance(pick, int) or pick < 0 or pick >= len(options):
        raise _bad(lang)
    return housing_area.make(options[pick], radius)


def _options(user_id: int, area, lang: str) -> dict:
    """Что можно выбрать в форме для этого места: те же списки и правила, что на карточке фильтра в боте."""
    from user_handlers import housing_card
    from user_jobs import housing_area, housing_journey_store, housing_wbs
    hm = _hm()
    kind = _kind(area)
    covering = housing_card.covering_sources(area)
    jobcenter = None
    if housing_card.jobcenter_for(area, 1) is not None:
        jobcenter = {str(n): {"cold": housing_card.jobcenter_for(area, n)[0], "warm": housing_card.jobcenter_for(area, n)[1]}
                     for n in housing_card.JOBCENTER_CHOICES}
    household = None
    try:
        household = housing_journey_store.get_household(int(user_id))
    except Exception:
        logger.exception("h1: household")
    return {
        "area": housing_area.label(area, lang), "kind": kind, "radius": int((area or {}).get("radius_km") or 0),
        "sources": [{"key": s, "label": hm.SOURCE_LABEL.get(s, s)} for s in covering],
        "districts_welt": list(hm.IMMOWELT_DISTRICTS) if kind == "potsdam" else [],
        "districts_other": list(hm.PROPOT_DISTRICTS) if kind == "potsdam" else [],
        "bezirke": housing_area.berlin_bezirke() if kind == "berlin" else [],
        "coops": ([{"key": c["key"], "label": c["label"]} for c in hm.coop_watchdog.COOPERATIVES] if housing_area.covers_potsdam(area) else []),
        "berlin_coops": ({"count": housing_card.coop_offers.count()} if housing_area.touches_berlin(area) else None),
        "ptypes": [{"key": k, "label": _t("housing.ptype.%s" % k, lang)} for k in hm.PROPERTY_TYPE_KEYS],
        "jobcenter": jobcenter, "household": household if household in housing_card.JOBCENTER_CHOICES else None,
        "wbs": [{"v": "", "label": housing_card.wbs_label(None, lang)}] + [{"v": v, "label": housing_card.wbs_label(v, lang)} for v in housing_wbs.CHOICES],
        "radii": list(housing_area.RADII), "presets": {k: list(v) for k, v in housing_card.PRESETS.items()},
        "max_value": dict(housing_card.MAX_VALUE),
    }


@op("housing", "search_options")
def search_options(user_id, lang, params):
    _need_access(user_id, lang)
    current = None
    if params.get("id") not in (None, 0, ""):
        current = _find(user_id, _int_param(params, "id", lang), lang)[1]["criteria"].get("area")
    area = current
    if params.get("place") is not None:
        area = _resolve_place(params["place"], lang, current)
    return _options(user_id, area, lang)


@op("housing", "search_get")
def search_get(user_id, lang, params):
    _need_access(user_id, lang)
    number, search = _find(user_id, _int_param(params, "id", lang), lang)
    return {"search": _ser(number, search, lang), "options": _options(user_id, search["criteria"].get("area"), lang)}


def _number_value(raw, name: str, lang: str):
    """Число из формы, как в боте (_set_value и housing_web._number): пусто = без ограничения; больше нуля и не больше MAX_VALUE."""
    card = _card()
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    if isinstance(raw, bool):
        raise Refuse("bad_number", _t("web.bad_number", lang))
    value = _num(raw) if not isinstance(raw, str) else card._parse_number(raw)
    if value is None or value <= 0 or value > card.MAX_VALUE.get(name, 100000):
        raise Refuse("bad_number", _t("web.bad_number", lang))
    return value


def _apply(user_id: int, search_id: int, p: dict, lang: str) -> None:
    """Применяет форму так же, как housing_web.save_search + районы карточки: место, площадки, цены/комнаты/площадь/WBS, районы или округа."""
    from user_jobs import housing_area, housing_wbs
    card, hm = _card(), _hm()
    cvals = p.get("c") if p.get("c") is not None else {}
    if not isinstance(cvals, dict):
        raise _bad(lang)
    failed: List[str] = []
    number, search = _find(user_id, search_id, lang)
    # 1. место
    if p.get("place") is not None:
        current = search["criteria"].get("area")
        place = _resolve_place(p["place"], lang, current)
        if place and current and current.get("bezirke") and housing_area.is_berlin_city(place) and housing_area.is_berlin_city(current):
            place["bezirke"] = list(current["bezirke"])
        if place != current:
            failed += card.change_place(search, place)
            number, search = _find(user_id, search_id, lang)
    area = search["criteria"].get("area")
    # 2. площадки, кооперативы, типы жилья
    if p.get("sources") is not None:
        wanted = _str_list(p["sources"], lang, allowed=set(hm.AVAILABLE_SOURCE_KEYS))
        wanted = [s for s in card.covering_sources(area) if s in set(wanted)]
        if not wanted:
            raise Refuse("pick_a_portal", _t("web.pick_a_portal", lang))
        coops = _str_list(p["coops"], lang, allowed=set(hm.COOP_KEYS)) if p.get("coops") is not None else list(search.get("coops") or [])
        ptypes = (_str_list(p["ptypes"], lang, allowed=set(hm.PROPERTY_TYPE_KEYS)) if p.get("ptypes") is not None
                  else list(search["criteria"].get("property_types") or []))
        berlin = p["berlin_coops"] if isinstance(p.get("berlin_coops"), bool) else (search["criteria"].get("berlin_coops") is not False)
        failed += card.change_sources(search, wanted, coops=coops, ptypes=ptypes, berlin_coops=berlin)
        number, search = _find(user_id, search_id, lang)
    # 3. цены, комнаты, площадь, WBS, районы
    criteria = dict(search["criteria"])
    for name in NUMBER_FIELDS:
        if name in cvals:
            value = _number_value(cvals[name], name, lang)
            if name in card.PRICE_MIN_OF and value != criteria.get(name):
                criteria[card.PRICE_MIN_OF[name]] = None
            criteria[name] = value
    for low, high in (("min_rooms", "max_rooms"), ("min_area_m2", "max_area_m2")):
        if criteria.get(low) is not None and criteria.get(high) is not None and criteria[low] > criteria[high]:
            raise Refuse("min_over_max", _t("web.min_over_max", lang))
    if "wbs" in p:
        wbs = p["wbs"]
        if wbs not in (None, "", "any", *housing_wbs.CHOICES):
            raise _bad(lang)
        criteria["wbs"] = housing_wbs.normalize(wbs)
    if p.get("districts") is not None:
        chosen = _str_list(p["districts"], lang, limit=80)
        if chosen:
            if not housing_area.is_default(area):
                raise _bad(lang)
            sources_now = [s for s, _f in search["members"]]
            names = hm._canonical_districts(sources_now)
            if any(c not in names for c in chosen):
                raise _bad(lang)
            criteria["districts"] = [] if set(names) <= set(chosen) else chosen
        else:
            criteria["districts"] = []
    if p.get("bezirke") is not None:
        chosen = sorted(set(_str_list(p["bezirke"], lang, limit=40)))
        if chosen:
            if not housing_area.is_berlin_city(area):
                raise _bad(lang)
            if any(c not in housing_area.berlin_bezirke() for c in chosen):
                raise _bad(lang)
        if housing_area.is_berlin_city(area):
            new_area = dict(area or {})
            if not chosen or set(chosen) >= set(housing_area.berlin_bezirke()):
                new_area.pop("bezirke", None)
            else:
                new_area["bezirke"] = chosen
            criteria["area"] = new_area
    failed += card.apply_criteria(search, criteria)
    if failed:
        raise Refuse("save_failed", _t("web.save_failed", lang, sources=", ".join(sorted({_source_label(s) for s in failed}))))


@op("housing", "search_save")
def search_save(user_id, lang, params):
    """Создать (нет `id`) или изменить поиск. Лимиты бесплатного (один поиск, один включённый) решают функции бота."""
    from user_handlers import housing_card, housing_web
    from user_jobs import housing_journey_store
    _need_access(user_id, lang)
    uid = int(user_id)
    persons = None
    if "persons" in params:
        persons = params["persons"]
        if isinstance(persons, bool) or not isinstance(persons, int) or persons < 0 or persons > max(housing_card.JOBCENTER_CHOICES):
            raise _bad(lang)
    with _lock(uid):
        created = params.get("id") in (None, 0, "")
        if created:
            if not housing_card.can_add_search(uid):
                raise Paywall("more_searches", _t("web.second_paid", lang))
            search_id = housing_web.new_search(uid)
            if search_id is None:
                raise Refuse("save_failed", i18n.t("housing.finalize.nothing_saved", lang))
        else:
            search_id = _int_param(params, "id", lang)
            _find(uid, search_id, lang)
        try:
            _apply(uid, search_id, params, lang)
        except Refuse:
            if created:
                housing_web.delete_search(uid, search_id)       # неполный новый поиск не оставляем
            raise
        if persons is not None:
            try:
                housing_journey_store.set_household(uid, persons or None)
            except Exception:
                logger.exception("h1: could not save the household of %s", uid)
        activated = False
        if created:
            activated = bool(housing_web.start_new_search(uid, search_id))
            try:        # первый фильтр человека (journey), как после мастера; без очереди заданий отчёт «за 30 дней» не планируется
                number, fresh = _find(uid, search_id, lang)
                saved = [(source, fid, fresh["criteria"]) for source, fid in fresh["members"]]
                _hm()._after_filter_saved(SimpleNamespace(), uid, saved, search_id=search_id)
            except Exception:
                logger.exception("h1: after-saved hook")
        else:
            housing_web._record_web_edit(uid, search_id)
        number, fresh = _find(uid, search_id, lang)
        message = _t("web.only_one_active", lang) if created and not activated else ""
        return {"id": int(search_id), "created": created, "activated": activated, "message": message, "search": _ser(number, fresh, lang)}


@op("housing", "search_pause")
def search_pause(user_id, lang, params):
    from user_handlers import housing_web
    _need_access(user_id, lang)
    uid, sid = int(user_id), _int_param(params, "id", lang)
    paused = _bool_param(params, "paused", lang)
    with _lock(uid):
        _find(uid, sid, lang)
        if not housing_web.set_paused(uid, sid, paused):
            raise Paywall("more_searches", _t("web.only_one_active", lang))
        number, fresh = _find(uid, sid, lang)
        return {"search": _ser(number, fresh, lang)}


@op("housing", "search_delete")
def search_delete(user_id, lang, params):
    from user_handlers import housing_web
    _need_access(user_id, lang)
    uid, sid = int(user_id), _int_param(params, "id", lang)
    with _lock(uid):
        _find(uid, sid, lang)
        housing_web.delete_search(uid, sid)
        return {"deleted": True, **_limits(uid)}


@op("housing", "monitoring_set")
def monitoring_set(user_id, lang, params):
    """Включить/выключить мониторинг целиком: все поиски на паузу или назад. Бесплатному включается только один, как в боте (can_activate)."""
    from user_handlers import housing_card, housing_found, housing_web
    _need_access(user_id, lang)
    uid = int(user_id)
    on = _bool_param(params, "on", lang)
    with _lock(uid):
        hm = _hm()
        if on:
            if housing_found.is_found(uid):
                housing_found.resume(uid)
                housing_card.pause_extra_searches(uid)
            else:
                for search in _searches(uid):
                    if hm._search_paused(search) and housing_card.can_activate(uid, search["search_id"]):
                        housing_web.set_paused(uid, search["search_id"], False)
        else:
            for search in _searches(uid):
                if not hm._search_paused(search):
                    housing_web.set_paused(uid, search["search_id"], True)
    return _status(uid, lang)


@op("housing", "found_set")
def found_set(user_id, lang, params):
    """«🏠 Я знайшов(ла) житло»: поиски на паузу, вечерняя сводка, поздравление в чат (как в боте); и обратно «повернути пошук»."""
    from user_handlers import housing_found
    _need_access(user_id, lang)
    uid = int(user_id)
    found = _bool_param(params, "found", lang)
    with _lock(uid):
        if found and not housing_found.is_found(uid):
            housing_found.mark_found(uid)
            try:
                housing_found.congrats(bot(), uid)
            except Exception:
                logger.exception("h1: congrats")
        elif not found:
            housing_found.resume(uid)
    return _status(uid, lang)


# ------------------------------------------------------------------------------------------------------ подходящие сейчас

@op("housing", "matches")
def matches(user_id, lang, params):
    """Как «🔍 Квартири, що підходять»: подписчику список, бесплатному число (список в подписке)."""
    from user_jobs import housing_letter_store, housing_links, housing_tier
    _need_access(user_id, lang)
    uid, hm = int(user_id), _hm()

    def work():
        filters = hm.user_filters(uid)
        if not filters:
            empty = {"filters": 0, "total": 0, "items": [], "more": 0, "full": _full(uid), "locked": not _full(uid)}
            if empty["locked"]:
                empty["teasers"] = []
            return empty
        seen, found = set(), []
        for item in filters:
            source = item.get("source") or "immowelt"
            for entry in hm._current_matches(source, item):
                key = str(entry.get("listing_key") or entry.get("url") or entry.get("title") or "")
                if (source, key) in seen:
                    continue
                seen.add((source, key))
                found.append((source, key, entry))
        return {"filters": len(filters), "found": found}

    data = _deadline(work, lang)
    full = _full(uid)
    if "found" not in data:
        return data
    found = data["found"]
    if not full and not housing_tier.is_premium(uid):
        # Бесплатному (решение 28.11.2026): число, ОДНА полная самая свежая квартира и до TEASERS_SHOWN картинок остальных (только img).
        text = (_t("housing.matches.free_count", lang, n=len(found), price=housing_tier.PREMIUM_PRICE_EUR, hours=housing_tier.free_delay_hours())
                if found else _t("housing.matches.free_none", lang))
        ordered = sorted(found, key=lambda f: f[2].get("first_seen_at") if isinstance(f[2].get("first_seen_at"), datetime) else datetime.min, reverse=True)
        teasers = []
        for _source, _key, entry in ordered[1:]:
            image = _image(entry)
            if image:
                teasers.append({"img": image})
            if len(teasers) >= TEASERS_SHOWN:
                break
        items = [_match_item(uid, *ordered[0])] if ordered else []
        return {"filters": data["filters"], "total": len(found), "items": items, "more": max(0, len(found) - len(items)), "full": False,
                "locked": True, "teasers": teasers, "message": text}
    items = [_match_item(uid, source, key, entry) for source, key, entry in found[:MATCHES_SHOWN]]
    return {"filters": data["filters"], "total": len(found), "items": items, "more": max(0, len(found) - MATCHES_SHOWN), "full": True, "locked": False}


def _match_item(uid: int, source: str, key: str, entry: dict) -> dict:
    from user_jobs import housing_letter_store, housing_links
    url = entry.get("url") or entry.get("detail_url")
    if url and entry.get("listing_key"):
        url = housing_links.link_for(uid, source, str(entry["listing_key"]), str(url))
    price = _first(entry.get("price_eur"), entry.get("total_rent_eur"))
    ref = housing_letter_store.ref_for(source, str(entry["listing_key"])) if entry.get("listing_key") else None
    return {"src": source, "srcl": _source_label(source), "t": str(entry.get("title") or "Wohnung")[:160],
            "d": str(entry.get("district") or "")[:60], "r": _num(entry.get("rooms")), "m2": _num(entry.get("area_m2")),
            "p": _num(price), "img": _image(entry), "url": _https(url), "ref": ref}


# --------------------------------------------------------------------------------------------------------- «Подався», заявки

def _app_item(app: dict, lang: str) -> dict:
    from database import DBSession, HousingLetterRef
    from user_handlers import housing_tracker
    session = DBSession()
    try:
        ref = session.query(HousingLetterRef).filter(HousingLetterRef.source == app["source"], HousingLetterRef.listing_key == app["listing_key"]).first()
        ref_id = int(ref.id) if ref is not None else None
    finally:
        session.close()
    return {"id": int(app["id"]), "t": str(app.get("title") or "")[:200], "a": str(app.get("address") or "")[:200], "price": str(app.get("price_text") or "")[:40],
            "url": _https(app.get("url")), "status": app["status"], "status_text": housing_tracker._status_text(app, lang),
            "viewing": _iso(app.get("viewing_at")), "ts": _iso(app.get("created_at")), "ref": ref_id}


def _need_tracker(lang: str) -> None:
    if not _tracker_on():
        raise Refuse("tracker_off", _t("housing.track.unknown", lang))


@op("housing", "apps")
def apps(user_id, lang, params):
    """«📋 Мої заявки»: подписчику до 100, бесплатному последние FREE_LIMIT и «есть ещё N» (как show_list)."""
    from user_handlers import housing_tracker
    _need_access(user_id, lang)
    _need_tracker(lang)
    uid = int(user_id)
    premium = _full(uid)
    total = housing_tracker.count_apps(uid)
    size = 100 if premium else housing_tracker.FREE_LIMIT
    rows = housing_tracker.list_apps(uid, size)
    hidden = max(0, total - len(rows))
    message = ""
    if not premium:
        message = (_t("housing.track.free_more", lang, n=hidden) + " " if hidden else "") + _t("housing.track.free_sub", lang)
    return {"items": [_app_item(a, lang) for a in rows], "total": total, "hidden_count": hidden, "full": premium, "message": message}


@op("housing", "apply")
def apply_(user_id, lang, params):
    """«✅ Подався» под квартирой: записать заявку (то же, что ссылка ap<ref> в боте)."""
    from user_handlers import housing_tracker
    _need_access(user_id, lang)
    _need_tracker(lang)
    uid = int(user_id)
    source, key, _sent = _resolve_listing(uid, lang, params.get("ref"))
    with _lock(uid):
        result = housing_tracker.mark_applied(uid, source, key)
    if result is None:
        raise Refuse("unknown", _t("housing.track.unknown", lang))
    return {"id": int(result["id"]), "created": bool(result["created"]), "title": str(result["title"] or ""),
            "message": _t("housing.track.marked" if result["created"] else "housing.track.already", lang, title=str(result["title"] or ""))}


def _own_app(user_id: int, app_id: int, lang: str) -> dict:
    from user_handlers import housing_tracker
    app = housing_tracker.get_app(int(user_id), int(app_id))
    if app is None:
        raise Refuse("not_found", _t("housing.track.gone", lang))
    return app


@op("housing", "app_status")
def app_status(user_id, lang, params):
    from user_handlers import housing_tracker
    _need_access(user_id, lang)
    _need_tracker(lang)
    uid, app_id = int(user_id), _int_param(params, "id", lang)
    new = params.get("status")
    if new not in APP_STATUS_BY_SITE:
        raise _bad(lang)
    _own_app(uid, app_id, lang)
    housing_tracker.set_status(uid, app_id, new)
    return {"app": _app_item(_own_app(uid, app_id, lang), lang), "message": _t("housing.track.saved", lang)}


@op("housing", "app_viewing")
def app_viewing(user_id, lang, params):
    """Дата просмотра: тот же разбор текста, что в боте («завтра 15:00», «28.11 10:30»); напоминания только подписчику."""
    from user_handlers import housing_tracker
    from user_jobs import housing_dateparse
    _need_access(user_id, lang)
    _need_tracker(lang)
    uid, app_id = int(user_id), _int_param(params, "id", lang)
    text = params.get("text")
    if not isinstance(text, str) or len(text) > 80:
        raise _bad(lang)
    _own_app(uid, app_id, lang)
    moment = housing_dateparse.parse_viewing(text)
    if moment is None:
        raise Refuse("date_bad", _t("housing.track.date_bad", lang))
    if not housing_tracker.set_viewing(uid, app_id, moment):
        raise Refuse("not_found", _t("housing.track.gone", lang))
    note = _t("housing.track.reminders_on" if _full(uid) else "housing.track.reminders_free", lang)
    return {"app": _app_item(_own_app(uid, app_id, lang), lang),
            "message": _t("housing.track.date_saved", lang, when=housing_tracker._when(moment)) + " " + note}


@op("housing", "app_found")
def app_found(user_id, lang, params):
    """«🏠 Отримав(ла) квартиру» в заявке: статус, пауза поисков и поздравление, как в боте."""
    from user_handlers import housing_tracker
    _need_access(user_id, lang)
    _need_tracker(lang)
    uid, app_id = int(user_id), _int_param(params, "id", lang)
    _own_app(uid, app_id, lang)
    with _lock(uid):
        housing_tracker.set_status(uid, app_id, "found")
        if not housing_tracker._call_mark_found(uid, bot()):
            try:
                bot().send_message(chat_id=uid, text=i18n.t("housing.track.found_congrats", lang))
            except Exception:
                logger.exception("h1: found congrats")
    return {"app": _app_item(_own_app(uid, app_id, lang), lang), "message": _t("housing.track.found_congrats", lang)}


@op("housing", "app_remove")
def app_remove(user_id, lang, params):
    from user_handlers import housing_tracker
    _need_access(user_id, lang)
    _need_tracker(lang)
    uid, app_id = int(user_id), _int_param(params, "id", lang)
    _own_app(uid, app_id, lang)
    housing_tracker.hide_app(uid, app_id)
    return {"removed": True, "message": _t("housing.track.removed", lang)}


# ---------------------------------------------------------------------------------------------------------- статистика

@op("housing", "stats")
def stats(user_id, lang, params):
    """Мои отправки по дням и площадкам. Подписчику 7 или 30 дней, бесплатному 7 (как в старом кабинете)."""
    from user_handlers import housing_web
    _need_access(user_id, lang)
    full = _full(user_id)
    days = params.get("days", 7)
    if isinstance(days, bool) or days not in (7, 30):
        raise _bad(lang)
    if not full:
        if days > housing_web.FREE_STATS_DAYS:      # 30 дней только подписчику: единый замок вместо тихой подмены на 7
            raise Paywall("stats_full")
        days = housing_web.FREE_STATS_DAYS
    rows = housing_web._sends(int(user_id), days=days)
    by_day, by_source = {}, {}
    for row in rows:
        local = pytz.utc.localize(row["sent_at"]).astimezone(BERLIN_TZ).date()
        by_day[local] = by_day.get(local, 0) + 1
        by_source[row["source"]] = by_source.get(row["source"], 0) + 1
    today = datetime.now(BERLIN_TZ).date()
    series = [{"d": (today - timedelta(days=i)).isoformat(), "n": by_day.get(today - timedelta(days=i), 0)} for i in range(days - 1, -1, -1)]
    sources = [{"src": s, "label": _source_label(s), "n": n} for s, n in sorted(by_source.items(), key=lambda kv: -kv[1])]
    return {"days": days, "total": len(rows), "series": series, "sources": sources, "full": full}


_AREA_BINS = (0, 30, 40, 50, 60, 70, 80, 100, 120, 150, float("inf"))
_PRICE_BINS = (0, 500, 700, 900, 1100, 1300, 1500, 1800, 2200, float("inf"))
_ROOM_BINS = (0.5, 1.5, 2.5, 3.5, 4.5, float("inf"))


def _bins(values, edges):
    counts = [0] * (len(edges) - 1)
    for value in values:
        if value is None:
            continue
        for i in range(len(edges) - 1):
            if edges[i] <= value < edges[i + 1]:
                counts[i] += 1
                break
    return counts


def _edge_labels(edges):
    out = []
    for i in range(len(edges) - 1):
        lo, hi = edges[i], edges[i + 1]
        out.append("%d+" % lo if hi == float("inf") else "%d–%d" % (lo, hi))
    return out


@op("housing", "market")
def market(user_id, lang, params):
    """Как «📊 Статистика» в боте (всё найденное по рынку за неделю или месяц): числа по комнатам, цене и площади; рисует сайт."""
    from user_jobs import housing_stats_store
    _need_access(user_id, lang)
    period = params.get("period", "week")
    if period not in ("week", "month"):
        raise _bad(lang)
    days = _hm()._STATS_PERIOD_DAYS[period]
    rows = housing_stats_store.fetch_listings_since(datetime.utcnow() - timedelta(days=days))
    return {
        "period": period, "days": days, "count": len(rows),
        "rooms": {"labels": ["1", "2", "3", "4", "5+"], "counts": _bins([r[0] for r in rows], _ROOM_BINS)},
        "price": {"labels": _edge_labels(_PRICE_BINS), "counts": _bins([r[2] for r in rows], _PRICE_BINS)},
        "area": {"labels": _edge_labels(_AREA_BINS), "counts": _bins([r[1] for r in rows], _AREA_BINS)},
    }


# --------------------------------------------------------------------------------------------------------- уведомления

def _notify_state(uid: int) -> dict:
    from user_jobs import housing_tier, user_settings_store
    hm = _hm()
    prefs = dict(hm._notification_prefs(uid))
    premium = housing_tier.is_premium(uid)
    return {"quiet": bool(housing_tier.wants_quiet_night(uid)) if premium else False, "quiet_label": housing_tier.quiet_hours_label(),
            "digest": "daily" if prefs.get("digest_mode") == "daily" else "instant",
            "news": bool(user_settings_store.get_news_subscribed(uid)), "premium": bool(premium)}


@op("housing", "notify_get")
def notify_get(user_id, lang, params):
    _need_access(user_id, lang)
    return _notify_state(int(user_id))


@op("housing", "notify_set")
def notify_set(user_id, lang, params):
    """Тихая ночь (только подписка), режим доставки «сразу/раз в день», новости бота: те же функции, что экран «🔔 Сповіщення» в боте."""
    from user_jobs import housing_tier, user_settings_store
    _need_access(user_id, lang)
    uid, hm = int(user_id), _hm()
    if "quiet" in params:
        enabled = _bool_param(params, "quiet", lang)
        if not housing_tier.is_premium(uid):
            raise Paywall("quiet_night", _t("housing.toast.quiet_locked", lang, price=housing_tier.PREMIUM_PRICE_EUR))
        user_settings_store.set_quiet_night_off(uid, not enabled)
    if "digest" in params:
        mode = params["digest"]
        if mode not in ("instant", "daily"):
            raise _bad(lang)
        try:
            hm._set_notification_prefs(uid, digest_mode=mode)
        except Exception:
            logger.exception("h1: digest mode")
            raise Refuse("save_failed", _t("housing.toast.settings_update_failed", lang))
    if "news" in params:
        user_settings_store.set_news_subscribed(uid, _bool_param(params, "news", lang))
    return _notify_state(uid)


@op("housing", "letters")
def letters(user_id, lang, params):
    """Мои письма арендодателям (только чтение, подписчику): архив, как «История» старого кабинета. Составление письма: пакет H4."""
    from user_jobs import housing_letter_store
    _need_access(user_id, lang)
    full = _full(user_id)       # свои письма видны и бесплатному (там максимум одно пробное, 28.11.2026)
    items = []
    for item in housing_letter_store.list_letters(int(user_id)):
        ref = housing_letter_store.ref_for(item["source"], item["listing_key"]) if item.get("source") and item.get("listing_key") else None
        items.append({"ref": ref, "t": str(item.get("title") or item.get("listing_key") or "")[:200], "url": _https(item.get("url")),
                      "ts": _iso(item.get("created_at")), "text": str(item.get("letter") or "")[:6000]})
    return {"items": items, "full": full}


from site_bridge import housing_extra  # noqa: E402,F401  (пакет H6: папка заявника, «запроси друга», справка; операции той же области)

from site_bridge import housing_widen  # noqa: E402,F401  (fix8: «Розширити фільтр» на сайте, операции search_report/search_relax той же области)
