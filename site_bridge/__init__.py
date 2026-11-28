"""Мост сайта «Потсдам» с ботом (пятая очередь, 27.11.2026).

Сайт (контейнер mastera) рисует интерфейс, а всё, что связано с данными, правами, оплатой и текстами, делает бот. Сайт зовёт бота так:

    POST http://tgbot:5013/int/<domain>/<op>
    X-Bridge-Key: <общий секрет>
    {"user_id": 123, "lang": "uk", "params": {...}}

Ответ всегда JSON: {"ok": true, "data": {...}} или {"ok": false, "error": "<код>", "message": "<текст для человека, необязательно>"}.
Коды HTTP: 200 ok; 400 плохой запрос (error "bad_request"); 403 нет/неверный секрет; 404 нет такой области/операции или запрос пришёл
через прокси; 413 слишком большое тело; 422 операция отказала по делу (error = код операции, например "limit", "not_allowed"); 500 внутренняя ошибка.

Безопасность. Сервер порта 5013 доступен снаружи (Traefik и прежний Funnel ведут на него старый кабинет), поэтому:
  * маршрут принимает только запросы с верным секретом (сравнение постоянного времени); секрет читается из файла BRIDGE_KEY_FILE
    (в томе данных бота, по умолчанию /app/config/bridge.key; нет файла: мост выключен, все /int/* отвечают 404);
  * запрос с заголовками прокси (X-Forwarded-*, Forwarded, X-Real-IP) отвечает 404, ещё до проверки секрета: сайт ходит напрямую по
    внутренней сети и таких заголовков не шлёт, а всё, что пришло через Traefik/Funnel, их несёт;
  * user_id приходит ТОЛЬКО от сайта, который уже проверил подпись Telegram; бот ему верит, потому что секрет знает только сайт.

Как добавить операцию: в файле своей области (housing.py, pay.py, terms.py, letter.py, misc.py)

    from site_bridge import op, Refuse

    @op("housing", "list")
    def list_(user_id, lang, params):
        ...                      # вызывать СУЩЕСТВУЮЩИЕ функции бота; права, лимиты, тексты, цены не переписывать
        return {"items": [...]}  # любой JSON-совместимый словарь
        # отказ по делу: raise Refuse("limit", "текст для человека")

Файлы областей друг друга не трогают. Подробности для авторов: docs/bridge-contract.md в каталоге сайта.
"""

from __future__ import annotations

import hmac
import importlib
import json
import logging
import os
import time
from typing import Callable, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

KEY_FILE = os.environ.get("BRIDGE_KEY_FILE", "/app/config/bridge.key")
DOMAINS = ("housing", "pay", "terms", "letter", "misc")      # по файлу на область
DEFAULT_MAX_BODY = 64 * 1024
PROXY_HEADERS = ("x-forwarded-for", "x-forwarded-host", "x-forwarded-proto", "x-forwarded-server", "forwarded", "x-real-ip")
LANGS = ("uk", "ru", "de")

_OPS: Dict[Tuple[str, str], dict] = {}
_loaded = False


class Refuse(Exception):
    """Операция отказала по делу (не ошибка бота): код и текст для человека уходят на сайт, HTTP 422."""

    def __init__(self, code: str, message: str = "", **extra):
        super().__init__(code)
        self.code = code
        self.message = message
        self.extra = extra          # дополнительные поля ответа (например feature у замка)


class Paywall(Refuse):
    """Функция закрыта подпиской (28.11.2026): ответ единый для всех операций моста, `{ok:false, error:"paywall", feature:"<ключ>"}`.
    Ключ из `paywall.features` (docs/paywall-contract.md); сайт на такую ошибку показывает лист замка."""

    def __init__(self, feature: str, message: str = ""):
        super().__init__("paywall", message, feature=feature)


def op(domain: str, name: str, need_user: bool = True, max_body: int = DEFAULT_MAX_BODY) -> Callable:
    """Регистрирует операцию. Функция получает (user_id: int, lang: str, params: dict) и возвращает словарь.
    need_user=False: операция не привязана к человеку (user_id может быть 0). max_body: допустимый размер тела запроса в байтах
    (по умолчанию 64 КБ; для загрузки файлов, например чека, поднять явно)."""

    def deco(fn: Callable) -> Callable:
        _OPS[(domain, name)] = {"fn": fn, "need_user": bool(need_user), "max_body": int(max_body)}
        return fn

    return deco


def registered() -> list:
    load()
    return sorted("%s.%s" % key for key in _OPS)


def load() -> None:
    """Подгружает файлы областей; сломанная область не мешает остальным (запись в журнал)."""
    global _loaded
    if _loaded:
        return
    _loaded = True
    for domain in DOMAINS:
        try:
            importlib.import_module("site_bridge." + domain)
        except Exception:
            logger.exception("site_bridge: area %s failed to load", domain)


def _key() -> Optional[bytes]:
    try:
        with open(KEY_FILE, "rb") as handle:
            value = handle.read().strip()
        return value or None
    except OSError:
        return None


def bot():
    """telegram.Bot для операций, которым нужно что-то отправить (например чек админу). Токен из окружения, как у самого бота."""
    import telegram

    if not hasattr(bot, "_b"):
        bot._b = telegram.Bot(os.environ["BOT_TOKEN"])
    return bot._b


def _reply(handler, status: int, obj: dict) -> None:
    body = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.end_headers()
    if getattr(handler, "command", "") != "HEAD":
        handler.wfile.write(body)


def _not_found(handler) -> None:
    _reply(handler, 404, {"ok": False, "error": "not_found"})


def handle(handler) -> bool:
    """Вызывается из встроенного HTTP-сервера бота. True: путь был /int/..., ответ уже отправлен (вызывающему больше ничего не делать)."""
    path = handler.path.split("?", 1)[0]
    if not path.startswith("/int/"):
        return False
    try:
        _dispatch(handler, path)
    except Exception:
        logger.exception("site_bridge: unexpected failure on %s", path)
        try:
            _reply(handler, 500, {"ok": False, "error": "internal"})
        except Exception:
            pass
    return True


def _dispatch(handler, path: str) -> None:
    # 1. всё, что пришло через прокси, и всё без включённого моста: 404, секрет даже не проверяем
    if any(handler.headers.get(h) for h in PROXY_HEADERS):
        return _not_found(handler)
    key = _key()
    if not key:
        return _not_found(handler)
    # 2. секрет
    got = (handler.headers.get("X-Bridge-Key") or "").encode()
    if not got or not hmac.compare_digest(got, key):
        return _reply(handler, 403, {"ok": False, "error": "forbidden"})
    if handler.command != "POST":
        return _reply(handler, 405, {"ok": False, "error": "method"})
    # 3. маршрут
    load()
    parts = path[len("/int/"):].strip("/").split("/")
    entry = _OPS.get((parts[0], parts[1])) if len(parts) == 2 else None
    if not entry:
        return _not_found(handler)
    # 4. тело
    try:
        length = int(handler.headers.get("Content-Length") or 0)
    except ValueError:
        length = -1
    if length < 0:
        return _reply(handler, 400, {"ok": False, "error": "bad_request"})
    if length > entry["max_body"]:
        return _reply(handler, 413, {"ok": False, "error": "too_large"})
    try:
        req = json.loads(handler.rfile.read(length) or b"{}")
        if not isinstance(req, dict):
            raise ValueError
        user_id = req.get("user_id", 0)
        if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id < 0:
            raise ValueError
        params = req.get("params") or {}
        if not isinstance(params, dict):
            raise ValueError
    except (ValueError, TypeError):
        return _reply(handler, 400, {"ok": False, "error": "bad_request"})
    if entry["need_user"] and user_id <= 0:
        return _reply(handler, 400, {"ok": False, "error": "bad_request", "message": "user_id"})
    lang = req.get("lang")
    if lang not in LANGS:
        lang = _bot_lang(user_id)
    # 5. операция
    started = time.time()
    try:
        data = entry["fn"](user_id, lang, params)
    except Refuse as refusal:
        body = {"ok": False, "error": refusal.code, "message": refusal.message}
        body.update(getattr(refusal, "extra", None) or {})
        return _reply(handler, 422, body)
    except Exception:
        logger.exception("site_bridge: %s/%s failed", parts[0], parts[1])
        return _reply(handler, 500, {"ok": False, "error": "internal"})
    logger.info("site_bridge: %s.%s ok in %.0f ms", parts[0], parts[1], (time.time() - started) * 1000)
    _reply(handler, 200, {"ok": True, "data": data if data is not None else {}})


def _bot_lang(user_id: int) -> str:
    if user_id > 0:
        try:
            import i18n

            return i18n.get_lang(user_id)
        except Exception:
            pass
    return "uk"
