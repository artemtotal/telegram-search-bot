"""Мост: оплата жилья и «Підтримати проєкт» на сайте (пакет H2, 28.11.2026). Файл принадлежит пакету H2; остальные сюда не пишут.

Деньги, реквизиты, пакеты, курс берутся ТОЛЬКО из функций бота (user_handlers.housing_payment, support, user_jobs.housing_packages):
здесь нет ни одной суммы и ни одного номера карты. Сценарий тот же, что в чате:
  pay.info      статус подписки, пакеты с суммами, последняя оплата, открытый выбор с реквизитами (только чтение);
  pay.create    выбор пакета -> housing_payment.create_payment (запись «ждём квитанцию») + реквизиты (как _pick_package);
  pay.cancel    housing_payment.cancel_open (как кнопка «Скасувати»);
  pay.receipt   файл чека -> housing_payment.submit_receipt (общий с чатом шаг: день миттєвих, карточка админу с кнопками
                «Дозволити/Відхилити/Написати», статус sent). Одобрение остаётся у админа в Telegram (housing:pay:ok/no), второго пути нет;
  pay.support_info / support_card / support_done   то же, что экраны support.py (суммы 1/2/3/5/10 € или своя, карта, «Я переказав(ла)»).
"""

import base64
import binascii
import hashlib
import html
import io
import logging
import re
import threading
import time
from datetime import timedelta
from types import SimpleNamespace

from site_bridge import Refuse, bot, op

logger = logging.getLogger(__name__)

MAX_BODY = 11 * 1024 * 1024            # тело запроса (base64 + JSON); та же граница в bridge.py LIMITS ("pay", "receipt")
MAX_FILE = 8 * 1024 * 1024             # файл чека: base64 раздувает на треть, 8 МБ + JSON укладываются в MAX_BODY
MAX_NOTE = 200
RECEIPTS_PER_PAYMENT = 3               # повторные страницы чека допустимы, но не бесконечно
RECEIPT_GAP = 30.0                     # секунд между двумя отправками одного человека
CREATE_PER_HOUR = 20
SUPPORT_DONE_GAP = 20.0
SUPPORT_DONE_PER_DAY = 5
REUSE_WINDOW = timedelta(minutes=10)   # повторный выбор того же пакета возвращает прежнюю запись, а не плодит новые
LAST_WINDOW = timedelta(days=30)

_lock = threading.Lock()
_user_locks = {}
_sent_hashes = {}     # payment_id -> [sha256, ...] уже отправленных админу файлов (память процесса; после перезапуска пусто)
_last_receipt = {}    # user_id -> время последней отправки
_creates = {}         # user_id -> [время, ...]
_support_done = {}    # user_id -> [время, ...]


def _hp():
    from user_handlers import housing_payment

    return housing_payment


def _t(key, lang, **kwargs):
    import i18n

    return i18n.t(key, lang, **kwargs)


def _user_lock(user_id):
    with _lock:
        return _user_locks.setdefault(int(user_id), threading.Lock())


def _iso(value):
    return value.strftime("%Y-%m-%dT%H:%M:%SZ") if value else None


def _date(value):
    return value.strftime("%Y-%m-%d") if value else None


def _plain(text):
    return html.unescape(re.sub(r"<[^>]+>", "", text or "")).strip()


def _person(user_id):
    """Объект «как telegram.User» для имени в карточке админа: имя берём у Telegram, нет — номер."""
    first = last = username = ""
    try:
        chat = bot().get_chat(int(user_id), timeout=4)
        first, last, username = chat.first_name or "", chat.last_name or "", chat.username or ""
    except Exception:
        logger.info("site_bridge.pay: could not read the name of %s", user_id)
    return SimpleNamespace(id=int(user_id), first_name=first, last_name=last, username=username)


def _display_name(user_id):
    from user_handlers import housing_monitor

    return housing_monitor._display_name(_person(user_id))


def _requisites(lang):
    hp = _hp()
    return {
        "card": hp._format_card(hp.PAY_CARD),
        "card_raw": hp.PAY_CARD,
        "holder": hp.PAY_CARD_HOLDER,
        "warn": _t("paysite.card_warn", lang),
    }


def _packages(lang, rate):
    """Список пакетов с подписями и суммами в гривнах: из housing_packages (или, при HOUSING_PACKAGES_ENABLED=0, «N місяців» как в чате)."""
    hp = _hp()
    from user_jobs import housing_packages as hpk

    items = []
    if hpk.enabled():
        for code in hpk.CODES:
            name = _t("pay.pkg.name." + code, lang)
            eur, uah = hpk.price_eur(code), hpk.amount_uah(code, rate)
            items.append({
                "code": code, "name": name, "term": _t("pay.pkg.term." + code, lang), "eur": eur, "uah": uah,
                "label": _t("pay.pkg.btn", lang, name=name, eur=eur, uah=uah),
            })
    else:
        for months in hp.MONTH_OPTIONS:
            uah = hp.amount_uah(months, rate)
            items.append({
                "code": "n%d" % months, "name": "%d" % months, "term": "", "eur": months * _price_month(), "uah": uah,
                "label": _t("housing.pay.month_btn", lang, months=months, uah=uah),
            })
    return items


def _price_month():
    from user_jobs import housing_tier

    return housing_tier.PREMIUM_PRICE_EUR


def _pick(code):
    """(package|None, months) по коду страницы; None, если код не из текущего списка."""
    from user_jobs import housing_packages as hpk

    if hpk.enabled():
        return (code, 0) if hpk.valid(code) else None
    match = re.fullmatch(r"n([1-6])", code or "")
    return (None, int(match.group(1))) if match else None


def _view(payment, lang):
    from user_jobs import housing_packages as hpk

    pkg = payment.package if hpk.valid(payment.package) else None
    return {
        "id": int(payment.id),
        "status": payment.status,
        "package": pkg,
        "name": _t("pay.pkg.name." + pkg, lang) if pkg else "%d" % payment.months,
        "months": int(payment.months),
        "eur": int(payment.amount_eur),
        "uah": int(payment.amount_uah),
        "created": _iso(payment.created_at),
        "receipt_at": _iso(payment.receipt_at),
        "decided_at": _iso(payment.decided_at),
        "expires": _date(payment.expires_at),
        "provisional_until": _iso(payment.provisional_until),
    }


def _last_payment(user_id):
    """Последняя не отменённая оплата человека за 30 дней (для статуса на странице)."""
    hp = _hp()
    from database import HousingPayment

    session = hp.DBSession()
    try:
        row = (
            session.query(HousingPayment)
            .filter(HousingPayment.user_id == int(user_id), HousingPayment.status != "cancelled")
            .filter(HousingPayment.created_at >= hp.utc_now() - LAST_WINDOW)
            .order_by(HousingPayment.id.desc()).first()
        )
        if row is not None:
            session.expunge(row)
        return row
    finally:
        session.close()


def _cash_url():
    hp = _hp()
    return "tg://user?id=%d" % hp.ADMIN_ID if hp.ADMIN_ID else ""


@op("pay", "info")
def info(user_id, lang, params):
    """Всё, что нужно странице оплаты: статус подписки, пакеты с суммами, последняя оплата, открытый выбор с реквизитами. Только чтение."""
    hp = _hp()
    from user_jobs import housing_tier

    enabled = bool(hp.enabled())
    rate = hp.eur_uah_rate()
    expiry = hp._current_expiry(user_id)
    found_until = hp.until_found_expiry(user_id)
    out = {
        "enabled": enabled,
        "premium": bool(housing_tier.is_premium(user_id)),
        "until": _date(expiry),
        "until_found": _date(found_until),
        "rate": hp._format_rate(rate),
        "packages": _packages(lang, rate) if enabled and found_until is None else [],
        "last": None,
        "open": None,
        "cash_url": _cash_url(),
        "cash_note": _t("paysite.cash_note", lang),
        "max_mb": MAX_FILE // (1024 * 1024),
        "types": ["image/jpeg", "image/png", "application/pdf"],
    }
    if found_until is not None:
        out["until_found_text"] = _t("paysite.until_found", lang, date=found_until.strftime("%d.%m.%Y"))
    last = _last_payment(user_id)
    if last is not None:
        out["last"] = _view(last, lang)
    if enabled:
        opened = hp.open_payment(user_id)
        if opened is not None:
            out["open"] = dict(_view(opened, lang), **_requisites(lang))
    return out


@op("pay", "create")
def create(user_id, lang, params):
    """Выбор пакета: как _pick_package в чате. Прежние невыбранные записи человека отменяются create_payment'ом."""
    hp = _hp()
    if not hp.enabled():
        raise Refuse("unavailable", _t("paysite.unavailable", lang))
    picked = _pick(str(params.get("package") or ""))
    if picked is None:
        raise Refuse("bad_request", _t("paysite.closed", lang))
    package, months = picked
    found_until = hp.until_found_expiry(user_id)
    if found_until is not None:
        raise Refuse("until_found", _t("paysite.until_found", lang, date=found_until.strftime("%d.%m.%Y")))
    now = time.time()
    with _lock:
        stamps = [x for x in _creates.get(int(user_id), []) if now - x < 3600]
        if len(stamps) >= CREATE_PER_HOUR:
            raise Refuse("rate_limit", _t("paysite.rate_limit", lang))
        stamps.append(now)
        _creates[int(user_id)] = stamps
    with _user_lock(user_id):
        # двойной тап: тот же пакет за последние минуты -> прежняя запись (чтобы не плодить строки)
        prev = hp.open_payment(user_id, since=hp.utc_now() - REUSE_WINDOW)
        if (prev is not None and prev.status == "awaiting_receipt" and (prev.package or None) == package
                and (package is not None or int(prev.months) == months)
                and abs(float(prev.eur_uah_rate) - float(hp.eur_uah_rate())) < 1e-9):
            payment = prev
        else:
            payment = hp.create_payment(user_id, _display_name(user_id), months, hp.eur_uah_rate(), package=package)
    return {"payment": dict(_view(payment, lang), **_requisites(lang)), "cash_url": _cash_url(), "cash_note": _t("paysite.cash_note", lang)}


@op("pay", "cancel")
def cancel(user_id, lang, params):
    """Как кнопка «Скасувати» в чате: невыбранные (awaiting_receipt) записи человека закрываются. Отправленные чеки не трогаются."""
    _hp().cancel_open(user_id)
    return {"ok": True}


def _kind(raw):
    if raw[:3] == b"\xff\xd8\xff":
        return "jpg"
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if raw[:5] == b"%PDF-":
        return "pdf"
    return None


@op("pay", "receipt", max_body=MAX_BODY)
def receipt(user_id, lang, params):
    """Чек с сайта: те же шаги, что при фото в чате (housing_payment.submit_receipt): день миттєвих, карточка админу с кнопками, статус sent."""
    hp = _hp()
    if not hp.enabled():
        raise Refuse("unavailable", _t("paysite.unavailable", lang))
    try:
        payment_id = int(params.get("payment_id"))
    except (TypeError, ValueError):
        raise Refuse("bad_request", _t("paysite.closed", lang))
    note = params.get("note")
    note = note.strip()[:MAX_NOTE] if isinstance(note, str) else ""
    data = params.get("data")
    if not isinstance(data, str) or not data:
        raise Refuse("bad_file", _t("paysite.bad_file", lang))
    if len(data) > MAX_BODY:
        raise Refuse("too_big", _t("paysite.too_big", lang, mb=MAX_FILE // (1024 * 1024)))
    try:
        raw = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        raise Refuse("bad_file", _t("paysite.bad_file", lang))
    if not raw:
        raise Refuse("bad_file", _t("paysite.bad_file", lang))
    if len(raw) > MAX_FILE:
        raise Refuse("too_big", _t("paysite.too_big", lang, mb=MAX_FILE // (1024 * 1024)))
    kind = _kind(raw)
    if kind is None:
        raise Refuse("bad_type", _t("paysite.bad_type", lang))
    digest = hashlib.sha256(raw).hexdigest()
    with _user_lock(user_id):
        # чужая или закрытая оплата: только открытый выбор ЭТОГО человека (как open_payment в чате)
        payment = hp.get_payment(payment_id)
        if payment is None or int(payment.user_id) != int(user_id):
            raise Refuse("not_found", _t("paysite.closed", lang))
        opened = hp.open_payment(user_id)
        if opened is None or int(opened.id) != int(payment.id):
            raise Refuse("closed", _t("paysite.closed", lang))
        payment = opened
        with _lock:
            sent = _sent_hashes.get(payment.id, [])
            if digest in sent:
                raise Refuse("duplicate", _t("paysite.duplicate", lang))
            if len(sent) >= RECEIPTS_PER_PAYMENT:
                raise Refuse("too_many", _t("paysite.too_many", lang))
            if time.time() - _last_receipt.get(int(user_id), 0.0) < RECEIPT_GAP:
                raise Refuse("too_fast", _t("paysite.too_fast", lang))
            _sent_hashes[payment.id] = sent + [digest]       # занимаем место до отправки: двойной тап и таймаут сайта не продублируют чек
            _last_receipt[int(user_id)] = time.time()
        caption_note = _t("paysite.note_prefix", lang) + (": " + note if note else "")
        b = bot()

        def send_to_admin(caption, keyboard):
            def fresh(name):
                stream = io.BytesIO(raw)
                stream.name = name
                return stream

            if kind in ("jpg", "png"):
                try:
                    b.send_photo(chat_id=hp.ADMIN_ID, photo=fresh("receipt." + kind), caption=caption, parse_mode="HTML",
                                 reply_markup=keyboard, timeout=6)
                    return
                except Exception:
                    logger.info("site_bridge.pay: photo was refused by Telegram, sending as a document")
            b.send_document(chat_id=hp.ADMIN_ID, document=fresh("receipt." + kind), caption=caption, parse_mode="HTML",
                            reply_markup=keyboard, timeout=6)

        try:
            result = hp.submit_receipt(b, payment, user_id, caption_note, send_to_admin)
        except Exception:
            result = None
            logger.exception("site_bridge.pay: submit_receipt failed for %s", user_id)
        if result is None:
            with _lock:
                _sent_hashes[payment.id] = [x for x in _sent_hashes.get(payment.id, []) if x != digest]
                _last_receipt.pop(int(user_id), None)
            raise Refuse("send_failed", _t("housing.access.request_failed", lang))
    after = hp.get_payment(payment.id)
    return {
        "status": "sent",
        "provisional": result == "provisional",
        "message": _t("paysite.received_prov" if result == "provisional" else "paysite.received", lang),
        "payment": _view(after or payment, lang),
    }


# --- «Підтримати проєкт» -----------------------------------------------------------

def _support():
    from user_handlers import support

    return support


def _support_enabled(lang):
    if not _support().enabled():
        raise Refuse("unavailable", _t("support.unavailable", lang))


def _cents(params, lang):
    """Сумма в центах: готовая (`cents`, одна из кнопок или своя) или текст «4,5» как в чате (support.parse_eur)."""
    sp = _support()
    cents = params.get("cents")
    if isinstance(cents, bool):
        cents = None
    if isinstance(cents, int):
        if not 100 <= cents <= sp.MAX_CUSTOM_EUR * 100:
            raise Refuse("bad_amount", _t("support.custom_prompt", lang, max=sp.MAX_CUSTOM_EUR))
        return cents
    text = params.get("text")
    parsed = sp.parse_eur(text[:20]) if isinstance(text, str) else None
    if parsed is None:
        raise Refuse("bad_amount", _t("support.custom_prompt", lang, max=sp.MAX_CUSTOM_EUR))
    return parsed


@op("pay", "support_info")
def support_info(user_id, lang, params):
    """Экран «Підтримати проєкт»: вступ, курс, готовые суммы (1/2/3/5/10 €) с гривнами. Из support.py, чисел на сайте нет."""
    sp = _support()
    hp = _hp()
    if not sp.enabled():
        return {"enabled": False, "message": _t("support.unavailable", lang)}
    intro = _plain(_t("support.choose", lang, rate=hp._format_rate(hp.eur_uah_rate())))
    title, _, body = intro.partition("\n\n")
    return {
        "enabled": True,
        "title": title,
        "intro": body,
        "rate": hp._format_rate(hp.eur_uah_rate()),
        "amounts": [{"cents": eur * 100, "eur": eur, "uah": sp._uah(eur * 100), "label": _t("support.amount_btn", lang, eur=eur, uah=sp._uah(eur * 100))}
                    for eur in sp.AMOUNTS_EUR],
        "custom_label": _t("support.btn.custom", lang),
        "custom_prompt": _t("support.custom_prompt", lang, max=sp.MAX_CUSTOM_EUR),
        "custom_max": sp.MAX_CUSTOM_EUR,
    }


@op("pay", "support_card")
def support_card(user_id, lang, params):
    """Карта и сумма в гривнах для выбранной суммы (как support:a:<cents> и «Своя сума»)."""
    sp = _support()
    _support_enabled(lang)
    cents = _cents(params, lang)
    return dict(
        _requisites(lang), cents=cents, eur_text=sp._eur_text(cents), uah=sp._uah(cents),
        done_label=_t("support.btn.done", lang),
    )


@op("pay", "support_done")
def support_done(user_id, lang, params):
    """«Я переказав(ла)»: админу уходит то же сообщение, что из чата (support._notify_admin_done). Лимиты: пауза и число в сутки."""
    sp = _support()
    _support_enabled(lang)
    cents = _cents(params, lang)
    now = time.time()
    with _lock:
        stamps = [x for x in _support_done.get(int(user_id), []) if now - x < 86400]
        if stamps and now - stamps[-1] < SUPPORT_DONE_GAP or len(stamps) >= SUPPORT_DONE_PER_DAY:
            raise Refuse("rate_limit", _t("paysite.rate_limit", lang))
        stamps.append(now)
        _support_done[int(user_id)] = stamps
    sp._notify_admin_done(bot(), _person(user_id), cents)
    return {"thanks": _t("support.thanks", lang)}
