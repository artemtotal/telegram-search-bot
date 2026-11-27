"""Кнопки под уведомлениями бота, которые ведут на сайт «Потсдам» внутри Telegram (28.11.2026, пакет H5a).

Под сообщением о квартире:
  🏠 Відкрити в Потсдамботі  -> https://t.me/Potsdambot/potsdam?startapp=go_housing_listing_<ref>   (карточка этой квартиры: «Подався», письмо)
  🔗 Оголошення на сайті     -> адрес объявления на портале (тот же переход /o/<token>, что и в тексте)
ref = housing_letter_store.ref_for(source, listing_key) (его же отдаёт мост site_bridge/housing.py).

Правила безопасности (это массовые сообщения живым людям):
 * сам текст уведомления не меняется ни на знак; здесь только клавиатура;
 * любая ошибка = клавиатуры просто нет (None), сообщение уходит как раньше;
 * нет ссылки Mini App (masters.json app_link) или "site_buttons": false = клавиатуры нет;
 * у альбома (send_media_group) кнопок быть не может: после него идёт короткое сообщение с кнопками, и его сбой ничего не отменяет.
"""
import logging
from typing import Dict, Optional

logger = logging.getLogger(__name__)

MAX_URL = 2000


def kw(markup) -> Dict[str, object]:
    """Именованные аргументы для send_message/send_photo: пусто, когда клавиатуры нет (вызов остаётся прежним)."""
    return {"reply_markup": markup} if markup is not None else {}


def _button(text: str, url: str):
    from telegram import InlineKeyboardButton
    return InlineKeyboardButton(text, url=url)


def _safe_url(url) -> str:
    url = str(url or "").strip()
    if not url.lower().startswith(("https://", "http://")) or len(url) > MAX_URL or any(ord(ch) < 33 or ord(ch) == 127 for ch in url):
        return ""
    return url


def listing_keyboard(user_id: int, source: str, listing_key: str, detail_url: str = ""):
    """InlineKeyboardMarkup под квартирой или None. Не бросает исключений."""
    try:
        from user_handlers import masters_hint
        if not masters_hint.SITE_BUTTONS or not masters_hint.APP_LINK or not listing_key:
            return None
        import i18n
        from telegram import InlineKeyboardMarkup
        from user_jobs import housing_letter_store, housing_links
        ref = housing_letter_store.ref_for(source, str(listing_key))
        if ref is None:
            return None
        app = masters_hint.app_url(f"go_housing_listing_{int(ref)}")
        if not app:
            return None
        lang = i18n.get_lang(int(user_id))
        rows = [[_button(i18n.t("site.btn.app", lang), app)]]
        shown = _safe_url(housing_links.link_for(int(user_id), source, str(listing_key), _safe_url(detail_url)))
        if shown:
            rows.append([_button(i18n.t("site.btn.source", lang), shown)])
        return InlineKeyboardMarkup(rows)
    except Exception:
        logger.exception("Could not build the site buttons for %s %s", source, listing_key)
        return None


def terms_keyboard(user_id: int, section: str):
    """Кнопка «Відкрити в Потсдамботі» под уведомлением о терминах: section = dp | consul. None, если нельзя."""
    try:
        from user_handlers import masters_hint
        if not masters_hint.SITE_BUTTONS or section not in ("dp", "consul"):
            return None
        app = masters_hint.app_url(f"go_terms_{section}")
        if not app:
            return None
        import i18n
        from telegram import InlineKeyboardMarkup
        return InlineKeyboardMarkup([[_button(i18n.t("site.btn.app", i18n.get_lang(int(user_id))), app)]])
    except Exception:
        logger.exception("Could not build the site button for terms %s", section)
        return None


def after_album(bot, chat_id: int, markup) -> None:
    """У альбома кнопок не бывает: короткое сообщение с ними сразу следом (без звука). Сбой не отменяет отправку квартиры."""
    if markup is None:
        return
    try:
        import i18n
        bot.send_message(
            chat_id=chat_id, text=i18n.t("site.btn.album_note", i18n.get_lang(int(chat_id))),
            reply_markup=markup, disable_notification=True,
        )
    except Exception:
        logger.warning("Could not send the site buttons after an album to %s", chat_id, exc_info=True)
