"""«✍️ Лист орендодавцю» - a German cover letter (Anschreiben) for one flat,
a subscription feature (25.10.2026).

Every flat the bot sends carries a deep link (housing_letter_store.letter_link);
tapping it opens /start lt<ref>. A subscriber without a profile answers six
short questions once (any language), then gets a ready letter to copy; a
free-tier person gets what the feature is and the subscription button.
"""

import html
import logging
import threading
from datetime import date
from typing import Dict, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackContext

import i18n
from database import (
    DBSession,
    ImmoscoutListing,
    ImmoweltListing,
    KarlmarxListing,
    KleinanzeigenListing,
    LocalsListing,
    ProPotsdamListing,
    RegiomaklerListing,
    SchobaListing,
    SemmelhaackListing,
    VonoviaListing,
)
from user_jobs import housing_letter_store as store
from user_jobs import housing_tier

logger = logging.getLogger(__name__)

LISTING_MODELS = {
    "immowelt": ImmoweltListing,
    "propotsdam": ProPotsdamListing,
    "semmelhaack": SemmelhaackListing,
    "schoba": SchobaListing,
    "regiomakler": RegiomaklerListing,
    "kleinanzeigen": KleinanzeigenListing,
    "locals": LocalsListing,
    "karlmarx": KarlmarxListing,
    "vonovia": VonoviaListing,
    "immoscout": ImmoscoutListing,
}
SOURCE_LABEL = {
    "immowelt": "Immowelt", "propotsdam": "ProPotsdam", "semmelhaack": "SEMMELHAACK",
    "schoba": "SCHOBA", "regiomakler": "ImmoTeam/alpha", "kleinanzeigen": "Kleinanzeigen",
    "locals": "locals®", "karlmarx": "Karl Marx", "vonovia": "Vonovia", "immoscout": "ImmoScout24",
}
# Захист від зациклення кнопки «Інший варіант» - листів на людину за день.
DAILY_LIMIT = 20
STATE_KEY = "housing_letter"
_daily: Dict[tuple, int] = {}


def _lang(user_id: int) -> str:
    return i18n.get_lang(int(user_id))


def _upgrade_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(i18n.t("housing.btn.request_access", lang), callback_data="housing:access_request")],
        [InlineKeyboardButton(i18n.t("housing.btn.back_to_monitor", lang), callback_data="housing:menu")],
    ])


def _listing(source: str, listing_key: str) -> Optional[Dict[str, object]]:
    model = LISTING_MODELS.get(source)
    if model is None:
        return None
    session = DBSession()
    try:
        row = session.query(model).filter(model.listing_key == listing_key).first()
        if row is None:
            return None
        data = {
            name: getattr(row, name, None)
            for name in ("title", "address", "district", "rooms", "area_m2", "price_eur", "price_warm_eur", "detail_url")
        }
    finally:
        session.close()
    if not data.get("detail_url") and source == "immowelt":
        data["detail_url"] = f"https://www.immowelt.de/expose/{listing_key}"
    data["source"] = source
    return data


def _num(value) -> str:
    try:
        return f"{float(value):g}"
    except (TypeError, ValueError):
        return ""


def _listing_label(listing: Dict[str, object]) -> str:
    parts = [str(listing.get("title") or "Wohnung"), str(listing.get("address") or "")]
    if _num(listing.get("rooms")):
        parts.append(f"🚪 {_num(listing.get('rooms'))}")
    if _num(listing.get("area_m2")):
        parts.append(f"{_num(listing.get('area_m2'))} m²")
    return " · ".join(part for part in parts if part)


def _prompt(listing: Dict[str, object], profile: Dict[str, str], variant: bool) -> str:
    flat = [
        f"Anzeige: {listing.get('title') or 'Mietwohnung'}",
        f"Adresse/Lage: {listing.get('address') or listing.get('district') or 'Potsdam'}",
        f"Portal: {SOURCE_LABEL.get(str(listing.get('source')), listing.get('source'))}",
    ]
    if _num(listing.get("rooms")):
        flat.append(f"Zimmer: {_num(listing.get('rooms'))}")
    if _num(listing.get("area_m2")):
        flat.append(f"Wohnfläche: {_num(listing.get('area_m2'))} m²")
    if _num(listing.get("price_eur")):
        flat.append(f"Miete: {_num(listing.get('price_eur'))} €")
    person = [
        f"Name: {profile.get('full_name') or '-'}",
        f"Haushalt: {profile.get('household') or '-'}",
        f"Beruf/Einkommen: {profile.get('income') or '-'}",
        f"Einzug ab: {profile.get('move_in') or '-'}",
        f"Kontakt: {profile.get('contact') or '-'}",
        f"Weiteres: {profile.get('about') or '-'}",
    ]
    return (
        "Schreibe ein kurzes, höfliches Bewerbungsschreiben (Anschreiben) auf Deutsch an einen Vermieter "
        "in Potsdam für die folgende Mietwohnung.\n\n"
        "WOHNUNG:\n" + "\n".join(flat) + "\n\n"
        "BEWERBER (Angaben können auf Ukrainisch, Russisch oder Deutsch sein - sinngemäß übersetzen):\n"
        + "\n".join(person) + "\n\n"
        "REGELN:\n"
        "- 120 bis 180 Wörter, Sie-Form, Anrede «Sehr geehrte Damen und Herren,».\n"
        "- Beziehe dich konkret auf diese Wohnung (Lage, Zimmer, Größe).\n"
        "- Erfinde NICHTS: keine Arbeitgeber, Beträge, Daten oder Unterlagen, die oben nicht stehen. "
        "Fehlt eine Angabe (\"-\"), lass das Thema weg.\n"
        "- Übernimmt laut Angaben das Jobcenter die Miete, erwähne sachlich, dass die Miete über das "
        "Jobcenter gezahlt wird und eine Mietübernahmebescheinigung vorgelegt werden kann.\n"
        "- Biete allgemein an, übliche Unterlagen (z. B. Einkommensnachweise, SCHUFA-Auskunft, "
        "Mietschuldenfreiheitsbescheinigung) gern nachzureichen.\n"
        "- Bitte um einen Besichtigungstermin.\n"
        "- Schluss «Mit freundlichen Grüßen», darunter der Name und, falls angegeben, der Kontakt.\n"
        "- Gib NUR den Brieftext aus: kein Betreff, keine Erklärungen, kein Markdown.\n"
        + ("- Formuliere diesmal deutlich anders als ein Standardschreiben (anderer Einstieg und Aufbau).\n"
           if variant else "")
    )


def _call_ai(prompt: str) -> str:
    from user_handlers import msg_ai

    return msg_ai._call_ai(prompt, max_tokens=1200, timeout=90)


def _over_limit(user_id: int) -> bool:
    key = (int(user_id), date.today().isoformat())
    _daily[key] = _daily.get(key, 0) + 1
    return _daily[key] > DAILY_LIMIT


def _generate_and_send(bot, user_id: int, ref_id: int, variant: bool) -> None:
    lang = _lang(user_id)
    ref = store.resolve_ref(ref_id)
    listing = _listing(ref["source"], ref["listing_key"]) if ref else None
    if listing is None:
        bot.send_message(chat_id=user_id, text=i18n.t("housing.letter.unknown", lang))
        return
    profile = store.get_profile(user_id) or {}
    if _over_limit(user_id):
        bot.send_message(chat_id=user_id, text=i18n.t("housing.letter.limit", lang, n=DAILY_LIMIT))
        return
    progress = bot.send_message(chat_id=user_id, text=i18n.t("housing.letter.generating", lang))

    def work():
        try:
            letter = _call_ai(_prompt(listing, profile, variant))
        except Exception:
            logger.exception("Letter generation failed for %s", user_id)
            letter = ""
        try:
            if not letter or letter == "RATE_LIMIT":
                bot.edit_message_text(
                    chat_id=user_id, message_id=progress.message_id, text=i18n.t("housing.letter.failed", lang),
                )
                return
            bot.edit_message_text(
                chat_id=user_id, message_id=progress.message_id, parse_mode="HTML",
                text=i18n.t("housing.letter.result_header", lang, listing=html.escape(_listing_label(listing))),
            )
            rows = [[
                InlineKeyboardButton(i18n.t("housing.letter.btn.again", lang), callback_data=f"housing:letter:again:{ref_id}"),
                InlineKeyboardButton(i18n.t("housing.letter.btn.edit_profile", lang), callback_data="housing:letter:profile"),
            ]]
            url = str(listing.get("detail_url") or "")
            if url.startswith("https://"):
                rows.append([InlineKeyboardButton(i18n.t("housing.letter.btn.open", lang), url=url)])
            bot.send_message(
                chat_id=user_id, text=f"<pre>{html.escape(letter[:3800])}</pre>", parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(rows),
            )
            logger.info("Letter for %s %s sent to %s", ref["source"], ref["listing_key"], user_id)
        except Exception:
            logger.exception("Could not deliver a letter to %s", user_id)

    threading.Thread(target=work, name=f"letter-{user_id}", daemon=True).start()


# ---------- profile questionnaire ----------

def _ask(bot, user_id: int, step: int) -> None:
    lang = _lang(user_id)
    field = store.PROFILE_FIELDS[step]
    rows = []
    if field in store.OPTIONAL_FIELDS:
        rows.append([InlineKeyboardButton(i18n.t("housing.letter.btn.skip", lang), callback_data="housing:letter:skip")])
    rows.append([InlineKeyboardButton(i18n.t("housing.letter.btn.cancel", lang), callback_data="housing:letter:cancel")])
    bot.send_message(
        chat_id=user_id, text=i18n.t(f"housing.letter.q.{field}", lang), reply_markup=InlineKeyboardMarkup(rows),
    )


def _start_profile(bot, context: CallbackContext, user_id: int, ref_id: Optional[int]) -> None:
    old = store.get_profile(user_id) or {}
    context.user_data[STATE_KEY] = {"step": 0, "answers": dict(old), "ref": ref_id}
    bot.send_message(chat_id=user_id, text=i18n.t("housing.letter.intro_profile", _lang(user_id)))
    _ask(bot, user_id, 0)


def _advance(bot, context: CallbackContext, user_id: int, value: str) -> None:
    state = context.user_data.get(STATE_KEY) or {}
    step = int(state.get("step", 0))
    state.setdefault("answers", {})[store.PROFILE_FIELDS[step]] = value
    step += 1
    if step < len(store.PROFILE_FIELDS):
        state["step"] = step
        context.user_data[STATE_KEY] = state
        _ask(bot, user_id, step)
        return
    context.user_data.pop(STATE_KEY, None)
    store.save_profile(user_id, state["answers"])
    bot.send_message(chat_id=user_id, text=i18n.t("housing.letter.profile_saved", _lang(user_id)))
    if state.get("ref"):
        _generate_and_send(bot, user_id, int(state["ref"]), variant=False)


def handle_text(update: Update, context: CallbackContext) -> bool:
    """An answer to the questionnaire; False if no questionnaire is open."""
    state = context.user_data.get(STATE_KEY) if context.user_data is not None else None
    if not state or not update.message or not update.message.text or not update.effective_user:
        return False
    _advance(context.bot, context, int(update.effective_user.id), update.message.text.strip())
    return True


# ---------- entry points ----------

def handle_start(update: Update, context: CallbackContext, payload: str) -> None:
    user_id = int(update.effective_user.id)
    lang = _lang(user_id)
    try:
        ref_id = int(payload[len(store.START_PREFIX):])
    except ValueError:
        ref_id = 0
    if not housing_tier.is_premium(user_id):
        context.bot.send_message(
            chat_id=user_id, parse_mode="HTML", reply_markup=_upgrade_keyboard(lang),
            text=i18n.t("housing.letter.locked", lang, price=housing_tier.PREMIUM_PRICE_EUR),
        )
        return
    if not store.resolve_ref(ref_id):
        context.bot.send_message(chat_id=user_id, text=i18n.t("housing.letter.unknown", lang))
        return
    if not store.get_profile(user_id):
        _start_profile(context.bot, context, user_id, ref_id)
        return
    _generate_and_send(context.bot, user_id, ref_id, variant=False)


def show_menu(update: Update, context: CallbackContext) -> None:
    """«✍️ Листи орендодавцям» in the housing menu: what it is, the profile."""
    user_id = int(update.effective_user.id)
    lang = _lang(user_id)
    if not housing_tier.is_premium(user_id):
        context.bot.send_message(
            chat_id=user_id, parse_mode="HTML", reply_markup=_upgrade_keyboard(lang),
            text=i18n.t("housing.letter.locked", lang, price=housing_tier.PREMIUM_PRICE_EUR),
        )
        return
    profile = store.get_profile(user_id)
    if profile:
        summary = i18n.t(
            "housing.letter.profile_summary", lang,
            **{field: html.escape(profile.get(field) or "—") for field in store.PROFILE_FIELDS},
        )
    else:
        summary = i18n.t("housing.letter.profile_empty", lang)
    context.bot.send_message(
        chat_id=user_id, parse_mode="HTML",
        text=i18n.t("housing.letter.menu", lang) + "\n\n" + summary,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(
                i18n.t("housing.letter.btn.edit_profile" if profile else "housing.letter.btn.fill_profile", lang),
                callback_data="housing:letter:profile",
            )],
            [InlineKeyboardButton(i18n.t("housing.btn.back_to_monitor", lang), callback_data="housing:menu")],
        ]),
    )


def handle_callback(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    user_id = int(update.effective_user.id)
    lang = _lang(user_id)
    action = query.data.split(":")[2] if query.data.count(":") >= 2 else ""
    if action == "menu":
        query.answer()
        show_menu(update, context)
        return
    if not housing_tier.is_premium(user_id):
        query.answer(i18n.t("housing.letter.locked_short", lang), show_alert=True)
        return
    if action == "profile":
        query.answer()
        _start_profile(context.bot, context, user_id, None)
    elif action == "skip":
        query.answer()
        if context.user_data.get(STATE_KEY):
            _advance(context.bot, context, user_id, "")
    elif action == "cancel":
        query.answer()
        context.user_data.pop(STATE_KEY, None)
        context.bot.send_message(chat_id=user_id, text=i18n.t("housing.letter.cancelled", lang))
    elif action == "again":
        query.answer()
        try:
            ref_id = int(query.data.split(":")[3])
        except (IndexError, ValueError):
            return
        _generate_and_send(context.bot, user_id, ref_id, variant=True)
    else:
        query.answer()
