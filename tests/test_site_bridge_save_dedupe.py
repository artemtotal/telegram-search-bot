"""28.11.2026: housing.search_save не плодит дубль нового поиска (тот же пользователь, те же параметры, 60 с);
правка существующего поиска не затронута."""

import unittest
from types import SimpleNamespace
from unittest import mock

from site_bridge import housing
from tests import test_site_bridge_housing as t
from user_handlers import housing_monitor as hm


class SaveDedupeTests(t.Base):
    def setUp(self):
        super().setUp()
        housing._recent_created.clear()
        self.addCleanup(housing._recent_created.clear)
        self.clock = [1000.0]
        patch = mock.patch.object(housing, "time", SimpleNamespace(monotonic=lambda: self.clock[0]))
        patch.start()
        self.addCleanup(patch.stop)

    def count(self, user=t.USER):
        return len(hm.user_searches(user))

    def test_same_params_within_a_minute_return_the_same_search(self):
        t.PREMIUM.add(t.USER)
        first = self.new_search()
        self.clock[0] += 30
        second = self.new_search()
        self.assertEqual(second["id"], first["id"])
        self.assertTrue(second["search"]["id"] == first["search"]["id"])
        self.assertFalse(second["created"])
        self.assertEqual(self.count(), 1)

    def test_after_a_minute_a_new_search_is_created(self):
        first = self.new_search()
        self.clock[0] += 61
        second = self.new_search()
        self.assertNotEqual(second["id"], first["id"])
        self.assertTrue(second["created"])
        self.assertEqual(self.count(), 2)

    def test_other_params_or_other_user_are_not_twins(self):
        first = self.new_search()
        other_params = self.new_search(c={"max_price_eur": 901, "min_rooms": 2})
        self.assertNotEqual(other_params["id"], first["id"])
        t.PREMIUM.add(t.OTHER)
        hm.ALLOWED_USER_IDS.add(t.OTHER)
        self.addCleanup(hm.ALLOWED_USER_IDS.discard, t.OTHER)
        other_user = self.new_search(user=t.OTHER)
        self.assertNotEqual(other_user["id"], first["id"])

    def test_twin_deleted_meanwhile_is_created_again(self):
        first = self.new_search()
        t.call("search_delete", t.USER, {"id": first["id"]})
        again = self.new_search()
        self.assertTrue(again["created"])
        self.assertEqual(self.count(), 1)

    def test_editing_an_existing_search_is_not_affected(self):
        made = self.new_search()
        params = {"id": made["id"], "c": {"max_price_eur": 700}}
        one = t.call("search_save", t.USER, params)
        two = t.call("search_save", t.USER, params)
        self.assertFalse(one["created"] or two["created"])
        self.assertNotIn("duplicate", two)
        self.assertEqual(two["search"]["c"]["max_price_eur"], 700)



if __name__ == "__main__":
    unittest.main()
