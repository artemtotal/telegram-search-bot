"""Журнал кожної перевірки ДП Документ і звіт тестових тижнів.

Щоб вибрати розклад перевірок, треба бачити, коли форма запису з'являється
після «всі місця зайняті» і як часто перевірка впирається в Cloudflare, - а
останній статус і список знахідок цього не показують.
"""

import unittest
from datetime import datetime, timedelta
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import database
from database import Base, EqueueCheckLog, EqueueSubscription


class EqueueCheckLogTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://')
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        import user_handlers.equeue_monitor as monitor

        self.monitor = monitor
        self.patches = [
            mock.patch.object(database, 'DBSession', self.Session),
            mock.patch.object(monitor, 'DBSession', self.Session),
            mock.patch.object(monitor, 'ADMIN_ID', 312029534),
            mock.patch.object(monitor.i18n, 'get_lang', lambda _user_id: 'ru'),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in self.patches:
            patch.stop()

    def _payload(self, status, text='Наразі всі місця зайняті'):
        return {
            'source': self.monitor.SERVICE_KEY,
            'status': status,
            'available': status == 'available',
            'reason': 'test',
            'text_sample': text,
        }

    def _subscribe(self, user_id, **fields):
        session = self.Session()
        now = datetime.utcnow()
        values = dict(
            user_id=user_id, service=self.monitor.SERVICE_KEY, active=True,
            last_status='none', created_at=now, updated_at=now,
        )
        values.update(fields)
        session.add(EqueueSubscription(**values))
        session.commit()
        session.close()

    def _rows(self):
        session = self.Session()
        try:
            return session.query(EqueueCheckLog).order_by(EqueueCheckLog.id).all()
        finally:
            session.close()

    def _log(self, minutes_ago, status):
        session = self.Session()
        session.add(EqueueCheckLog(
            service=self.monitor.SERVICE_KEY,
            checked_at=datetime.utcnow() - timedelta(minutes=minutes_ago),
            status=status,
        ))
        session.commit()
        session.close()

    def test_every_check_is_logged_with_how_many_people_were_notified(self):
        self._subscribe(1)
        self._subscribe(2)
        bot = mock.Mock()

        self.monitor.handle_browser_result(bot, self._payload('none'))
        response = self.monitor.handle_browser_result(bot, self._payload('available', 'Номер телефону'))
        self.monitor.handle_browser_result(bot, self._payload('blocked', 'Just a moment'))

        rows = self._rows()
        self.assertEqual([row.status for row in rows], ['none', 'available', 'blocked'])
        self.assertEqual([row.subscribers for row in rows], [2, 2, 2])
        self.assertEqual([row.notified for row in rows], [0, 2, 0])
        self.assertNotIn('sent', response)

    def test_page_text_is_kept_only_when_it_changes(self):
        bot = mock.Mock()
        self.monitor.handle_browser_result(bot, self._payload('none', 'Наразі всі місця зайняті'))
        self.monitor.handle_browser_result(bot, self._payload('none', 'Наразі  всі місця\nзайняті'))
        self.monitor.handle_browser_result(bot, self._payload('available', 'Номер телефону'))

        texts = [row.page_text for row in self._rows()]
        self.assertEqual(texts, ['Наразі всі місця зайняті', None, 'Номер телефону'])

    def test_cloudflare_between_two_open_forms_is_not_a_new_appearance(self):
        self._log(100, 'none')
        self._log(85, 'available')
        self._log(70, 'blocked')
        self._log(55, 'available')
        self._log(40, 'none')
        self._log(25, 'available')

        session = self.Session()
        rows = session.query(EqueueCheckLog).order_by(EqueueCheckLog.checked_at).all()
        session.close()
        found = self.monitor._appearances(rows, None)

        self.assertEqual(len(found), 2)
        self.assertEqual(int((found[0][1] - found[0][0]).total_seconds() // 60), 45)
        self.assertIsNone(found[1][1])

    def test_report_counts_checks_cloudflare_and_appearances(self):
        self._log(100, 'none')
        self._log(85, 'available')
        self._log(70, 'blocked')
        self._log(55, 'blocked')
        self._log(40, 'none')
        self._subscribe(1)
        self._subscribe(2, active=False)

        text = self.monitor.monitoring_report_text('ru')

        self.assertIn('сейчас 1, за всё время 2', text)
        self.assertIn('Мест нет: 2', text)
        self.assertIn('Cloudflare: 2 (40%)', text)
        self.assertIn('останавливал проверку 1 раз', text)
        self.assertIn('Форма записи появлялась 1 раз', text)
        self.assertIn('держалась до 45 мин', text)

    def test_unsubscribing_right_after_a_notification_is_counted(self):
        self._log(30, 'none')
        notified = datetime.utcnow() - timedelta(minutes=20)
        self._subscribe(1, active=False, last_notified_at=notified, updated_at=notified + timedelta(minutes=5))
        self._subscribe(2, active=False, last_notified_at=None, updated_at=datetime.utcnow())

        text = self.monitor.monitoring_report_text('ru')

        self.assertIn('+2 / −2; из них отписались в течение часа после уведомления: 1', text)

    def test_empty_log_says_so(self):
        self.assertIn('журнал проверок ещё пуст', self.monitor.monitoring_report_text('ru'))

    def test_report_job_sends_only_on_day_seven_and_fourteen(self):
        context = mock.Mock()
        self._log(60 * 24 * 7, 'none')
        self.monitor.report_job(context)
        self.assertEqual(context.bot.send_message.call_count, 1)

        session = self.Session()
        session.query(EqueueCheckLog).delete()
        session.commit()
        session.close()
        self._log(60 * 24 * 3, 'none')
        self.monitor.report_job(context)
        self.assertEqual(context.bot.send_message.call_count, 1)


if __name__ == '__main__':
    unittest.main()
