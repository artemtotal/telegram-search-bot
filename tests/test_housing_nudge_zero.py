"""tools/nudge_2026_11_25_zero.py and the «▶️ Почати пошук» button (housing_resume), 25.11.2026."""

import importlib.util
import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database import Base, HousingAccessUser, HousingPayment, HousingSendLog
from user_handlers import housing_card, housing_resume
from user_jobs import housing_tier

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("nudge_zero", os.path.join(_ROOT, "tools", "nudge_2026_11_25_zero.py"))
nudge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(nudge)

USER = 4242
CRITERIA = {"max_price_eur": 800, "min_rooms": 2}


class _Market:
    def __init__(self, days, counts):
        self.days = days
        self.counts = counts

    def member_filters(self, members):
        return [(source, {"filter_id": fid}) for source, fid in members]

    def count(self, filters, code=None):
        return self.counts


class _Db(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        factory = sessionmaker(bind=self.engine)
        self._saved = [(nudge, nudge.DBSession), (housing_tier, housing_tier.DBSession)]
        nudge.DBSession = factory
        housing_tier.DBSession = factory
        self.session = factory()

    def tearDown(self):
        self.session.close()
        for module, session in self._saved:
            module.DBSession = session
        self.engine.dispose()

    def _pay(self, user_id=USER, when=None):
        if self.session.query(HousingAccessUser).filter_by(user_id=user_id).count():
            return
        self.session.add(HousingAccessUser(
            user_id=user_id, display_name='x', active=True, is_trial=False,
            created_at=datetime.utcnow(), updated_at=datetime.utcnow(),
        ))
        self.session.add(HousingPayment(
            user_id=user_id, display_name='x', months=1, amount_eur=5, amount_uah=250, eur_uah_rate=50,
            status='approved', created_at=datetime(2026, 11, 11, 9, 0), decided_at=when or datetime(2026, 11, 11, 9, 26),
        ))
        self.session.commit()


class CandidateTests(_Db):
    def test_only_paid_people_without_a_single_flat(self):
        self._pay(USER)
        self._pay(1)
        self._pay(2)
        self.session.add(HousingSendLog(user_id=1, source='x', listing_key='k', delayed=False, sent_at=datetime.utcnow()))
        self.session.commit()
        # 3 has access but no approved payment (a gift or the admin): not a candidate
        self.session.add(HousingAccessUser(user_id=3, display_name='g', active=True, is_trial=False,
                                           created_at=datetime.utcnow(), updated_at=datetime.utcnow()))
        self.session.commit()
        self.assertEqual(nudge.candidates(), [2, USER])

    def test_counting_sends(self):
        self.session.add(HousingSendLog(user_id=USER, source='x', listing_key='k', delayed=False,
                                        sent_at=datetime.utcnow() - timedelta(days=20)))
        self.session.commit()
        self.assertEqual(nudge.sends_since(USER), 1)
        self.assertEqual(nudge.sends_since(USER, datetime.utcnow() - timedelta(days=14)), 0)


class MessageTests(_Db):
    def _build(self, paused, week_count, month_count=0, offers=(0, []), lang='uk'):
        self._pay()
        search = {"search_id": 9, "criteria": CRITERIA, "members": [("kleinanzeigen", 1)], "coops": []}
        live = {"search_id": 9, "items": [{"active": not paused}]}
        with mock.patch.object(nudge.housing_search_store, 'list_for_user', return_value=[search]), \
                mock.patch.object(nudge.hm, 'user_searches', return_value=[live]), \
                mock.patch.object(nudge, '_lang', return_value=lang), \
                mock.patch.object(nudge.housing_relax, 'offers', return_value=offers):
            return nudge.build_messages(USER, _Market(14, week_count), _Market(30, month_count))

    def test_a_truly_empty_market_gets_the_honest_text_and_the_relax_buttons(self):
        messages, notes = self._build(False, 0, 0, offers=(0, [('price', 12), ('rooms', 7)]))
        [message] = messages
        self.assertEqual(notes, [])
        self.assertIn('жодної квартири', message['text'])
        self.assertIn('11.11', message['text'])
        self.assertIn('на 100 € дорожче — <b>12</b>', message['text'])
        callbacks = [callback for _label, callback in message['buttons']]
        self.assertEqual(callbacks, ['housing:relax:9:price', 'housing:relax:9:rooms', 'housing:self_manage'])
        self.assertNotIn('Для порівняння', message['text'])  # zero is not shown

    def test_a_baseline_above_zero_is_shown_but_only_then(self):
        [message], _ = self._build(False, 0, 3, offers=(3, [('price', 9)]))
        self.assertIn('Для порівняння', message['text'])
        self.assertIn('3', message['text'])

    def test_no_offers_says_so_and_keeps_the_filters_button(self):
        [message], _ = self._build(False, 0, 0, offers=(0, []))
        self.assertEqual([c for _l, c in message['buttons']], ['housing:self_manage'])

    def test_flats_existed_so_nothing_is_sent_and_the_reason_is_named(self):
        messages, notes = self._build(False, 5)
        self.assertEqual(messages, [])
        self.assertIn('підходило 5', notes[0])

    def test_a_never_started_search_gets_the_start_button_instead_of_the_zero_claim(self):
        [message], _ = self._build(True, 948)
        self.assertTrue(message['paused'])
        self.assertIn('на паузі', message['text'])
        self.assertIn('948', message['text'])
        self.assertNotIn('жодної квартири', message['text'])
        self.assertEqual(message['buttons'][0][1], 'housing:resume:9')
        [paused_zero], _ = self._build(True, 0)
        self.assertNotIn('Підходящих квартир', paused_zero['text'])  # zero is not shown

    def test_german_and_russian_are_complete(self):
        for lang, needle in (('de', 'pausiert'), ('ru', 'на паузе')):
            [message], _ = self._build(True, 10, lang=lang)
            self.assertIn(needle, message['text'])
            self.assertNotIn('[[', message['text'])
        for lang in ('uk', 'ru', 'de'):
            self.assertEqual(set(nudge.TEXT[lang]), set(nudge.TEXT['uk']))

    def test_no_searches_nothing_to_send(self):
        with mock.patch.object(nudge.housing_search_store, 'list_for_user', return_value=[]):
            self.assertEqual(nudge.build_messages(USER, _Market(14, 0), _Market(30, 0)), ([], ['немає пошуків']))


class MainTests(_Db):
    def _run(self, argv, now_night=False):
        out = io.StringIO()
        with redirect_stdout(out), \
                mock.patch.object(nudge.housing_relax, 'Market', side_effect=lambda days, *a: _Market(days, 0)), \
                mock.patch.object(nudge, '_night_now', return_value=now_night), \
                mock.patch.object(nudge.housing_search_store, 'list_for_user', return_value=[
                    {"search_id": 9, "criteria": CRITERIA, "members": [("kleinanzeigen", 1)], "coops": []}]), \
                mock.patch.object(nudge.hm, 'user_searches', return_value=[{"search_id": 9, "items": [{"active": False}]}]), \
                mock.patch.object(nudge.housing_journey_store, 'mark_quiet_nudge_sent') as marked, \
                mock.patch.dict(os.environ, {'BOT_TOKEN': 'x'}):
            code = nudge.main(argv)
        return code, out.getvalue(), marked

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.mkdtemp()
        self._done = mock.patch.object(nudge, 'DONE_FILE', os.path.join(self._tmp, 'done.json'))
        self._done.start()
        self._pay()

    def tearDown(self):
        self._done.stop()
        super().tearDown()

    def test_dry_run_prints_and_sends_nothing(self):
        with mock.patch('telegram.Bot') as bot:
            code, out, marked = self._run([str(USER)])
        self.assertEqual(code, 0)
        self.assertIn('housing:resume:9', out)
        self.assertIn("'dry_run': True", out)
        bot.assert_not_called()
        marked.assert_not_called()
        self.assertFalse(os.path.exists(nudge.DONE_FILE))

    def test_send_goes_out_once_and_is_remembered(self):
        with mock.patch('telegram.Bot') as bot:
            bot.return_value.send_message = mock.Mock()
            code, out, marked = self._run([str(USER), '--send'])
            self.assertEqual(bot.return_value.send_message.call_count, 1)
            kwargs = bot.return_value.send_message.call_args.kwargs
            self.assertEqual(kwargs['chat_id'], USER)
            self.assertEqual(kwargs['parse_mode'], 'HTML')
            marked.assert_called_once_with(USER)
            code, out, marked = self._run([str(USER), '--send'])
            self.assertIn('вже надсилалось', out)
            self.assertEqual(bot.return_value.send_message.call_count, 1)

    def test_night_needs_a_flag(self):
        with mock.patch('telegram.Bot') as bot:
            code, out, _ = self._run([str(USER), '--send'], now_night=True)
        self.assertEqual(code, 2)
        bot.assert_not_called()

    def test_a_person_who_already_got_flats_is_skipped(self):
        self.session.add(HousingSendLog(user_id=USER, source='x', listing_key='k', delayed=False, sent_at=datetime.utcnow()))
        self.session.commit()
        code, out, _ = self._run([str(USER)])
        self.assertIn('квартир уже надсилали', out)
        self.assertNotIn('housing:resume', out)

    def test_not_a_subscriber_is_skipped(self):
        code, out, _ = self._run(['999'])
        self.assertIn('не підписник', out)


class ResumeButtonTests(unittest.TestCase):
    def _update(self, data, user_id=USER):
        query = SimpleNamespace(data=data, answer=mock.Mock(), edit_message_reply_markup=mock.Mock())
        return SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=user_id)), query

    def _context(self):
        return SimpleNamespace(bot=mock.Mock(), user_data={})

    def test_it_starts_a_paused_search_like_the_card_button(self):
        update, query = self._update('housing:resume:9')
        search = {"search_id": 9, "user_id": USER, "items": [{"active": False}]}
        context = self._context()
        seen = {}

        def started(u, c, s, active):
            seen['state'] = dict(c.user_data['hcard'])
            seen['active'] = active

        with mock.patch.object(housing_card, '_load', return_value=(1, search)), \
                mock.patch.object(housing_resume.hm, 'is_allowed', return_value=True), \
                mock.patch.object(housing_card, 'can_activate', return_value=True), \
                mock.patch.object(housing_card, '_set_active', side_effect=started):
            housing_resume.apply(update, context)
        self.assertEqual(seen, {'state': {'sid': 9, 'new': True}, 'active': True})
        self.assertNotIn('hcard', context.user_data)
        query.edit_message_reply_markup.assert_called_once_with(reply_markup=None)

    def test_someone_elses_search_is_refused(self):
        update, query = self._update('housing:resume:9')
        search = {"search_id": 9, "user_id": 1, "items": [{"active": False}]}
        with mock.patch.object(housing_card, '_load', return_value=(1, search)), \
                mock.patch.object(housing_card, '_set_active') as started:
            housing_resume.apply(update, self._context())
        started.assert_not_called()
        self.assertTrue(query.answer.call_args.kwargs.get('show_alert'))

    def test_an_already_running_search_is_left_alone(self):
        update, query = self._update('housing:resume:9')
        search = {"search_id": 9, "user_id": USER, "items": [{"active": True}]}
        with mock.patch.object(housing_card, '_load', return_value=(1, search)), \
                mock.patch.object(housing_resume.hm, 'is_allowed', return_value=True), \
                mock.patch.object(housing_card, '_set_active') as started:
            housing_resume.apply(update, self._context())
        started.assert_not_called()

    def test_bad_data_is_ignored(self):
        update, query = self._update('housing:resume:x')
        housing_resume.apply(update, self._context())
        query.answer.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
