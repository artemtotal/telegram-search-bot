"""Де шукати житло: місто чи поштовий індекс плюс радіус, як на Immowelt.

Етап 1 розширення пошуку на Берлін і Бранденбург (02.11.2026, див.
docs/housing-region-plan.md). Умова пошуку `criteria["area"]`:

    {"kind": "city", "name": "Werder (Havel)", "code": "12069", "radius_km": 10}
    {"kind": "plz", "plz": "10247", "name": "Berlin", "code": "11000", "radius_km": 5}

Порожня (None) — «лише Потсдам», як було до цього етапу: так поводяться всі
старі фільтри, і для них нічого не змінюється.

Координати — з довідника індексів GeoNames (`geo_plz_be_bb.tsv`, лише Берлін і
Бранденбург). Квартира в зоні, якщо її індекс:
* для «лише місто» — один з індексів цього міста (або сам індекс);
* з радіусом — ближче за радіус до будь-якої точки міста (до центру індексу).
Квартири, зібрані пошуком «Потсдам» без індексу (старі записи), вважаються
потсдамськими.
"""

from __future__ import annotations

import difflib
import json
import math
import re
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Tuple

DATA_FILE = Path(__file__).with_name("geo_plz_be_bb.tsv")
# Індекс Берліна → округ (етап 7, 02.11.2026): «Берлін» у місці може мати
# `bezirke` — тоді підходять лише індекси цих округів.
BEZIRKE_FILE = Path(__file__).with_name("geo_berlin_bezirke.tsv")
POTSDAM = "Potsdam"
POTSDAM_CODE = "12054"
BERLIN = "Berlin"
BERLIN_CODE = "11000"
RADII = (0, 5, 10, 20, 30, 50)
MAX_CHOICES = 8
_PLZ_RE = re.compile(r"(?<!\d)(\d{5})(?!\d)")

# Як люди пишуть кирилицею найчастіші міста; решту бере транслітерація.
_ALIASES = {
    "берлін": BERLIN, "берлин": BERLIN, "потсдам": POTSDAM,
    "котбус": "Cottbus", "коттбус": "Cottbus",
    "франкфурт": "Frankfurt (Oder)", "франкфурт на одері": "Frankfurt (Oder)", "франкфурт на одере": "Frankfurt (Oder)",
    "бранденбург": "Brandenburg an der Havel", "бранденбург на хафелі": "Brandenburg an der Havel",
    "бранденбург на хафеле": "Brandenburg an der Havel", "brandenburg": "Brandenburg an der Havel",
    "frankfurt": "Frankfurt (Oder)", "frankfurt oder": "Frankfurt (Oder)",
}
_CYRILLIC = {
    "а": "a", "б": "b", "в": "w", "г": "g", "ґ": "g", "д": "d", "е": "e", "є": "je", "ё": "jo", "ж": "sch",
    "з": "s", "и": "i", "і": "i", "ї": "i", "й": "j", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "ch", "ц": "z", "ч": "tsch",
    "ш": "sch", "щ": "schtsch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "ju", "я": "ja",
}


def fold(text: Any) -> str:
    """Для порівняння назв: без регістру, умлаутів, дужок і дефісів."""
    value = str(text or "").replace("ß", "ss").casefold()
    value = "".join(ch for ch in unicodedata.normalize("NFKD", value) if not unicodedata.combining(ch))
    value = re.sub(r"[()\-/.,]", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _translit(text: str) -> str:
    return "".join(_CYRILLIC.get(ch, ch) for ch in text)


# --- довідник ---------------------------------------------------------------------

@lru_cache(maxsize=1)
def _rows() -> Tuple[Tuple[str, str, str, str, str, float, float], ...]:
    """(plz, місто, населений пункт, округ, код округу, lat, lon)."""
    rows = []
    for line in DATA_FILE.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        plz, city, place, kreis, code, lat, lon = line.split("\t")
        rows.append((plz, city, place, kreis, code, float(lat), float(lon)))
    return tuple(rows)


@lru_cache(maxsize=1)
def _by_plz() -> Dict[str, List[Tuple]]:
    result: Dict[str, List[Tuple]] = {}
    for row in _rows():
        result.setdefault(row[0], []).append(row)
    return result


@lru_cache(maxsize=1)
def _by_city() -> Dict[Tuple[str, str], List[Tuple]]:
    result: Dict[Tuple[str, str], List[Tuple]] = {}
    for row in _rows():
        result.setdefault((row[1], row[4]), []).append(row)
    return result


@lru_cache(maxsize=1)
def _name_index() -> Dict[str, set]:
    """Назва для порівняння → {(місто, код округу)}. «Werder (Havel)» знаходиться і
    як «werder havel», і як «werder»; «Golm» — як Потсдам."""
    index: Dict[str, set] = {}
    for _plz, city, place, _kreis, code, _lat, _lon in _rows():
        for name in {city, place, re.sub(r"\s*\(.*?\)", "", city)}:
            key = fold(name)
            if key:
                index.setdefault(key, set()).add((city, code))
    return index


def _city_area(city: str, code: str) -> Dict:
    kreis = _by_city()[(city, code)][0][3]
    return {"kind": "city", "name": city, "code": code, "kreis": kreis, "radius_km": 0}


def _sorted_choices(found: Iterable[Tuple[str, str]]) -> List[Dict]:
    # Кругові міста й більші громади (більше індексів) — першими.
    ordered = sorted(set(found), key=lambda item: (-len(_by_city()[item]), item[0]))
    return [_city_area(city, code) for city, code in ordered[:MAX_CHOICES]]


def find(text: Any) -> List[Dict]:
    """Варіанти місця за тим, що людина написала: місто чи індекс.

    Індекс Потсдама дає «Потсдам» (там є райони); інший індекс — сам індекс.
    Порожній список — такого в Берліні й Бранденбурзі немає."""
    raw = str(text or "").strip()
    plz_match = _PLZ_RE.fullmatch(raw.replace(" ", ""))
    if plz_match:
        rows = _by_plz().get(plz_match.group(1)) or []
        if not rows:
            return []
        city, code = rows[0][1], rows[0][4]
        if code == POTSDAM_CODE:
            return [_city_area(POTSDAM, POTSDAM_CODE)]
        return [{"kind": "plz", "plz": plz_match.group(1), "name": city, "code": code,
                 "kreis": rows[0][3], "radius_km": 0}]
    key = fold(raw)
    if not key:
        return []
    alias = _ALIASES.get(key)
    if alias:
        key = fold(alias)
    index = _name_index()
    for candidate in dict.fromkeys((key, fold(_translit(key)))):
        if candidate in index:
            return _sorted_choices(index[candidate])
        starts = [
            item for name, items in index.items() if len(candidate) >= 3 and name.startswith(candidate) for item in items
        ]
        if starts:
            return _sorted_choices(starts)
    # Схожі назви — у порядку схожості, а не розміру міста.
    close = difflib.get_close_matches(fold(_translit(key)), list(index), n=MAX_CHOICES, cutoff=0.75)
    found: List[Tuple[str, str]] = []
    for name in close:
        found.extend(item for item in sorted(index[name]) if item not in found)
    return [_city_area(city, code) for city, code in found[:MAX_CHOICES]]


def make(city_area: Dict, radius_km: int) -> Optional[Dict]:
    """Місце з радіусом для збереження; «лише Потсдам» — None, як у старих фільтрів."""
    radius = int(radius_km or 0)
    if radius not in RADII:
        radius = min(RADII, key=lambda value: abs(value - radius))
    if is_potsdam(city_area) and radius == 0:
        return None
    area = {key: city_area[key] for key in ("kind", "name", "code", "kreis", "plz") if city_area.get(key)}
    area["radius_km"] = radius
    return area


def is_potsdam(area: Optional[Dict]) -> bool:
    return area is None or (area.get("kind") == "city" and area.get("code") == POTSDAM_CODE)


def is_default(area: Optional[Dict]) -> bool:
    """«Лише Потсдам» — там працюють райони й місцеві агентства, як раніше."""
    return area is None or (is_potsdam(area) and not area.get("radius_km"))


@lru_cache(maxsize=1)
def bezirk_of_plz() -> Dict[str, str]:
    result: Dict[str, str] = {}
    for line in BEZIRKE_FILE.read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#"):
            plz, name = line.split("	")
            result[plz] = name
    return result


def berlin_bezirke() -> List[str]:
    return sorted(set(bezirk_of_plz().values()))


def is_berlin_city(area: Optional[Dict]) -> bool:
    """«Берлін, лише місто» — там можна обрати округи."""
    return (
        area is not None and area.get("kind") == "city" and area.get("code") == BERLIN_CODE
        and not int(area.get("radius_km") or 0)
    )


def label(area: Optional[Dict], lang: str = "uk") -> str:
    if area is None:
        return POTSDAM
    name = str(area.get("name") or "")
    bezirke = list(area.get("bezirke") or [])
    if bezirke and is_berlin_city(area):
        shown = ", ".join(bezirke[:2]) + (f" +{len(bezirke) - 2}" if len(bezirke) > 2 else "")
        return f"{name} ({shown})"
    if area.get("kind") == "plz":
        name = f"{area.get('plz')} {name}"
    radius = int(area.get("radius_km") or 0)
    if radius:
        name += f" + {radius} {'km' if lang == 'de' else 'км'}"
    return name


# --- відстані ----------------------------------------------------------------------

def _distance_km(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(h))


def _area_key(area: Optional[Dict]) -> Tuple:
    if area is None:
        return ("city", POTSDAM, POTSDAM_CODE, "", 0)
    return (area.get("kind"), area.get("name"), area.get("code"), area.get("plz") or "", int(area.get("radius_km") or 0))


def _points(key: Tuple) -> List[Tuple[float, float]]:
    kind, name, code, plz, _radius = key
    rows = _by_plz().get(plz, []) if kind == "plz" else _by_city().get((name, code), [])
    return [(row[5], row[6]) for row in rows]


@lru_cache(maxsize=512)
def _allowed(key: Tuple) -> FrozenSet[str]:
    kind, name, code, plz, radius = key
    if kind == "plz":
        own = {plz}
    else:
        own = {row[0] for row in _by_city().get((name, code), [])}
    if not radius:
        return frozenset(own)
    points = _points(key)
    near = {
        row[0] for row in _rows()
        if any(_distance_km((row[5], row[6]), point) <= radius for point in points)
    }
    return frozenset(own | near)


def allowed_plz(area: Optional[Dict]) -> FrozenSet[str]:
    allowed = _allowed(_area_key(area))
    bezirke = tuple(sorted(area.get("bezirke") or ())) if area else ()
    if bezirke and is_berlin_city(area):
        return _in_bezirke(allowed, bezirke)
    return allowed


@lru_cache(maxsize=256)
def _in_bezirke(allowed: FrozenSet[str], bezirke: Tuple[str, ...]) -> FrozenSet[str]:
    wanted = set(bezirke)
    mapping = bezirk_of_plz()
    return frozenset(plz for plz in allowed if mapping.get(plz) in wanted)


def listing_plz(listing: Dict) -> str:
    plz = str(listing.get("plz") or "").strip()
    if plz:
        return plz
    match = _PLZ_RE.search(str(listing.get("address") or ""))
    return match.group(1) if match else ""


def from_potsdam_search(listing: Dict) -> bool:
    """Квартира з пошуку «Потсдам» (і всі старі записи, де регіону ще не було)."""
    return str(listing.get("region") or "potsdam") == "potsdam"


def listing_in_area(listing: Dict, area: Optional[Dict]) -> bool:
    if is_potsdam(area) and from_potsdam_search(listing):
        # Усе, що знайшов пошук «Потсдам», — Потсдам, навіть з індексом, якого
        # нема в довіднику (як і до етапу 1).
        return True
    plz = listing_plz(listing)
    if not plz:
        return False
    return plz in allowed_plz(area)


def covers_potsdam(area: Optional[Dict]) -> bool:
    """Шукають у самому Потсдамі (з радіусом чи без): тоді Immowelt (його
    фільтри живуть у check-Wohnung і місця не знають), кооперативи й ліміти
    Jobcenter — як раніше."""
    return is_potsdam(area)


def touches_potsdam(area: Optional[Dict]) -> bool:
    """Хоч частина Потсдама в зоні («Werder + 10 км»): тоді працюють і
    потсдамські агентства — їхні квартири перевіряються за індексом (етап 2)."""
    return is_potsdam(area) or bool(allowed_plz(area) & allowed_plz(None))


def touches_berlin(area: Optional[Dict]) -> bool:
    """Хоч частина Берліна в зоні: тоді шукають і міські компанії Берліна (етап 8)."""
    if area is None:
        return False
    berlin = {"kind": "city", "name": BERLIN, "code": BERLIN_CODE, "radius_km": 0}
    return bool(allowed_plz(area) & allowed_plz(berlin))


# --- збереження в записах площадок ---------------------------------------------------

# update_filter без area лишає місце як є: старі шляхи (майстер адміна) його не знають.
KEEP = object()


def load_area(raw: Any) -> Optional[Dict]:
    try:
        area = json.loads(raw) if raw else None
    except (TypeError, ValueError):
        return None
    return area if isinstance(area, dict) else None


def dump_area(area: Optional[Dict]) -> Optional[str]:
    return json.dumps(area, ensure_ascii=False, sort_keys=True) if area else None
