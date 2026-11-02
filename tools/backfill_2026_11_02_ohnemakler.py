"""Разово, 02.11.2026: додати ohne-makler.net у вже створені пошуки.

create_filter сам позначає вже онлайн-квартири як надіслані — розсилки не буде.
Пошук на паузі — новий запис теж на паузі. Пропускаємо пошуки, де людина
вимкнула площадку (sources_off) або де вона не шукає (place_sources).
"""
import sys

sys.path.insert(0, "/app")

from user_handlers import housing_card, housing_monitor as hm  # noqa: E402
from user_jobs import housing_search_store  # noqa: E402

dry = "--apply" not in sys.argv
added = skipped = 0
owners = sorted({int(search["user_id"]) for search in housing_search_store.list_all()})
for owner in owners:
    for search in hm.user_searches(owner):
        sources = {source for source, _ in search["members"]}
        if "ohnemakler" in sources or "ohnemakler" not in housing_card.place_sources(search["criteria"]):
            skipped += 1
            continue
        if sources and sources <= {"karlmarx"}:
            skipped += 1  # старі фільтри «лише Karl Marx» без меж (див. immoscout backfill 12.10)
            continue
        added += 1
        if dry:
            continue
        filter_id = housing_card._create_member("ohnemakler", owner, search["criteria"], list(sources))
        if filter_id is None:
            print("failed", owner, search["search_id"])
            continue
        if hm._search_paused(search):
            hm._set_one_filter_active("ohnemakler", filter_id, False, owner)
        housing_search_store.add_member(search["search_id"], "ohnemakler", filter_id)
print({"dry": dry, "added": added, "skipped": skipped})
