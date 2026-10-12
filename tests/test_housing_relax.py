"""«Якщо трохи розширити фільтр — було б на N більше» (12.10.2026)."""

import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import mock

from user_handlers import housing_monitor, housing_relax

# Сім'я з п'яти з опитування: від 4 кімнат, від 70 м², до 1200 / 1500 €.
FAMILY = {
    "districts": [], "min_rooms": 4.0, "max_rooms": None, "min_area_m2": 70.0, "max_area_m2": None,
    "min_price_eur": 400.0, "max_price_eur": 1200.0, "min_price_warm_eur": None, "max_price_warm_eur": 1500.0,
}


def _flat(rooms, area, kalt, warm):
    return {"rooms": rooms, "area_m2": area, "price_eur": kalt, "price_warm_eur": warm}


def _market(listings):
    market = housing_relax.Market.__new__(housing_relax.Market)
    market.days = housing_relax.RELAX_DAYS
    market.local = {"semmelhaack": listings}
    market._immowelt_filters = []
    market._local_filters = {}
    return market


class RelaxTests(unittest.TestCase):
    def test_only_sensible_relaxations_are_offered(self):
        self.assertEqual(housing_relax.available(FAMILY), ["price", "rooms", "area", "minprice"])
        self.assertEqual(housing_relax.available({"min_rooms": 1, "districts": ["Babelsberg"]}), ["districts"])

    def test_price_step_raises_every_upper_price_including_propotsdam_total_rent(self):
        relaxed = housing_relax.relax({"max_price_eur": 1200, "max_total_rent_eur": 1500, "min_price_eur": 400}, "price")
        self.assertEqual(relaxed["max_price_eur"], 1300)
        self.assertEqual(relaxed["max_total_rent_eur"], 1600)
        self.assertEqual(relaxed["min_price_eur"], 400)

    def test_district_relaxation_keeps_the_storage_format(self):
        self.assertEqual(housing_relax.relax({"districts": "Babelsberg"}, "districts")["districts"], "")
        self.assertEqual(housing_relax.relax({"districts": ["Babelsberg"]}, "districts")["districts"], [])

    def test_one_room_less_from_two_means_from_one(self):
        self.assertEqual(housing_relax.relax({"min_rooms": 2}, "rooms")["min_rooms"], 1)

    def test_offers_count_with_the_real_matcher_and_skip_useless_ones(self):
        market = _market([
            _flat(4, 96, 1250, 1700),
            _flat(3, 75, 900, 1200), _flat(3, 72, 950, 1250), _flat(3, 80, 1100, 1450),
        ])
        filters = [("semmelhaack", dict(FAMILY))]
        base, found = housing_relax.offers(market, FAMILY, filters)
        self.assertEqual(base, 0)
        # +100 € не дотягує до 1700 € тепла — такої кнопки нема.
        self.assertEqual(found, [("rooms", 3)])

    def test_small_gain_on_a_broad_filter_is_not_offered(self):
        listings = [_flat(2, 60, 700, 900) for _ in range(20)] + [_flat(1, 40, 600, 800)]
        market = _market(listings)
        criteria = {"min_rooms": 2, "max_price_eur": 1000}
        base, found = housing_relax.offers(market, criteria, [("semmelhaack", dict(criteria))])
        self.assertEqual(base, 20)
        self.assertEqual(found, [])

    def test_labels_are_localized(self):
        self.assertEqual(housing_relax.offer_label("rooms", FAMILY, "ru"), "от 3 комн. вместо 4")
        self.assertEqual(housing_relax.offer_label("area", FAMILY, "de"), "ab 60 m² statt 70")
        self.assertEqual(housing_relax.offer_label("price", FAMILY, "uk"), "на 100 € дорожче")


class ApplyTests(unittest.TestCase):
    def test_button_recreates_the_filter_with_the_relaxed_criteria(self):
        search = {
            "search_id": 95, "user_id": 353089809, "criteria": dict(FAMILY, property_types=[]),
            "members": [("immowelt", 137), ("semmelhaack", 107)], "coops": ["gewoba"],
            "created_at": datetime.utcnow(),
        }
        query = mock.Mock(data="housing:relax:95:rooms")
        update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=353089809))
        context = SimpleNamespace(user_data={})
        with mock.patch.object(housing_relax.housing_search_store, "get", return_value=search), \
                mock.patch.object(housing_monitor, "is_allowed", return_value=True), \
                mock.patch.object(housing_monitor, "_finalize_multi_filter") as finalize, \
                mock.patch.object(housing_relax.i18n, "get_lang", return_value="ru"):
            housing_monitor.handle_callback(update, context)
        state = finalize.call_args[0][2]
        self.assertEqual(state["replace_search_id"], 95)
        self.assertEqual(state["min_rooms"], 3)
        self.assertEqual(state["min_area_m2"], 70.0)
        self.assertEqual(state["sources_selected"], ["immowelt", "semmelhaack"])
        self.assertEqual(state["coops_selected"], ["gewoba"])
        query.edit_message_reply_markup.assert_called_once_with(reply_markup=None)

    def test_someone_elses_filter_is_refused(self):
        query = mock.Mock(data="housing:relax:95:rooms")
        update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=1))
        with mock.patch.object(housing_relax.housing_search_store, "get", return_value=None), \
                mock.patch.object(housing_monitor, "_finalize_multi_filter") as finalize:
            housing_relax.apply(update, SimpleNamespace(user_data={}))
        finalize.assert_not_called()
        query.answer.assert_called_once()


class QuietJobTests(unittest.TestCase):
    def _search(self, user_id, age_days=10):
        return {
            "search_id": user_id, "user_id": user_id, "criteria": dict(FAMILY), "coops": [],
            "members": [("semmelhaack", 1)], "created_at": datetime.utcnow() - timedelta(days=age_days),
        }

    def _run(self, searches, got_flats=(), due=True, week_flats=()):
        market = _market([_flat(3, 75, 900, 1200)])
        market._local_filters = {"semmelhaack": {1: dict(FAMILY, active=True)}}
        week = _market(list(week_flats))
        week._local_filters = market._local_filters
        bot = mock.Mock()
        with mock.patch.object(housing_relax.housing_search_store, "list_all", return_value=searches), \
                mock.patch.object(housing_relax.housing_search_store, "list_for_user", side_effect=lambda uid: [s for s in searches if s["user_id"] == uid]), \
                mock.patch.object(housing_relax, "_users_with_flats_since", return_value=set(got_flats)), \
                mock.patch.object(housing_relax, "Market", side_effect=[market, week]), \
                mock.patch.object(housing_monitor, "_all_immowelt_filters", return_value=[]), \
                mock.patch.object(housing_relax.housing_journey_store, "quiet_nudge_due", return_value=due), \
                mock.patch.object(housing_relax.housing_journey_store, "mark_quiet_nudge_sent") as mark, \
                mock.patch.object(housing_relax.i18n, "get_lang", return_value="ru"), \
                mock.patch.object(housing_relax, "SEND_PAUSE_SECONDS", 0):
            housing_relax.quiet_filters_job(SimpleNamespace(bot=bot))
        return bot, mark

    def test_a_week_of_silence_gets_an_explanation_with_buttons(self):
        bot, mark = self._run([self._search(5)])
        bot.send_message.assert_called_once()
        kwargs = bot.send_message.call_args.kwargs
        self.assertIn("ни одной квартиры", kwargs["text"])
        self.assertIn("от 3 комн. вместо 4", kwargs["text"])
        callbacks = [b.callback_data for row in kwargs["reply_markup"].inline_keyboard for b in row]
        self.assertIn("housing:relax:5:rooms", callbacks)
        mark.assert_called_once_with(5)

    def test_people_who_got_flats_or_were_told_recently_or_are_new_are_left_alone(self):
        bot, _ = self._run([self._search(5)], got_flats={5})
        bot.send_message.assert_not_called()
        bot, _ = self._run([self._search(5)], due=False)
        bot.send_message.assert_not_called()
        bot, _ = self._run([self._search(5, age_days=3)])
        bot.send_message.assert_not_called()

    def test_no_market_excuse_when_matching_flats_did_appear_this_week(self):
        """Підхожа квартира за тиждень була, але не дійшла (безкоштовна
        затримка чи збій) — казати «на сайтах нічого не було» не можна."""
        bot, mark = self._run([self._search(5)], week_flats=[_flat(4, 90, 1100, 1400)])
        bot.send_message.assert_not_called()
        mark.assert_not_called()


class PickerTests(unittest.TestCase):
    def test_wizard_no_longer_asks_for_a_minimum_price(self):
        self.assertNotIn("min_price", housing_monitor.CRITERIA_PICKER_KEYS)
        state = {"sources_selected": ["semmelhaack", "propotsdam"], "criteria_selected": list(housing_monitor.CRITERIA_PICKER_KEYS)}
        steps = housing_monitor._visible_multi_keys(state)
        self.assertFalse(set(steps) & housing_monitor.MIN_PRICE_STEP_KEYS)


if __name__ == "__main__":
    unittest.main()
