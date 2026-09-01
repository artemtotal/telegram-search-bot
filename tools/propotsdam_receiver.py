"""Host-side ProPotsdam/easysquare collector HTTP service.

Credentials are read from environment variables only:
- PROPOTSDAM_USERNAME
- PROPOTSDAM_PASSWORD

Run this with its own interpreter, never a bare ``python`` off PATH. It used to
resolve to Hermes' virtualenv, which left this service — and the Playwright
driver it spawns out of that venv's site-packages — holding files inside it, and
``hermes update`` aborts while anything locks them. The dedicated venv lives
outside this repository on purpose: the repo doubles as a Docker build context
with no .dockerignore, so a local .venv would be copied into the bot image.

    uv venv C:\\opt\\propotsdam-receiver\\.venv --python 3.11
    uv pip install --python C:\\opt\\propotsdam-receiver\\.venv\\Scripts\\python.exe "playwright>=1.54,<2"

Playwright's bundled browsers are not needed: ``scan`` drives the system Chrome
through ``PROPOTSDAM_BROWSER``.

Photos are cached here rather than linked. ``api5/accndocs2/<resourceId>`` only
opens under a portal login, so Telegram cannot fetch one by URL and the bot —
which runs in a container with neither the session nor the Chrome profile —
cannot either. ``scan`` therefore downloads every listing's photos through the
browser's own session and serves them as bytes on
``GET /api/propotsdam/photo/<resourceId>``:

- ``PROPOTSDAM_PHOTO_DIR`` — cache directory (default: next to the profile)
- ``PROPOTSDAM_PHOTO_KEEP_DAYS`` — drop photos unseen for this long, 0 disables
- ``PROPOTSDAM_PHOTO_MAX_BYTES`` — refuse anything larger
- ``PROPOTSDAM_PHOTO_TIMEOUT_MS`` — per-photo download timeout
"""

import hashlib
import json
import logging
import os
import re
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from urllib.parse import parse_qs, unquote, urlsplit

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from user_jobs import propotsdam_parser

logger = logging.getLogger("propotsdam_receiver")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

HOST = os.getenv("PROPOTSDAM_RECEIVER_HOST", "127.0.0.1")
PORT = int(os.getenv("PROPOTSDAM_RECEIVER_PORT", "18766") or 18766)
PORTAL_URL = propotsdam_parser.PORTAL_URL
PROFILE_DIR = Path(os.getenv("PROPOTSDAM_PROFILE_DIR", str(Path.home() / "AppData" / "Local" / "PotsdamBot" / "propotsdam-browser")))
BROWSER = os.getenv("PROPOTSDAM_BROWSER", r"C:\Program Files\Google\Chrome\Application\chrome.exe")
PHOTO_DIR = Path(os.getenv("PROPOTSDAM_PHOTO_DIR", str(PROFILE_DIR.parent / "propotsdam-photos")))
PHOTO_MAX_BYTES = int(os.getenv("PROPOTSDAM_PHOTO_MAX_BYTES", str(12 * 1024 * 1024)) or 12 * 1024 * 1024)
PHOTO_KEEP_DAYS = int(os.getenv("PROPOTSDAM_PHOTO_KEEP_DAYS", "30") or 30)
PHOTO_TIMEOUT_MS = int(os.getenv("PROPOTSDAM_PHOTO_TIMEOUT_MS", "30000") or 30000)
DETAIL_DIR = Path(os.getenv("PROPOTSDAM_DETAIL_DIR", str(PROFILE_DIR.parent / "propotsdam-details")))
DETAIL_MAX_PER_SCAN = int(os.getenv("PROPOTSDAM_DETAIL_MAX", "3") or 3)
DETAIL_ENABLED = os.getenv("PROPOTSDAM_DETAIL_ENABLED", "1").strip() not in {"", "0", "false", "no"}
_scan_lock = Lock()
_state_lock = Lock()
_last_result = {"ok": False, "error": "no scan yet", "listings": []}
_scan_running = False
_scan_started_at = None


def _click_text(page, patterns, timeout=5000):
    for pattern in patterns:
        try:
            page.get_by_text(re.compile(pattern, re.I)).first.click(timeout=timeout)
            page.wait_for_timeout(1200)
            return True
        except Exception:
            pass
    return False


def _first_visible(page, selectors, timeout=5000):
    """Первое реально видимое поле из списка вариантов.

    Портал построен на SAPUI5, и в разметке хватает скрытых полей: слепой
    `.first` по общему селектору упирался в одно из них и падал по таймауту —
    именно так однажды перестал работать вход, а следом отвалились фото,
    которые открываются только под логином.
    """
    for selector in selectors:
        locator = page.locator(selector)
        for index in range(min(locator.count(), 5)):
            candidate = locator.nth(index)
            try:
                if candidate.is_visible():
                    candidate.wait_for(state="visible", timeout=timeout)
                    return candidate
            except Exception:
                continue
    return None


def _login_if_needed(page):
    username = os.getenv("PROPOTSDAM_USERNAME", "")
    password = os.getenv("PROPOTSDAM_PASSWORD", "")
    if not username or not password:
        raise RuntimeError("PROPOTSDAM_USERNAME/PROPOTSDAM_PASSWORD are not set")
    if not (page.locator("input[type='password']").count() or page.get_by_text(re.compile("anmelden", re.I)).count()):
        return
    # Кнопка «Anmelden» стартового экрана — она же открывает саму форму.
    for selector in ("#easy-login", "button:has-text('Anmelden')"):
        try:
            page.locator(selector).first.click(timeout=3000)
            page.wait_for_timeout(1500)
            break
        except Exception:
            continue
    email = _first_visible(page, (
        "input[name='login-user']",
        "input[placeholder*='Mail' i]",
        "input[type='email']",
        "input[name*='mail' i]",
        "input[type='text']",
    ))
    password_field = _first_visible(page, ("input[name='login-password']", "input[type='password']"))
    if email is None or password_field is None:
        # Слово «Anmelden» трапляється і в уже відкритому порталі, тож сюди
        # можна дійти залогіненим — тоді полів просто немає, і це не помилка.
        logger.info("ProPotsdam: форми входу немає, вважаємо, що сесія вже є")
        return
    email.fill(username, timeout=7000)
    password_field.fill(password, timeout=7000)
    # Кнопка отправки формы называется так же, как та, что форму открыла, —
    # берём именно ту, что лежит рядом с полями.
    for selector in ("[id*='LoginOkBtn']", "button:has-text('Anmelden')"):
        try:
            page.locator(selector).last.click(timeout=3000)
            break
        except Exception:
            continue
    else:
        password_field.press("Enter")
    try:
        page.wait_for_load_state("networkidle", timeout=20000)
    except PlaywrightTimeoutError:
        pass
    page.wait_for_timeout(2000)


def _navigate_to_list(page):
    page.goto(PORTAL_URL, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(3000)
    _login_if_needed(page)
    for pattern in ["Immobiliensuche", "^Immobilien$", "Immobilien"]:
        _click_text(page, [pattern], timeout=5000)
    try:
        page.wait_for_load_state("networkidle", timeout=20000)
    except PlaywrightTimeoutError:
        pass
    page.wait_for_timeout(3000)
    _load_all_pages(page)


def _load_all_pages(page, max_clicks=50):
    """The listing view paginates behind a "mehr anzeigen" (show more)
    button instead of showing everything up front. A single click - which
    is all this used to do, as part of _navigate_to_list's one-shot pattern
    list - only ever loaded the first page, so listings on later pages
    (including the last one) were silently never scanned. Keeps clicking
    until the button is gone (or `max_clicks` as a safety cap against an
    unexpected always-present button) so every page's xmlforms response
    gets captured by `scan`'s response listener.
    """
    for _ in range(max_clicks):
        if not _click_text(page, ["mehr anzeigen"], timeout=3000):
            return
        try:
            page.wait_for_load_state("networkidle", timeout=20000)
        except PlaywrightTimeoutError:
            pass


def _extract_cards_from_dom(page):
    raw_cards = page.evaluate(
        """
        () => {
          const textOf = (node) => (node?.innerText || '').replace(/\s+/g, ' ').trim();
          const nodes = [...document.querySelectorAll('mat-card, .mat-card, [class*=card], [class*=list-item], [class*=object], [class*=immobil], li, article')];
          return nodes.map((node) => ({
            text: textOf(node),
            links: [...node.querySelectorAll('a[href]')].map(a => a.href),
            images: [...node.querySelectorAll('img[src]')].map(img => img.src),
          })).filter(x => x.text && /(Zimmer|Wohnfläche|Gesamt|Stadtteil|Verfügbar|m²|EUR)/i.test(x.text));
        }
        """
    )
    listings = []
    for card in raw_cards:
        text = card.get("text") or ""
        extra = {"raw_text": text}
        def after(label):
            match = re.search(label + r"\s*:?\s*([^|\n]+?)(?=\s+(?:Stadtteil|Zimmer|Wohnfläche|Gesamt|Verfügbar)|$)", text, re.I)
            return match.group(1).strip() if match else ""
        listings.append(propotsdam_parser.normalize_listing({
            "title": text.split(" Stadtteil ")[0].strip()[:160],
            "address": after("Adresse"),
            "district": after("Stadtteil"),
            "rooms": after("Zimmer"),
            "area": after("Wohnfläche"),
            "total_rent": after("Gesamtmiete|Gesamtmi(?:ete)?"),
            "available_from": after("Verfügbar(?: ab)?"),
            "detail_url": (card.get("links") or [""])[0],
            "image_url": (card.get("images") or [""])[0],
            "extra": extra,
        }))
    return listings


PHOTO_ROUTE = "/api/propotsdam/photo/"


def _content_type(body):
    if body.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if body.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if body.startswith(b"RIFF") and body[8:12] == b"WEBP":
        return "image/webp"
    return "application/octet-stream"


def _photo_path(resource_id):
    """Файл кеша для resourceId — или None, если id непохож на настоящий.

    Проверка не косметическая: этот же id приходит снаружи HTTP-запросом за
    фото, и без неё «../..» в пути превратился бы в чтение чужого файла.
    """
    candidate = unquote(str(resource_id or "")).strip()
    if not propotsdam_parser.RESOURCE_ID_RE.fullmatch(candidate):
        return None
    return PHOTO_DIR / "{}.bin".format(candidate)


RESOURCE_ID_ATTR_RE = re.compile(r'resourceId="([^"]+)"')


def _listing_detail_path(listing):
    """Имя файла снимка. Ключом бывает и GUID портала, и — на DOM-пути —
    целый URL, из которого имя файла не сделать; такие сводим к хешу, чтобы
    объявление не осталось без снимка вовсе."""
    key = str(listing.get("listing_key") or "").strip()
    if not key:
        return None
    if not propotsdam_parser.RESOURCE_ID_RE.fullmatch(key):
        key = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
    return DETAIL_DIR / "{}.json".format(key)


def _open_offer_list(page):
    """Доводить сторінку до самого переліку квартир.

    Не через `_navigate_to_list`: той перебирає ще й «^Immobilien$», і меню
    встигає згорнутись — клік тоді летить у невидимий елемент. Тут потрібні
    рівно три кроки: розкрити плитку «Immobiliensuche», дотиснути «mehr
    anzeigen» і взяти ОСТАННІЙ збіг «Immobilien». Перший — заголовок розділу,
    він нікуди не веде; клік по самому боксу відкриває натомість «Anfragen».
    """
    # Портал — SPA з хеш-адресою: goto на ту саму адресу нічого не перезавантажує,
    # і меню лишається в тому стані, в якому його покинув попередній крок обходу.
    # Саме тому знімок працював у свіжій вкладці й мовчки не працював у обході.
    page.goto(PORTAL_URL, wait_until="domcontentloaded", timeout=60000)
    page.reload(wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(3000)
    _login_if_needed(page)
    page.wait_for_timeout(1500)
    # Тільки «Immobiliensuche»: вона й розкриває підменю. Додатковий клік по
    # «MEHR ANZEIGEN» тут шкідливий — після розкриття перша така кнопка вже
    # належить розділу «Anfragen», і обхід їхав саме туди.
    _click_text(page, ["Immobiliensuche"], timeout=4000)
    page.wait_for_timeout(1500)
    items = page.get_by_text(re.compile(r"^\s*Immobilien\s*$", re.I))
    for index in range(items.count() - 1, -1, -1):
        candidate = items.nth(index)
        try:
            if not candidate.is_visible():
                continue
            candidate.click(timeout=4000, force=True)
        except Exception:
            continue
        page.wait_for_timeout(4000)
        if re.search(r"Zimmer|Gesamtmiete|EINTR", page.evaluate("() => document.body.innerText") or "", re.I):
            return True
    return False


def _open_listing(page, listing):
    """Раскрывает карточку объявления в списке портала.

    Обычный click тут не работает: поверх карточки лежит собственный
    контейнер портала и перехватывает указатель, поэтому событие шлём
    элементу напрямую — так же, как приходится добираться до самого списка.
    """
    needles = [str(listing.get("title") or "").strip()[:40], str(listing.get("address") or "").strip()]
    for needle in [item for item in needles if len(item) > 6]:
        try:
            target = page.get_by_text(needle, exact=False).first
            target.dispatch_event("click")
            page.wait_for_timeout(3500)
            return True
        except Exception:
            continue
    return False


def _capture_details(page, listings):
    """Открывает карточки объявлений и забирает то, чего нет в списке.

    В списке портал показывает только Gesamtmiete и пару обложек, хотя сам
    знает и Kaltmiete (в его форме поиска есть отдельные поля "Kaltmiete bis"
    и "Gesamtmiete bis"), и полную галерею. Сюда же попадает всё остальное
    содержимое карточки: пока неизвестно, как именно портал верстает деталь,
    поэтому снимок сохраняется целиком — по нему потом пишется точный разбор,
    а не наугад.

    Каждое объявление открывается один раз за всю его жизнь: снимок уже есть —
    значит и фото из него уже забраны. Шаг целиком необязательный, любая его
    ошибка не должна ронять сам обход.
    """
    if not DETAIL_ENABLED or not listings:
        return {"opened": 0, "resource_ids": [], "skipped": 0}
    # Знімок робиться в тій самій вкладці, але ОСТАННІМ кроком обходу — коли
    # список уже зібрано, а фото завантажено. Окрема вкладка виглядала
    # безпечнішою, та портал у ній просто не відкриває перелік квартир:
    # друга «сесія перегляду» отримує урізаний інтерфейс. Тож захистом
    # служить порядок кроків, а не ізоляція.

    DETAIL_DIR.mkdir(parents=True, exist_ok=True)
    try:
        if not _open_offer_list(page):
            raise RuntimeError("перелік квартир не відкрився")
    except Exception as exc:
        logger.warning("Could not open the ProPotsdam listing view for a snapshot: %s", exc)
        return {"opened": 0, "skipped": 0, "resource_ids": []}
    opened = 0
    skipped = 0
    resource_ids = []
    for listing in listings:
        if opened >= DETAIL_MAX_PER_SCAN:
            break
        path = _listing_detail_path(listing)
        if path is None or path.exists():
            skipped += 1
            continue

        detail_xml = []
        collect = lambda response: (
            detail_xml.append(response.text()) if "xmlforms" in response.url else None
        )
        page.on("response", collect)
        try:
            if not _open_listing(page, listing):
                logger.warning("Could not open ProPotsdam listing %s", listing.get("listing_key"))
                continue
            snapshot = {
                "listing_key": listing.get("listing_key"),
                "captured_at": _now_iso(),
                "url": page.url,
                "text": page.evaluate("() => document.body.innerText") or "",
                "html": page.content(),
                "images": page.evaluate("() => [...document.querySelectorAll('img[src]')].map(i => i.src)"),
                "xmlforms": detail_xml,
            }
            path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
            opened += 1
            for xml_text in detail_xml:
                for resource_id in RESOURCE_ID_ATTR_RE.findall(xml_text):
                    if propotsdam_parser.RESOURCE_ID_RE.fullmatch(resource_id) and resource_id not in resource_ids:
                        resource_ids.append(resource_id)
            # Головне, заради чого картку взагалі відкривають: у списку є лише
            # Gesamtmiete, а всередині — повна розбивка, включно з холодною
            # орендою, яку портал нібито «не публікує».
            prices = propotsdam_parser.parse_card_prices(snapshot["text"])
            for key, value in prices.items():
                if value is not None:
                    listing[key] = value
            logger.info(
                "ProPotsdam detail %s: Kaltmiete=%s Betriebskosten=%s Heizkosten=%s Gesamtmiete=%s, фото %s",
                listing.get("listing_key"), prices.get("price_eur"), prices.get("nebenkosten_eur"),
                prices.get("heizkosten_eur"), prices.get("total_rent_eur"), len(resource_ids),
            )
        except Exception as exc:
            logger.warning("ProPotsdam detail capture failed for %s: %s", listing.get("listing_key"), exc)
        finally:
            page.remove_listener("response", collect)
            # Деталь открыта поверх списка: без возврата следующее объявление
            # искать уже негде.
            try:
                page.go_back(timeout=15000)
                page.wait_for_timeout(2000)
                back_on_list = "Zimmer" in (page.evaluate("() => document.body.innerText") or "")
            except Exception:
                back_on_list = False
            if not back_on_list and opened < DETAIL_MAX_PER_SCAN:
                # Саме _open_offer_list, а не _navigate_to_list: другий доходить
                # лише до підменю, тож після першої ж картки обхід упирався і
                # решта оголошень за один прохід не відкривалась узагалі.
                try:
                    if not _open_offer_list(page):
                        logger.warning("Could not return to the ProPotsdam listing view")
                        break
                except Exception as exc:
                    logger.warning("Could not return to the ProPotsdam listing view: %s", exc)
                    break

    logger.info("ProPotsdam details opened=%s skipped=%s extra photos=%s", opened, skipped, len(resource_ids))
    return {"opened": opened, "skipped": skipped, "resource_ids": resource_ids}


def _cache_photos(page, listings, extra_resource_ids=()):
    """Скачивает все фото объявлений сессией самого браузера.

    Ссылка api5/accndocs2/<id> открывается только под логином портала, поэтому
    Telegram по URL её не заберёт, а бот в контейнере не имеет ни сессии, ни
    доступа к профилю Chrome. Качаем здесь, где сессия уже есть, и отдаём боту
    байтами через /api/propotsdam/photo/<id>.

    Уже скачанное не перекачивается: в устоявшемся состоянии новых файлов
    столько же, сколько новых квартир, — обычно ноль.
    """
    wanted = []
    for listing in listings:
        for resource_id in propotsdam_parser.image_resource_ids(listing):
            if resource_id not in wanted:
                wanted.append(resource_id)
    # Карточка объявления показывает всю галерею, список — одну-две обложки.
    for resource_id in extra_resource_ids:
        if resource_id not in wanted:
            wanted.append(resource_id)
    if not wanted:
        return {"wanted": 0, "saved": 0, "cached": 0, "failed": 0}

    PHOTO_DIR.mkdir(parents=True, exist_ok=True)
    saved = cached = failed = 0
    for resource_id in wanted:
        path = _photo_path(resource_id)
        if path is None:
            failed += 1
            logger.warning("Skipping ProPotsdam photo with an unusable resourceId: %r", resource_id)
            continue
        if path.exists() and path.stat().st_size:
            # Пока фото есть в выдаче, оно не должно устареть до удаления.
            path.touch()
            cached += 1
            continue
        url = propotsdam_parser.IMAGE_URL_TEMPLATE.format(resource_id=resource_id)
        try:
            response = page.request.get(url, timeout=PHOTO_TIMEOUT_MS)
            if not response.ok:
                raise RuntimeError("HTTP {}".format(response.status))
            body = response.body()
            if not body:
                raise RuntimeError("empty body")
            if len(body) > PHOTO_MAX_BYTES:
                raise RuntimeError("{} bytes is over the cap".format(len(body)))
            path.write_bytes(body)
            saved += 1
        except Exception as exc:
            failed += 1
            logger.warning("Could not cache ProPotsdam photo %s: %s", resource_id, exc)
    logger.info(
        "ProPotsdam photos wanted=%s saved=%s already_cached=%s failed=%s",
        len(wanted), saved, cached, failed,
    )
    return {"wanted": len(wanted), "saved": saved, "cached": cached, "failed": failed}


def _prune_photos():
    """Удаляет фото, которых давно нет в выдаче: снятая квартира не вернётся."""
    if PHOTO_KEEP_DAYS <= 0 or not PHOTO_DIR.exists():
        return 0
    deadline = time.time() - PHOTO_KEEP_DAYS * 86400
    removed = 0
    for path in PHOTO_DIR.glob("*.bin"):
        try:
            if path.stat().st_mtime < deadline:
                path.unlink()
                removed += 1
        except OSError as exc:
            logger.warning("Could not prune cached photo %s: %s", path.name, exc)
    if removed:
        logger.info("Pruned %s cached ProPotsdam photos", removed)
    return removed


def scan():
    with _scan_lock:
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        xml_bodies = []
        responses = []
        with sync_playwright() as p:
            kwargs = {"headless": False, "viewport": {"width": 1440, "height": 1000}}
            if Path(BROWSER).exists():
                kwargs["executable_path"] = BROWSER
            context = p.chromium.launch_persistent_context(str(PROFILE_DIR), **kwargs)
            page = context.pages[0] if context.pages else context.new_page()

            def on_response(response):
                url = response.url
                if "xmlforms" not in url:
                    return
                responses.append(url)
                try:
                    xml_bodies.append(response.text())
                except Exception as exc:
                    logger.warning("Could not read xmlforms body from %s: %s", url, exc)

            page.on("response", on_response)
            _navigate_to_list(page)
            listings = []
            for xml_text in xml_bodies:
                listings.extend(propotsdam_parser.parse_boxlist_xml(xml_text))
            if not listings:
                listings = _extract_cards_from_dom(page)
            # Пока браузер жив: только у него есть сессия портала.
            # Фото — раньше снимков карточек: снимок необязателен, а фото нет,
            # и первая же попытка открыть карточку показала, почему это важно —
            # неудачный клик увёл страницу со списка, и фото не скачались вовсе.
            photos = _cache_photos(page, listings)
            details = _capture_details(page, listings)
            if details["resource_ids"]:
                extra = _cache_photos(page, [], details["resource_ids"])
                for key in photos:
                    photos[key] += extra[key]
            result = {
                "ok": True,
                "url": page.url,
                "listings": listings,
                "count": len(listings),
                "photos": photos,
                "details": {"opened": details["opened"], "skipped": details["skipped"]},
                "xmlforms": responses[-20:],
            }
            context.close()
        _prune_photos()
        return result


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _store_result(result):
    global _last_result
    result["finished_at"] = _now_iso()
    with _state_lock:
        _last_result = result
    return result


def _scan_to_result():
    try:
        return scan()
    except Exception as exc:
        logger.exception("scan failed")
        return {"ok": False, "error": str(exc), "listings": []}


def _background_scan():
    global _scan_running
    try:
        _store_result(_scan_to_result())
    finally:
        with _state_lock:
            _scan_running = False


def _start_scan():
    """Kick off a scan in the background. False when one is already running.

    The scan drives a real browser: it usually finishes in about a minute, but a
    slow portal or a long "mehr anzeigen" pagination can push it past any timeout
    the caller is willing to wait. Blocking the caller for the whole scan meant a
    single slow run was reported as a failure — and, because `scan()` serialises
    on `_scan_lock`, the next 15-minute tick queued behind it and failed too.
    """
    global _scan_running, _scan_started_at
    with _state_lock:
        if _scan_running:
            return False
        _scan_running = True
        _scan_started_at = _now_iso()
    threading.Thread(target=_background_scan, name="propotsdam-scan", daemon=True).start()
    return True


def _snapshot():
    with _state_lock:
        payload = dict(_last_result)
        payload["running"] = _scan_running
        payload["started_at"] = _scan_started_at
    return payload


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        logger.info(fmt, *args)

    def _json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _photo(self, resource_id):
        path = _photo_path(resource_id)
        if path is None or not path.exists():
            self._json(404, {"ok": False, "error": "unknown photo"})
            return
        try:
            body = path.read_bytes()
        except OSError as exc:
            logger.warning("Could not read cached photo %s: %s", resource_id, exc)
            self._json(500, {"ok": False, "error": "photo unreadable"})
            return
        self.send_response(200)
        self.send_header("Content-Type", _content_type(body))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path.startswith(PHOTO_ROUTE):
            self._photo(path[len(PHOTO_ROUTE):])
        elif path == "/health":
            snapshot = _snapshot()
            self._json(200, {
                "ok": True,
                "last_ok": bool(snapshot.get("ok")),
                "count": len(snapshot.get("listings") or []),
                "running": snapshot.get("running"),
                "finished_at": snapshot.get("finished_at"),
            })
        elif path == "/api/propotsdam/listings":
            self._json(200, _snapshot())
        else:
            self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        parts = urlsplit(self.path)
        if parts.path == "/api/propotsdam/scan":
            # ?wait=1 keeps the original blocking behaviour for manual runs.
            if parse_qs(parts.query).get("wait", ["0"])[0] == "1":
                result = _store_result(_scan_to_result())
                self._json(200 if result.get("ok") else 500, result)
                return
            started = _start_scan()
            snapshot = _snapshot()
            self._json(202, {
                "ok": True,
                "started": started,
                "running": True,
                "last_finished_at": snapshot.get("finished_at"),
                "last_ok": bool(snapshot.get("ok")),
                "last_count": len(snapshot.get("listings") or []),
            })
        else:
            self._json(404, {"ok": False, "error": "not found"})


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    logger.info("ProPotsdam receiver listening on http://%s:%s", HOST, PORT)
    server.serve_forever()


if __name__ == "__main__":
    main()
