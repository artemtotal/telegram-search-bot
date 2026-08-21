import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database import Base, CoopWatchdogStatus
from user_jobs import coop_watchdog


class FakeBot:
    def __init__(self):
        self.sent = []

    def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text, kwargs))


class FakeResponse:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


GEWOBA_EMPTY = "<html><body>Wir können derzeit keine freien Wohnungen anbieten.</body></html>"
GEWOBA_NOT_EMPTY = "<html><body><div class='angebot'>3 Zimmer, 75 m2, Paul-Neumann-Str.</div></body></html>"
WBG_EMPTY = "<html><body>Derzeit haben wir leider keine freien Objekte.</body></html>"
DAHEIM_EMPTY = "<html><body>Zur Zeit können wir Ihnen leider keine freien Wohnungen anbieten.</body></html>"


class CoopWatchdogTests(unittest.TestCase):
    def _fresh_session(self):
        engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
        Base.metadata.create_all(engine)
        return engine, sessionmaker(bind=engine)

    def test_disabled_flag_skips_the_scan_entirely(self):
        context = SimpleNamespace(bot=FakeBot())
        with mock.patch.object(coop_watchdog, 'CHECK_ENABLED', False), \
             mock.patch('requests.get') as get:
            result = coop_watchdog.check_job(context)

        get.assert_not_called()
        self.assertEqual(result, {'ok': 1, 'enabled': 0, 'alerts': 0})

    def test_first_ever_check_baselines_silently_without_alerting(self):
        """Brand-new row (was_empty=None) must not fire an alert just because
        the site happens to already show no vacancies on day one."""
        engine, test_session = self._fresh_session()
        context = SimpleNamespace(bot=FakeBot())
        original_session = coop_watchdog.DBSession
        coop_watchdog.DBSession = test_session
        try:
            with mock.patch.object(coop_watchdog, 'ADMIN_ID', 312029534), \
                 mock.patch('requests.get', side_effect=[
                     FakeResponse(GEWOBA_EMPTY), FakeResponse(WBG_EMPTY), FakeResponse(DAHEIM_EMPTY),
                 ]):
                result = coop_watchdog.check_job(context)
        finally:
            coop_watchdog.DBSession = original_session
            engine.dispose()

        self.assertEqual(len(context.bot.sent), 0)
        self.assertEqual(result['alerts'], 0)

    def test_transition_from_empty_to_not_empty_alerts_the_admin(self):
        engine, test_session = self._fresh_session()
        now = datetime.utcnow()
        session = test_session()
        session.add(CoopWatchdogStatus(key='gewoba', was_empty=True, last_checked_at=now, last_status='ok'))
        session.add(CoopWatchdogStatus(key='wbg1903', was_empty=True, last_checked_at=now, last_status='ok'))
        session.add(CoopWatchdogStatus(key='wbg_daheim', was_empty=True, last_checked_at=now, last_status='ok'))
        session.commit()
        session.close()

        context = SimpleNamespace(bot=FakeBot())
        original_session = coop_watchdog.DBSession
        coop_watchdog.DBSession = test_session
        try:
            with mock.patch.object(coop_watchdog, 'ADMIN_ID', 312029534), \
                 mock.patch(
                     'user_jobs.coop_watchdog.coop_watchdog_store.list_subscriber_ids', return_value=[]
                 ), \
                 mock.patch('requests.get', side_effect=[
                     FakeResponse(GEWOBA_NOT_EMPTY), FakeResponse(WBG_EMPTY), FakeResponse(DAHEIM_EMPTY),
                 ]):
                result = coop_watchdog.check_job(context)
        finally:
            coop_watchdog.DBSession = original_session
            engine.dispose()

        self.assertEqual(len(context.bot.sent), 1)
        self.assertEqual(context.bot.sent[0][0], 312029534)
        self.assertIn('Gewoba', context.bot.sent[0][1])
        self.assertEqual(result['alerts'], 1)

    def test_transition_also_notifies_subscribed_users_generically(self):
        """Subscribers get a plain "check yourself" nudge - no per-listing
        data exists yet for these sources, see CoopWatchdogFilter's docstring."""
        engine, test_session = self._fresh_session()
        now = datetime.utcnow()
        session = test_session()
        session.add(CoopWatchdogStatus(key='gewoba', was_empty=True, last_checked_at=now, last_status='ok'))
        session.add(CoopWatchdogStatus(key='wbg1903', was_empty=True, last_checked_at=now, last_status='ok'))
        session.add(CoopWatchdogStatus(key='wbg_daheim', was_empty=True, last_checked_at=now, last_status='ok'))
        session.commit()
        session.close()

        context = SimpleNamespace(bot=FakeBot())
        original_session = coop_watchdog.DBSession
        coop_watchdog.DBSession = test_session
        try:
            with mock.patch.object(coop_watchdog, 'ADMIN_ID', 312029534), \
                 mock.patch(
                     'user_jobs.coop_watchdog.coop_watchdog_store.list_subscriber_ids',
                     side_effect=lambda key: [544675510, 5115109366] if key == 'gewoba' else [],
                 ), \
                 mock.patch('requests.get', side_effect=[
                     FakeResponse(GEWOBA_NOT_EMPTY), FakeResponse(WBG_EMPTY), FakeResponse(DAHEIM_EMPTY),
                 ]):
                result = coop_watchdog.check_job(context)
        finally:
            coop_watchdog.DBSession = original_session
            engine.dispose()

        recipients = {chat_id for chat_id, _text, _kwargs in context.bot.sent}
        self.assertEqual(recipients, {312029534, 544675510, 5115109366})
        self.assertEqual(result['alerts'], 3)

    def test_daheim_transition_from_empty_to_not_empty_alerts_the_admin(self):
        engine, test_session = self._fresh_session()
        now = datetime.utcnow()
        session = test_session()
        session.add(CoopWatchdogStatus(key='gewoba', was_empty=True, last_checked_at=now, last_status='ok'))
        session.add(CoopWatchdogStatus(key='wbg1903', was_empty=True, last_checked_at=now, last_status='ok'))
        session.add(CoopWatchdogStatus(key='wbg_daheim', was_empty=True, last_checked_at=now, last_status='ok'))
        session.commit()
        session.close()

        context = SimpleNamespace(bot=FakeBot())
        original_session = coop_watchdog.DBSession
        coop_watchdog.DBSession = test_session
        try:
            with mock.patch.object(coop_watchdog, 'ADMIN_ID', 312029534), \
                 mock.patch(
                     'user_jobs.coop_watchdog.coop_watchdog_store.list_subscriber_ids', return_value=[]
                 ), \
                 mock.patch('requests.get', side_effect=[
                     FakeResponse(GEWOBA_EMPTY), FakeResponse(WBG_EMPTY),
                     FakeResponse("<html><body>Es gibt jetzt eine 3-Zimmer-Wohnung.</body></html>"),
                 ]):
                result = coop_watchdog.check_job(context)
        finally:
            coop_watchdog.DBSession = original_session
            engine.dispose()

        self.assertEqual(len(context.bot.sent), 1)
        self.assertIn('Daheim', context.bot.sent[0][1])
        self.assertEqual(result['alerts'], 1)

    def test_staying_empty_across_scans_does_not_alert_again(self):
        engine, test_session = self._fresh_session()
        now = datetime.utcnow()
        session = test_session()
        session.add(CoopWatchdogStatus(key='gewoba', was_empty=True, last_checked_at=now, last_status='ok'))
        session.add(CoopWatchdogStatus(key='wbg1903', was_empty=True, last_checked_at=now, last_status='ok'))
        session.add(CoopWatchdogStatus(key='wbg_daheim', was_empty=True, last_checked_at=now, last_status='ok'))
        session.commit()
        session.close()

        context = SimpleNamespace(bot=FakeBot())
        original_session = coop_watchdog.DBSession
        coop_watchdog.DBSession = test_session
        try:
            with mock.patch.object(coop_watchdog, 'ADMIN_ID', 312029534), \
                 mock.patch('requests.get', side_effect=[
                     FakeResponse(GEWOBA_EMPTY), FakeResponse(WBG_EMPTY), FakeResponse(DAHEIM_EMPTY),
                 ]):
                result = coop_watchdog.check_job(context)
        finally:
            coop_watchdog.DBSession = original_session
            engine.dispose()

        self.assertEqual(len(context.bot.sent), 0)
        self.assertEqual(result['alerts'], 0)

    def test_fetch_failure_for_one_site_does_not_block_the_others(self):
        engine, test_session = self._fresh_session()
        now = datetime.utcnow()
        session = test_session()
        session.add(CoopWatchdogStatus(key='gewoba', was_empty=True, last_checked_at=now, last_status='ok'))
        session.add(CoopWatchdogStatus(key='wbg1903', was_empty=True, last_checked_at=now, last_status='ok'))
        session.add(CoopWatchdogStatus(key='wbg_daheim', was_empty=True, last_checked_at=now, last_status='ok'))
        session.commit()
        session.close()

        context = SimpleNamespace(bot=FakeBot())
        original_session = coop_watchdog.DBSession
        coop_watchdog.DBSession = test_session
        try:
            with mock.patch.object(coop_watchdog, 'ADMIN_ID', 312029534), \
                 mock.patch('requests.get', side_effect=[
                     RuntimeError('timeout'), FakeResponse(WBG_EMPTY), FakeResponse(DAHEIM_EMPTY),
                 ]):
                result = coop_watchdog.check_job(context)

            self.assertEqual(result['ok'], 1)
            session = test_session()
            gewoba_row = session.query(CoopWatchdogStatus).filter(CoopWatchdogStatus.key == 'gewoba').first()
            wbg_row = session.query(CoopWatchdogStatus).filter(CoopWatchdogStatus.key == 'wbg1903').first()
            daheim_row = session.query(CoopWatchdogStatus).filter(CoopWatchdogStatus.key == 'wbg_daheim').first()
            session.close()

            self.assertEqual(gewoba_row.last_status, 'error')
            self.assertEqual(wbg_row.last_status, 'ok')
            self.assertEqual(daheim_row.last_status, 'ok')
        finally:
            coop_watchdog.DBSession = original_session
            engine.dispose()


if __name__ == '__main__':
    unittest.main()
