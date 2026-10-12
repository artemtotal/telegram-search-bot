"""Як записи площадок складаються в один фільтр людини.

Пошуки (`housing_search`) з'явились 12.10.2026; усе, що майстер завів раніше,
лежить окремими записами без жодного спільного ідентифікатора. Один прохід
майстра записує всі локальні площадки в ту саму хвилину — за часом їх і
збираємо. Immowelt живе в check-Wohnung без часу створення, тож його запис
приєднуємо до пошуку з тими самими умовами.
"""

from collections import Counter
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Sequence, Tuple

# Записи одного проходу майстра створюються за секунди; між двома проходами
# людини зазвичай хвилина й більше, а одна площадка двічі в одному проході не
# трапляється — саме це й розділяє сусідні проходи.
PASS_GAP = timedelta(minutes=3)

SOURCE_ORDER = (
    "immowelt", "immoscout", "propotsdam", "semmelhaack", "schoba", "regiomakler",
    "kleinanzeigen", "locals", "karlmarx", "vonovia",
)

Key = Tuple[str, int]


def item_key(item: Dict) -> Key:
    return (str(item.get("source")), int(item.get("filter_id") or 0))


def _norm(value) -> Optional[float]:
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def _most_common(values) -> Optional[float]:
    present = [value for value in (_norm(v) for v in values) if value is not None]
    if not present:
        return None
    return Counter(present).most_common(1)[0][0]


def _ordered(items: Sequence[Dict]) -> List[Dict]:
    def rank(item):
        source = str(item.get("source"))
        return SOURCE_ORDER.index(source) if source in SOURCE_ORDER else len(SOURCE_ORDER)

    return sorted(items, key=rank)


def criteria_from_members(items: Sequence[Dict]) -> Dict:
    """Умови пошуку так, як їх обирала людина, — зі збережених записів.

    Записи одного проходу мають ті самі межі; якщо хтось потім правив окрему
    площадку, беремо значення більшості. Повну оренду ProPotsdam зберігає як
    `*_total_rent_eur`.
    """
    items = _ordered(items)
    districts: List[str] = []
    for item in items:
        raw = item.get("districts")
        found = [d for d in (raw.split(",") if isinstance(raw, str) else (raw or [])) if d]
        if found:
            districts = found
            break
    return {
        "districts": districts,
        "min_rooms": _most_common(item.get("min_rooms") for item in items),
        "max_rooms": _most_common(item.get("max_rooms") for item in items),
        "min_area_m2": _most_common(item.get("min_area_m2") for item in items),
        "max_area_m2": _most_common(item.get("max_area_m2") for item in items),
        "min_price_eur": _most_common(item.get("min_price_eur") for item in items),
        "max_price_eur": _most_common(item.get("max_price_eur") for item in items),
        "min_price_warm_eur": _most_common(_first(item, "min_price_warm_eur", "min_total_rent_eur") for item in items),
        "max_price_warm_eur": _most_common(_first(item, "max_price_warm_eur", "max_total_rent_eur") for item in items),
    }


def _first(item: Dict, *fields):
    return next((item.get(field) for field in fields if item.get(field) is not None), None)


def _match_key(criteria: Dict) -> tuple:
    return tuple(_norm(criteria.get(field)) for field in (
        "min_rooms", "max_rooms", "min_area_m2", "max_area_m2", "min_price_eur", "max_price_eur",
    ))


def cluster_legacy(
    orphans: Sequence[Dict],
    created_at: Dict[Key, datetime],
    searches: Sequence[Dict],
) -> Tuple[List[List[Dict]], List[Tuple[int, Dict]]]:
    """Розкладає записи без пошуку: (нові пошуки, приєднання до наявних).

    Повертає список груп записів для нових пошуків і пари (search_id, запис)
    для записів, що доповнюють уже наявний пошук.
    """
    timed = sorted(
        (item for item in orphans if item_key(item) in created_at),
        key=lambda item: (created_at[item_key(item)], item_key(item)),
    )
    untimed = sorted(
        (item for item in orphans if item_key(item) not in created_at),
        key=item_key,
    )

    groups: List[Dict] = []
    for item in timed:
        when = created_at[item_key(item)]
        source = item_key(item)[0]
        last = groups[-1] if groups else None
        if last and when - last["last"] <= PASS_GAP and source not in last["sources"]:
            last["items"].append(item)
            last["sources"].add(source)
            last["last"] = when
        else:
            groups.append({"items": [item], "sources": {source}, "first": when, "last": when})

    # Куди можна дописати запис без часу: нові групи й наявні пошуки, де цієї
    # площадки ще немає. Спершу давніші — так перший запис Immowelt іде до
    # першого проходу, другий до другого.
    targets = []
    for search in searches:
        targets.append({
            "kind": "search", "search_id": search["search_id"],
            "sources": {source for source, _ in search["members"]},
            "key": _match_key(search["criteria"]), "when": search.get("created_at") or datetime.min,
        })
    for group in groups:
        targets.append({
            "kind": "group", "group": group, "sources": group["sources"],
            "key": _match_key(criteria_from_members(group["items"])), "when": group["first"],
        })
    targets.sort(key=lambda target: target["when"])

    attach: List[Tuple[int, Dict]] = []
    solo: List[List[Dict]] = []
    for item in untimed:
        source = item_key(item)[0]
        key = _match_key(criteria_from_members([item]))
        target = next(
            (t for t in targets if source not in t["sources"] and t["key"] == key and any(v is not None for v in key)),
            None,
        )
        if target is None:
            solo.append([item])
            continue
        target["sources"].add(source)
        if target["kind"] == "search":
            attach.append((target["search_id"], item))
        else:
            target["group"]["items"].append(item)

    return [group["items"] for group in groups] + solo, attach
