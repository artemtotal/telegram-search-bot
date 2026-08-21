# coding: utf-8
"""Minimal per-user i18n: keyed JSON translation files (translations/<lang>.json),
resolved per-request via t(key, lang). Ukrainian is the source of truth — a
missing key/lang falls back to it rather than raising, so a translation gap
shows up as Ukrainian text (or the raw key, as a last resort) instead of a
crashed handler. See tests/test_i18n.py for the coverage guarantee that keeps
ru/de from silently drifting out of sync with uk.
"""

import json
import os

from user_jobs import user_settings_store

SUPPORTED_LANGS = ("uk", "ru", "de")
DEFAULT_LANG = "uk"
LANG_LABELS = {"uk": "🇺🇦 Українська", "ru": "🇷🇺 Русский", "de": "🇩🇪 Deutsch"}

_TRANSLATIONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "translations")


def _load_translations():
    translations = {}
    for lang in SUPPORTED_LANGS:
        path = os.path.join(_TRANSLATIONS_DIR, f"{lang}.json")
        with open(path, "r", encoding="utf-8") as fh:
            translations[lang] = json.load(fh)
    return translations


_TRANSLATIONS = _load_translations()


def get_lang(user_id: int) -> str:
    lang = user_settings_store.get_language(user_id)
    return lang if lang in SUPPORTED_LANGS else DEFAULT_LANG


def t(key: str, lang: str = DEFAULT_LANG, **kwargs) -> str:
    table = _TRANSLATIONS.get(lang) or _TRANSLATIONS[DEFAULT_LANG]
    text = table.get(key) or _TRANSLATIONS[DEFAULT_LANG].get(key) or f"[[{key}]]"
    if kwargs:
        try:
            return text.format(**kwargs)
        except (KeyError, IndexError):
            return text
    return text
