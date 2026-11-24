"""Пакети підписки на миттєві сповіщення (25.11.2026).

Замість вибору 1-6 місяців - три пакети:
- m1          - 1 місяць, 5 €;
- m3          - 3 місяці, 12 €;
- until_found - «Поки не знайду житло», 15 € разово: миттєві сповіщення, поки
                людина шукає, технічно - 12 місяців.

Ціни - у змінних середовища HOUSING_PRICE_M1_EUR / _M3_EUR / _UNTIL_FOUND_EUR.
Гривні рахуються так само, як раніше: курс HOUSING_EUR_UAH_RATE, округлення
вгору до 10 грн. Увімкнено за замовчуванням; HOUSING_PACKAGES_ENABLED=0
повертає старий вибір 1-6 місяців (за 5 € на місяць).

Модуль без залежностей від бази, щоб його могли імпортувати всі.
"""

import math
import os
from typing import Dict, Optional

M1 = "m1"
M3 = "m3"
UNTIL_FOUND = "until_found"
CODES = (M1, M3, UNTIL_FOUND)

# Технічний термін пакета «Поки не знайду»: людина платить один раз, тримати
# підписку відкритою довше року ми не обіцяємо.
UNTIL_FOUND_MONTHS = 12

UAH_ROUND_TO = 10


def _price(name: str, default: int) -> int:
    raw = (os.getenv(name, "") or "").strip()
    try:
        value = int(float(raw.replace(",", "."))) if raw else default
    except ValueError:
        value = default
    return value if value > 0 else default


PRICES_EUR: Dict[str, int] = {
    M1: _price("HOUSING_PRICE_M1_EUR", 5),
    M3: _price("HOUSING_PRICE_M3_EUR", 12),
    UNTIL_FOUND: _price("HOUSING_PRICE_UNTIL_FOUND_EUR", 15),
}
MONTHS: Dict[str, int] = {M1: 1, M3: 3, UNTIL_FOUND: UNTIL_FOUND_MONTHS}


def enabled() -> bool:
    return (os.getenv("HOUSING_PACKAGES_ENABLED", "1") or "1").strip() != "0"


def valid(code: Optional[str]) -> bool:
    return code in CODES


def price_eur(code: str) -> int:
    return PRICES_EUR[code]


def months(code: str) -> int:
    return MONTHS[code]


def amount_uah(code: str, rate: float) -> int:
    raw = price_eur(code) * float(rate)
    return int(math.ceil(raw / UAH_ROUND_TO) * UAH_ROUND_TO)


def for_months(count: int) -> Optional[str]:
    """Пакет, що відповідає старому вибору «N місяців» (кнопки в уже
    надісланих повідомленнях): 1 і 3 - так, решта - ні."""
    return {1: M1, 3: M3}.get(int(count))


def effective_months(code: Optional[str], stored_months: int) -> int:
    """Скільки місяців давати за оплату: за пакетом, а для старих записів
    без пакета - як було збережено."""
    if valid(code):
        return months(code)
    return int(stored_months)


# Підписи для адміна (картка чека, кнопка «Дозволити»).
ADMIN_LABEL: Dict[str, str] = {M1: "1 міс.", M3: "3 міс.", UNTIL_FOUND: "«Поки не знайду»"}


def admin_label(code: Optional[str], stored_months: int) -> str:
    if valid(code):
        return ADMIN_LABEL[code]
    return f"{int(stored_months)} міс."
