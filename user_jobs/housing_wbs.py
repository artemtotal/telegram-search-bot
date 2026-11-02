"""WBS (Wohnberechtigungsschein) у фільтрі житла — 02.11.2026, на прохання Артема.

Багато квартир міських компаній і частина оголошень на порталах здаються
лише з WBS («nur mit WBS», «WBS ab 160 erforderlich»). Людині без WBS такі
квартири марні, тож у картці фільтра є вибір (`criteria["wbs"]`):

    None    — не важливо (типово, як у всіх старих фільтрах): приходить усе;
    "none"  — WBS немає: квартир, де WBS обов'язковий, не надсилати;
    "100" … "220" — WBS є, з таким рівнем доходу (100, 140, 160, 180, 220 %).

Рівень у квартири — це найвищий дохід, з яким її ще дають: «WBS 140» не
підходить власнику «WBS 160» (дохід вищий), а «WBS 180» — підходить. Якщо
рівень не названо («WBS erforderlich»), квартира підходить будь-якому WBS.

Звідки знаємо вимогу: поле `wbs` (inberlinwohnen) або заголовок оголошення.
Опису більшість площадок не дає — тож «WBS» лише в описі ми не побачимо.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional, Tuple

CHOICES = ("none", "100", "140", "160", "180", "220")

_NOT_NEEDED_RE = re.compile(
    r"\b(?:ohne|kein(?:en)?)\s+WBS\b|WBS[\s:,-]*(?:ist\s+)?nicht\s+(?:erforderlich|notwendig|n[öo]tig)"
    r"|\bfrei\s+finanziert\b|\bnicht\s+erforderlich\b",
    re.IGNORECASE,
)
_NEEDED_RE = re.compile(
    r"\bWBS\b|Wohnberechtigungsschein|\bwbs-?pflichtig|\bsozialwohnung|\bgef[öo]rdert(?:e|er)?\s+wohn",
    re.IGNORECASE,
)
_LEVEL_RE = re.compile(r"\bWBS[\s-]*(?:ab|mit|bis|f[üu]r)?[\s-]*(\d{3})\b|\b(\d{3})\s*%?\s*[-–]?\s*WBS\b", re.IGNORECASE)


def requirement(listing: Dict[str, Any]) -> Tuple[bool, Optional[int]]:
    """(чи потрібен WBS, рівень або None)."""
    explicit = str(listing.get("wbs") or "").strip()
    texts = [explicit] if explicit else []
    texts.append(str(listing.get("title") or ""))
    for text in texts:
        if not text:
            continue
        if _NOT_NEEDED_RE.search(text):
            return False, None
        if _NEEDED_RE.search(text) or (text is explicit and "erforderlich" in text.casefold()):
            match = _LEVEL_RE.search(text)
            level = None
            if match:
                number = int(match.group(1) or match.group(2))
                level = number if 100 <= number <= 300 else None
            return True, level
    return False, None


def allowed(listing: Dict[str, Any], wanted: Any) -> bool:
    """Чи підходить квартира людині з таким вибором WBS."""
    choice = str(wanted or "").strip()
    if not choice:
        return True
    needed, level = requirement(listing)
    if not needed:
        return True
    if choice == "none":
        return False
    try:
        own = int(choice)
    except ValueError:
        return True
    return level is None or level >= own


def normalize(value: Any) -> Optional[str]:
    text = str(value or "").strip().casefold()
    return text if text in CHOICES else None
