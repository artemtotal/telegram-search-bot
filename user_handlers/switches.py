"""Вимикачі нових функцій бота: змінна оточення або файл у томі з даними (26.11.2026).

Контейнер бота не перестворюємо, тож нові змінні оточення додати не можна. Тому вимикач читається
так само, як у masters_hint: змінна оточення важливіша (якщо вона є й не порожня), інакше файл
/app/config/bot_switches.json, наприклад {"housing_pay_nudge": true, "ad_shadow": true}.
Файл читається з кешем за часом зміни, тому правка діє без перезапуску; перезапуск теж підходить.
"""
import json
import logging
import os
from typing import Dict

logger = logging.getLogger(__name__)

CONFIG_PATH = os.environ.get("BOT_SWITCHES_CONFIG", "/app/config/bot_switches.json")

_cache: Dict[str, object] = {"path": None, "mtime": None, "data": {}}

_TRUE = ("1", "true", "on", "yes", "так", "да")


def _file_cfg() -> dict:
    path = CONFIG_PATH
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {}
    if _cache["path"] == path and _cache["mtime"] == mtime:
        return _cache["data"]  # type: ignore[return-value]
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        logger.warning("Could not read %s", path)
        data = {}
    _cache.update(path=path, mtime=mtime, data=data)
    return data


def flag(env_name: str, key: str, default: bool) -> bool:
    """Змінна оточення (якщо задана й не порожня), інакше ключ у файлі, інакше типове значення."""
    raw = os.environ.get(env_name)
    if raw is None or raw.strip() == "":
        raw = _file_cfg().get(key)
    if raw is None:
        return default
    return str(raw).strip().lower() in _TRUE
