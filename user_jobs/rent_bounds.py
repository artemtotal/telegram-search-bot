"""Цінова межа, коли площадка публікує не ту оренду, яку обмежила людина.

Фільтр має дві межі — холодну (Kaltmiete) і повну (Warmmiete), — а кожна
площадка дає свою: ProPotsdam і Karl Marx лише повну, частина оголошень
Kleinanzeigen лише холодну. Раніше невідома величина означала «умова не
застосовується», і людина з межею «до 640 € холодна» отримувала з Karl Marx
квартири за 1723 € повної (аудит 13.10.2026: 9 людей, у яких на двох
площадках ціна не обмежувалась ніяк).

Тепер відсутню величину оцінюємо з тієї, що є:
- холодна відома, повної немає: холодна ніколи не більша за повну, тож
  холодна понад межу повної — точно задорого;
- повна відома, холодної немає: дозволяємо повній перевищити холодну межу
  на приблизні комунальні. Запас щедрий навмисно — краще зайва квартира,
  ніж загублена.

Ті самі числа в приймачі Immowelt: check-Wohnung, app/wide_immowelt.py.
"""

from typing import Optional

# Nebenkosten у Потсдамі зазвичай 2,5–3,5 €/м².
WARM_ALLOWANCE_EUR_PER_M2 = 4.0
# Те саме, коли площі немає: повна не більша за холодну межу × 1,5.
WARM_ALLOWANCE_FACTOR = 1.5


def warm_ceiling(max_cold: float, area_m2: Optional[float]) -> float:
    if area_m2:
        return max_cold + WARM_ALLOWANCE_EUR_PER_M2 * area_m2
    return max_cold * WARM_ALLOWANCE_FACTOR


def missing_price_within(
    cold: Optional[float],
    warm: Optional[float],
    area_m2: Optional[float],
    max_cold: Optional[float],
    max_warm: Optional[float],
) -> bool:
    """False — оголошення точно дорожче за межу, хоч прямої величини й немає.

    Коли обидві ціни відомі (або не відома жодна), тут нічого не вирішується:
    прямі порівняння робить сам `matches_filter`.
    """
    if warm is None and cold is not None and max_warm is not None:
        return cold <= max_warm
    if cold is None and warm is not None and max_cold is not None and max_warm is None:
        return warm <= warm_ceiling(max_cold, area_m2)
    return True
