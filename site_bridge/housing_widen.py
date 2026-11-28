"""Мост: «Розширити фільтр» на сайте (пакет fix8, 28.11.2026). Операции области housing; подключается одной строкой в конце housing.py.

В боте после сохранения/правки поиска и раз в неделю по «тихому» фильтру человек видит отчёт «за 30 днів підійшло б N» и до трёх кнопок-поступок
(housing_relax: на 100 € дорожче, на одну комнату меньше, без минимальной площади, усі райони и т. п.). Здесь то же самое, без сообщения Telegram:
  search_report  {id}        отчёт по поиску: сколько подошло бы за RELAX_DAYS дней и предложения (housing_relax.report_for_search); ничего не пишет
  search_relax   {id, code}  применить поступку (housing_relax.relax + housing_card.apply_criteria, как правка поиска); дальше страница просит новый отчёт

Только вызовы СУЩЕСТВУЮЩИХ функций бота: счёт, пороги, шаги, подписи кнопок (offer_label) и тексты отчёта берутся из housing_relax / housing_monitor / i18n.
Поиск сверяется с user_id (housing_search_store.get(id, user_id) и список housing_monitor.user_searches). Чужой поиск = not_found.
Новые тексты (только про сайт, где у бота «Мої фільтри» → ✏️): translations/hrelax.<lang>.json.
"""

from __future__ import annotations

import logging

import i18n
from site_bridge import Refuse, op
from site_bridge import housing as _h

logger = logging.getLogger(__name__)


def _relax():
    from user_handlers import housing_relax
    return housing_relax


def _bad(lang):
    return Refuse("bad_request", i18n.t("hsite.bad_request", lang))


def _items(search: dict, base: int, found: list, lang: str) -> list:
    hr = _relax()
    criteria = search["criteria"]
    return [{"code": code, "label": _h._plain(hr.offer_label(code, criteria, lang)), "count": int(count), "gain": int(count) - int(base),
             "btn": _h._plain(i18n.t("housing.relax.btn", lang, label=hr.offer_label(code, criteria, lang), gain=int(count) - int(base)))}
            for code, count in found]


def _report(user_id: int, search_id: int, lang: str) -> dict:
    hr, hm = _relax(), _h._hm()
    number, search = _h._find(user_id, search_id, lang)               # поиск этого человека, иначе not_found
    result = _h._deadline(lambda: hr.report_for_search(int(user_id), int(search_id)), lang)
    base = result[1] if result else None
    if result is None or base is None:                                # площадки не отвечают или записей уже нет: как в боте, отчёта нет
        return {"id": int(search_id), "n": number, "available": False, "days": int(hr.RELAX_DAYS), "note": i18n.t("hrelax.na", lang)}
    _s, base, found = result
    thin = int(base) <= int(hm.THIN_FILTER_MAX_HITS)
    return {
        "id": int(search_id), "n": number, "available": True, "days": int(hr.RELAX_DAYS), "total": int(base), "thin": bool(thin),
        "summary": _h._t("housing.report.user", lang, days=hr.RELAX_DAYS, total=int(base)),
        "intro": _h._t("housing.relax.intro", lang, days=hr.RELAX_DAYS),
        "offers": _items(search, int(base), found, lang),
        "tap": i18n.t("hrelax.tap", lang),
        "thin_note": i18n.t("hrelax.thin", lang) if (thin and not found) else "",
        "paused": bool(hm._search_paused(search)),
    }


@op("housing", "search_report")
def search_report(user_id, lang, params):
    _h._need_access(user_id, lang)
    return _report(int(user_id), _h._int_param(params, "id", lang), lang)


@op("housing", "search_relax")
def search_relax(user_id, lang, params):
    from user_handlers import housing_card, housing_web
    hr = _relax()
    _h._need_access(user_id, lang)
    uid, sid = int(user_id), _h._int_param(params, "id", lang)
    code = params.get("code")
    if not isinstance(code, str) or code not in hr.RELAX_CODES:
        raise _bad(lang)
    with _h._lock(uid):
        number, search = _h._find(uid, sid, lang)
        if code not in hr.available(search["criteria"]):
            raise Refuse("not_offered", i18n.t("hrelax.not_offered", lang))
        criteria = hr.relax(search["criteria"], code)
        if criteria == search["criteria"]:
            raise Refuse("not_offered", i18n.t("hrelax.not_offered", lang))
        failed = housing_card.apply_criteria(search, criteria)
        if failed:
            raise Refuse("save_failed", _h._t("web.save_failed", lang, sources=", ".join(sorted({_h._source_label(s) for s in failed}))))
        housing_web._record_web_edit(uid, sid)
        number, fresh = _h._find(uid, sid, lang)
        return {"id": sid, "applied": code, "done": i18n.t("hrelax.done", lang), "search": _h._ser(number, fresh, lang)}
