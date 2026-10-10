"""Кнопка «Я пройшов перевірку» в сповіщенні про Cloudflare.

Бот не бачить, коли адмін пройшов капчу, тож після натискання розширення
робить позачергову перевірку, а бот показує, що саме воно побачило: Cloudflare
досі не пускає чи сторінка відкрилась, але прочитана неправильно.
"""

import unittest
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import database
from database import Base

ADMIN = 312029534


class EqueueManualCheckTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://')
        Base.metadata.create_all(self.engine)
        Session = sessionmaker(bind=self.engine)
        import user_handlers.equeue_monitor as monitor

        self.monitor = monitor
        self.patches = [
            mock.patch.object(database, 'DBSession', Session),
            mock.patch.object(monitor, 'DBSession', Session),
            mock.patch.object(monitor, 'ADMIN_ID', ADMIN),
            mock.patch.object(monitor.i18n, 'get_lang', lambda _user_id: 'uk'),
            mock.patch.dict(monitor._pending_manual_checks, clear=True),
        ]
        for patch in self.patches:
            patch.start()
        self.bot = mock.Mock()

    def tearDown(self):
        for patch in self.patches:
            patch.stop()

    def _payload(self, status, **extra):
        payload = {
            'source': 'dp_document_berlin',
            'status': status,
            'reason': 'причина',
            'title': 'Заголовок',
            'text_sample': 'Наразі всі місця зайняті <b>',
        }
        payload.update(extra)
        return payload

    def test_blocked_alert_has_done_button(self):
        self.monitor.handle_browser_result(self.bot, self._payload('blocked'))
        _args, kwargs = self.bot.send_message.call_args
        button = kwargs['reply_markup'].inline_keyboard[0][0]
        self.assertEqual(button.callback_data, 'equeue:cf_done')

    def test_button_sends_request_to_receiver(self):
        query = mock.Mock()
        query.message.chat_id = ADMIN
        context = mock.Mock()
        with mock.patch.object(self.monitor.requests, 'post') as post:
            post.return_value.ok = True
            self.monitor._request_manual_check(query, context, 'uk')
        url = post.call_args[0][0]
        self.assertTrue(url.endswith('/api/dp-document/check-request'))
        request_id = post.call_args[1]['json']['request_id']
        self.assertEqual(self.monitor._pending_manual_checks[request_id], ADMIN)
        context.job_queue.run_once.assert_called_once()

    def test_receiver_down_keeps_nothing_pending(self):
        query = mock.Mock()
        with mock.patch.object(self.monitor.requests, 'post', side_effect=self.monitor.requests.ConnectionError()):
            self.monitor._request_manual_check(query, mock.Mock(), 'uk')
        self.assertEqual(self.monitor._pending_manual_checks, {})
        self.assertTrue(query.answer.call_args[1].get('show_alert'))

    def test_answer_reports_what_extension_saw_instead_of_alert(self):
        self.monitor._pending_manual_checks['abc'] = ADMIN
        self.monitor.handle_browser_result(self.bot, self._payload('blocked', request_id='abc'))
        self.assertEqual(self.bot.send_message.call_count, 1)
        args, kwargs = self.bot.send_message.call_args
        self.assertEqual(args[0], ADMIN)
        self.assertIn('Позачергова перевірка', args[1])
        self.assertIn('&lt;b&gt;', args[1])
        self.assertIsNotNone(kwargs['reply_markup'])
        self.assertEqual(self.monitor._pending_manual_checks, {})

    def test_successful_answer_has_no_button(self):
        self.monitor._pending_manual_checks['abc'] = ADMIN
        self.monitor.handle_browser_result(self.bot, self._payload('none', request_id='abc'))
        report = [c for c in self.bot.send_message.call_args_list if 'Позачергова' in c[0][1]]
        self.assertEqual(len(report), 1)
        self.assertIsNone(report[0][1]['reply_markup'])

    def test_timeout_only_when_no_answer(self):
        context = mock.Mock()
        context.job.context = 'abc'
        self.monitor._manual_check_timeout(context)
        context.bot.send_message.assert_not_called()
        self.monitor._pending_manual_checks['abc'] = ADMIN
        self.monitor._manual_check_timeout(context)
        context.bot.send_message.assert_called_once()


if __name__ == '__main__':
    unittest.main()
