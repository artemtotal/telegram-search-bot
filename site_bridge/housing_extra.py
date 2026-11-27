"""Мост: то, что в меню жилья бота было, а на сайте не было (пакет H6, 28.11.2026). Файл принадлежит пакету H6; подключается одной строкой в конце housing.py,
операции остаются в области housing (адрес POST /int/housing/<имя>).

Только вызовы СУЩЕСТВУЮЩИХ функций бота; своих прав, лимитов и текстов нет:
  mappe_info    «📁 Папка заявника»: подписка, вопросы анкеты (из housing_mappe), ответы, лимит на сутки, идёт ли сборка
  mappe_save    сохранить анкету папки (те же поля и правила, что у диалога бота; поля «Листов» не дублируются, если совпадают)
  mappe_make    собрать папку (housing_mappe.generate: PDF и список документов приходят в ЧАТ с ботом, как из кнопки бота; только подписчику)
  mappe_status  идёт ли сборка и появилась ли новая папка (для опроса страницей)
  mappe_delete  «🗑 Видалити мої дані» (housing_mappe_store.delete_all: анкета папки и анкета писем, как в боте)
  invite        «🎁 Запроси друга»: ссылка, текст, счётчики (housing_referral)
  help          «❓ Довідка» (housing_monitor._faq_text) и «📡 Статус» (_status_lines)

Файл ничего не пишет без явного действия человека: info/status/invite/help только читают.
"""

from __future__ import annotations

import html
import logging
import re
from urllib.parse import quote

import i18n
from site_bridge import Refuse, op

logger = logging.getLogger(__name__)

_TAGS = re.compile(r"<[^>]+>")
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_BOLD = re.compile(r"<b>(.*?)</b>", re.S)


def _plain(text) -> str:
    return html.unescape(_TAGS.sub("", str(text or ""))).strip()


def _bad(lang):
    return Refuse("bad_request", i18n.t("h6.bad", lang))


def _hm():
    from user_handlers import housing_monitor

    return housing_monitor


def _mappe():
    from user_handlers import housing_mappe

    return housing_mappe


def _premium(user_id: int) -> bool:
    from user_jobs import housing_tier

    return bool(housing_tier.is_premium(int(user_id)))


def _need_access(user_id: int, lang: str) -> None:
    if not _hm().is_allowed(int(user_id)):
        raise Refuse("no_access", i18n.t("hsite.no_access", lang))


def _mt(key: str, lang: str, **kw) -> str:
    return _plain(i18n.t("housing.mappe." + key, lang, **kw))


def _require_mappe(user_id: int, lang: str) -> None:
    m = _mappe()
    if not m.ENABLED:
        raise Refuse("off", _mt("off", lang))
    if not _premium(user_id):
        from user_jobs import housing_tier

        raise Refuse("locked", _mt("locked", lang, price=housing_tier.PREMIUM_PRICE_EUR))


# ---------------------------------------------------------------- Mappe

KINDS = {
    "occupation": "choice", "adults": "number", "children": "number",
    "pets": "yesno", "smoking": "yesno", "debts": "yesno", "insolvency": "yesno",
}


def _fields(lang: str) -> list:
    m = _mappe()
    out = []
    for key in m.FIELDS:
        item = {"key": key, "kind": KINDS.get(key, "text"), "label": _mt("q." + key, lang), "letter": key in m.LETTER_FIELDS}
        if key == "occupation":
            item["choices"] = [{"v": code, "label": _mt("occ." + code, lang)} for code in m.CHOICES["occupation"]]
        elif key in m.NUMBER_FIELDS:
            item["choices"] = [{"v": code, "label": code} for code in m.CHOICES[key]]
        elif KINDS.get(key) == "yesno":
            item["choices"] = [{"v": "yes", "label": _mt("btn.yes", lang)}, {"v": "no", "label": _mt("btn.no", lang)}]
        if key == "employer":
            item["label_other"] = _mt("q.employer_other", lang)
        out.append(item)
    return out


def _rules() -> dict:
    """Какие вопросы нужны при каком ответе: считает сама функция бота (_applicable), страница только выбирает строку по «работа|животные»."""
    m = _mappe()
    rules = {}
    for occ in ("", "works", "jobcenter", "other"):
        for pets in ("", "yes", "no"):
            answers = {k: v for k, v in (("occupation", occ), ("pets", pets)) if v}
            rules["%s|%s" % (occ, pets)] = [f for f in m.FIELDS if m._applicable(f, answers)]
    return rules


def _usage(user_id: int) -> dict:
    from user_jobs import housing_mappe_store as store

    m = _mappe()
    used = store.count_today(int(user_id))
    return {"limit": int(m.DAILY_LIMIT), "used": used, "left": max(int(m.DAILY_LIMIT) - used, 0)}


@op("housing", "mappe_info")
def mappe_info(user_id, lang, params):
    from user_jobs import housing_letter_store, housing_mappe_store as store

    _need_access(user_id, lang)
    m = _mappe()
    out = {"enabled": bool(m.ENABLED), "premium": _premium(user_id)}
    if not m.ENABLED:
        out["message"] = _mt("off", lang)
        return out
    if not out["premium"]:
        from user_jobs import housing_tier

        out["message"] = _mt("locked", lang, price=housing_tier.PREMIUM_PRICE_EUR)
        return out
    answers = store.get_answers(int(user_id))
    letter = housing_letter_store.get_profile(int(user_id)) or {}
    out.update({
        "intro": _mt("menu", lang), "note": _mt("intro", lang),
        "fields": _fields(lang), "rules": _rules(),
        "answers": m.merged(answers or {}, letter), "saved": answers is not None,
        "ready": m.next_field(answers or {}, letter) is None,
        "busy": int(user_id) in m._busy, "delete_ask": _mt("delete_ask", lang),
    })
    out.update(_usage(user_id))
    return out


def _clean_answers(params, lang: str, letter: dict) -> dict:
    m = _mappe()
    raw = params.get("answers")
    if not isinstance(raw, dict) or len(raw) > 40:
        raise _bad(lang)
    from user_jobs import housing_mappe_store as store

    values = {}
    for key, value in raw.items():
        if key not in m.FIELDS:
            raise _bad(lang)
        if value is None:
            value = ""
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise _bad(lang)
        value = _CTRL.sub("", str(value)).strip()
        if len(value) > store.MAX_VALUE_LEN:
            raise _bad(lang)
        if value:
            if key in m.NUMBER_FIELDS:
                if not value.isdigit() or not 0 <= int(value) <= 20 or (key == "adults" and int(value) < 1):
                    raise Refuse("bad_number", _mt("need_number", lang))
                value = str(int(value))
            elif key in m.CHOICES and value not in m.CHOICES[key]:
                raise _bad(lang)
        values[key] = value
    final = {}
    for key in m.FIELDS:
        if not m._applicable(key, values):
            continue
        value = values.get(key, "")
        if key in m.LETTER_FIELDS and value and value == (letter.get(key) or "").strip():
            continue            # то же, что в анкете «Листов»: берём оттуда, чтобы правка анкеты писем доходила и до папки
        final[key] = value
    return final


@op("housing", "mappe_save")
def mappe_save(user_id, lang, params):
    from user_jobs import housing_letter_store, housing_mappe_store as store

    _require_mappe(user_id, lang)
    letter = housing_letter_store.get_profile(int(user_id)) or {}
    final = _clean_answers(params, lang, letter)
    store.save_answers(int(user_id), final)
    m = _mappe()
    answers = store.get_answers(int(user_id)) or {}
    return {"saved": True, "message": _mt("saved", lang), "answers": m.merged(answers, letter),
            "ready": m.next_field(answers, letter) is None}


@op("housing", "mappe_make")
def mappe_make(user_id, lang, params):
    from site_bridge import bot
    from user_jobs import housing_letter_store, housing_mappe_store as store

    _require_mappe(user_id, lang)
    m = _mappe()
    ref = params.get("ref")
    ref_id = None
    if ref not in (None, "", 0):
        if isinstance(ref, bool) or not isinstance(ref, (int, str)) or not str(ref).isdigit() or not 0 < int(ref) < 2 ** 31:
            raise _bad(lang)
        ref_id = int(ref)
        found = housing_letter_store.resolve_ref(ref_id)
        listing = None
        if found:
            from user_handlers import housing_letter

            listing = housing_letter._listing(found["source"], found["listing_key"])
        if listing is None:
            raise Refuse("not_found", i18n.t("housing.letter.unknown", lang))
    answers = store.get_answers(int(user_id)) or {}
    letter = housing_letter_store.get_profile(int(user_id)) or {}
    if m.next_field(answers, letter) is not None:
        raise Refuse("no_profile", i18n.t("h6.mappe.noprofile", lang))
    if int(user_id) in m._busy:
        raise Refuse("busy", i18n.t("h6.mappe.busy", lang))
    if store.count_today(int(user_id)) >= m.DAILY_LIMIT:
        raise Refuse("limit", _mt("limit", lang, n=m.DAILY_LIMIT))
    before = store.count_today(int(user_id))
    m.generate(bot(), int(user_id), ref_id)     # тот же путь, что у кнопки бота: «Збираю…», PDF и список документов уходят в чат этого человека
    return {"started": True, "before": before, "message": i18n.t("h6.mappe.started", lang)}


@op("housing", "mappe_status")
def mappe_status(user_id, lang, params):
    from user_jobs import housing_mappe_store as store

    _need_access(user_id, lang)
    before = params.get("before", 0)
    if isinstance(before, bool) or not isinstance(before, int) or before < 0:
        raise _bad(lang)
    busy = int(user_id) in _mappe()._busy
    today = store.count_today(int(user_id))
    return {"busy": busy, "done": (not busy) and today > before, "today": today}


@op("housing", "mappe_delete")
def mappe_delete(user_id, lang, params):
    from user_jobs import housing_mappe_store as store

    _require_mappe(user_id, lang)
    store.delete_all(int(user_id))
    return {"deleted": True, "message": _mt("deleted", lang)}


# ---------------------------------------------------------------- «Запроси друга»

@op("housing", "invite")
def invite(user_id, lang, params):
    from user_jobs import housing_pay_store

    _need_access(user_id, lang)
    from user_handlers import housing_referral as ref

    if not ref.enabled():
        return {"enabled": False}
    link = ref.link_for(int(user_id))
    invited, paid = housing_pay_store.referral_counts(int(user_id))
    out = {"enabled": True, "link": link, "invited": int(invited), "paid": int(paid)}
    if not link:
        out["intro"], out["outro"] = _plain(i18n.t("pay.ref.no_link", lang)), ""
        return out
    text = _plain(i18n.t("pay.ref.menu", lang, link="{LINK}"))
    head, _sep, tail = text.partition("{LINK}")
    share_text = i18n.t("pay.ref.share_text", lang)
    out.update({
        "intro": head.strip(), "outro": tail.strip(),
        "share": "https://t.me/share/url?url=%s&text=%s" % (quote(link, safe=""), quote(share_text)),
    })
    return out


# ---------------------------------------------------------------- «Довідка» и «Статус»

def _sections(text: str) -> list:
    """Текст бота: блоки через пустую строку, жирная первая строка блока = заголовок."""
    out = []
    for block in re.split(r"\n\s*\n", str(text or "").strip()):
        block = block.strip()
        if not block:
            continue
        m = re.match(r"^([^<\n]{0,4})<b>(.*?)</b>\s*(.*)$", block, re.S)
        if m:
            out.append({"h": (m.group(1).strip() + " " + _plain(m.group(2))).strip(), "p": _plain(m.group(3))})
        else:
            out.append({"h": "", "p": _plain(block)})
    return out


@op("housing", "help")
def help_(user_id, lang, params):
    from site_bridge import housing

    _need_access(user_id, lang)
    hm = _hm()
    sections = _sections(hm._faq_text(lang))
    status = []
    try:
        status = [_plain(line) for line in housing._deadline(lambda: hm._status_lines(lang), lang)]
    except Refuse:
        status = []
    except Exception:
        logger.exception("h6: could not read the monitoring status")
    return {"sections": sections, "status_title": _plain(i18n.t("housing.status_screen.title", lang)), "status": [s for s in status if s]}
