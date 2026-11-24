"""Одне очікування тексту від людини за раз (25.11.2026, злиття потоків A/B/C).

Бот чекає вільний текст у кількох місцях: своя сума доната (support), анкета
листа (housing_letter), питання папки (housing_mappe), дата перегляду в трекері
(housing_tracker), «написати докладніше» в опитуванні (housing_pay_survey),
своє число / місце в картці пошуку (housing_card, поле ``input``).
Порядок перевірки в anonymous_posts / housing_monitor.handle_private_text
фіксований, тож без цього модуля старе, забуте очікування (наприклад, дата
перегляду) з'їдало б відповідь на нове питання (наприклад, папки).

- ``claim(user_data, key)`` - викликати, коли починається нове очікування:
  усі інші очікування забуваються;
- ``clear_all(user_data)`` - кнопка нижнього меню: людина пішла з діалогу.
Картку пошуку (``hcard``) не стираємо цілком - лише її поле ``input``.
"""

from typing import MutableMapping, Optional

SUPPORT = "support_custom_at"
LETTER = "housing_letter"
MAPPE = "housing_mappe"
TRACKER = "housing_apply_date"
SURVEY = "pay_more"
CARD = "hcard"
KEYS = (SUPPORT, LETTER, MAPPE, TRACKER, SURVEY)


def claim(user_data: Optional[MutableMapping], key: str) -> None:
    """Нове очікування ``key``: інші забуваємо."""
    if user_data is None:
        return
    for other in KEYS:
        if other != key:
            user_data.pop(other, None)
    if key != CARD:
        card = user_data.get(CARD)
        if isinstance(card, dict):
            card.pop("input", None)


def clear_all(user_data: Optional[MutableMapping]) -> None:
    """Людина натиснула кнопку нижнього меню - жодного очікування."""
    claim(user_data, "")
