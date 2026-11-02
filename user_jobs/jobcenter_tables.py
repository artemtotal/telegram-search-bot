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

Округи Бранденбургу — нижче (KREIS_TABLES): Havelland, Potsdam-Mittelmark,
Oberhavel за їхніми чинними таблицями. Решта — поки без кнопки Jobcenter.
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


# --- округи Бранденбургу (02.11.2026) -----------------------------------------------
#
# Лише ті, де знайшлася чинна офіційна таблиця:
# * Havelland — «Information zu den Kosten der Unterkunft und Heizung», gültig ab
#   01.01.2026 (havelland.de, Infoblatt_KdU.pdf): Ost / Mitte / West;
# * Potsdam-Mittelmark — GA 62-1, Stand 01.01.2026 (potsdam-mittelmark.de): чотири
#   Planregionen; 5+ осіб — ставка за м² × площа (100 м², +10 м² на особу);
# * Oberhavel — Handlungsrichtlinie vom 05.11.2024 (oberhavel.de, 2244_34729_1.PDF):
#   чотири ринки.
# Teltow-Fläming (на сайті лише 2018), Dahme-Spreewald (суми в окремому додатку),
# Brandenburg an der Havel, Cottbus тощо — поки без таблиці, кнопки Jobcenter там нема.
#
# Тепла межа — як у Берліні: Bruttokalt + опалення «Erdgas, будинок 250–500 м²» з
# Heizspiegel у таблиці Havelland (22,30 €/м² на рік × належна площа).
HEATING_EUR_PER_M2_YEAR = 22.30
AREA_BY_PERSONS = {1: 50, 2: 65, 3: 80, 4: 90}

KREIS_TABLES: Dict[str, Tuple[Dict[int, float], float]] = {
    "havelland_ost": ({1: 660.00, 2: 820.00, 3: 1060.00, 4: 1240.00, 5: 1460.00}, 132.00),
    "havelland_mitte": ({1: 570.00, 2: 760.00, 3: 1000.00, 4: 1170.00, 5: 1370.00}, 114.00),
    "havelland_west": ({1: 400.00, 2: 490.00, 3: 650.00, 4: 740.00, 5: 870.00}, 80.00),
    "pm_1": ({1: 619.00, 2: 837.85, 3: 1073.60, 4: 1213.20, 5: 1588.00}, 158.80),
    "pm_2": ({1: 608.00, 2: 740.35, 3: 868.80, 4: 1069.20, 5: 1201.00}, 120.10),
    "pm_3": ({1: 376.00, 2: 530.40, 3: 652.80, 4: 678.60, 5: 908.00}, 90.80),
    "pm_4": ({1: 423.50, 2: 532.35, 3: 697.60, 4: 813.60, 5: 887.00}, 88.70),
    "ohv_1": ({1: 537.00, 2: 734.00, 3: 949.00, 4: 1164.00, 5: 1361.00}, 137.00),
    "ohv_2": ({1: 471.00, 2: 654.00, 3: 836.00, 4: 1050.00, 5: 1247.00}, 125.00),
    "ohv_3": ({1: 374.00, 2: 494.00, 3: 667.00, 4: 863.00, 5: 917.00}, 92.00),
    "ohv_4": ({1: 355.00, 2: 441.00, 3: 529.00, 4: 701.00, 5: 892.00}, 90.00),
}

HAVELLAND, PM, OHV = "12063", "12069", "12065"
# (місто як у довіднику індексів, код округу) → таблиця. Частини громад (Elstal,
# Bergholz-Rehbrücke…) довідник називає окремо — вони тут поруч із громадою.
_PLACES = {
    "havelland_ost": (HAVELLAND, ["Brieselang", "Bredow", "Zeestow", "Dallgow-Döberitz", "Döberitz", "Seeburg",
                                  "Falkensee", "Schönwalde", "Pausin", "Perwenitz", "Wansdorf", "Paaren im Glien"]),
    "havelland_mitte": (HAVELLAND, ["Ketzin", "Etzin", "Falkenrehde", "Tremmen", "Nauen", "Börnicke", "Groß Behnitz",
                                    "Klein Behnitz", "Kienberg", "Markee", "Ribbeck", "Tietzow", "Berge", "Bergerdamm",
                                    "Lietzow", "Wachow", "Wustermark", "Elstal"]),
    "havelland_west": (HAVELLAND, ["Rathenow", "Premnitz", "Milow", "Bützer", "Großwudicke", "Jerchel", "Vieritz",
                                   "Zollchow", "Nitzahn", "Möthlitz", "Rhinow", "Gollenberg", "Großderschau", "Havelaue",
                                   "Kleßen-Görne", "Seeblick", "Friesack", "Mühlenberge", "Paulinenaue", "Pessin",
                                   "Retzow", "Nennhausen", "Kotzen", "Märkisch Luch", "Stechow-Ferchesar"]),
    "pm_1": (PM, ["Kleinmachnow", "Teltow", "Stahnsdorf", "Bergholz-Rehbrücke", "Saarmund", "Fahlhorst", "Nudow",
                  "Philippsthal", "Tremsdorf"]),
    "pm_2": (PM, ["Werder (Havel)", "Derwitz", "Krielow", "Töplitz", "Schwielowsee", "Beelitz", "Michendorf",
                  "Fresdorf", "Langerwisch", "Wildenbruch", "Wilhelmshorst", "Seddiner See"]),
    "pm_3": (PM, ["Beetzsee", "Beetzseeheide", "Havelsee", "Päwesin", "Roskow", "Groß Kreutz", "Bochow", "Deetz", "Götz",
                  "Jeserig", "Schmergow", "Kloster Lehnin", "Trechwitz", "Wusterwitz", "Bensdorf", "Rosenau", "Ziesar",
                  "Buckautal", "Görzke", "Gräben", "Wenzlow", "Wollin"]),
    "pm_4": (PM, ["Belzig", "Hagelberg", "Lühsdorf", "Schwanebeck", "Brück", "Borkheide", "Borkwalde", "Golzow", "Linthe",
                  "Planebruch", "Treuenbrietzen", "Niemegk", "Mühlenfließ", "Planetal", "Rabenstein/Fläming",
                  "Wiesenburg"]),
    "ohv_1": (OHV, ["Birkenwerder", "Hohen Neuendorf", "Bergfelde", "Borgsdorf", "Stolpe", "Glienicke/Nordbahn",
                    "Mühlenbeck", "Schildow", "Schönfließ", "Zühlsdorf"]),
    "ohv_2": (OHV, ["Hennigsdorf", "Oranienburg", "Lehnitz", "Germendorf", "Friedrichsthal", "Malz", "Schmachtenhagen",
                    "Wensickendorf", "Zehlendorf", "Velten", "Leegebruch"]),
    "ohv_3": (OHV, ["Kremmen", "Oberkrämer", "Liebenwalde", "Freienhagen", "Hammer", "Kreuzbruch", "Neuholland",
                    "Löwenberger Land", "Nassenheide"]),
    "ohv_4": (OHV, ["Zehdenick", "Badingen", "Mildenberg", "Klein-Mutz", "Burgwall", "Kappe", "Marienthal", "Zabelsdorf",
                    "Wesendorf", "Gransee", "Großwoltersdorf", "Schönermark", "Sonnenberg", "Stechlin",
                    "Fürstenberg/Havel", "Altthymen", "Barsdorf", "Blumenow", "Bredereiche", "Himmelpfort", "Steinförde",
                    "Zootzen"]),
}
PLACE_TABLE = {(name, code): table for table, (code, names) in _PLACES.items() for name in names}


def heating(persons: int) -> float:
    persons = max(1, int(persons))
    area = AREA_BY_PERSONS.get(persons, 90 + 10 * (persons - 4))
    return round(area * HEATING_EUR_PER_M2_YEAR / 12, 2)


def kreis(table: str, persons: int) -> Tuple[float, float]:
    """(Bruttokaltmiete €, тепла межа €) за таблицею округу."""
    by_persons, extra = KREIS_TABLES[table]
    cold = _by_persons(by_persons, extra, persons)
    return cold, round(cold + heating(persons), 2)


def for_place(place: Optional[Dict], persons: int) -> Optional[Tuple[float, float]]:
    """Ліміти для місця поза Потсдамом; None — таблиці для цього місця ще немає."""
    if place and place.get("code") == BERLIN_CODE:
        return berlin(persons)
    table = PLACE_TABLE.get((str((place or {}).get("name") or ""), str((place or {}).get("code") or "")))
    if table:
        return kreis(table, persons)
    return None
