"""Weekly digest: all new flats vs the ones matching the person's filters."""

import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database import Base, HousingSendLog, KleinanzeigenListing
from user_handlers import housing_digest, housing_monitor
from user_jobs import housing_journey_store, housing_tier, kleinanzeigen_store


class HousingDigestTests(unittest.TestCase):
    MODULES = (housing_monitor, housing_digest, housing_tier, housing_journey_store, kleinanzeigen_store)

    def setUp(self):
        self.engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self._saved = [(module, module.DBSession) for module in self.MODULES]
        for module in self.MODULES:
            module.DBSession = sessionmaker(bind=self.engine)
        # Immowelt: 3 flats in the week, the person's Immowelt filter catches one.
        self.immowelt_rows = [
            {'listing_key': 'iw-a', 'rooms': 2, 'area_m2': 55, 'price_eur': 600, 'match': True},
            {'listing_key': 'iw-b', 'rooms': 3, 'area_m2': 80, 'price_eur': 1400, 'match': False},
            {'listing_key': 'iw-c', 'rooms': 1, 'area_m2': 30, 'price_eur': 500, 'match': False},
        ]

        def fake_preview(criteria):
            if 'max_price_eur' in criteria:
                return {'rows': self.immowelt_rows}
            return {'rows': [dict(row, match=True) for row in self.immowelt_rows]}

        self._patches = [
            mock.patch.object(housing_monitor, '_preview_criteria', side_effect=fake_preview),
            mock.patch.object(housing_monitor, '_all_immowelt_filters', return_value=[
                {'filter_id': 9, 'user_id': 777, 'active': True, 'districts': [], 'max_price_eur': 700},
            ]),
            mock.patch.object(housing_digest.time, 'sleep'),
        ]
        for patch in self._patches:
            patch.start()

    def tearDown(self):
        for patch in self._patches:
            patch.stop()
        for module, session in self._saved:
            module.DBSession = session
        self.engine.dispose()

    def _add_listing(self, key, rooms, price, age_days=1, gone_after=None):
        first_seen = datetime.utcnow() - timedelta(days=age_days)
        session = sessionmaker(bind=self.engine)()
        session.add(KleinanzeigenListing(
            listing_key=key, title='W', address='Potsdam', rooms=rooms, area_m2=50, price_eur=price,
            first_seen_at=first_seen,
            last_seen_at=first_seen + gone_after if gone_after else datetime.utcnow(),
            is_active=gone_after is None,
        ))
        session.commit()
        session.close()

    def _run(self, only=None):
        bot = mock.Mock()
        result = housing_digest.weekly_digest_job(SimpleNamespace(bot=bot), only=only)
        return bot, result

    def test_the_digest_compares_the_whole_week_with_what_matched(self):
        kleinanzeigen_store.create_filter(user_id=777, title='t', min_rooms=2, max_price_eur=700)
        self._add_listing('k-fit', 2, 650)
        self._add_listing('k-gone', 3, 600, gone_after=timedelta(minutes=30))
        self._add_listing('k-big', 4, 1500)
        self._add_listing('k-old', 2, 650, age_days=9)  # before the week - not counted
        session = sessionmaker(bind=self.engine)()
        session.add(HousingSendLog(user_id=777, source='kleinanzeigen', listing_key='k-fit',
                                   delayed=True, sent_at=datetime.utcnow() - timedelta(days=1)))
        session.commit()
        session.close()

        bot, result = self._run()

        self.assertEqual(result, {'sent': 1, 'failed': 0})
        kwargs = bot.send_photo.call_args.kwargs
        self.assertEqual(kwargs['chat_id'], 777)
        caption = kwargs['caption']
        # 3 Kleinanzeigen + 3 Immowelt in the week; k-fit, k-gone, iw-a matched.
        self.assertIn('<b>6</b>', caption)
        self.assertIn('Під ваші фільтри: <b>3</b>', caption)
        self.assertIn('надіслав вам: <b>1</b>', caption)
        # k-gone vanished within the free delay - a free user never got it.
        self.assertIn('щонайменше 1', caption)
        callbacks = [b.callback_data for row in kwargs['reply_markup'].inline_keyboard for b in row]
        self.assertIn('housing:digest_off', callbacks)
        self.assertIn('housing:access_request', callbacks)

    def test_a_thin_filter_gets_the_warning(self):
        kleinanzeigen_store.create_filter(user_id=777, title='t', min_rooms=5)
        self.immowelt_rows = [dict(row, match=False) for row in self.immowelt_rows]
        self._add_listing('k-1', 2, 650)

        bot, _ = self._run()

        self.assertIn('⚠️', bot.send_photo.call_args.kwargs['caption'])

    def test_a_subscriber_is_not_told_about_missed_flats(self):
        kleinanzeigen_store.create_filter(user_id=777, title='t', min_rooms=2)
        self._add_listing('k-gone', 3, 600, gone_after=timedelta(minutes=30))

        with mock.patch.object(housing_tier, 'is_premium', return_value=True):
            bot, _ = self._run()

        kwargs = bot.send_photo.call_args.kwargs
        self.assertNotIn('щонайменше', kwargs['caption'])
        callbacks = [b.callback_data for row in kwargs['reply_markup'].inline_keyboard for b in row]
        self.assertNotIn('housing:access_request', callbacks)

    def test_people_who_turned_it_off_and_people_without_active_filters_get_nothing(self):
        kleinanzeigen_store.create_filter(user_id=555, title='t', min_rooms=2)
        paused = kleinanzeigen_store.create_filter(user_id=444, title='t', min_rooms=2)
        kleinanzeigen_store.set_filter_active(paused, False, user_id=444)
        housing_journey_store.set_digest_off(555)

        self.assertEqual(housing_digest.recipients(housing_monitor._all_immowelt_filters()), {777})

    def test_the_off_button_turns_it_off(self):
        query = SimpleNamespace(data='housing:digest_off', answer=mock.Mock(), edit_message_reply_markup=mock.Mock())
        update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=777))

        housing_monitor.handle_callback(update, SimpleNamespace(bot=mock.Mock()))

        self.assertEqual(housing_journey_store.digest_off_ids(), {777})
        query.edit_message_reply_markup.assert_called_once_with(reply_markup=None)


if __name__ == '__main__':
    unittest.main()
