"""Мост: письмо арендодателю (пакет H4). Файл принадлежит пакету H4; остальные сюда не пишут.

Всё делают СУЩЕСТВУЮЩИЕ функции бота: профиль и ссылки на квартиры `housing_letter_store`, подписка `housing_tier.is_premium`, квартира, текст запроса,
вызов модели и суточный лимит `housing_letter` (`_listing`, `_prompt`, `_call_ai`, `_over_limit`, `DAILY_LIMIT`), история `housing_letter_store.list_letters/log_letter`.
Новое только здесь: перевод в JSON, проверка параметров страницы и запуск генерации в фоне (модель отвечает 3-10 с, а мост ждёт 8 с):
  start запускает поток, status отдаёт состояние. Результат в Telegram не отправляется (это делает бот только в своём диалоге).

Операции (все need_user, адрес POST /int/letter/<имя>), права как в боте: письма только подписчикам (иначе отказ "locked" с текстом бота):
  overview                    — подписка, анкета (поля и значения), сколько писем сегодня и лимит, число писем в истории
  profile_save {поля...}      — сохранить анкету (те же шесть полей; обязательные: имя, семья, доход, дата въезда)
  listing {ref}               — квартира по ссылке ref (housing_letter_ref), прежнее письмо на неё, если было
  start {ref, variant}        — запустить генерацию (считается в суточный лимит бота); один незаконченный запуск на человека
  status {job}                — состояние запуска: running | done (текст письма) | failed
  history                     — мои письма (последние 50)
"""

import html
import logging
import re
import threading
import time
import uuid
from datetime import date, datetime

import i18n
from site_bridge import Paywall, op, Refuse

logger = logging.getLogger(__name__)

JOB_TTL = 3600           # сколько секунд помнить результат запуска
JOB_RUNNING_MAX = 150    # запуск старше этого считается зависшим (модель ждём до 90 с)
HISTORY_SHOWN = 50
MAX_FIELD = 500          # как в housing_letter_store.save_profile

_jobs = {}               # job id -> {"user", "ref", "state", "letter", "started", "listing", "variant"}
_lock = threading.Lock()


def _plain(text) -> str:
    """Текст бота (HTML для Telegram) -> обычный текст для страницы."""
    return html.unescape(re.sub(r"<[^>]+>", "", str(text or ""))).strip()


def _t(key, lang, **kw):
    return i18n.t("letter.site." + key, lang, **kw)


def _store():
    from user_jobs import housing_letter_store

    return housing_letter_store


def _hl():
    from user_handlers import housing_letter

    return housing_letter


def _premium(user_id: int) -> bool:
    from user_jobs import housing_tier

    return bool(housing_tier.is_premium(int(user_id)))


def _locked_text(lang: str) -> str:
    from user_jobs import housing_tier

    return _plain(i18n.t("housing.letter.locked", lang, price=housing_tier.PREMIUM_PRICE_EUR))


def _require_premium(user_id: int, lang: str) -> None:
    """Письма: подписчик или бесплатный с нетронутой пробой (одно письмо навсегда). Иначе единый замок {error:"paywall", feature:"letter"}."""
    if not _store().can_write(int(user_id)):
        raise Paywall("letter", _locked_text(lang))


def _trial_fields(user_id: int) -> dict:
    """free_left (0/1) и locked (письма закрыты подпиской; при free_left=1 бесплатному можно написать одно) для overview и listing."""
    return {"free_left": int(_store().free_left(int(user_id))), "locked": not _premium(user_id)}


def _ref(params) -> int:
    raw = params.get("ref")
    if isinstance(raw, bool) or not isinstance(raw, (int, str)):
        raise Refuse("bad_request", "")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise Refuse("bad_request", "")
    if not 0 < value < 2 ** 31:
        raise Refuse("bad_request", "")
    return value


def _bad(lang):
    return Refuse("bad_request", _t("err.bad", lang))


def _iso(value) -> str:
    try:
        return value.isoformat(timespec="seconds") + "Z"
    except Exception:
        return ""


def _used_today(user_id: int) -> int:
    return int(_hl()._daily.get((int(user_id), date.today().isoformat()), 0))


def _limit_block(user_id: int) -> dict:
    limit = int(_hl().DAILY_LIMIT)
    used = _used_today(user_id)
    return {"limit": limit, "used": min(used, limit), "left": max(limit - used, 0)}


def _fields() -> list:
    s = _store()
    return [{"key": key, "optional": key in s.OPTIONAL_FIELDS, "max": MAX_FIELD} for key in s.PROFILE_FIELDS]


def _flat(listing: dict) -> dict:
    hl = _hl()
    url = str(listing.get("detail_url") or "")
    title = str(listing.get("title") or "Wohnung")
    return {
        "label": hl._listing_label(listing),
        "title": title,
        "address": str(listing.get("address") or listing.get("district") or ""),
        "rooms": hl._num(listing.get("rooms")),
        "area": hl._num(listing.get("area_m2")),
        "price": hl._num(listing.get("price_eur")),
        "source": hl.SOURCE_LABEL.get(str(listing.get("source")), str(listing.get("source") or "")),
        "url": url if url.startswith("https://") else "",
        "subject": ("Anfrage zur Wohnung: " + title)[:120],
    }


def _free_may_see(user_id: int, found: dict) -> bool:
    """Бесплатному (пробное письмо) доступна квартира, которую он получил, или уже созревшая для бесплатного тарифа (старше задержки):
    раньше подписчиков по номеру ничего не открываем."""
    from database import DBSession, HousingSendLog
    from user_jobs import housing_tier

    session = DBSession()
    try:
        if session.query(HousingSendLog.id).filter(HousingSendLog.user_id == int(user_id), HousingSendLog.source == found["source"],
                                                   HousingSendLog.listing_key == found["listing_key"]).first():
            return True
    finally:
        session.close()
    from site_bridge import housing

    row = housing._load_listings([(found["source"], found["listing_key"])]).get((found["source"], found["listing_key"])) or {}
    seen = row.get("first_seen_at")
    return isinstance(seen, datetime) and datetime.utcnow() - seen >= housing_tier.FREE_DELAY


def _resolve(ref: int, lang: str, user_id: int = 0):
    s = _store()
    found = s.resolve_ref(ref)
    listing = _hl()._listing(found["source"], found["listing_key"]) if found else None
    if listing is None or (user_id and not _premium(user_id) and not _free_may_see(user_id, found)):
        raise Refuse("not_found", i18n.t("housing.letter.unknown", lang))
    return found, listing


def _last_for(user_id: int, source: str, key: str):
    for item in _store().list_letters(user_id, HISTORY_SHOWN):
        if item["source"] == source and item["listing_key"] == key:
            return {"text": item["letter"], "ts": _iso(item["created_at"])}
    return None


def _refs_for(items) -> dict:
    """ref по (source, listing_key) для истории: только чтение, новых ссылок не создаём."""
    from database import DBSession, HousingLetterRef

    out = {}
    if not items:
        return out
    session = DBSession()
    try:
        keys = {i["listing_key"] for i in items}
        for row in session.query(HousingLetterRef).filter(HousingLetterRef.listing_key.in_(keys)).all():
            out[(row.source, row.listing_key)] = int(row.id)
    finally:
        session.close()
    return out


def _mappe_on() -> bool:
    """Включена ли «Папка заявника» (пакет H6): страница письма предлагает «Папка під цю квартиру» только если да."""
    try:
        from user_handlers import housing_mappe

        return bool(housing_mappe.ENABLED)
    except Exception:
        return False


def _profile_out(user_id: int):
    return _store().get_profile(int(user_id))


def _profile_view(user_id: int):
    """Анкета для формы: пустые поля дополнены из анкеты «Папки заявника» (ничего не записывается)."""
    from user_jobs import housing_profile_sync

    return housing_profile_sync.letter_view(int(user_id))


# ---------------------------------------------------------------- операции

@op("letter", "overview")
def overview(user_id, lang, params):
    premium = _premium(user_id)
    out = {"premium": premium, "fields": _fields(), "mappe": _mappe_on(), **_trial_fields(user_id)}
    if not premium:
        out["locked_text"] = _locked_text(lang)
        if not out["free_left"]:
            return out
    out["profile"] = _profile_view(user_id)
    out.update(_limit_block(user_id))
    out["count"] = len(_store().list_letters(user_id, HISTORY_SHOWN))
    return out


@op("letter", "profile_save")
def profile_save(user_id, lang, params):
    _require_premium(user_id, lang)
    s = _store()
    answers = {}
    for key in s.PROFILE_FIELDS:
        value = params.get(key, "")
        if value is None:
            value = ""
        if not isinstance(value, str) or len(value) > 4 * MAX_FIELD:
            raise _bad(lang)
        value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", value).strip()
        if len(value) > MAX_FIELD:
            raise Refuse("too_long", _t("err.long", lang, n=MAX_FIELD))
        if not value and key not in s.OPTIONAL_FIELDS:
            raise Refuse("required", _t("err.required", lang))
        answers[key] = value
    s.save_profile(user_id, answers)
    return {"profile": _profile_out(user_id), "message": i18n.t("housing.letter.profile_saved", lang)}


@op("letter", "listing")
def listing(user_id, lang, params):
    _require_premium(user_id, lang)
    ref = _ref(params)
    found, row = _resolve(ref, lang, user_id)
    return {
        "ref": ref, "flat": _flat(row), "profile": _profile_view(user_id), "fields": _fields(),
        "last": _last_for(user_id, found["source"], found["listing_key"]), **_limit_block(user_id),
        "mappe": _mappe_on(), **_trial_fields(user_id),
    }


def _purge(now: float) -> None:
    for jid in [j for j, v in _jobs.items() if now - v["started"] > JOB_TTL]:
        _jobs.pop(jid, None)


def _work(jid: str, user_id: int, found: dict, row: dict, profile: dict, variant: bool, mode: str = "sub") -> None:
    hl, s = _hl(), _store()
    letter = ""
    try:
        letter = hl._call_ai(hl._prompt(row, profile, variant))
    except Exception:
        logger.exception("site letter generation failed for %s", user_id)
        letter = ""
    ok = bool(letter) and letter != "RATE_LIMIT"
    s.finish_letter(user_id, mode, ok)        # пробное письмо тратится только при успешной генерации (общая логика с чатом бота)
    if ok:
        try:
            s.log_letter(user_id, found["source"], found["listing_key"], hl._listing_label(row), str(row.get("detail_url") or ""), letter)
        except Exception:
            logger.exception("site letter: could not log the letter for %s", user_id)
    with _lock:
        job = _jobs.get(jid)
        if job is not None:
            job["state"] = "done" if ok else "failed"
            job["letter"] = letter if ok else ""


@op("letter", "start")
def start(user_id, lang, params):
    _require_premium(user_id, lang)
    ref = _ref(params)
    variant = params.get("variant", False)
    if not isinstance(variant, bool):
        raise _bad(lang)
    found, row = _resolve(ref, lang, user_id)
    profile = _profile_out(user_id)
    if not _store().profile_complete(profile):          # «Про мене» дополнена из папки заявника на экране: сохраняем, чтобы чат и сайт видели одно и то же
        shown = _profile_view(user_id)
        if _store().profile_complete(shown):
            _store().save_profile(user_id, shown, sync=False)
            profile = _profile_out(user_id)
    if not profile or any(not profile.get(k) for k in _store().PROFILE_FIELDS if k not in _store().OPTIONAL_FIELDS):
        raise Refuse("no_profile", _t("err.noprofile", lang))
    now = time.time()
    with _lock:
        _purge(now)
        for jid, job in _jobs.items():     # уже идёт запуск этого человека: не плодим вызовов модели
            if job["user"] == user_id and job["state"] == "running" and now - job["started"] < JOB_RUNNING_MAX:
                return {"job": jid, "state": "running", "again": True}
        if _hl()._over_limit(user_id):
            raise Refuse("limit", i18n.t("housing.letter.limit", lang, n=_hl().DAILY_LIMIT))
        mode = _store().begin_letter(user_id)      # "sub" | "trial" (проба занята под этот запуск) | "busy" | None (замок)
        if mode is None:
            raise Paywall("letter", _locked_text(lang))
        if mode == "busy":
            raise Refuse("busy", i18n.t("housing.letter.trial_busy", lang))
        jid = uuid.uuid4().hex[:20]
        _jobs[jid] = {"user": user_id, "ref": ref, "state": "running", "letter": "", "started": now, "variant": variant}
    threading.Thread(target=_work, args=(jid, user_id, found, row, profile, variant, mode), name="site-letter-%s" % user_id, daemon=True).start()
    return {"job": jid, "state": "running", "again": False}


@op("letter", "status")
def status(user_id, lang, params):
    jid = params.get("job")
    if not isinstance(jid, str) or not re.fullmatch(r"[0-9a-f]{20}", jid):
        raise _bad(lang)
    with _lock:
        job = dict(_jobs.get(jid) or {})
    if not job or job["user"] != user_id:
        raise Refuse("not_found", _t("err.nojob", lang))
    state = job["state"]
    if state == "running" and time.time() - job["started"] > JOB_RUNNING_MAX:
        state = "failed"
    out = {"job": jid, "state": state, "ref": job["ref"]}
    if state == "done":
        out["letter"] = job["letter"]
        try:
            _found, row = _resolve(job["ref"], lang)
            out["flat"] = _flat(row)
        except Refuse:
            pass
        out.update(_limit_block(user_id))
    elif state == "failed":
        out["message"] = i18n.t("housing.letter.failed", lang)
    return out


@op("letter", "history")
def history(user_id, lang, params):
    # свои письма видны и бесплатному (там максимум пробное): замка нет, чужих писем не бывает (список по user_id)
    items = _store().list_letters(user_id, HISTORY_SHOWN)
    refs = _refs_for(items)
    out = []
    for it in items:
        url = str(it.get("url") or "")
        out.append({
            "title": str(it.get("title") or ""), "url": url if url.startswith("https://") else "", "ts": _iso(it.get("created_at")),
            "text": str(it.get("letter") or ""), "ref": refs.get((it["source"], it["listing_key"])),
        })
    return {"items": out}
