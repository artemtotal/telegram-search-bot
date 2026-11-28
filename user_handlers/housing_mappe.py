"""«📁 Папка заявника» (Bewerbungsmappe, 25.11.2026): one German PDF a landlord can
read - title page, cover letter, Mieterselbstauskunft, list of documents to
attach. A subscription feature, like «✍️ Листи».

The questionnaire reuses what «✍️ Листи» already knows (name, move-in date,
contact) and asks the rest once, in short questions; every question can be
skipped - the PDF then has an empty line to fill in by hand. Facts are printed
exactly as the person gave them, free text is translated to German, nothing is
invented. No scans of documents are taken or kept.

Off switch: HOUSING_MAPPE=0 (the menu button disappears, the callbacks refuse).
"""

import html
import io
import json
import logging
import os
import re
import threading
import time
from datetime import date
from typing import Dict, List, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackContext

import i18n
from user_handlers import housing_letter
from user_jobs import housing_letter_store, housing_mappe_pdf, housing_mappe_store as store, housing_tier

logger = logging.getLogger(__name__)

ENABLED = os.getenv("HOUSING_MAPPE", "1") != "0"
DAILY_LIMIT = int(os.getenv("HOUSING_MAPPE_DAILY_LIMIT", "10"))
STATE_KEY = "housing_mappe"
PREFIX = "housing:mappe:"

FIELDS = (
    "full_name", "birth_date", "address", "move_in", "contact",
    "occupation", "employer", "net_income",
    "adults", "children", "pets", "pet_kind", "smoking", "debts", "insolvency",
)
YES_NO = ("yes", "no")
CHOICES = {
    "occupation": ("works", "jobcenter", "other"),
    "adults": ("1", "2", "3", "4"),
    "children": ("0", "1", "2", "3", "4"),
    "pets": YES_NO, "smoking": YES_NO, "debts": YES_NO, "insolvency": YES_NO,
}
NUMBER_FIELDS = ("adults", "children")
# These three are already in the «✍️ Листи» questionnaire.
LETTER_FIELDS = ("full_name", "move_in", "contact")
# Free text that has to be German in the PDF.
TRANSLATE_FIELDS = ("employer", "pet_kind", "move_in")

_busy: set = set()
# Result of the last build per person, kept in memory for the site (bridge ops mappe_status / mappe_pdf): the site shows the document
# list and hands over the PDF itself, so nobody has to go back to the chat. {user_id: {"ok", "pdf", "occupation", "error", "ts"}}
RESULT_TTL = 20 * 60
_results: Dict[int, dict] = {}


def _forget_old_results() -> None:
    limit = time.time() - RESULT_TTL
    for key in [k for k, v in list(_results.items()) if v.get("ts", 0) < limit]:
        _results.pop(key, None)


def last_result(user_id: int) -> Optional[dict]:
    _forget_old_results()
    return _results.get(int(user_id))


def _lang(user_id: int) -> str:
    return i18n.get_lang(int(user_id))


def _t(key: str, lang: str, **kwargs) -> str:
    return i18n.t(f"housing.mappe.{key}", lang, **kwargs)


# ---------- which questions are still open ----------

def _applicable(field: str, answers: Dict[str, str]) -> bool:
    if field == "employer":
        return answers.get("occupation") in ("works", "other")
    if field == "net_income":
        return answers.get("occupation") == "works"
    if field == "pet_kind":
        return answers.get("pets") == "yes"
    return True


def _known(field: str, answers: Dict[str, str], letter: Dict[str, str], force: bool) -> bool:
    if field in answers:
        return True
    return (not force) and field in LETTER_FIELDS and bool(letter.get(field))


def next_field(answers: Dict[str, str], letter: Dict[str, str], force: bool = False) -> Optional[str]:
    for field in FIELDS:
        if _applicable(field, answers) and not _known(field, answers, letter, force):
            return field
    return None


def merged(answers: Dict[str, str], letter: Dict[str, str]) -> Dict[str, str]:
    """What is known about the person: an answer here wins, then «✍️ Листи»."""
    result = {field: (answers.get(field) or "").strip() for field in FIELDS}
    for field in LETTER_FIELDS:
        if field not in answers:
            result[field] = (letter.get(field) or "").strip()
    return result


# ---------- text helpers ----------

_NUMBER_RE = re.compile(r"^\s*(\d{1,3}(?:[ .]?\d{3})*|\d+)(?:[.,](\d{1,2}))?\s*(?:€|eur|euro|євро|евро)?\s*$", re.IGNORECASE)


def format_income(raw: str) -> str:
    """«1800» -> «1.800 € netto»; anything that is not a plain amount stays as written."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    match = _NUMBER_RE.match(raw)
    if not match:
        return raw
    whole = re.sub(r"[ .]", "", match.group(1))
    grouped = f"{int(whole):,}".replace(",", ".")
    cents = match.group(2)
    return f"{grouped}{',' + cents if cents else ''} € netto"


def _json_object(text: str) -> Optional[dict]:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def translate(texts: Dict[str, str]) -> Dict[str, str]:
    """Free-text answers -> German, one AI call. On any failure the person's own
    words are printed as they are: better the original than a wrong translation."""
    texts = {key: value for key, value in texts.items() if value}
    if not texts:
        return {}
    prompt = (
        "Übersetze die folgenden Angaben eines Wohnungsbewerbers ins Deutsche. Sie können auf Ukrainisch, "
        "Russisch, Englisch oder schon auf Deutsch sein. Eigennamen, Firmennamen, Adressen und Zahlen "
        "unverändert lassen. Nichts hinzufügen, nichts weglassen, nichts erklären.\n"
        "Antworte NUR mit einem JSON-Objekt mit denselben Schlüsseln.\n\n"
        + json.dumps(texts, ensure_ascii=False)
    )
    try:
        answer = housing_letter._call_ai(prompt)
    except Exception:
        logger.exception("Mappe translation failed")
        return dict(texts)
    data = _json_object(answer or "") or {}
    result = {}
    for key, original in texts.items():
        value = data.get(key)
        if isinstance(value, str) and value.strip() and len(value) <= 3 * len(original) + 60:
            result[key] = value.strip()
        else:
            result[key] = original
    return result


def letter_profile(values: Dict[str, str], letter: Dict[str, str]) -> Dict[str, str]:
    """The profile the cover-letter prompt reads - only what the person said."""
    occupation = values.get("occupation")
    if occupation == "jobcenter":
        income = "Die Miete wird vom Jobcenter übernommen"
    elif occupation == "works":
        income = ", ".join(x for x in (values.get("employer"), format_income(values.get("net_income", ""))) if x)
    elif occupation == "other":
        income = values.get("employer", "")
    else:
        income = ""
    about = [letter.get("about", "")]
    if values.get("smoking") == "no":
        about.append("Nichtraucher")
    if values.get("pets") == "no":
        about.append("keine Haustiere")
    elif values.get("pets") == "yes" and values.get("pet_kind"):
        about.append("Haustier: " + values["pet_kind"])
    return {
        "full_name": values.get("full_name", ""),
        "household": housing_mappe_pdf.persons_line(values.get("adults", ""), values.get("children", ""))
        or letter.get("household", ""),
        "income": income or letter.get("income", ""),
        "move_in": values.get("move_in", ""),
        "contact": values.get("contact", ""),
        "about": ", ".join(x for x in about if x),
    }


def _general_prompt(profile: Dict[str, str]) -> str:
    person = [
        f"Name: {profile.get('full_name') or '-'}",
        f"Haushalt: {profile.get('household') or '-'}",
        f"Beruf/Einkommen: {profile.get('income') or '-'}",
        f"Einzug ab: {profile.get('move_in') or '-'}",
        f"Kontakt: {profile.get('contact') or '-'}",
        f"Weiteres: {profile.get('about') or '-'}",
    ]
    return (
        "Schreibe ein kurzes, höfliches Bewerbungsschreiben (Anschreiben) auf Deutsch an einen Vermieter. "
        "Es ist ein allgemeines Schreiben, das der Bewerber für verschiedene Wohnungen verwenden kann: "
        "nenne keine konkrete Wohnung, Adresse oder Stadt.\n\n"
        "BEWERBER (Angaben können auf Ukrainisch, Russisch oder Deutsch sein - sinngemäß übersetzen):\n"
        + "\n".join(person) + "\n\n"
        "REGELN:\n"
        "- 120 bis 180 Wörter, Sie-Form, Anrede «Sehr geehrte Damen und Herren,».\n"
        "- Erfinde NICHTS: keine Arbeitgeber, Beträge, Daten oder Unterlagen, die oben nicht stehen. "
        "Fehlt eine Angabe (\"-\"), lass das Thema weg.\n"
        "- Übernimmt laut Angaben das Jobcenter die Miete, erwähne sachlich, dass die Miete über das "
        "Jobcenter gezahlt wird und eine Mietübernahmebescheinigung vorgelegt werden kann.\n"
        "- Verweise darauf, dass die Unterlagen (z. B. Einkommensnachweise, SCHUFA-Auskunft, "
        "Mietschuldenfreiheitsbescheinigung) dieser Bewerbungsmappe beiliegen.\n"
        "- Bitte um einen Besichtigungstermin.\n"
        "- Schluss «Mit freundlichen Grüßen», darunter der Name und, falls angegeben, der Kontakt.\n"
        "- Gib NUR den Brieftext aus: kein Betreff, keine Erklärungen, kein Markdown.\n"
    )


def collect_data(values: Dict[str, str], letter: Dict[str, str], listing: Optional[Dict[str, object]]) -> Dict[str, object]:
    """Everything build_pdf prints: the person's facts, German text, the cover letter."""
    german = translate({field: values.get(field, "") for field in TRANSLATE_FIELDS})
    profile = letter_profile(values, letter)
    try:
        prompt = housing_letter._prompt(listing, profile, False) if listing else _general_prompt(profile)
        cover = housing_letter._call_ai(prompt)
    except Exception:
        logger.exception("Mappe cover letter failed")
        cover = ""
    if cover == "RATE_LIMIT":
        cover = ""
    occupation = values.get("occupation", "")
    data: Dict[str, object] = {
        "name": values.get("full_name", ""),
        "birth_date": values.get("birth_date", ""),
        "address": values.get("address", ""),
        "contact": values.get("contact", ""),
        "move_in": german.get("move_in", ""),
        "occupation": occupation,
        "employer": german.get("employer", ""),
        "net_income": format_income(values.get("net_income", "")),
        "adults": values.get("adults", ""),
        "children": values.get("children", ""),
        "pets": values.get("pets", ""),
        "pet_kind": german.get("pet_kind", ""),
        "smoking": values.get("smoking", ""),
        "debts": values.get("debts", ""),
        "insolvency": values.get("insolvency", ""),
        "letter": cover,
        "listing": listing,
        "docs": housing_mappe_pdf.documents_list(occupation),
        "today": date.today().strftime("%d.%m.%Y"),
    }
    return data


# ---------- keyboards and messages ----------

def _upgrade_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(i18n.t("housing.btn.request_access", lang), callback_data="housing:access_request")],
        [InlineKeyboardButton(i18n.t("housing.btn.back_to_monitor", lang), callback_data="housing:menu")],
    ])


def _send_locked(bot, user_id: int) -> None:
    lang = _lang(user_id)
    bot.send_message(
        chat_id=user_id, parse_mode="HTML", reply_markup=_upgrade_keyboard(lang),
        text=_t("locked", lang, price=housing_tier.PREMIUM_PRICE_EUR),
    )


def _question_keyboard(field: str, lang: str) -> InlineKeyboardMarkup:
    rows = []
    choices = CHOICES.get(field)
    if choices == YES_NO:
        rows.append([InlineKeyboardButton(_t("btn.yes", lang), callback_data=f"{PREFIX}ans:yes"),
                     InlineKeyboardButton(_t("btn.no", lang), callback_data=f"{PREFIX}ans:no")])
    elif field == "occupation":
        rows.append([InlineKeyboardButton(_t(f"occ.{code}", lang), callback_data=f"{PREFIX}ans:{code}") for code in choices])
    elif choices:
        rows.append([InlineKeyboardButton(code, callback_data=f"{PREFIX}ans:{code}") for code in choices])
    rows.append([InlineKeyboardButton(_t("btn.skip", lang), callback_data=f"{PREFIX}skip")])
    rows.append([InlineKeyboardButton(_t("btn.cancel", lang), callback_data=f"{PREFIX}cancel")])
    return InlineKeyboardMarkup(rows)


def _ask(bot, user_id: int, field: str, state: Dict) -> None:
    lang = _lang(user_id)
    state["field"] = field
    key = "q.employer_other" if field == "employer" and state["answers"].get("occupation") == "other" else f"q.{field}"
    bot.send_message(chat_id=user_id, text=_t(key, lang), reply_markup=_question_keyboard(field, lang))


def _menu_keyboard(lang: str, has_answers: bool) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(_t("btn.make", lang), callback_data=f"{PREFIX}go")]]
    rows.append([InlineKeyboardButton(_t("btn.edit" if has_answers else "btn.fill", lang), callback_data=f"{PREFIX}edit")])
    rows.append([InlineKeyboardButton(_t("btn.delete", lang), callback_data=f"{PREFIX}del")])
    rows.append([InlineKeyboardButton(i18n.t("housing.btn.back_to_monitor", lang), callback_data="housing:menu")])
    return InlineKeyboardMarkup(rows)


def _summary(values: Dict[str, str], lang: str) -> str:
    lines = []
    if values.get("full_name"):
        lines.append(_t("sum.name", lang, value=html.escape(values["full_name"])))
    if values.get("occupation"):
        lines.append(_t("sum.work", lang, value=_t(f"occ.{values['occupation']}", lang)))
    persons = []
    if values.get("adults"):
        persons.append(_t("sum.adults", lang, n=html.escape(values["adults"])))
    if values.get("children") not in (None, "", "0"):
        persons.append(_t("sum.children", lang, n=html.escape(values["children"])))
    if persons:
        lines.append(_t("sum.people", lang, value=", ".join(persons)))
    return "\n".join(lines)


# ---------- the questionnaire ----------

def _start_questions(bot, context: CallbackContext, user_id: int, ref_id: Optional[int], force: bool, generate: bool) -> None:
    context.user_data.pop(housing_letter.STATE_KEY, None)
    from user_jobs import housing_text_wait
    housing_text_wait.claim(context.user_data, STATE_KEY)
    saved = store.get_answers(user_id) or {}
    state = {"answers": {} if force else dict(saved), "force": force, "ref": ref_id, "generate": generate}
    context.user_data[STATE_KEY] = state
    bot.send_message(chat_id=user_id, text=_t("intro", _lang(user_id)))
    _next(bot, context, user_id)


def _next(bot, context: CallbackContext, user_id: int) -> None:
    state = context.user_data.get(STATE_KEY)
    if state is None:
        return
    letter = housing_letter_store.get_profile(user_id) or {}
    field = next_field(state["answers"], letter, bool(state.get("force")))
    if field is not None:
        _ask(bot, user_id, field, state)
        return
    context.user_data.pop(STATE_KEY, None)
    store.save_answers(user_id, state["answers"])
    bot.send_message(chat_id=user_id, text=_t("saved", _lang(user_id)))
    if state.get("generate"):
        generate(bot, user_id, state.get("ref"))
    else:
        _send_menu(bot, user_id)


def _record(bot, context: CallbackContext, user_id: int, value: str) -> None:
    state = context.user_data.get(STATE_KEY)
    if not state or not state.get("field"):
        return
    state["answers"][state["field"]] = value.strip()[:store.MAX_VALUE_LEN]
    state["field"] = None
    _next(bot, context, user_id)


def handle_text(update: Update, context: CallbackContext) -> bool:
    """An answer to a question; False if no questionnaire is open."""
    state = context.user_data.get(STATE_KEY) if context.user_data is not None else None
    if not state or not state.get("field") or not update.message or not update.message.text or not update.effective_user:
        return False
    user_id = int(update.effective_user.id)
    field = state["field"]
    text = update.message.text.strip()
    if field in CHOICES and field not in NUMBER_FIELDS:
        context.bot.send_message(chat_id=user_id, text=_t("use_buttons", _lang(user_id)))
        _ask(context.bot, user_id, field, state)
        return True
    if field in NUMBER_FIELDS:
        if not text.isdigit() or not 0 <= int(text) <= 20 or (field == "adults" and int(text) < 1):
            context.bot.send_message(chat_id=user_id, text=_t("need_number", _lang(user_id)))
            _ask(context.bot, user_id, field, state)
            return True
        text = str(int(text))
    _record(context.bot, context, user_id, text)
    return True


# ---------- generation ----------

def checklist_keys(occupation: str) -> tuple:
    keys = {
        "works": ("works",),
        "jobcenter": ("jobcenter", "jobcenter2"),
        "other": ("other",),
    }.get(occupation, ("unknown",))
    return keys + ("id", "schufa", "debts")


def _checklist_text(occupation: str, lang: str) -> str:
    parts = [_t("docs_header", lang)]
    for key in checklist_keys(occupation):
        parts.append(_t(f"doc.{key}", lang))
    parts.append(_t("docs_footer", lang))
    return "\n\n".join(parts)


def generate(bot, user_id: int, ref_id: Optional[int] = None) -> None:
    lang = _lang(user_id)
    if user_id in _busy:
        return
    if store.count_today(user_id) >= DAILY_LIMIT:
        bot.send_message(chat_id=user_id, text=_t("limit", lang, n=DAILY_LIMIT))
        return
    listing = None
    ref = None
    if ref_id:
        ref = housing_letter_store.resolve_ref(int(ref_id))
        listing = housing_letter._listing(ref["source"], ref["listing_key"]) if ref else None
        if listing is None:
            bot.send_message(chat_id=user_id, text=i18n.t("housing.letter.unknown", lang))
            ref_id = None
    answers = store.get_answers(user_id) or {}
    letter = housing_letter_store.get_profile(user_id) or {}
    values = merged(answers, letter)
    progress = bot.send_message(chat_id=user_id, text=_t("generating", lang))
    _busy.add(user_id)
    _results.pop(user_id, None)
    _forget_old_results()

    def work():
        try:
            try:
                data = collect_data(values, letter, listing)
                pdf = housing_mappe_pdf.build_pdf(data)
            except Exception:
                logger.exception("Mappe PDF failed for %s", user_id)
                _results[user_id] = {"ok": False, "error": _t("failed", lang), "ts": time.time()}
                bot.edit_message_text(chat_id=user_id, message_id=progress.message_id, text=_t("failed", lang))
                return
            if not data.get("letter"):
                _results[user_id] = {"ok": False, "error": _t("failed", lang), "ts": time.time()}
                bot.edit_message_text(chat_id=user_id, message_id=progress.message_id, text=_t("failed", lang))
                return
            try:
                bot.delete_message(chat_id=user_id, message_id=progress.message_id)
            except Exception:
                pass
            try:
                store.log_generation(user_id, ref["source"] if ref and listing else None,
                                     ref["listing_key"] if ref and listing else None)
            except Exception:
                logger.exception("Could not log the mappe for %s", user_id)
            # The site takes the result from here; the chat copy below is an archive and may fail (blocked bot) without losing the PDF.
            _results[user_id] = {"ok": True, "pdf": pdf, "occupation": values.get("occupation", ""), "ts": time.time()}
            bot.send_document(
                chat_id=user_id, document=io.BytesIO(pdf), filename="Bewerbungsmappe.pdf",
                caption=_t("pdf_caption", lang),
            )
            rows = [[InlineKeyboardButton(_t("btn.again", lang), callback_data=f"{PREFIX}go" + (f":{ref_id}" if ref_id else "")),
                     InlineKeyboardButton(_t("btn.edit", lang), callback_data=f"{PREFIX}edit")],
                    [InlineKeyboardButton(i18n.t("housing.btn.back_to_monitor", lang), callback_data="housing:menu")]]
            bot.send_message(
                chat_id=user_id, text=_checklist_text(values.get("occupation", ""), lang), parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(rows),
            )
            logger.info("Mappe sent to %s", user_id)
        except Exception:
            logger.exception("Could not deliver the mappe to %s", user_id)
        finally:
            _busy.discard(user_id)

    threading.Thread(target=work, name=f"mappe-{user_id}", daemon=True).start()


# ---------- entry points ----------

def _send_menu(bot, user_id: int) -> None:
    lang = _lang(user_id)
    answers = store.get_answers(user_id)
    letter = housing_letter_store.get_profile(user_id) or {}
    summary = _summary(merged(answers or {}, letter), lang) if (answers or letter) else ""
    text = _t("menu", lang)
    if summary:
        text += "\n\n" + summary
    bot.send_message(chat_id=user_id, parse_mode="HTML", text=text, reply_markup=_menu_keyboard(lang, bool(answers)))


def show_menu(update: Update, context: CallbackContext) -> None:
    user_id = int(update.effective_user.id)
    if not housing_tier.is_premium(user_id):
        _send_locked(context.bot, user_id)
        return
    _send_menu(context.bot, user_id)


def handle_callback(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    user_id = int(update.effective_user.id)
    lang = _lang(user_id)
    parts = query.data.split(":")
    action = parts[2] if len(parts) > 2 else ""
    arg = parts[3] if len(parts) > 3 else ""
    if not ENABLED:
        query.answer(_t("off", lang), show_alert=True)
        return
    if not housing_tier.is_premium(user_id):
        if action == "menu":
            query.answer()
            _send_locked(context.bot, user_id)
        else:
            query.answer(_t("locked_short", lang), show_alert=True)
        return
    query.answer()
    bot = context.bot
    if action == "menu":
        context.user_data.pop(STATE_KEY, None)
        _send_menu(bot, user_id)
    elif action == "go":
        ref_id = int(arg) if arg.isdigit() else None
        answers = store.get_answers(user_id) or {}
        letter = housing_letter_store.get_profile(user_id) or {}
        if next_field(answers, letter) is None:
            context.user_data.pop(STATE_KEY, None)
            generate(bot, user_id, ref_id)
        else:
            _start_questions(bot, context, user_id, ref_id, force=False, generate=True)
    elif action == "edit":
        _start_questions(bot, context, user_id, None, force=True, generate=False)
    elif action == "ans":
        state = context.user_data.get(STATE_KEY)
        field = state.get("field") if state else None
        if field and arg in CHOICES.get(field, ()):
            _record(bot, context, user_id, arg)
    elif action == "skip":
        if context.user_data.get(STATE_KEY):
            _record(bot, context, user_id, "")
    elif action == "cancel":
        context.user_data.pop(STATE_KEY, None)
        bot.send_message(chat_id=user_id, text=_t("cancelled", lang))
    elif action == "del":
        bot.send_message(
            chat_id=user_id, text=_t("delete_ask", lang),
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(_t("btn.delete_yes", lang), callback_data=f"{PREFIX}delok"),
                InlineKeyboardButton(_t("btn.cancel", lang), callback_data=f"{PREFIX}menu"),
            ]]),
        )
    elif action == "delok":
        context.user_data.pop(STATE_KEY, None)
        store.delete_all(user_id)
        bot.send_message(chat_id=user_id, text=_t("deleted", lang))
