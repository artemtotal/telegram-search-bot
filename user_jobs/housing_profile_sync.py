"""Одна анкета, два хранилища (28.11.2026): «✍️ Про мене» (housing_letter_store, для листа орендодавцю)
і анкета «📁 Папки заявника» (housing_mappe_store). Спільні за змістом поля живуть узгоджено в обидва боки:

  ім'я, дата в'їзду, контакт  <->  full_name, move_in, contact (як є);
  склад сім'ї                 <->  household  <-> adults + children (у листі це рядок persons_line; назад розбираємо
                                   тільки рядок саме такого вигляду, вільний текст не чіпаємо);
  робота і дохід              ->   income (з occupation, employer, net_income; назад з вільного тексту не розбираємо).

Правило одне для чату, сайту й мосту (виклик із самих сховищ при збереженні): значення, яке людина щойно змінила,
пишемо в друге сховище тільки якщо там порожньо або там було те саме старе значення; інше заповнене не затираємо;
порожнім нічого не затираємо. Запис у друге сховище йде без зворотного виклику (sync=False), циклу немає.
"""

import logging
from typing import Dict, Optional

logger = logging.getLogger(__name__)

DIRECT = ("full_name", "move_in", "contact")


def _s(value) -> str:
    return str(value or "").strip()


def propagate(old, new, other) -> Optional[str]:
    """Яке значення записати в друге сховище, або None, якщо його не чіпати."""
    old, new, other = _s(old), _s(new), _s(other)
    if not new or new == other:
        return None
    if not other:
        return new
    if new != old and other == old:
        return new
    return None


def _pdf():
    from user_jobs import housing_mappe_pdf

    return housing_mappe_pdf


def _mappe():
    from user_handlers import housing_mappe

    return housing_mappe


def household_text(adults, children) -> str:
    return _pdf().persons_line(_s(adults), _s(children))


def parse_household(text) -> Optional[Dict[str, str]]:
    """«2 Erwachsene, 1 Kind» -> {adults, children}; тільки рядки, що їх робить persons_line."""
    text = _s(text)
    if not text:
        return None
    for adults in ("1", "2", "3", "4"):
        for children in ("0", "1", "2", "3", "4"):
            if household_text(adults, children) == text:
                return {"adults": adults, "children": children}
    return None


def income_text(values: Dict[str, str]) -> str:
    return _s(_mappe().letter_profile({k: _s(v) for k, v in values.items()}, {}).get("income"))


def _answers_view(answers: Dict[str, str], letter: Dict[str, str]) -> Dict[str, str]:
    """Анкета папки як її бачить людина: свої відповіді, для спільних полів (якщо ключа немає) значення з листа."""
    view = dict(answers)
    for field in DIRECT:
        if field not in answers and _s(letter.get(field)):
            view[field] = _s(letter.get(field))
    return view


# ---------------------------------------------------------------- після збереження

def after_mappe_save(user_id: int, old_answers: Optional[dict], new_answers: dict) -> None:
    """Анкету папки збережено -> заповнити/оновити «Про мене»."""
    from user_jobs import housing_letter_store as letter_store

    letter = letter_store.get_profile(user_id) or {}
    old_answers, new_answers = dict(old_answers or {}), dict(new_answers or {})
    old_v, new_v = _answers_view(old_answers, letter), _answers_view(new_answers, letter)
    update = {}
    for field in DIRECT:
        if field in new_answers:
            res = propagate(old_v.get(field), new_v.get(field), letter.get(field))
            if res:
                update[field] = res
    if "adults" in new_answers or "children" in new_answers:
        res = propagate(household_text(old_v.get("adults"), old_v.get("children")),
                        household_text(new_v.get("adults"), new_v.get("children")), letter.get("household"))
        if res:
            update["household"] = res
    if any(k in new_answers for k in ("occupation", "employer", "net_income")):
        res = propagate(income_text(old_v), income_text(new_v), letter.get("income"))
        if res:
            update["income"] = res
    if update:
        letter_store.save_profile(user_id, {**{k: _s(v) for k, v in letter.items()}, **update}, sync=False)


def after_letter_save(user_id: int, old_letter: Optional[dict], new_letter: dict) -> None:
    """«Про мене» збережено -> заповнити/оновити анкету папки (ключ у ній означає «питання вже було»)."""
    from user_jobs import housing_mappe_store as mappe_store

    old_letter, new_letter = dict(old_letter or {}), dict(new_letter or {})
    saved = mappe_store.get_answers(user_id)
    answers = dict(saved or {})
    update = {}
    for field in DIRECT:
        if field in answers:        # ключа немає: папка і так бере значення з «Про мене»
            res = propagate(old_letter.get(field), new_letter.get(field), answers.get(field))
            if res:
                update[field] = res
    new_h = parse_household(new_letter.get("household"))
    if new_h:
        old_h = parse_household(old_letter.get("household")) or {}
        for key, value in new_h.items():
            res = propagate(old_h.get(key), value, answers.get(key))
            if res:
                update[key] = res
    if update:
        mappe_store.save_answers(user_id, {**answers, **update}, sync=False)


# ---------------------------------------------------------------- вигляд для форм (без запису)

def letter_view(user_id: int) -> Optional[Dict[str, str]]:
    """«Про мене» з порожніми полями, доповненими з анкети папки (нічого не пишемо)."""
    from user_jobs import housing_letter_store as letter_store, housing_mappe_store as mappe_store

    letter = letter_store.get_profile(user_id)
    answers = mappe_store.get_answers(user_id)
    if not answers:
        return letter
    base = dict(letter or {f: "" for f in letter_store.PROFILE_FIELDS})
    fill = {f: _s(answers.get(f)) for f in DIRECT}
    fill["household"] = household_text(answers.get("adults"), answers.get("children"))
    fill["income"] = income_text(answers)
    for key, value in fill.items():
        if value and not _s(base.get(key)):
            base[key] = value
    return base


def mappe_view(user_id: int, merged_answers: Dict[str, str], letter: Dict[str, str]) -> Dict[str, str]:
    """Відповіді папки з порожніми adults/children, доповненими зі складу сім'ї в «Про мене»."""
    out = dict(merged_answers)
    parsed = parse_household((letter or {}).get("household"))
    if parsed:
        for key, value in parsed.items():
            if not _s(out.get(key)):
                out[key] = value
    return out
