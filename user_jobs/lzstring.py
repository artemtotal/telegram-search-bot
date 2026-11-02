"""LZString.decompressFromBase64 — порт алгоритму lz-string (pieroxy, MIT).

Immowelt кладе дані видачі в сторінку стиснутими саме так
(`classified-serp-init-data`, див. immowelt_parser). Пакета в образі немає,
а потрібна лише розпаковка — тож кілька десятків рядків тут.
"""

from typing import Optional

_KEY_BASE64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/="
_BASE64_INDEX = {ch: index for index, ch in enumerate(_KEY_BASE64)}


def decompress_from_base64(data: Optional[str]) -> Optional[str]:
    if data is None:
        return ""
    if data == "":
        return None
    return _decompress(len(data), 32, lambda index: _BASE64_INDEX.get(data[index], 0))


def _decompress(length: int, reset_value: int, get_next_value) -> Optional[str]:
    dictionary = {0: 0, 1: 1, 2: 2}
    enlarge_in = 4
    dict_size = 4
    num_bits = 3
    result = []
    state = {"val": get_next_value(0), "position": reset_value, "index": 1}

    def read_bits(count: int) -> int:
        bits = 0
        power = 1
        maxpower = 1 << count
        while power != maxpower:
            resb = state["val"] & state["position"]
            state["position"] >>= 1
            if state["position"] == 0:
                state["position"] = reset_value
                state["val"] = get_next_value(state["index"]) if state["index"] < length else 0
                state["index"] += 1
            bits |= (1 if resb > 0 else 0) * power
            power <<= 1
        return bits

    kind = read_bits(2)
    if kind == 0:
        c = chr(read_bits(8))
    elif kind == 1:
        c = chr(read_bits(16))
    else:
        return ""
    dictionary[3] = c
    w = c
    result.append(c)
    while True:
        if state["index"] > length:
            return ""
        c = read_bits(num_bits)
        if c == 0:
            dictionary[dict_size] = chr(read_bits(8))
            dict_size += 1
            c = dict_size - 1
            enlarge_in -= 1
        elif c == 1:
            dictionary[dict_size] = chr(read_bits(16))
            dict_size += 1
            c = dict_size - 1
            enlarge_in -= 1
        elif c == 2:
            return "".join(result)
        if enlarge_in == 0:
            enlarge_in = 1 << num_bits
            num_bits += 1
        if c in dictionary:
            entry = dictionary[c]
        elif c == dict_size:
            entry = w + w[0]
        else:
            return None
        result.append(entry)
        dictionary[dict_size] = w + entry[0]
        dict_size += 1
        enlarge_in -= 1
        w = entry
        if enlarge_in == 0:
            enlarge_in = 1 << num_bits
            num_bits += 1
