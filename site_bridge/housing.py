"""Мост: мониторинг жилья (операции пакета H1). Файл принадлежит пакету H1; остальные сюда не пишут.

Сейчас здесь только `housing.me` как проверка контракта на настоящей логике бота. Лента и прежние /api/h/* сайта пока работают сами
(сайт читает bot.db только на чтение), H1 переведёт их на мост."""

import logging

from site_bridge import op

logger = logging.getLogger(__name__)


@op("housing", "me")
def me(user_id, lang, params):
    """Состояние мониторинга человека: подписка и свои поиски. Только чтение, только существующие функции бота."""
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
    return {
        "allowed": bool(hm.is_allowed(user_id)),
        "tier": "premium" if premium else "free",
        "until": until,
        "trial": trial,
        "delay_h": housing_tier.free_delay_hours(),
        "found": bool(housing_found.is_found(user_id)),
        "monitoring": any(f["on"] for f in filters),
        "filters": filters,
    }
