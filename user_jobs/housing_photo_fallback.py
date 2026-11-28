"""Фото в уведомлениях о квартирах: запасной путь, когда Telegram не смог сам скачать картинку (28.11.2026).

Бот отдаёт Telegram адрес фото, и Telegram качает его сам. Иногда у него это не выходит
(`webpage_curl_failed`, `Failed to get http url content`: сетевой сбой, лимит CDN, редирект). Для альбома хуже
всего: одна плохая картинка из десяти отклоняет весь альбом, и раньше уведомление молча уходило голым текстом.

Теперь `send_photo` и `send_media_group` бота обёрнуты: при такой ошибке неудавшуюся картинку (в альбоме Telegram
называет её номером: «message #4») мы скачиваем сами и отправляем файлом; если скачать нельзя (404, не картинка,
слишком большая), она просто выпадает из альбома; если не осталось ни одной, исходная ошибка летит дальше и вызывающий
код делает прежний запасной ход (текст). Чужой запасной картинки нет.

Нагрузка на площадки: одно скачивание на неудавшуюся картинку, только после отказа Telegram. Всё в памяти, файлов
на диске нет. Любая другая ошибка (бот заблокирован, чат не найден, сеть) проходит как раньше, без повторов.
"""
import io
import ipaddress
import logging
import re
import threading
from typing import List, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

MAX_BYTES = 9 * 1024 * 1024          # лимит Telegram на фото 10 МБ
CONNECT_TIMEOUT = 5
READ_TIMEOUT = 10
MAX_ATTEMPTS = 3
USER_AGENT = "Mozilla/5.0 (compatible; PotsdambotImageFetch/1.0)"

# Ответы Bot API, при которых имеет смысл скачать картинку самим.
_RETRIABLE = (
    "webpage_curl_failed", "failed to get http url content", "wrong file identifier/http url",
    "wrong type of the web page content", "http url specified", "photo_invalid_dimensions",
    "image_process_failed", "photo_ext_invalid", "photo_save_file_invalid",
)
_INDEX_RE = re.compile(r"message #(\d+)")
# Рекламные баннеры порталов (Vonovia: «зелёный ток»), а не фото квартиры.
_PLACEHOLDER_RE = re.compile(r"/CAMP-", re.I)


# 28.11.2026: след последней отправки фото в этом потоке; DeliveryGate.sent() превращает его в одну строку журнала.
_local = threading.local()


def reset_trace() -> None:
    _local.trace = None


def _note(kind: str, count: int, fallback: bool) -> None:
    _local.trace = (kind, int(count), bool(fallback))


def log_send(source: str) -> None:
    """Одна строка INFO на отправленную квартиру: с фото или без. Ни чатов, ни адресов, ни имён."""
    kind, count, fallback = getattr(_local, "trace", None) or ("none", 0, False)
    _local.trace = None
    logger.info("housing send: source=%s photo=%s n=%d fallback=%s", source, kind, count, "yes" if fallback else "no")


def _media_count(args, kwargs) -> int:
    media = kwargs.get("media")
    if media is None and len(args) > 1:
        media = args[1]
    try:
        return len(media)
    except TypeError:
        return 0


def is_placeholder(url) -> bool:
    """True для рекламной картинки портала, которую нельзя выдавать за фото квартиры."""
    return bool(_PLACEHOLDER_RE.search(str(url or "")))


def real_photos(urls) -> List[str]:
    return [u for u in (str(x or "").strip() for x in (urls or [])) if u and not is_placeholder(u)]


def retriable(exc: BaseException) -> bool:
    from telegram.error import BadRequest
    if not isinstance(exc, BadRequest):
        return False
    text = str(exc).lower()
    return any(marker in text for marker in _RETRIABLE)


def failed_index(exc: BaseException, count: int) -> Optional[int]:
    """Какой по счёту элемент альбома не принят (с нуля) или None, если Telegram не сказал."""
    found = _INDEX_RE.search(str(exc))
    if not found:
        return None
    index = int(found.group(1)) - 1
    return index if 0 <= index < count else None


def _public_http(url: str) -> bool:
    try:
        parts = urlparse(url)
    except ValueError:
        return False
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False
    try:
        address = ipaddress.ip_address(parts.hostname)
    except ValueError:
        return parts.hostname not in ("localhost",)
    return address.is_global


def fetch(url: str) -> Optional[bytes]:
    """Одно скачивание картинки в памяти; None, если это не картинка или она не подошла."""
    if not _public_http(url):
        return None
    try:
        import requests
        with requests.get(
            url, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT), stream=True, allow_redirects=True,
            headers={"User-Agent": USER_AGENT, "Accept": "image/jpeg,image/png,image/*;q=0.8"},
        ) as response:
            if response.status_code != 200:
                return None
            length = response.headers.get("Content-Length")
            if length and length.isdigit() and int(length) > MAX_BYTES:
                return None
            data = bytearray()
            for chunk in response.iter_content(65536):
                data.extend(chunk)
                if len(data) > MAX_BYTES:
                    return None
    except Exception as exc:
        logger.info("Photo download failed for %s: %s", url[:120], exc)
        return None
    return _as_telegram_photo(bytes(data))


def _as_telegram_photo(data: bytes) -> Optional[bytes]:
    """JPEG и PNG в допустимых размерах уходят как есть; остальное (webp, avif...) и слишком большое перекодируется в JPEG."""
    if not data:
        return None
    try:
        from PIL import Image
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            width, height = image.size
            fmt = (image.format or "").upper()
            if not width or not height or max(width, height) / min(width, height) > 20:
                return None
            if fmt in ("JPEG", "PNG") and width + height <= 10000:
                return data
            if width + height > 10000:
                scale = 4000.0 / max(width, height)
                image = image.resize((max(1, int(width * scale)), max(1, int(height * scale))))
            out = io.BytesIO()
            image.convert("RGB").save(out, "JPEG", quality=88)
            return out.getvalue() if out.tell() <= MAX_BYTES else None
    except Exception as exc:
        logger.info("Downloaded file is not a usable photo: %s", exc)
        return None


def _send_photo_retry(orig, bot, args, kwargs, exc):
    url = kwargs.get("photo")
    if not isinstance(url, str):
        raise exc
    data = fetch(url)
    if data is None:
        logger.warning("Photo %s refused by Telegram and not downloadable; caller falls back to text", url[:120])
        raise exc
    logger.warning("Photo %s refused by Telegram (%s); sending it as a file", url[:120], exc)
    return orig(bot, *args, **dict(kwargs, photo=data))


def _send_group_retry(orig, bot, args, kwargs, exc):
    media = kwargs.get("media")
    if not media:
        raise exc
    # (источник: адрес или байты, подпись, режим разметки) — собственные записи, у InputMediaPhoto media после сборки уже InputFile
    items = []
    for item in media:
        source = getattr(item, "media", None)
        if not isinstance(source, str):
            raise exc
        items.append([source, getattr(item, "caption", None), getattr(item, "parse_mode", None)])
    for _ in range(MAX_ATTEMPTS):
        index = failed_index(exc, len(items))
        targets = [index] if index is not None else list(range(len(items)))
        for i in sorted(targets, reverse=True):
            source = items[i][0]
            if not isinstance(source, str):
                raise exc
            data = fetch(source)
            if data is None:
                logger.warning("Album photo %s refused by Telegram and not downloadable; dropped", source[:120])
                dropped = items.pop(i)
                if i == 0 and dropped[1] and items:
                    items[0][1], items[0][2] = dropped[1], dropped[2]
            else:
                items[i][0] = data
        if not items:
            raise exc
        if len(items) == 1:
            source, caption, parse_mode = items[0]
            photo_kwargs = {k: v for k, v in kwargs.items() if k not in ("media",)}
            photo_kwargs.update(photo=source, caption=caption)
            if caption and parse_mode:
                photo_kwargs["parse_mode"] = parse_mode
            from telegram import Bot
            return [Bot.send_photo(bot, *args, **photo_kwargs)]
        from telegram import InputMediaPhoto
        rebuilt = [InputMediaPhoto(media=s, caption=c, parse_mode=p) if c else InputMediaPhoto(media=s) for s, c, p in items]
        try:
            logger.warning("Album refused by Telegram (%s); resending with %d photo(s)", exc, len(rebuilt))
            return orig(bot, *args, **dict(kwargs, media=rebuilt))
        except Exception as again:
            if not retriable(again):
                raise
            exc = again
    raise exc


def install() -> bool:
    """Оборачивает telegram.Bot.send_photo / send_media_group один раз за процесс."""
    import telegram
    if getattr(telegram.Bot.send_photo, "_housing_photo_fallback", False):
        return False
    orig_photo = telegram.Bot.send_photo
    orig_group = telegram.Bot.send_media_group

    def send_photo(self, *args, **kwargs):
        try:
            result = orig_photo(self, *args, **kwargs)
        except Exception as exc:
            if not retriable(exc):
                _note("none", 0, True)       # вызывающий код пошлёт текст
                raise
            try:
                result = _send_photo_retry(orig_photo, self, args, kwargs, exc)
            except Exception:
                _note("none", 0, True)
                raise
            _note("file", 1, True)
            return result
        _note("photo", 1, False)
        return result

    def send_media_group(self, *args, **kwargs):
        try:
            result = orig_group(self, *args, **kwargs)
        except Exception as exc:
            if not retriable(exc):
                _note("none", 0, True)
                raise
            try:
                result = _send_group_retry(orig_group, self, args, kwargs, exc)
            except Exception:
                _note("none", 0, True)
                raise
            count = len(result) if isinstance(result, (list, tuple)) else _media_count(args, kwargs)
            _note("album" if count > 1 else "file", count, True)
            return result
        _note("album", _media_count(args, kwargs), False)
        return result

    for wrapper, original in ((send_photo, orig_photo), (send_media_group, orig_group)):
        wrapper.__name__ = original.__name__
        wrapper.__doc__ = original.__doc__
        wrapper._housing_photo_fallback = True
    telegram.Bot.send_photo = send_photo
    telegram.Bot.send_media_group = send_media_group
    return True
