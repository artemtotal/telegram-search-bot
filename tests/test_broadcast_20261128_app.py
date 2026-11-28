"""tools/broadcast_20261128_app.py и user_handlers/app_news.py: получатели, тексты, скорость, RetryAfter,
блокировка, возобновление, маркер, режимы; кнопка «Не надсилати новини». Отправка подменена, сеть не нужна."""

import importlib.util
import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from telegram import InlineKeyboardMarkup
from telegram.error import BadRequest, RetryAfter, Unauthorized

import i18n
from database import Base, HousingAccessUser, HousingLeaveEvent, UserSettings
from user_handlers import app_news
from user_jobs import housing_tier, user_settings_store

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("bc_app", os.path.join(_ROOT, "tools", "broadcast_20261128_app.py"))
bc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bc)


class _Db(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        factory = sessionmaker(bind=self.engine)
        self._saved = [(m, m.DBSession) for m in (bc, housing_tier, user_settings_store)]
        for module, _ in self._saved:
            module.DBSession = factory
        self.session = factory()
        self.tmp = tempfile.mkdtemp()
        self._patches = [
            mock.patch.object(bc, 'MARKER', os.path.join(self.tmp, 'b.done')),
            mock.patch.object(bc, 'LOG', os.path.join(self.tmp, 'b.log')),
            mock.patch.object(bc, 'ADMIN_LOG', os.path.join(self.tmp, 'b.admin.log')),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self.session.close()
        for module, session in self._saved:
            module.DBSession = session
        self.engine.dispose()

    def _user(self, uid, lang='uk', news_on=True, paid=False, events=()):
        self.session.add(UserSettings(user_id=uid, language=lang, news_subscribed=news_on, updated_at=datetime.utcnow()))
        if paid:
            self.session.add(HousingAccessUser(user_id=uid, display_name='x', active=True, is_trial=False,
                                               created_at=datetime.utcnow(), updated_at=datetime.utcnow()))
        base = datetime.utcnow() - timedelta(days=5)
        for i, kind in enumerate(events):
            self.session.add(HousingLeaveEvent(user_id=uid, kind=kind, had_filters=False,
                                               created_at=base + timedelta(hours=i)))
        self.session.commit()


class PlanTests(_Db):
    def test_recipients_groups_languages_and_skips(self):
        self._user(10, 'uk')
        self._user(11, 'ru', paid=True)
        self._user(12, 'de')
        self._user(13, 'uk', news_on=False)                       # отписался
        self._user(14, 'ru', events=['blocked'])                  # заблокировал
        self._user(15, 'de', events=['blocked', 'unblocked'])     # вернулся
        self._user(16, 'uk', events=['unblocked', 'blocked'])     # последним заблокировал
        self._user(17, 'xx')                                       # язык вне набора
        plan, skipped = bc.build_plan()
        got = {uid: (grp, lang) for uid, grp, lang in plan}
        self.assertEqual(skipped, 2)
        self.assertEqual(sorted(got), [10, 11, 12, 15, 17])
        self.assertEqual(got[11], ('paid', 'ru'))
        self.assertEqual(got[10], ('free', 'uk'))
        self.assertEqual(got[15], ('free', 'de'))
        self.assertEqual(got[17][1], 'uk')

    def test_texts_by_group_and_language(self):
        for lang in ('uk', 'ru', 'de'):
            self.assertNotEqual(bc.text_for(lang, True), bc.text_for(lang, False))
            for paid in (True, False):
                text = bc.text_for(lang, paid)
                self.assertNotIn('—', text)
                self.assertNotIn('–', text)
                self.assertLess(len(text), 4096)
        self.assertTrue(bc.text_for('uk', False).startswith('Привіт! У Потсдамбота'))
        self.assertIn('Відкрити в Потсдамботі', bc.text_for('uk', True))
        self.assertIn('Hallo!', bc.text_for('de', False))
        self.assertEqual(bc.text_for('en', False), bc.TEXT_ALL['uk'])


class KeyboardTests(unittest.TestCase):
    def test_two_buttons(self):
        for lang in ('uk', 'ru', 'de'):
            markup = bc.keyboard(lang)
            flat = [b for row in markup.inline_keyboard for b in row]
            self.assertEqual(len(flat), 2)
            self.assertEqual(flat[1].callback_data, 'appnews:off')
            self.assertTrue(flat[0].web_app is not None or flat[0].url)
            self.assertNotIn('[[', flat[0].text + flat[1].text)


class RunTests(_Db):
    def _send(self, behavior=None):
        calls = []

        def send(uid, text, markup):
            calls.append((uid, text, markup))
            if behavior:
                behavior(uid, len([c for c in calls if c[0] == uid]))
        return send, calls

    def test_pace_one_per_second_and_marker_at_end(self):
        plan = [(1, 'free', 'uk'), (2, 'paid', 'ru'), (3, 'free', 'de')]
        send, calls = self._send()
        sleeps = []
        counts = bc.run(plan, send, sleeps.append)
        self.assertEqual(counts['ok'], 3)
        self.assertEqual(sleeps, [1.0, 1.0, 1.0])
        self.assertEqual([c[0] for c in calls], [1, 2, 3])
        self.assertEqual(calls[1][1], bc.TEXT_PAID['ru'])
        self.assertEqual(calls[2][1], bc.TEXT_ALL['de'])
        self.assertTrue(os.path.exists(bc.MARKER))
        self.assertEqual(set(bc.read_journal(bc.LOG).values()), {'ok'})

    def test_retry_after_waits_and_retries(self):
        def behavior(uid, n):
            if n == 1:
                raise RetryAfter(7)
        send, calls = self._send(behavior)
        sleeps = []
        counts = bc.run([(1, 'free', 'uk')], send, sleeps.append)
        self.assertEqual(counts['ok'], 1)
        self.assertEqual(len(calls), 2)
        self.assertEqual(sleeps[0], 8.0)

    def test_blocked_logged_not_retried_and_skipped_on_rerun(self):
        def behavior(uid, n):
            if uid == 1:
                raise Unauthorized('Forbidden: bot was blocked by the user')
        send, calls = self._send(behavior)
        counts = bc.run([(1, 'free', 'uk'), (2, 'free', 'uk')], send, lambda s: None)
        self.assertEqual((counts['blocked'], counts['ok']), (1, 1))
        self.assertEqual([c[0] for c in calls], [1, 2])
        self.assertEqual(bc.read_journal(bc.LOG), {1: 'blocked', 2: 'ok'})
        send2, calls2 = self._send()
        counts2 = bc.run([(1, 'free', 'uk'), (2, 'free', 'uk')], send2, lambda s: None)
        self.assertEqual(calls2, [])
        self.assertEqual(counts2['skipped'], 2)

    def test_other_error_logged_run_continues_and_is_retried_next_time(self):
        def behavior(uid, n):
            if uid == 1:
                raise BadRequest('chat not found')
        send, calls = self._send(behavior)
        counts = bc.run([(1, 'free', 'uk'), (2, 'free', 'uk')], send, lambda s: None)
        self.assertEqual((counts['error'], counts['ok']), (1, 1))
        send2, calls2 = self._send()
        bc.run([(1, 'free', 'uk'), (2, 'free', 'uk')], send2, lambda s: None)
        self.assertEqual([c[0] for c in calls2], [1])

    def test_resume_skips_already_sent(self):
        bc.journal(bc.LOG, 1, 'free', 'uk', 'ok')
        send, calls = self._send()
        counts = bc.run([(1, 'free', 'uk'), (2, 'free', 'uk')], send, lambda s: None)
        self.assertEqual([c[0] for c in calls], [2])
        self.assertEqual(counts['skipped'], 1)

    def test_retry_after_forever_gives_up_with_error(self):
        def behavior(uid, n):
            raise RetryAfter(1)
        send, calls = self._send(behavior)
        counts = bc.run([(1, 'free', 'uk')], send, lambda s: None)
        self.assertEqual(counts['error'], 1)
        self.assertEqual(len(calls), bc.MAX_RETRY_AFTER_ATTEMPTS)


class MainTests(_Db):
    def _main(self, argv, factory=None):
        out = io.StringIO()
        with redirect_stdout(out), mock.patch.object(bc.time, 'sleep', lambda s: None):
            code = bc.main(argv, send_factory=factory or (lambda: self._boom))
        return code, out.getvalue()

    @staticmethod
    def _boom(*a, **k):
        raise AssertionError('must not send')

    def test_needs_exactly_one_mode(self):
        with self.assertRaises(SystemExit):
            self._main([])
        with self.assertRaises(SystemExit):
            self._main(['--dry', '--send'])

    def test_dry_counts_and_sends_nothing(self):
        self._user(10, 'uk')
        self._user(11, 'ru', paid=True)
        self._user(12, 'de', events=['blocked'])
        code, out = self._main(['--dry'])
        self.assertEqual(code, 0)
        self.assertIn("'recipients': 2", out)
        self.assertIn("'skipped_blocked': 1", out)
        self.assertFalse(os.path.exists(bc.MARKER))
        self.assertFalse(os.path.exists(bc.LOG))

    def test_send_refuses_when_marker_exists(self):
        self._user(10)
        open(bc.MARKER, 'w').write('x')
        code, _ = self._main(['--send'])
        self.assertEqual(code, 3)

    def test_send_runs_and_sets_marker(self):
        self._user(10, 'uk')
        self._user(11, 'ru', paid=True)
        calls = []
        with mock.patch.object(bc, 'PAUSE_SECONDS', 0):
            code, _ = self._main(['--send'], lambda: (lambda uid, text, markup: calls.append((uid, text))))
        self.assertEqual(code, 0)
        self.assertEqual([c[0] for c in calls], [10, 11])
        self.assertTrue(os.path.exists(bc.MARKER))
        code2, _ = self._main(['--send'])
        self.assertEqual(code2, 3)

    def test_only_admin_sends_both_texts_to_admin_only(self):
        self._user(10, 'uk')
        self._user(99, 'ru')
        calls = []
        with mock.patch.dict(os.environ, {'ADMIN_ID': '99'}), mock.patch.object(bc, 'PAUSE_SECONDS', 0):
            code, _ = self._main(['--only-admin'], lambda: (lambda uid, text, markup: calls.append((uid, text))))
        self.assertEqual(code, 0)
        self.assertEqual(calls, [(99, bc.TEXT_ALL['ru']), (99, bc.TEXT_PAID['ru'])])
        self.assertFalse(os.path.exists(bc.MARKER))
        self.assertFalse(os.path.exists(bc.LOG))


class OffButtonTests(_Db):
    def _update(self, uid, markup):
        query = mock.Mock()
        query.message.reply_markup = markup
        update = mock.Mock()
        update.callback_query = query
        update.effective_user.id = uid
        return update, query

    def test_unsubscribes_and_confirms_in_language(self):
        self._user(10, 'de')
        update, query = self._update(10, bc.keyboard('de'))
        app_news.handle_off(update, mock.Mock())
        self.assertFalse(user_settings_store.get_news_subscribed(10))
        text = query.answer.call_args[0][0]
        self.assertEqual(text, i18n.t('appnews.off.done', 'de'))
        self.assertIn('Benachrichtigungen', text)
        sent_markup = query.edit_message_reply_markup.call_args[0][0]
        self.assertIsInstance(sent_markup, InlineKeyboardMarkup)
        flat = [b for row in sent_markup.inline_keyboard for b in row]
        self.assertEqual(len(flat), 1)
        self.assertNotEqual(flat[0].callback_data, 'appnews:off')

    def test_dropped_from_future_recipients(self):
        self._user(10, 'uk')
        update, _ = self._update(10, bc.keyboard('uk'))
        app_news.handle_off(update, mock.Mock())
        plan, _ = bc.build_plan()
        self.assertEqual(plan, [])

    def test_all_languages_have_keys(self):
        for lang in ('uk', 'ru', 'de'):
            for key in ('appnews.btn.open', 'appnews.btn.off', 'appnews.off.done', 'appnews.off.failed'):
                self.assertNotIn('[[', i18n.t(key, lang))
        self.assertNotEqual(i18n.t('appnews.off.done', 'ru'), i18n.t('appnews.off.done', 'uk'))

    def test_handler_pattern(self):
        self.assertTrue(app_news.callback_handler.pattern.match('appnews:off'))
        self.assertFalse(app_news.callback_handler.pattern.match('housing:menu'))


if __name__ == '__main__':
    unittest.main()
