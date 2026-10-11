"""Коли востаннє слали адміну тривогу про поломку джерела.

Монітори рахували паузу між тривогами від `last_checked_at` свого статусу, а він
оновлюється на кожній перевірці, зокрема й невдалій. Тож пауза ніколи не
спливала: адмін отримував одну тривогу на початку поломки і тишу далі, хоч би
скільки вона тривала. Так ProPotsdam мовчав 26.09–11.10.2026.

Тут пам'ятаємо саме час відправленої тривоги. Пам'ять у процесі: після
перезапуску бота перша ж невдала перевірка нагадає ще раз — це й потрібно.
"""

import threading
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

_lock = threading.Lock()
_last_alert_at: Dict[str, datetime] = {}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def due(key: str, cooldown: timedelta, now: Optional[datetime] = None) -> bool:
    """Чи минула пауза з останньої тривоги `key` (або тривоги ще не було)."""
    with _lock:
        last = _last_alert_at.get(key)
    return last is None or (now or _now()) - last >= cooldown


def mark(key: str, now: Optional[datetime] = None) -> None:
    with _lock:
        _last_alert_at[key] = now or _now()


def reset(key: Optional[str] = None) -> None:
    """Забути тривоги одного джерела (або всіх — для тестів)."""
    with _lock:
        if key is None:
            _last_alert_at.clear()
        else:
            _last_alert_at.pop(key, None)
