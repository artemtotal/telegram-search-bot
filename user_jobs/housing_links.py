"""Links to flats through our own redirect, and the honest «who looked» numbers
(24.11.2026).

Every flat link in a notification can go through
<HOUSING_WEB_URL>/o/<token>; the web handler answers 302 to the portal
address saved here. That gives two real numbers per flat: how many people
opened it (this module) and how many said they applied (housing_tracker).

Safety rules, in order of importance:
 * every public function swallows its own errors and falls back to the
   direct portal link - a broken redirect must never lose a flat;
 * HOUSING_LINK_REDIRECT=0 (or no HOUSING_WEB_URL, or the web server of this
   process not started) = direct links, exactly as before;
 * the redirect target is only ever the address saved in housing_link_ref,
   never anything taken from the request (no open redirect).
"""

import html
import logging
import os
import re
import secrets
from datetime import datetime
from typing import Dict, Optional, Tuple
from urllib.parse import urlparse

import pytz

import i18n
from database import DBSession, HousingApplication, HousingLinkOpen, HousingLinkRef, HousingSendLog

logger = logging.getLogger(__name__)

BERLIN = pytz.timezone("Europe/Berlin")
_web_up = False

# Роботи й попередній перегляд посилань: не рахуються як «відкрили».
_ROBOT_RE = re.compile(
    r"bot|crawl|spider|slurp|preview|facebookexternalhit|whatsapp|viber|skype|curl|wget|python-requests|"
    r"headless|monitor|uptime|embedly|vkshare|pinterest|quora|discord|slack|bitly|go-http-client|okhttp",
    re.I,
)


def utc_now() -> datetime:
    return datetime.utcnow()


def _flag(name: str, default: str = "1") -> bool:
    return str(os.getenv(name, default) or "").strip().lower() not in ("", "0", "false", "no", "off")


def redirect_enabled() -> bool:
    """Головний вимикач переходів."""
    return _flag("HOUSING_LINK_REDIRECT") and bool(public_url()) and _web_up


def social_enabled() -> bool:
    return _flag("HOUSING_SOCIAL_LINE")


def public_url() -> str:
    return os.getenv("HOUSING_WEB_URL", "").strip().rstrip("/")


def mark_web_up(up: bool = True) -> None:
    """Called by housing_web.start(): links go through us only while the
    server that answers them is running in this very process."""
    global _web_up
    _web_up = bool(up)


def safe_target(url: str) -> bool:
    url = str(url or "")
    if not url or any(ord(ch) < 33 or ord(ch) == 127 for ch in url):
        return False
    parsed = urlparse(url)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _new_token() -> str:
    return secrets.token_urlsafe(8)


def token_for(user_id: int, source: str, listing_key: str, url: str) -> Optional[str]:
    """The token of this (person, flat); made on first use. None on any problem."""
    session = DBSession()
    try:
        query = session.query(HousingLinkRef).filter(
            HousingLinkRef.user_id == int(user_id), HousingLinkRef.source == str(source),
            HousingLinkRef.listing_key == str(listing_key),
        )
        row = query.first()
        if row is None:
            row = HousingLinkRef(
                token=_new_token(), user_id=int(user_id), source=str(source), listing_key=str(listing_key),
                url=str(url)[:2000], created_at=utc_now(),
            )
            session.add(row)
            try:
                session.commit()
            except Exception:
                session.rollback()
                row = query.first()  # гонка двох обходів: токен уже зроблено
                if row is None:
                    raise
        elif row.url != str(url)[:2000]:
            row.url = str(url)[:2000]
            session.commit()
        return str(row.token)
    except Exception:
        session.rollback()
        logger.exception("Could not make a link token for %s %s %s", user_id, source, listing_key)
        return None
    finally:
        session.close()


def link_for(user_id: int, source: str, listing_key: str, url: str) -> str:
    """Our link for the flat, or `url` itself when anything is off."""
    try:
        if not url or not listing_key or not redirect_enabled() or not safe_target(url):
            return url
        token = token_for(user_id, source, listing_key, url)
        return f"{public_url()}/o/{token}" if token else url
    except Exception:
        logger.exception("link_for failed, sending the direct link")
        return url


def rewrite_text(text: str, user_id: int, source: str, listing_key: str, url: str) -> str:
    """Replaces the portal address in a ready notification text with our link.
    Text that doesn't contain the address is returned untouched."""
    try:
        url = str(url or "").strip()
        if not url or not text or not redirect_enabled():
            return text
        escaped = html.escape(url)
        variants = [escaped] if escaped == url else [escaped, url]
        found = next((v for v in variants if v in text), None)
        if found is None:
            return text
        ours = link_for(user_id, source, listing_key, url)
        if ours == url:
            return text
        result = text.replace(found, html.escape(ours))
        return result if ours in result.replace("&amp;", "&") else text
    except Exception:
        logger.exception("rewrite_text failed, sending the direct link")
        return text


def stored_url(model, listing_key: str) -> str:
    if model is None:
        return ""
    session = DBSession()
    try:
        row = session.query(model).get(str(listing_key))
        return str(getattr(row, "detail_url", "") or "") if row is not None else ""
    except Exception:
        return ""
    finally:
        session.close()


def rewrite_for_gate(text: str, user_id: int, source: str, listing_key: str, model) -> str:
    """Used by DeliveryGate.finish: the address comes from the stored listing."""
    if not redirect_enabled():
        return text
    return rewrite_text(text, user_id, source, listing_key, stored_url(model, listing_key))


# ---------- the web side ----------

def is_robot(method: str, user_agent: str) -> bool:
    if str(method).upper() != "GET":
        return True
    agent = str(user_agent or "").strip()
    return not agent or bool(_ROBOT_RE.search(agent))


def resolve(token: str) -> Optional[Dict[str, object]]:
    token = str(token or "")
    if not (4 <= len(token) <= 40) or not re.fullmatch(r"[A-Za-z0-9_-]+", token):
        return None
    session = DBSession()
    try:
        row = session.query(HousingLinkRef).filter(HousingLinkRef.token == token).first()
        if row is None or not safe_target(row.url):
            return None
        return {"token": row.token, "user_id": int(row.user_id), "source": row.source,
                "listing_key": row.listing_key, "url": row.url}
    finally:
        session.close()


def record_open(ref: Dict[str, object], human: bool) -> None:
    """A database problem here must not block the redirect."""
    session = DBSession()
    try:
        session.add(HousingLinkOpen(
            token=str(ref["token"]), user_id=int(ref["user_id"]), source=str(ref["source"]),
            listing_key=str(ref["listing_key"]), opened_at=utc_now(), is_human=bool(human),
        ))
        session.commit()
    except Exception:
        session.rollback()
        logger.exception("Could not record a link open")
    finally:
        session.close()


def handle_open(token: str, method: str, user_agent: str) -> Tuple[int, Optional[str]]:
    """(302, address) for a known token, (404, None) otherwise."""
    ref = resolve(token)
    if ref is None:
        return 404, None
    try:
        record_open(ref, human=not is_robot(method, user_agent))
    except Exception:
        logger.exception("record_open failed")
    return 302, str(ref["url"])


def not_found_page() -> str:
    """Тримовний, без жодних даних з запиту."""
    from user_jobs import housing_letter_store

    lines = [html.escape(i18n.t("housing.link.not_found", lang)) for lang in i18n.SUPPORTED_LANGS]
    username = getattr(housing_letter_store, "_bot_username", None)
    link = f'<p><a href="https://t.me/{html.escape(str(username))}">@{html.escape(str(username))}</a></p>' if username else ""
    return (
        '<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>404</title></head><body style="font-family:sans-serif;padding:24px;max-width:480px;margin:auto">'
        + "".join(f"<p>{line}</p>" for line in lines) + link + "</body></html>"
    )


# ---------- honest numbers under a flat ----------

def _berlin_hhmm(moment: datetime) -> str:
    return pytz.utc.localize(moment).astimezone(BERLIN).strftime("%H:%M")


def social_numbers(listing_key: str, source: Optional[str] = None) -> Dict[str, object]:
    """Real numbers for one flat: when subscribers got it, how many people
    opened it, how many marked it as applied. Missing/zero = not present."""
    session = DBSession()
    try:
        key = str(listing_key)
        sent = session.query(HousingSendLog.sent_at).filter(
            HousingSendLog.listing_key == key, HousingSendLog.delayed.is_(False),
        )
        opened = session.query(HousingLinkOpen.user_id).filter(
            HousingLinkOpen.listing_key == key, HousingLinkOpen.is_human.is_(True),
        )
        applied = session.query(HousingApplication.user_id).filter(HousingApplication.listing_key == key)
        if source:
            sent = sent.filter(HousingSendLog.source == str(source))
            opened = opened.filter(HousingLinkOpen.source == str(source))
            applied = applied.filter(HousingApplication.source == str(source))
        first_sent = sent.order_by(HousingSendLog.sent_at.asc()).first()
        return {
            "instant_at": first_sent[0] if first_sent else None,
            "opened": len({int(r[0]) for r in opened.all()}),
            "applied": len({int(r[0]) for r in applied.all()}),
        }
    finally:
        session.close()


def listing_social_line(listing_key: str, lang: str, source: Optional[str] = None) -> str:
    """«⚡ Підписники отримали о 14:05 · відкрили: 3 · подались: 1» or "".
    Each part only when it is real (>= 1). Never raises."""
    try:
        if not social_enabled() or not listing_key:
            return ""
        numbers = social_numbers(listing_key, source)
        parts = []
        if numbers["instant_at"] is not None:
            parts.append(i18n.t("housing.social.instant", lang, time=_berlin_hhmm(numbers["instant_at"])))
        if int(numbers["opened"]) >= 1:
            parts.append(i18n.t("housing.social.opened", lang, n=int(numbers["opened"])))
        if int(numbers["applied"]) >= 1:
            parts.append(i18n.t("housing.social.applied", lang, n=int(numbers["applied"])))
        return " · ".join(parts)
    except Exception:
        logger.exception("listing_social_line failed")
        return ""


def free_social_footer(user_id: int, source: str, listing_key: str) -> str:
    """The extra footer line for a free-tier person ("\\n" + line or "")."""
    try:
        line = listing_social_line(str(listing_key), i18n.get_lang(int(user_id)), source)
        return "\n" + line if line else ""
    except Exception:
        return ""
