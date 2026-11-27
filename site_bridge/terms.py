"""Мост: термины ДП «Документ» и посольство в Берліні (пакет H3). Файл принадлежит пакету H3; остальные сюда не пишут.

Операции вызывают СУЩЕСТВУЮЩИЕ функции бота (`equeue_monitor`, `econsul_monitor`): подписка, отписка, статус, история находок, «Я записався».
Права и тексты те же, что в боте. Новое только здесь: перевод результата в JSON и проверка параметров от страницы (они не доверенные).
Уведомления о найденном термине по-прежнему шлёт бот в Telegram; этот файл ничего людям не отправляет, кроме того, что делает сам бот
при «Я записався» (поздравление человеку и строка админу, `support.booked`).

Операции (все need_user, адрес POST /int/terms/<имя>):
  overview                                      — оба мониторинга одним запросом (для страницы-выбора)
  equeue_state / equeue_sub {on} / equeue_booked — ДП «Документ»
  econsul_state / econsul_sub {codes|all} / econsul_unsub {code|all} / econsul_booked {codes|all} — посольство (e-Consul)
"""

import html
import logging
import re
from types import SimpleNamespace

import i18n
from site_bridge import op, Refuse

logger = logging.getLogger(__name__)

HISTORY_SHOWN = 10          # сколько последних находок отдаём странице (в боте меню показывает 3)
MAX_CODES = 40              # сколько кодов услуг принимаем за раз
CODE_RE = re.compile(r"^[^\s\x00-\x1f\x7f][^\x00-\x1f\x7f]{0,62}$")


# ---------------------------------------------------------------- общее

def _plain(text) -> str:
    """Текст бота (HTML для Telegram) -> обычный текст для страницы."""
    return html.unescape(re.sub(r"<[^>]+>", "", str(text or ""))).strip()


def _bad(lang):
    return Refuse("bad_request", i18n.t("terms.err.bad", lang))


def _who(user_id: int):
    """Объект «пользователь» для функций бота, которые берут user.id/username/full_name. Имя берём из того, что бот уже знает
    (прежняя подписка, таблица участников чата); сайт сам имён не присылает."""
    from database import DBSession, EqueueSubscription, User

    username, name = "", ""
    session = DBSession()
    try:
        row = (session.query(EqueueSubscription).filter(EqueueSubscription.user_id == user_id)
               .order_by(EqueueSubscription.updated_at.desc()).first())
        if row is not None:
            username, name = row.username or "", row.display_name or ""
        if not username or not name:
            known = session.query(User).filter(User.id == user_id).first()
            if known is not None:
                username = username or (known.username or "")
                name = name or (known.fullname or "")
    finally:
        session.close()
    return SimpleNamespace(id=int(user_id), username=username, full_name=name, first_name=name, last_name="")


def _booked_query(user):
    """Подставной «callback query» для support.booked: кнопок на странице нет, остальное делает сама функция бота."""
    return SimpleNamespace(from_user=user, answer=lambda *a, **k: None, edit_message_reply_markup=lambda *a, **k: None)


def _send_booked(user, title: str, menu_callback: str) -> bool:
    """Поздравление человеку и строка админу ровно как после кнопки «Я записався» под уведомлением. Ошибка отправки не ломает отписку."""
    from user_handlers import support

    try:
        from site_bridge import bot

        support.booked(_booked_query(user), bot(), title, menu_callback)
        return True
    except Exception:
        logger.exception("terms: could not send the booked message to %s", user.id)
        return False


def _berlin(value) -> str:
    from user_handlers import equeue_monitor as eq

    return eq._format_berlin_time(value)


# ---------------------------------------------------------------- ДП «Документ»

def _eq_check(user_id: int, lang: str):
    from user_handlers import equeue_monitor as eq

    if not eq.is_allowed(user_id):
        raise Refuse("not_allowed", i18n.t("equeue.not_allowed", lang))
    return eq


def _eq_status_code():
    """Код последней проверки (как выбирает бот: общая запись сервиса, иначе последняя подписка) и время."""
    from database import DBSession, EqueueStatus, EqueueSubscription
    from user_handlers import equeue_monitor as eq

    session = DBSession()
    try:
        row = session.query(EqueueStatus).filter(EqueueStatus.service == eq.SERVICE_KEY).first()
        if row is None or row.last_checked_at is None:
            row = (session.query(EqueueSubscription)
                   .filter(EqueueSubscription.service == eq.SERVICE_KEY, EqueueSubscription.last_checked_at.isnot(None))
                   .order_by(EqueueSubscription.last_checked_at.desc()).first())
        if not row or row.last_checked_at is None:
            return "never", None
        status = row.last_status or "unknown"
        return (status if status in ("available", "none", "blocked") else "other"), eq._format_berlin_time(row.last_checked_at)
    finally:
        session.close()


def _eq_state(user_id: int, lang: str) -> dict:
    eq = _eq_check(user_id, lang)
    active = eq._is_active(user_id)
    code, checked = _eq_status_code()
    found = eq.recent_sightings(HISTORY_SHOWN)
    intro = _plain(i18n.t("equeue.menu.text", lang, title="", status=i18n.t("equeue.subscription.on" if active else "equeue.subscription.off", lang)))
    # «🛂 \n\nБот може … місця.\n\nСтатус підписки: …»: берём среднюю часть (описание), статус страница показывает сама
    parts = [p.strip() for p in intro.split("\n\n") if p.strip()]
    intro = parts[1] if len(parts) >= 2 else intro
    ago = None
    if found:
        ago = _plain(i18n.t("equeue.sightings.ago", lang, hours=int((eq.utc_now() - found[0]).total_seconds() // 3600)))
    return {
        "title": eq.SERVICE_TITLE,
        "intro": intro,
        "subscribed": bool(active),
        "status": {"code": code, "checked": checked, "text": _plain(eq._latest_status_text(user_id, lang))},
        "sightings": [_berlin(value) for value in found],
        "sightings_title": _plain(i18n.t("equeue.sightings.title" if found else "equeue.sightings.none", lang)),
        "sightings_ago": ago,
        "open_url": eq.SERVICE_URL,
    }


@op("terms", "equeue_state")
def equeue_state(user_id, lang, params):
    return _eq_state(user_id, lang)


@op("terms", "equeue_sub")
def equeue_sub(user_id, lang, params):
    """Включить/выключить подписку на проверку ДП «Документ» (как «🔔 Підписатися»/«🔕 Відписатися» в боте)."""
    eq = _eq_check(user_id, lang)
    on = params.get("on")
    if not isinstance(on, bool):
        raise _bad(lang)
    if on:
        eq._upsert_subscription(_who(user_id))
    else:
        eq._deactivate_subscription(user_id)
    data = _eq_state(user_id, lang)
    data["message"] = _plain(i18n.t("equeue.prefix.subscribed" if on else "equeue.prefix.unsubscribed", lang))
    return data


@op("terms", "equeue_booked")
def equeue_booked(user_id, lang, params):
    """«✅ Я записався»: подписка выключается, человеку и админу уходит то же, что после кнопки под уведомлением."""
    eq = _eq_check(user_id, lang)
    if not eq._is_active(user_id):
        raise Refuse("not_subscribed", i18n.t("terms.err.not_subscribed", lang))
    eq._deactivate_subscription(user_id)
    sent = _send_booked(_who(user_id), i18n.t("anon.btn.equeue", lang), "equeue:menu")
    data = _eq_state(user_id, lang)
    data["booked"] = True
    data["tg_sent"] = sent
    data["message"] = _plain(i18n.t("support.booked_plain", lang, service=i18n.t("anon.btn.equeue", lang)))
    return data


# ---------------------------------------------------------------- посольство (e-Consul)

def _ec_check(user_id: int, lang: str):
    from user_handlers import econsul_monitor as ec

    if not ec.is_allowed(user_id):
        raise Refuse("not_allowed", i18n.t("econsul.not_allowed", lang))
    return ec


def _ec_status(state, lang: str, ec):
    """Код и текст статуса проверки: те же условия, что в `econsul_monitor._status_text` (текст берём у бота)."""
    if state is None or state.last_checked_at is None:
        code = "never"
    elif state.last_status in ec.BROKEN_STATUSES:
        code = "broken"
    elif state.last_ok_at is None or ec.utc_now() - state.last_ok_at > ec.STALE_AFTER:
        code = "stale"
    else:
        code = "ok"
    return {"code": code, "text": _plain(ec._status_text(state, lang))}


def _day_text(nearest, ec):
    """'2026-12-01 09:20' -> '01.12.2026 09:20' (как в боте)."""
    if not nearest:
        return None
    date, _, time = str(nearest).partition(" ")
    return f"{ec._date_dot(date)} {time}".strip()


def _ec_intro(lang: str) -> str:
    """Описание из меню бота без заголовка («🇺🇦 <название>», первая часть до пустой строки)."""
    parts = [p.strip() for p in _plain(i18n.t("econsul.menu.text", lang, institution="")).split("\n\n") if p.strip()]
    return "\n\n".join(parts[1:])


def _ec_state(user_id: int, lang: str) -> dict:
    from database import DBSession, EconsulService, EconsulState, EqueueAvailableSighting

    ec = _ec_check(user_id, lang)
    session = DBSession()
    try:
        state = session.query(EconsulState).filter(EconsulState.source == ec.SOURCE).first()
        codes = ec._active_codes(ec._user_subscriptions(session, user_id))
        all_services = {row.code: row for row in session.query(EconsulService).all()}
        served = ec._served_services(session)
        institution = (state.institution_name if state else None) or i18n.t("econsul.default_institution", lang)

        subs = []
        if "*" not in codes:
            for code in sorted(codes, key=lambda item: (all_services[item].name if item in all_services else item)):
                svc = all_services.get(code)
                subs.append({
                    "code": code, "name": svc.name if svc else code,
                    "free": int(svc.free_count or 0) if svc else 0,
                    "nearest": _day_text(svc.nearest, ec) if svc and svc.free_count else None,
                })
        services = []
        for svc in served:
            services.append({
                "code": svc.code, "name": svc.name, "minutes": svc.minutes,
                "free": int(svc.free_count or 0), "nearest": _day_text(svc.nearest, ec) if svc.free_count else None,
                "subscribed": ("*" in codes) or svc.code in codes,
            })
        rows = (session.query(EqueueAvailableSighting).filter(EqueueAvailableSighting.service == ec.SOURCE)
                .order_by(EqueueAvailableSighting.found_at.desc()).limit(HISTORY_SHOWN).all())
        sightings = [{"at": ec._berlin(row.found_at), "what": row.reason or ""} for row in rows]
        lifetime = ec.typical_lifetime(session)
        life_text = _plain(i18n.t("econsul.lifetime", lang, duration=ec._duration_text(lifetime, lang))) if lifetime is not None else None
        return {
            "institution": institution,
            "intro": _ec_intro(lang),
            "status": _ec_status(state, lang, ec),
            "all": "*" in codes,
            "subscriptions": subs,
            "services": services,
            "has_services": bool(served),
            "can_add": ("*" not in codes) and any(not item["subscribed"] for item in services),
            "sightings": sightings,
            "sightings_title": _plain(i18n.t("econsul.sightings.title" if sightings else "econsul.sightings.none", lang)),
            "lifetime": life_text,
            "free_title": _plain(i18n.t("econsul.free.title" if any(s["free"] for s in services) else "econsul.free.nothing", lang)),
            "book_url": ec.BOOKING_URL,
        }
    finally:
        session.close()


def _codes_param(params, lang):
    codes = params.get("codes")
    if not isinstance(codes, list) or not codes or len(codes) > MAX_CODES:
        raise _bad(lang)
    out = []
    for code in codes:
        if not isinstance(code, str) or not CODE_RE.match(code) or code == "*":
            raise _bad(lang)
        if code not in out:
            out.append(code)
    return out


def _flag(params, name, lang):
    value = params.get(name)
    if value is None:
        return False
    if not isinstance(value, bool):
        raise _bad(lang)
    return value


@op("terms", "econsul_state")
def econsul_state(user_id, lang, params):
    return _ec_state(user_id, lang)


@op("terms", "econsul_sub")
def econsul_sub(user_id, lang, params):
    """Подписаться: {"codes": [коды услуг]} или {"all": true} («🔔 Усі послуги одразу»). Как кнопки выбора в боте."""
    from database import DBSession

    ec = _ec_check(user_id, lang)
    want_all = _flag(params, "all", lang)
    if want_all == ("codes" in params):          # нужно ровно одно из двух
        raise _bad(lang)
    codes = {"*"} if want_all else set(_codes_param(params, lang))
    session = DBSession()
    try:
        active = ec._active_codes(ec._user_subscriptions(session, user_id))
        if "*" in active:
            raise Refuse("already_all", _plain(i18n.t("econsul.pick.already_all", lang)))
        free = {svc.code for svc in ec._served_services(session) if svc.code not in active}
        if not free:
            raise Refuse("nothing_left", _plain(i18n.t("econsul.pick.nothing_left", lang)))
        if not want_all and not codes <= free:      # неизвестная, не обслуживаемая или уже включённая услуга
            raise Refuse("not_found", i18n.t("terms.err.not_found", lang))
        ec._subscribe(session, _who(user_id), codes)
        session.commit()
    finally:
        session.close()
    data = _ec_state(user_id, lang)
    data["message"] = _plain(i18n.t("econsul.prefix.subscribed", lang, names=ec._service_names(codes, lang)))
    return data


@op("terms", "econsul_unsub")
def econsul_unsub(user_id, lang, params):
    """Отписаться: {"code": "<код>"} (❌ у услуги; "*" = подписка «все услуги») или {"all": true} («🗑 Відписатися від усіх»)."""
    from database import DBSession

    ec = _ec_check(user_id, lang)
    everything = _flag(params, "all", lang)
    code = params.get("code")
    if everything == (code is not None):
        raise _bad(lang)
    if not everything and (not isinstance(code, str) or not (code == "*" or CODE_RE.match(code))):
        raise _bad(lang)
    session = DBSession()
    try:
        active = ec._active_codes(ec._user_subscriptions(session, user_id))
        if not active or (not everything and code not in active):
            raise Refuse("not_found", i18n.t("terms.err.not_found", lang))
        ec._unsubscribe(session, _who(user_id), None if everything else code)
        session.commit()
        left = ec._active_codes(ec._user_subscriptions(session, user_id))
    finally:
        session.close()
    data = _ec_state(user_id, lang)
    data["message"] = _plain(i18n.t("econsul.prefix.unsubscribed_all" if not left else "econsul.toast.unsubscribed", lang))
    return data


@op("terms", "econsul_booked")
def econsul_booked(user_id, lang, params):
    """«✅ Я записався»: {"all": true} отписывает от всего, {"codes": [...]} только от названных услуг (как «Лише від …» в боте)."""
    from database import DBSession

    ec = _ec_check(user_id, lang)
    everything = _flag(params, "all", lang)
    if everything == ("codes" in params):
        raise _bad(lang)
    codes = set() if everything else set(_codes_param(params, lang))
    session = DBSession()
    try:
        active = ec._active_codes(ec._user_subscriptions(session, user_id))
        if not active:
            raise Refuse("not_subscribed", i18n.t("terms.err.not_subscribed", lang))
        if not everything:
            served = {svc.code for svc in ec._served_services(session)}
            allowed = (served | active) if "*" in active else active
            if not codes <= allowed:
                raise Refuse("not_found", i18n.t("terms.err.not_found", lang))
        user = _who(user_id)
        if everything:
            ec._unsubscribe(session, user, None)
        else:
            ec._unsubscribe_codes(session, user, codes)
        session.commit()
    finally:
        session.close()
    title = i18n.t("anon.btn.econsul", lang) if everything else ec._service_names(codes, lang)
    sent = _send_booked(_who(user_id), _plain(title), "econsul:menu")
    data = _ec_state(user_id, lang)
    data["booked"] = True
    data["tg_sent"] = sent
    data["message"] = _plain(i18n.t("support.booked_plain", lang, service=_plain(title)))
    return data


# ---------------------------------------------------------------- обзор

@op("terms", "overview")
def overview(user_id, lang, params):
    """Для страницы-выбора: подписан ли человек на ДП и сколько у него подписок на посольство, без тяжёлых списков."""
    from database import DBSession, EconsulState
    from user_handlers import econsul_monitor as ec
    from user_handlers import equeue_monitor as eq

    out = {"equeue": {"allowed": bool(eq.is_allowed(user_id))}, "econsul": {"allowed": bool(ec.is_allowed(user_id))}}
    if out["equeue"]["allowed"]:
        out["equeue"]["subscribed"] = bool(eq._is_active(user_id))
        out["equeue"]["status"] = _eq_status_code()[0]
    if out["econsul"]["allowed"]:
        session = DBSession()
        try:
            codes = ec._active_codes(ec._user_subscriptions(session, user_id))
            state = session.query(EconsulState).filter(EconsulState.source == ec.SOURCE).first()
            out["econsul"]["all"] = "*" in codes
            out["econsul"]["count"] = len(codes)
            out["econsul"]["status"] = _ec_status(state, lang, ec)["code"]
        finally:
            session.close()
    else:
        out["econsul"]["message"] = i18n.t("econsul.not_allowed", lang)
    return out
