"""Free tier with a delay, the send log, filter reports to the admin,
the no-filter nudge and the two-week survey (27.09.2026)."""

import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database import (
    Base,
    HousingAccessUser,
    HousingSendLog,
    KleinanzeigenListing,
)
from database import HousingHeldMatch, HousingSearch
from user_handlers import housing_monitor
from user_jobs import housing_journey_store, housing_missed, housing_tier, kleinanzeigen_store


def _engine():
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return engine


class _InMemoryDb(unittest.TestCase):
    MODULES = (housing_tier, housing_journey_store, kleinanzeigen_store, housing_missed)

    def setUp(self):
        self.engine = _engine()
        self._saved = [(module, module.DBSession) for module in self.MODULES]
        for module in self.MODULES:
            module.DBSession = sessionmaker(bind=self.engine)
        self._saved.append((housing_monitor, housing_monitor.DBSession))
        housing_monitor.DBSession = sessionmaker(bind=self.engine)

    def tearDown(self):
        for module, session in self._saved:
            module.DBSession = session
        self.engine.dispose()

    def _session(self):
        return sessionmaker(bind=self.engine)()


class DeliveryGateTests(_InMemoryDb):
    def _listing(self, key, age):
        session = self._session()
        session.add(KleinanzeigenListing(
            listing_key=key, title='W', address='Potsdam', first_seen_at=datetime.utcnow() - age,
            last_seen_at=datetime.utcnow(), is_active=True,
        ))
        session.commit()
        session.close()
        return {'listing_key': key}

    def test_a_free_user_waits_until_the_listing_is_old_enough(self):
        fresh = self._listing('fresh', timedelta(minutes=20))
        old = self._listing('old', housing_tier.FREE_DELAY + timedelta(minutes=1))
        gate = housing_tier.DeliveryGate('kleinanzeigen', KleinanzeigenListing)

        self.assertFalse(gate.due(777, fresh))
        self.assertTrue(gate.due(777, old))
        self.assertIn('5 €', gate.footer(777))

    def test_a_subscriber_gets_everything_at_once_without_the_note(self):
        fresh = self._listing('fresh', timedelta(minutes=1))
        session = self._session()
        session.add(HousingAccessUser(
            user_id=777, display_name='x', active=True, is_trial=False,
            created_at=datetime.utcnow(), updated_at=datetime.utcnow(),
        ))
        session.commit()
        session.close()
        gate = housing_tier.DeliveryGate('kleinanzeigen', KleinanzeigenListing)

        self.assertTrue(gate.due(777, fresh))
        self.assertEqual(gate.footer(777), '')

    def test_every_send_is_logged_with_its_tier(self):
        gate = housing_tier.DeliveryGate('kleinanzeigen', KleinanzeigenListing)

        gate.sent(777, 'k-1', 5)

        session = self._session()
        [row] = session.query(HousingSendLog).all()
        self.assertEqual((row.user_id, row.source, row.listing_key, row.filter_id, row.delayed),
                         (777, 'kleinanzeigen', 'k-1', 5, True))
        session.close()


class QuietNightTests(_InMemoryDb):
    """25.10.2026: free-tier flats no longer arrive at 2 a.m., and the
    "quiet night" setting now holds back every source, not just Immowelt."""

    def _listing(self, key):
        session = self._session()
        session.add(KleinanzeigenListing(
            listing_key=key, title='W', address='Potsdam',
            first_seen_at=datetime.utcnow() - housing_tier.FREE_DELAY - timedelta(minutes=1),
            last_seen_at=datetime.utcnow(), is_active=True,
        ))
        session.commit()
        session.close()
        return {'listing_key': key}

    def _subscribe(self, user_id):
        session = self._session()
        session.add(HousingAccessUser(
            user_id=user_id, display_name='x', active=True, is_trial=False,
            created_at=datetime.utcnow(), updated_at=datetime.utcnow(),
        ))
        session.commit()
        session.close()

    def test_the_window_is_berlin_time_in_summer_and_winter(self):
        with mock.patch.object(housing_tier, 'QUIET_START_HOUR', 23), \
                mock.patch.object(housing_tier, 'QUIET_END_HOUR', 8):
            # CEST: UTC+2
            self.assertTrue(housing_tier.is_quiet_hours(datetime(2026, 7, 1, 21, 30)))   # 23:30
            self.assertTrue(housing_tier.is_quiet_hours(datetime(2026, 7, 1, 0, 16)))    # 02:16
            self.assertFalse(housing_tier.is_quiet_hours(datetime(2026, 7, 1, 6, 0)))    # 08:00
            self.assertFalse(housing_tier.is_quiet_hours(datetime(2026, 7, 1, 20, 59)))  # 22:59
            # CET: UTC+1
            self.assertTrue(housing_tier.is_quiet_hours(datetime(2026, 12, 1, 22, 0)))   # 23:00
            self.assertTrue(housing_tier.is_quiet_hours(datetime(2026, 12, 1, 6, 59)))   # 07:59
            self.assertFalse(housing_tier.is_quiet_hours(datetime(2026, 12, 1, 7, 0)))   # 08:00

    def test_a_free_user_gets_nothing_at_night_without_touching_the_setting(self):
        listing = self._listing('k')
        with mock.patch.object(housing_tier, 'FREE_QUIET_NIGHT', True), \
                mock.patch.object(housing_tier, 'is_quiet_hours', return_value=True), \
                mock.patch.object(housing_tier, 'wants_quiet_night') as wants:
            gate = housing_tier.DeliveryGate('kleinanzeigen', KleinanzeigenListing)
            self.assertFalse(gate.due(777, listing))
        wants.assert_not_called()

    def test_by_day_the_free_user_gets_it_as_before(self):
        listing = self._listing('k')
        with mock.patch.object(housing_tier, 'FREE_QUIET_NIGHT', True), \
                mock.patch.object(housing_tier, 'is_quiet_hours', return_value=False):
            gate = housing_tier.DeliveryGate('kleinanzeigen', KleinanzeigenListing)
            self.assertTrue(gate.due(777, listing))

    def test_a_subscriber_gets_flats_at_night_unless_they_asked_for_quiet(self):
        listing = self._listing('k')
        self._subscribe(777)
        self._subscribe(888)
        with mock.patch.object(housing_tier, 'FREE_QUIET_NIGHT', True), \
                mock.patch.object(housing_tier, 'is_quiet_hours', return_value=True), \
                mock.patch.object(housing_tier, 'wants_quiet_night', side_effect=lambda uid: uid == 888):
            gate = housing_tier.DeliveryGate('kleinanzeigen', KleinanzeigenListing)
            self.assertTrue(gate.due(777, listing))
            self.assertFalse(gate.due(888, listing))

    def test_a_manual_last_hour_send_ignores_the_night(self):
        listing = self._listing('k')
        with mock.patch.object(housing_tier, 'FREE_QUIET_NIGHT', True), \
                mock.patch.object(housing_tier, 'is_quiet_hours', return_value=True):
            gate = housing_tier.DeliveryGate('kleinanzeigen', KleinanzeigenListing, quiet_hours=False)
            self.assertTrue(gate.due(777, listing))

    def test_the_setting_is_read_once_and_an_unreachable_receiver_means_no_hold(self):
        housing_tier.forget_quiet_pref(777)
        with mock.patch.object(housing_tier.requests, 'get', side_effect=OSError('down')) as get:
            self.assertFalse(housing_tier.wants_quiet_night(777))
            self.assertFalse(housing_tier.wants_quiet_night(777))
        self.assertEqual(get.call_count, 1)
        housing_tier.forget_quiet_pref(777)

    def test_free_users_see_the_night_in_the_menu_and_settings_without_a_toggle(self):
        with mock.patch.object(housing_tier, 'FREE_QUIET_NIGHT', True):
            self.assertIn('23:00', housing_monitor._tier_line(777))
            text = housing_monitor._notify_settings_text({}, 'uk', True, free_quiet=True)
            keyboard = housing_monitor._notify_settings_keyboard({}, 'uk', True, free_quiet=True)
        self.assertIn('безкоштовній', text)
        callbacks = [b.callback_data for row in keyboard.inline_keyboard for b in row]
        self.assertFalse(any(c.startswith('housing:notify_quiet') for c in callbacks))


class MissedFlatTests(_InMemoryDb):
    """25.10.2026: a held free-tier flat taken down before its delay ran out
    gets a short note instead of silence."""

    def _listing(self, key, age=timedelta(minutes=20), active=True, first_seen=None):
        session = self._session()
        session.add(KleinanzeigenListing(
            listing_key=key, title='Wohnung', address='Babelsberg', rooms=3, area_m2=75, price_eur=850,
            first_seen_at=first_seen or datetime.utcnow() - age, last_seen_at=datetime.utcnow(), is_active=active,
        ))
        session.commit()
        session.close()
        return {'listing_key': key, 'title': 'Wohnung', 'address': 'Babelsberg',
                'rooms': 3, 'area_m2': 75, 'price_eur': 850}

    def _has_filter(self, user_id):
        session = self._session()
        session.add(HousingSearch(user_id=user_id, criteria='{}', members='[]', coops='[]',
                                  created_at=datetime.utcnow()))
        session.commit()
        session.close()

    def _deactivate(self, key):
        session = self._session()
        session.query(KleinanzeigenListing).filter_by(listing_key=key).update({'is_active': False})
        session.commit()
        session.close()

    def _held(self):
        session = self._session()
        try:
            return [(r.user_id, r.source, r.listing_key) for r in session.query(HousingHeldMatch).all()]
        finally:
            session.close()

    def test_a_held_flat_is_written_down_once_and_forgotten_when_sent(self):
        listing = self._listing('k')
        gate = housing_tier.DeliveryGate('kleinanzeigen', KleinanzeigenListing)
        self.assertFalse(gate.due(777, listing))
        self.assertFalse(gate.due(777, listing))
        self.assertFalse(housing_tier.DeliveryGate('kleinanzeigen', KleinanzeigenListing).due(777, listing))
        self.assertEqual(self._held(), [(777, 'kleinanzeigen', 'k')])

        gate.sent(777, 'k', 1)
        self.assertEqual(self._held(), [])

    def test_near_misses_and_subscribers_are_not_written_down(self):
        listing = self._listing('k')
        housing_tier.DeliveryGate('kleinanzeigen', KleinanzeigenListing).due(777, listing, remember=False)
        self.assertEqual(self._held(), [])

    def test_a_flat_taken_down_while_held_brings_a_note_with_the_upgrade_button(self):
        self._has_filter(777)
        listing = self._listing('k')
        self._listing('still-there')
        housing_tier.DeliveryGate('kleinanzeigen', KleinanzeigenListing).due(777, listing)
        bot = mock.Mock()

        housing_missed.missed_flats_job(SimpleNamespace(bot=bot))
        bot.send_message.assert_not_called()
        self.assertEqual(len(self._held()), 1)

        self._deactivate('k')
        housing_missed.missed_flats_job(SimpleNamespace(bot=bot))
        [call] = bot.send_message.call_args_list
        self.assertEqual(call.kwargs['chat_id'], 777)
        self.assertIn('вже зняли', call.kwargs['text'])
        self.assertIn('Kleinanzeigen', call.kwargs['text'])
        self.assertIn('75 m²', call.kwargs['text'])
        callbacks = [b.callback_data for row in call.kwargs['reply_markup'].inline_keyboard for b in row]
        self.assertEqual(callbacks, ['housing:access_request'])
        self.assertEqual(self._held(), [])

    def test_a_broken_scan_that_deactivated_everything_sends_nothing(self):
        self._has_filter(777)
        listing = self._listing('k')
        housing_tier.DeliveryGate('kleinanzeigen', KleinanzeigenListing).due(777, listing)
        self._deactivate('k')
        bot = mock.Mock()

        housing_missed.missed_flats_job(SimpleNamespace(bot=bot))

        bot.send_message.assert_not_called()
        self.assertEqual(len(self._held()), 1)

    def test_a_flat_pushed_off_a_one_page_feed_is_not_called_gone(self):
        self._has_filter(777)
        listing = self._listing('k', first_seen=datetime.utcnow() - timedelta(hours=1))
        for n in range(housing_missed.PUSHED_OFF_AFTER + 1):
            self._listing(f'newer-{n}', first_seen=datetime.utcnow() - timedelta(minutes=n))
        housing_tier.DeliveryGate('kleinanzeigen', KleinanzeigenListing).due(777, listing)
        self._deactivate('k')
        bot = mock.Mock()

        housing_missed.missed_flats_job(SimpleNamespace(bot=bot))

        bot.send_message.assert_not_called()
        self.assertEqual(self._held(), [])

    def test_many_flats_gone_at_once_look_like_a_glitch(self):
        self._has_filter(777)
        self._listing('still-there')
        listings = [self._listing(f'k{n}') for n in range(housing_missed.SUSPICIOUS_GONE + 1)]
        gate = housing_tier.DeliveryGate('kleinanzeigen', KleinanzeigenListing)
        for n, listing in enumerate(listings):
            self.assertFalse(gate.due(777, listing))
            self._deactivate(f'k{n}')
        bot = mock.Mock()

        housing_missed.missed_flats_job(SimpleNamespace(bot=bot))

        bot.send_message.assert_not_called()

    def test_no_note_without_filters(self):
        listing = self._listing('k')
        self._listing('still-there')
        housing_tier.DeliveryGate('kleinanzeigen', KleinanzeigenListing).due(777, listing)
        self._deactivate('k')
        bot = mock.Mock()

        housing_missed.missed_flats_job(SimpleNamespace(bot=bot))

        bot.send_message.assert_not_called()
        self.assertEqual(self._held(), [])


class KleinanzeigenMonitorFreeTierTests(_InMemoryDb):
    """The gate wired into a real monitor: a fresh match waits, then goes."""

    def test_a_fresh_match_waits_for_a_free_user_and_is_not_marked_delivered(self):
        from user_jobs import kleinanzeigen_monitor

        filter_id = kleinanzeigen_store.create_filter(user_id=777, title='t', min_rooms=2)
        listing = {
            'listing_key': 'k-1', 'title': 'Wohnung', 'address': 'Potsdam', 'city': 'Potsdam',
            'rooms': 3, 'area_m2': 60, 'price_eur': 600, 'detail_url': 'https://www.kleinanzeigen.de/s-anzeige/1',
        }
        bot = mock.Mock()
        context = SimpleNamespace(bot=bot)
        with mock.patch.object(kleinanzeigen_monitor, '_fetch_listings', return_value=[listing]), \
             mock.patch.object(kleinanzeigen_monitor, '_add_full_rent', return_value=0):
            result = kleinanzeigen_monitor.check_job(context)
        self.assertEqual(result['sent'], 0)
        self.assertNotIn((filter_id, 'k-1'), kleinanzeigen_store.delivered_pairs())

        # Two hours later the same match goes out, with the free-tier note.
        session = self._session()
        session.query(KleinanzeigenListing).update(
            {'first_seen_at': datetime.utcnow() - housing_tier.FREE_DELAY - timedelta(minutes=1)})
        session.commit()
        session.close()
        with mock.patch.object(kleinanzeigen_monitor, '_fetch_listings', return_value=[listing]), \
             mock.patch.object(kleinanzeigen_monitor, '_add_full_rent', return_value=0):
            result = kleinanzeigen_monitor.check_job(context)
        self.assertEqual(result['sent'], 1)
        self.assertIn((filter_id, 'k-1'), kleinanzeigen_store.delivered_pairs())
        self.assertIn('5 €', bot.send_message.call_args.kwargs['text'])


class JourneyStoreTests(_InMemoryDb):
    def test_first_filter_is_recorded_once(self):
        self.assertTrue(housing_journey_store.mark_first_filter(1))
        self.assertFalse(housing_journey_store.mark_first_filter(1))

    def test_nudge_goes_to_visitors_without_a_filter_after_the_wait(self):
        housing_journey_store.touch_menu(1)
        housing_journey_store.touch_menu(2)
        housing_journey_store.mark_first_filter(2)
        not_before = datetime.utcnow() - timedelta(days=1)

        self.assertEqual(housing_journey_store.list_due_no_filter_nudges(timedelta(hours=3), not_before), [])
        self.assertEqual(housing_journey_store.list_due_no_filter_nudges(timedelta(0), not_before), [1])
        housing_journey_store.mark_no_filter_nudge_sent(1)
        self.assertEqual(housing_journey_store.list_due_no_filter_nudges(timedelta(0), not_before), [])

    def test_survey_is_due_two_weeks_after_the_first_filter_and_only_once(self):
        housing_journey_store.mark_first_filter(1)
        self.assertEqual(housing_journey_store.list_due_surveys(timedelta(days=14)), [])
        self.assertEqual(housing_journey_store.list_due_surveys(timedelta(0)), [1])
        housing_journey_store.mark_survey_sent(1)
        self.assertEqual(housing_journey_store.list_due_surveys(timedelta(0)), [])

    def test_one_survey_answer_per_person(self):
        self.assertTrue(housing_journey_store.save_survey_answer(1, 'few'))
        self.assertFalse(housing_journey_store.save_survey_answer(1, 'price'))


class FilterReportTests(_InMemoryDb):
    def test_saving_a_filter_schedules_the_report_and_records_the_first_filter(self):
        context = SimpleNamespace(job_queue=mock.Mock())

        housing_monitor._after_filter_saved(context, 777, [('kleinanzeigen', 1, {'min_rooms': 2})])

        context.job_queue.run_once.assert_called_once()
        self.assertEqual(housing_journey_store.list_due_surveys(timedelta(0)), [777])

    def test_the_report_counts_the_last_month_and_warns_about_a_thin_filter(self):
        filter_id = kleinanzeigen_store.create_filter(user_id=777, title='t', min_rooms=2, max_price_eur=500)
        session = self._session()
        for key, rooms, price, age in (('a', 2, 450, 3), ('b', 3, 480, 40), ('c', 1, 400, 2), ('d', 2, 900, 2)):
            session.add(KleinanzeigenListing(
                listing_key=key, title='W', address='Potsdam', rooms=rooms, price_eur=price,
                first_seen_at=datetime.utcnow() - timedelta(days=age), last_seen_at=datetime.utcnow(),
                is_active=False,
            ))
        session.commit()
        session.close()
        bot = mock.Mock()
        bot.get_chat.return_value = SimpleNamespace(id=777, first_name='Іван', last_name='', username='ivan')
        job = SimpleNamespace(context={
            'user_id': 777, 'saved': [('kleinanzeigen', filter_id, {'min_rooms': 2, 'max_price_eur': 500})],
            'edited': False,
        })

        with mock.patch.object(housing_monitor, 'ADMIN_ID', 312029534):
            housing_monitor._filter_report_job(SimpleNamespace(bot=bot, job=job))

        calls = {call.kwargs['chat_id']: call.kwargs for call in bot.send_message.call_args_list}
        # Only 'a' fits and is within 30 days - even though it's gone already.
        self.assertIn('<b>1</b>', calls[777]['text'])
        self.assertIn('⚠️', calls[777]['text'])
        admin_text = calls[312029534]['text']
        self.assertIn('Новий фільтр', admin_text)
        self.assertIn('Іван (@ivan)', admin_text)
        self.assertIn('Kleinanzeigen 1', admin_text)
        self.assertIn('Замало', admin_text)

    def test_an_ordinary_edit_waits_for_the_evening_summary(self):
        bot = mock.Mock()
        bot.get_chat.side_effect = Exception('no chat')
        job = SimpleNamespace(context={'user_id': 777, 'saved': [('kleinanzeigen', 999, {})], 'edited': True})

        with mock.patch.object(housing_monitor, 'ADMIN_ID', 312029534):
            housing_monitor._filter_report_job(SimpleNamespace(bot=bot, job=job))

        bot.send_message.assert_not_called()

    def test_a_wide_new_filter_does_not_ping_the_admin(self):
        bot = mock.Mock()
        job = SimpleNamespace(context={'user_id': 777, 'saved': [('kleinanzeigen', 1, {})], 'edited': False})

        with mock.patch.object(housing_monitor, 'ADMIN_ID', 312029534), \
                mock.patch.object(housing_monitor, '_filter_hits', return_value={'kleinanzeigen': 40}):
            housing_monitor._filter_report_job(SimpleNamespace(bot=bot, job=job))

        self.assertEqual([call.kwargs['chat_id'] for call in bot.send_message.call_args_list], [777])

    def test_the_evening_summary_counts_new_and_edited_filters_per_person(self):
        bot = mock.Mock()
        bot.get_chat.side_effect = lambda user_id: SimpleNamespace(
            id=user_id, first_name=f'U{user_id}', last_name='', username='')
        with mock.patch.object(housing_monitor, 'ADMIN_ID', 312029534):
            for user_id, edited, hits in ((777, False, {'kleinanzeigen': 40}), (777, True, {'kleinanzeigen': 35}),
                                          (888, False, {'kleinanzeigen': 1})):
                job = SimpleNamespace(context={'user_id': user_id, 'saved': [('kleinanzeigen', 1, {})],
                                               'edited': edited})
                with mock.patch.object(housing_monitor, '_filter_hits', return_value=hits):
                    housing_monitor._filter_report_job(SimpleNamespace(bot=bot, job=job))
            bot.send_message.reset_mock()

            housing_monitor.daily_filter_summary_job(SimpleNamespace(bot=bot))

        [call] = bot.send_message.call_args_list
        text = call.kwargs['text']
        self.assertEqual(call.kwargs['chat_id'], 312029534)
        self.assertIn('Нових: <b>2</b> (людей: 2)', text)
        self.assertIn('Змінено: <b>1</b> (людей: 1)', text)
        self.assertIn('Завузьких зараз: <b>1</b>', text)
        self.assertIn('U777 · <code>777</code> — нових 1, змін 1, за 30 днів підійшло б 35', text)
        self.assertIn('⚠️ U888', text)

    def test_a_quiet_day_sends_no_summary(self):
        bot = mock.Mock()
        with mock.patch.object(housing_monitor, 'ADMIN_ID', 312029534):
            housing_monitor.daily_filter_summary_job(SimpleNamespace(bot=bot))

        bot.send_message.assert_not_called()


class FollowupTests(_InMemoryDb):
    def test_a_visitor_without_a_filter_gets_one_nudge(self):
        housing_journey_store.touch_menu(777)
        bot = mock.Mock()

        with mock.patch.object(housing_monitor, 'NO_FILTER_NUDGE_AFTER', timedelta(0)), \
             mock.patch.object(housing_monitor, 'NO_FILTER_NUDGE_NOT_BEFORE', datetime.utcnow() - timedelta(days=1)), \
             mock.patch.object(housing_monitor, 'user_filters', return_value=[]):
            housing_monitor.housing_followups_job(SimpleNamespace(bot=bot))
            housing_monitor.housing_followups_job(SimpleNamespace(bot=bot))

        [call] = bot.send_message.call_args_list
        callbacks = [b.callback_data for row in call.kwargs['reply_markup'].inline_keyboard for b in row]
        self.assertIn('housing:self_add', callbacks)

    def test_someone_who_already_had_filters_gets_no_nudge_but_the_survey_clock_starts(self):
        housing_journey_store.touch_menu(777)
        bot = mock.Mock()

        with mock.patch.object(housing_monitor, 'NO_FILTER_NUDGE_AFTER', timedelta(0)), \
             mock.patch.object(housing_monitor, 'NO_FILTER_NUDGE_NOT_BEFORE', datetime.utcnow() - timedelta(days=1)), \
             mock.patch.object(housing_monitor, 'user_filters', return_value=[{'filter_id': 1}]):
            housing_monitor.housing_followups_job(SimpleNamespace(bot=bot))

        bot.send_message.assert_not_called()
        self.assertEqual(housing_journey_store.list_due_surveys(timedelta(0)), [777])

    def test_the_survey_goes_out_once_and_an_answer_reaches_the_admin(self):
        housing_journey_store.mark_first_filter(777)
        bot = mock.Mock()
        with mock.patch.object(housing_monitor, 'SURVEY_AFTER', timedelta(0)):
            housing_monitor.housing_followups_job(SimpleNamespace(bot=bot))
            housing_monitor.housing_followups_job(SimpleNamespace(bot=bot))
        [call] = bot.send_message.call_args_list
        callbacks = [b.callback_data for row in call.kwargs['reply_markup'].inline_keyboard for b in row]
        self.assertIn('housing:survey:price', callbacks)

        query = SimpleNamespace(data='housing:survey:price', answer=mock.Mock(), edit_message_text=mock.Mock())
        update = SimpleNamespace(
            callback_query=query,
            effective_user=SimpleNamespace(id=777, first_name='Іван', last_name='', username=None),
        )
        context = SimpleNamespace(bot=mock.Mock())
        with mock.patch.object(housing_monitor, 'ADMIN_ID', 312029534):
            housing_monitor.handle_callback(update, context)
            housing_monitor.handle_callback(update, context)

        [admin_call] = context.bot.send_message.call_args_list
        self.assertEqual(admin_call.kwargs['chat_id'], 312029534)
        self.assertIn('задорого', admin_call.kwargs['text'])
        self.assertIn('вже відповіли', query.answer.call_args.args[0])


class SurveyHardFollowupTests(_InMemoryDb):
    def _press(self, data, context):
        query = SimpleNamespace(data=data, answer=mock.Mock(), edit_message_text=mock.Mock())
        update = SimpleNamespace(
            callback_query=query,
            effective_user=SimpleNamespace(id=777, first_name='Ігор', last_name='', username=None),
        )
        with mock.patch.object(housing_monitor, 'ADMIN_ID', 312029534):
            housing_monitor.handle_callback(update, context)
        return query

    def test_hard_asks_what_exactly_and_the_detail_reaches_the_admin(self):
        context = SimpleNamespace(bot=mock.Mock())
        query = self._press('housing:survey:hard', context)
        markup = query.edit_message_text.call_args.kwargs['reply_markup']
        callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertEqual(callbacks, [f'housing:survey_hard:{d}' for d in housing_monitor.SURVEY_HARD_DETAILS])

        self._press('housing:survey_hard:night', context)
        again = self._press('housing:survey_hard:many', context)

        session = self._session()
        from database import HousingUserJourney
        self.assertEqual(session.query(HousingUserJourney).get(777).survey_answer, 'hard:night')
        session.close()
        texts = [c.kwargs['text'] for c in context.bot.send_message.call_args_list]
        self.assertEqual(len(texts), 2)
        self.assertIn('Незручно', texts[0])
        self.assertIn('уночі', texts[1])
        self.assertIn('вже відповіли', again.answer.call_args.args[0])

    def test_a_detail_without_the_hard_answer_is_ignored(self):
        housing_journey_store.save_survey_answer(777, 'few')
        self.assertFalse(housing_journey_store.save_survey_detail(777, 'hard', 'night'))


class OpenToAllMenuTests(_InMemoryDb):
    def test_anyone_can_use_monitoring_now(self):
        with mock.patch.object(housing_monitor, 'ALLOWED_USER_IDS', set()):
            self.assertTrue(housing_monitor.is_allowed(999))

    def test_a_free_user_sees_the_delay_and_the_upgrade_button(self):
        with mock.patch.object(housing_monitor, 'user_filters', return_value=[]):
            text = housing_monitor._render_menu(999, 'uk')
            keyboard = housing_monitor._menu_keyboard(999, 'uk')
        callbacks = [b.callback_data for row in keyboard.inline_keyboard for b in row]
        self.assertIn('затримкою ~2 год', text)
        self.assertIn('housing:access_request', callbacks)
        self.assertIn('housing:self_add', callbacks)

    def test_a_subscriber_sees_neither(self):
        with mock.patch.object(housing_monitor, 'user_filters', return_value=[]), \
             mock.patch.object(housing_monitor.housing_tier, 'is_premium', return_value=True), \
             mock.patch.object(housing_monitor.housing_access_store, 'list_users', return_value=[
                 {'user_id': 999, 'expires_at': datetime(2026, 11, 1)},
             ]):
            text = housing_monitor._render_menu(999, 'uk')
            keyboard = housing_monitor._menu_keyboard(999, 'uk')
        callbacks = [b.callback_data for row in keyboard.inline_keyboard for b in row]
        self.assertIn('01.11.2026', text)
        self.assertNotIn('housing:access_request', callbacks)


if __name__ == '__main__':
    unittest.main()
