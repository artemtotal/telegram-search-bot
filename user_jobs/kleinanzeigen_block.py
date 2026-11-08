"""Kleinanzeigen відмовляє (403/429) — що бот робить сам (08.11.2026).

З 08.11 бот живе на VPS з адресою дата-центру, і того ж вечора Kleinanzeigen
один раз відповів 403 на Бранденбург. Раніше адміну одразу йшло «поверніть
рідший інтервал (KLEINANZEIGEN_CHECK_INTERVAL_SECONDS)» — порада, яку треба
виконувати руками. Тепер:

1. На 403/429 — одна пауза (RETRY_AFTER_SECONDS) і ще одна спроба напряму.
2. Знову відмова — цей запит і решта цього обходу йдуть через проксі ПК
   (KLEINANZEIGEN_PROXY, SOCKS через тунель — як DP_PROXY у dp-runner).
3. Обхід, де була відмова, подовжує інтервал: 20 → 40 → 60 хв (не більше).
   Після CALM_RUNS_TO_STEP_DOWN спокійних обходів поспіль — крок назад.
4. Адміну миттєво нічого (правило з 01.11: рутина — лише у вечірньому зведенні
   21:00): скільки було відмов, скільки пройшло через проксі, який інтервал.
   Миттєво — лише коли Kleinanzeigen не відповідає і через проксі довше
   ALERT_AFTER поспіль.

Пам'ять у процесі: після перезапуску бота інтервал знову 20 хв — так і треба.
"""

import os
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import requests

BLOCK_CODES = (403, 429)
RETRY_AFTER_SECONDS = int(os.getenv("KLEINANZEIGEN_BLOCK_RETRY_SECONDS", "45") or 45)
PROXY = os.getenv("KLEINANZEIGEN_PROXY", "").strip()
MAX_LEVEL = 2                     # 0 → 20 хв, 1 → 40, 2 → 60
CALM_RUNS_TO_STEP_DOWN = 6
ALERT_AFTER = timedelta(hours=2)
# Планувальник будить обхід кожні 20 хв; хвилинний запас, щоб «через 40 хв»
# не перетворилось на 60 через різницю в секундах між запусками.
_SLACK = timedelta(minutes=1)

_lock = threading.Lock()
_state = {"level": 0, "calm": 0, "next_run_at": None, "failing_since": None, "run": {}}
_events: List[tuple] = []         # (час, "blocked" | "proxy_ok" | "proxy_failed")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def reset() -> None:
    """Для тестів."""
    with _lock:
        _state.update(level=0, calm=0, next_run_at=None, failing_since=None, run={})
        _events.clear()


def level() -> int:
    return _state["level"]


def interval_minutes(base_seconds: int) -> int:
    return base_seconds * (_state["level"] + 1) // 60


def _event(kind: str) -> None:
    with _lock:
        _events.append((_now(), kind))
        cutoff = _now() - timedelta(days=2)
        while _events and _events[0][0] < cutoff:
            _events.pop(0)


def begin_run(now: Optional[datetime] = None) -> bool:
    """False — інтервал зараз подовжений, цей запуск пропускаємо."""
    now = now or _now()
    with _lock:
        if _state["next_run_at"] and now < _state["next_run_at"]:
            return False
        _state["run"] = {"blocked": False, "via_proxy": False}
    return True


def is_block(exc: BaseException) -> bool:
    return getattr(getattr(exc, "response", None), "status_code", None) in BLOCK_CODES


def _status(response) -> int:
    return getattr(response, "status_code", 200) or 200


def _proxied(url: str, **kwargs):
    kwargs["proxies"] = {"http": PROXY, "https": PROXY}
    try:
        response = requests.get(url, **kwargs)
    except requests.RequestException:
        _event("proxy_failed")
        raise
    _event("proxy_failed" if _status(response) in BLOCK_CODES else "proxy_ok")
    return response


def get(url: str, **kwargs):
    """requests.get з реакцією на відмову. Викликач сам робить raise_for_status()."""
    run = _state["run"]
    if run.get("via_proxy") and PROXY:
        return _proxied(url, **kwargs)
    response = requests.get(url, **kwargs)
    if _status(response) not in BLOCK_CODES:
        return response
    run["blocked"] = True
    _event("blocked")
    time.sleep(RETRY_AFTER_SECONDS)
    response = requests.get(url, **kwargs)
    if _status(response) not in BLOCK_CODES or not PROXY:
        return response
    run["via_proxy"] = True
    return _proxied(url, **kwargs)


def end_run(ok: bool, base_seconds: int, now: Optional[datetime] = None) -> None:
    now = now or _now()
    with _lock:
        if _state["run"].get("blocked"):
            _state["level"] = min(_state["level"] + 1, MAX_LEVEL)
            _state["calm"] = 0
        else:
            _state["calm"] += 1
            if _state["level"] and _state["calm"] >= CALM_RUNS_TO_STEP_DOWN:
                _state["level"] -= 1
                _state["calm"] = 0
        step = timedelta(seconds=base_seconds * (_state["level"] + 1))
        _state["next_run_at"] = now + step - _SLACK if _state["level"] else None
        if ok:
            _state["failing_since"] = None
        elif _state["failing_since"] is None:
            _state["failing_since"] = now


def failing_too_long(now: Optional[datetime] = None) -> bool:
    since = _state["failing_since"]
    return since is not None and (now or _now()) - since >= ALERT_AFTER


def summary_lines(since: datetime, until: datetime, base_seconds: int) -> List[str]:
    """Рядки для вечірнього зведення; порожньо — день без відмов і інтервал звичайний."""
    def aware(value: datetime) -> datetime:
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

    since, until = aware(since), aware(until)
    with _lock:
        kinds = [kind for at, kind in _events if since <= at < until]
    if not kinds and not _state["level"]:
        return []
    blocked, ok, failed = kinds.count("blocked"), kinds.count("proxy_ok"), kinds.count("proxy_failed")
    line = f"🚫 Kleinanzeigen: відмов 403/429 — <b>{blocked}</b>"
    if ok or failed:
        line += f", через проксі ПК пройшло {ok}" + (f", не пройшло {failed}" if failed else "")
    line += f"; інтервал зараз {interval_minutes(base_seconds)} хв"
    return [line]
