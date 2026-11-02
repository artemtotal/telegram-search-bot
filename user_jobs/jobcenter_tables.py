"""Ліміти Jobcenter поза Потсдамом (етап 5 «Берлін і Бранденбург», 02.11.2026).

Потсдамська таблиця живе в housing_monitor.JOBCENTER_LIMITS. Тут — Берлін:
AV-Wohnen з 01.01.2026, памятка Jobcenter Berlin Treptow-Köpenick
«Informationen zur Anmietung von Wohnraum in Berlin»
(berlin.de/jobcenter-treptow-koepenick/wegweiser/infoblatt_av_wohnen_ab_01_2026-kurzform.pdf).

* Холодна межа — Richtwert Bruttokaltmiete (Nettokalt + kalte Betriebskosten).
* Теплої межі в Берліні як числа немає: Grenzwert Heizkosten залежить від
  опалювальної площі будинку й палива. Беремо рядок «Erdgas, 251–500 м²» —
  середній для багатоквартирного будинку; справжня межа конкретної квартири
  може бути трохи нижчою чи вищою.

Округи Бранденбургу — у кожного своя Richtlinie KdU, часто з кількома
Vergleichsräume всередині; їх додамо окремо, поки там кнопки Jobcenter немає.
"""

from typing import Dict, Optional, Tuple

BERLIN_BRUTTOKALT = {1: 449.00, 2: 543.40, 3: 668.80, 4: 752.40, 5: 903.72}
BERLIN_BRUTTOKALT_EXTRA = 106.32
BERLIN_HEATING = {1: 123.50, 2: 160.55, 3: 197.60, 4: 222.30, 5: 251.94}
BERLIN_HEATING_EXTRA = 29.64
BERLIN_CODE = "11000"


def _by_persons(table: Dict[int, float], extra: float, persons: int) -> float:
    persons = max(1, int(persons))
    top = max(table)
    if persons <= top:
        return table[persons]
    return round(table[top] + extra * (persons - top), 2)


def berlin(persons: int) -> Tuple[float, float]:
    """(Bruttokaltmiete €, тепла межа €) для Берліна."""
    cold = _by_persons(BERLIN_BRUTTOKALT, BERLIN_BRUTTOKALT_EXTRA, persons)
    heating = _by_persons(BERLIN_HEATING, BERLIN_HEATING_EXTRA, persons)
    return cold, round(cold + heating, 2)


def for_place(place: Optional[Dict], persons: int) -> Optional[Tuple[float, float]]:
    """Ліміти для місця поза Потсдамом; None — таблиці для цього місця ще немає."""
    if place and place.get("code") == BERLIN_CODE:
        return berlin(persons)
    return None
