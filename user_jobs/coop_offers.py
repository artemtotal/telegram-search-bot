"""Кооперативи Берліна — етап 9 «Берлін і Бранденбург» (02.11.2026).

Загального порталу в берлінських кооперативів немає, і кожен публікує
пропозиції по-своєму, тож окремий розбір для кожного не окупився б. Натомість
бот раз на 30 хвилин відкриває сторінку пропозицій кожного кооперативу й
стежить за посиланнями, схожими на квартиру («wohnung», «angebot», «objekt»,
«expose», «detail», `?id=`…). Нове посилання — сигнал: кооператив виставив
щось нове. Людям, у чиє місце пошуку входить Берлін і хто не вимкнув
«🏘 Кооперативи Берліна» (`criteria["berlin_coops"]`), приходить коротке
повідомлення з назвою кооперативу й посиланням.

Щоб плаваючі параметри в посиланнях (сесії, кеш) не будили людей щопів
години, нове посилання вважаємо справжнім, лише коли його видно два обходи
поспіль; параметри, крім схожих на номер об'єкта, відкидаємо. Перший обхід
кожного кооперативу — база.

Список — з каталогу genossenschaftsradar.de (02.11.2026): 19 кооперативів, у
яких є окрема сторінка пропозицій (Vaterland відповідає 404 — без нього).
"""

import html
import logging
import os
import re
import time
from datetime import datetime
from typing import Dict, Iterable, List, Set, Tuple
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import requests

import i18n
from database import CoopOfferLink, DBSession
from user_jobs import housing_area, housing_search_store, housing_tier, immoscout_store

logger = logging.getLogger(__name__)

CHECK_ENABLED = os.getenv("COOP_OFFERS_CHECK_ENABLED", "1") == "1"
CHECK_INTERVAL_SECONDS = 30 * 60
TIMEOUT = 30
PAUSE_SECONDS = 1.0
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0 Safari/537.36",
    "Accept-Language": "de-DE,de;q=0.9",
}

BERLIN_COOPS: List[Tuple[str, str, str]] = [
    ("1892", "Berliner Bau- und Wohnungsgenossenschaft von 1892 eG",
     "https://hpm2.immosolve.eu/immosolve_presentation/pub/modern/2145111/HP/immo.jsp"),
    ("altglienicke", "Wohnungsgenossenschaft Altglienicke eG", "http://wg-altglienicke.de/angebote/wohnungen"),
    ("amtsfeld", "Wohnungsbaugenossenschaft „Amtsfeld“ eG", "https://www.wbg-amtsfeld.de/wohnungsangebote/"),
    ("berolina", "Wohnungsbaugenossenschaft Berolina eG", "https://berolina.info/allgemein/wohnungssuche/"),
    ("bwv_koepenick", "Beamten-Wohnungs-Verein zu Köpenick eG",
     "https://www.bwv-zk.de/wohnen-bauen/vermietungsangebote.html"),
    ("charlotte", "Charlottenburger Baugenossenschaft eG",
     "https://charlotte1907.de/wohnungsangebote/woechentliche-angebote"),
    ("dpf", "Wohnungsbaugenossenschaft DPF eG", "https://www.dpfonline.de/interessenten/angebote/"),
    ("ewg_pankow", "Erste Wohnungsgenossenschaft Berlin-Pankow eG", "https://www.ewg-pankow.de/wohnen/"),
    ("freie_scholle", "Baugenossenschaft „Freie Scholle“ zu Berlin eG", "https://www.freiescholle.de/service/wohnungsangebote/"),
    ("friedrichsheim", "Bewohnergenossenschaft FriedrichsHeim eG",
     "https://www.friedrichsheim-eg.de/category/wohnungsangebote-fuer-alle/"),
    ("hub", "Wohnungsbaugenossenschaft „Humboldt-Universität“ eG", "https://www.wbg-hub.de/wohnen/wohnungssuche/"),
    ("ideal", "Baugenossenschaft IDEAL eG", "https://bg-ideal.de/bg-wohnen/mietangebote/"),
    ("koepenick_nord", "Wohnungsbaugenossenschaft „Köpenick Nord“ eG", "https://www.koepenick-nord.de/wohnungsangebote.html"),
    ("maerkische_scholle", "„Märkische Scholle“ Wohnungsunternehmen eG",
     "https://www.maerkische-scholle.de/wohnungsangebote.html"),
    ("mariendorf_lichtenrade", "Mariendorf-Lichtenrader Baugenossenschaft eG",
     "https://www.mlbaugenossen.de/angebote-mietobjekte/aktuelle-wohnangebote.html"),
    ("treptow_nord", "Wohnungsbau-Genossenschaft „Treptow Nord“ eG",
     "https://wbg-treptow-nord.de/wohnen-bei-uns/wohnungsangebote/"),
    ("treptow_sued", "Wohnungsgenossenschaft „Treptow-Süd“ eG", "https://www.wg-treptow-sued.de/wohnungen/"),
    ("vorwaerts", "Wohnungsbaugenossenschaft „VORWÄRTS“ eG",
     "https://www.wg-vorwaerts.de/leben-und-wohnen/wohnungsangebote/"),
    ("wilhelmsruh", "Wohnungsbaugenossenschaft Wilhelmsruh eG", "https://www.wbg-wilhelmsruh.de/wohnungen/freie-wohnungen/"),
]

_OFFER_RE = re.compile(r"wohnung|angebot|objekt|expose|exposé|immo|miet|detail|/\d{3,}", re.I)
_SKIP_RE = re.compile(r"\.(css|js|png|jpe?g|gif|svg|webp|ico|pdf|zip|docx?)$|mailto:|tel:|javascript:", re.I)
_KEEP_PARAMS = re.compile(r"^(id|oid|obj|objekt|objectid|object_id|nr|immo|immoid|expose|uid|wohnung)$", re.I)
_ANCHOR_RE = re.compile(r'<a\b[^>]*href="([^"#]+)"[^>]*>(.*?)</a>', re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")


def normalize(url: str) -> str:
    parts = urlparse(url)
    query = urlencode([(key, value) for key, value in parse_qsl(parts.query) if _KEEP_PARAMS.match(key)])
    return urlunparse((parts.scheme, parts.netloc.lower(), parts.path.rstrip("/") or "/", "", query, ""))


def offer_links(page_html: str, page_url: str) -> Dict[str, str]:
    """{посилання: текст} — схожі на квартиру посилання сторінки пропозицій."""
    found: Dict[str, str] = {}
    for href, inner in _ANCHOR_RE.findall(page_html or ""):
        url = urljoin(page_url, html.unescape(href.strip()))
        if not url.startswith("http") or _SKIP_RE.search(url):
            continue
        parts = urlparse(url)
        if not _OFFER_RE.search(parts.path + "?" + parts.query):
            continue
        key = normalize(url)
        if key == normalize(page_url):
            continue
        text = re.sub(r"\s+", " ", html.unescape(_TAG_RE.sub(" ", inner))).strip()
        found.setdefault(key, text[:120])
    return found


def _fetch(url: str) -> Tuple[str, str]:
    response = requests.get(url, headers=_HEADERS, timeout=TIMEOUT)
    response.raise_for_status()
    return response.text, response.url


# --- стан посилань ---------------------------------------------------------------------

def _known(session, coop_key: str) -> Dict[str, CoopOfferLink]:
    return {row.url: row for row in session.query(CoopOfferLink).filter(CoopOfferLink.coop_key == coop_key).all()}


def update_links(coop_key: str, links: Dict[str, str], now: datetime) -> List[Tuple[str, str]]:
    """Записує видимі посилання й повертає ті, що щойно стали справжніми новими
    (видно другий обхід поспіль). Перший обхід кооперативу — база."""
    session = DBSession()
    try:
        known = _known(session, coop_key)
        baseline = not known
        confirmed: List[Tuple[str, str]] = []
        for url, title in links.items():
            row = known.get(url)
            if row is None:
                session.add(CoopOfferLink(
                    coop_key=coop_key, url=url, title=title, first_seen_at=now, last_seen_at=now,
                    confirmed=baseline, notified=baseline,
                ))
                continue
            if not row.confirmed:
                row.confirmed = True
                if not row.notified:
                    confirmed.append((url, row.title or title))
                    row.notified = True
            row.last_seen_at = now
        # Те, що з'явилось і зникло за один обхід, — шум (сесії, банери).
        for url, row in known.items():
            if url not in links and not row.confirmed:
                session.delete(row)
        session.commit()
        return confirmed
    finally:
        session.close()


# --- кому ---------------------------------------------------------------------------------

def recipients() -> Set[int]:
    """Люди з активним пошуком, у місце якого входить Берлін, і не вимкненими
    кооперативами Берліна. Активність — за записом ImmoScout24: він є в кожному
    пошуку поза Потсдамом."""
    active_scout = {int(item["filter_id"]) for item in immoscout_store.list_filters(active_only=True)}
    users: Set[int] = set()
    for search in housing_search_store.list_all():
        criteria = search["criteria"]
        if criteria.get("berlin_coops") is False or not housing_area.touches_berlin(criteria.get("area")):
            continue
        if any(source == "immoscout" and int(fid) in active_scout for source, fid in search["members"]):
            users.add(int(search["user_id"]))
    return users


def _message(lang: str, coop_label: str, title: str, url: str) -> str:
    return i18n.t(
        "housing.coop_offer.new", lang,
        coop=html.escape(coop_label), title=html.escape(title or "—"), url=html.escape(url),
    )


def check_job(context) -> Dict[str, int]:
    if not CHECK_ENABLED:
        return {"ok": 1, "enabled": 0, "sent": 0}
    bot = context.bot
    now = datetime.utcnow()
    news: List[Tuple[str, str, str]] = []
    errors = 0
    for key, label, url in BERLIN_COOPS:
        try:
            page_html, final_url = _fetch(url)
        except Exception as exc:
            errors += 1
            logger.warning("Coop offers fetch failed for %s: %s", key, exc)
            continue
        for link, title in update_links(key, offer_links(page_html, final_url), now):
            news.append((label, title, link))
        time.sleep(PAUSE_SECONDS)
    sent = 0
    if news:
        for user_id in recipients():
            lang = i18n.get_lang(user_id)
            for label, title, link in news:
                try:
                    bot.send_message(chat_id=user_id, text=_message(lang, label, title, link), parse_mode="HTML",
                                     disable_web_page_preview=False)
                    sent += 1
                except Exception as exc:
                    if not housing_tier.recipient_gone(exc):
                        logger.exception("Could not send a coop offer to %s", user_id)
                    break
    logger.info("Berlin coop offers: coops=%s errors=%s new=%s sent=%s", len(BERLIN_COOPS), errors, len(news), sent)
    return {"ok": 1, "new": len(news), "sent": sent, "errors": errors}


def count() -> int:
    return len(BERLIN_COOPS)


def labels() -> Iterable[str]:
    return [label for _key, label, _url in BERLIN_COOPS]
