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
from user_handlers import housing_monitor
from user_jobs import housing_journey_store, housing_tier, kleinanzeigen_store


def _engine():
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return engine


class _InMemoryDb(unittest.TestCase):
    MODULES = (housing_tier, housing_journey_store, kleinanzeigen_store)

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

    def test_an_edit_reaches_the_admin_but_does_not_repeat_the_count_to_the_person(self):
        bot = mock.Mock()
        bot.get_chat.side_effect = Exception('no chat')
        job = SimpleNamespace(context={'user_id': 777, 'saved': [('kleinanzeigen', 999, {})], 'edited': True})

        with mock.patch.object(housing_monitor, 'ADMIN_ID', 312029534):
            housing_monitor._filter_report_job(SimpleNamespace(bot=bot, job=job))

        [call] = bot.send_message.call_args_list
        self.assertEqual(call.kwargs['chat_id'], 312029534)
        self.assertIn('Фільтр змінено', call.kwargs['text'])


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
