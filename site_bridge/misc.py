"""Мост: прочее (проверка связи и мелочи без своей области). Файл принадлежит пакету H5 и общему каркасу; остальные пакеты сюда не пишут."""

import time

from site_bridge import op


@op("misc", "ping", need_user=False)
def ping(user_id, lang, params):
    """Проверка сквозной связи сайт -> мост -> бот. Ничего не читает и не пишет."""
    return {"pong": True, "ts": int(time.time()), "user_id": user_id, "lang": lang}
