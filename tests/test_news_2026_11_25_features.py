"""tools/news_2026_11_25_features.py: paragraph switches, recipients, dry run by default, no repeat."""

import importlib.util
import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database import Base, HousingAccessUser, HousingSearch, HousingSendLog, HousingUserJourney, UserSettings
from user_jobs import housing_tier, user_settings_store

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("news_features", os.path.join(_ROOT, "tools", "news_2026_11_25_features.py"))
news = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(news)


class _Db(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        factory = sessionmaker(bind=self.engine)
        self._saved = [(m, m.DBSession) for m in (news, housing_tier, user_settings_store)]
        for module, _ in self._saved:
            module.DBSession = factory
        self.session = factory()
        self._tmp = tempfile.mkdtemp()
        self._done = mock.patch.object(news, 'DONE_FILE', os.path.join(self._tmp, 'news.done'))
        self._done.start()
        self._exclude = mock.patch.object(news, 'EXCLUDE', {1})
        self._exclude.start()

    def tearDown(self):
        self._done.stop()
        self._exclude.stop()
        self.session.close()
        for module, session in self._saved:
            module.DBSession = session
        self.engine.dispose()

    def _user(self, uid, lang='uk', news_on=True, search=False, journey=False, paid=False, sent=False):
        self.session.add(UserSettings(user_id=uid, language=lang, news_subscribed=news_on, updated_at=datetime.utcnow()))
        if search:
            self.session.add(HousingSearch(user_id=uid, criteria='{}', members='[]', coops='[]', created_at=datetime.utcnow()))
        if journey:
            self.session.add(HousingUserJourney(user_id=uid, first_menu_at=datetime.utcnow()))
        if paid:
            self.session.add(HousingAccessUser(user_id=uid, display_name='x', active=True, is_trial=False,
                                               created_at=datetime.utcnow(), updated_at=datetime.utcnow()))
        if sent:
            self.session.add(HousingSendLog(user_id=uid, source='s', listing_key='k', delayed=False, sent_at=datetime.utcnow()))
        self.session.commit()


class TextTests(unittest.TestCase):
    def test_only_the_chosen_paragraphs_appear_in_a_fixed_order(self):
        parts = news.parse_parts('mappe,price')
        self.assertEqual(parts, ['price', 'mappe'])
        text = news.compose('uk', parts, paid=False)
        self.assertIn('5 €', text)
        self.assertIn('12 €', text)
        self.assertIn('15 €', text)
        self.assertIn('Папка заявника', text)
        self.assertNotIn('Запроси друга', text)
        self.assertNotIn('Бранденбург', text)
        self.assertNotIn('вже все працює', text)

    def test_the_paid_line_only_for_subscribers(self):
        self.assertIn('У вас уже все працює', news.compose('uk', ['price'], paid=True))
        self.assertNotIn('У вас уже все працює', news.compose('uk', ['price'], paid=False))

    def test_unknown_paragraph_is_an_error(self):
        with self.assertRaises(ValueError):
            news.parse_parts('price,nonsense')

    def test_all_languages_have_every_paragraph_and_fit_in_one_message(self):
        for name in news.ORDER:
            self.assertEqual(set(news.PARTS[name]), {'uk', 'ru', 'de'})
        for lang in ('uk', 'ru', 'de'):
            text = news.compose(lang, news.ORDER, paid=True)
            self.assertLess(len(text), news.TELEGRAM_LIMIT)
            self.assertNotIn('{', text)
        self.assertIn('Bewerbungsmappe', news.compose('de', ['mappe'], False))
        self.assertIn('Папка заявителя', news.compose('ru', ['mappe'], False))

    def test_unknown_language_falls_back_to_ukrainian(self):
        self.assertIn('Пошук житла', news.compose('en', ['price'], False))


class RecipientTests(_Db):
    def test_housing_users_who_did_not_opt_out(self):
        self._user(10, search=True)
        self._user(11, journey=True)
        self._user(12, paid=True)
        self._user(13, sent=True)
        self._user(14)                       # never used housing
        self._user(15, search=True, news_on=False)  # unsubscribed
        self._user(1, search=True)           # excluded
        self.assertEqual(news.recipients(), [10, 11, 12, 13])


class MainTests(_Db):
    def _run(self, argv):
        out = io.StringIO()
        with redirect_stdout(out), mock.patch.dict(os.environ, {'BOT_TOKEN': 'x'}):
            try:
                code = news.main(argv)
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue()

    def setUp(self):
        super().setUp()
        self._user(10, 'uk', search=True)
        self._user(11, 'ru', search=True, paid=True)
        self._user(12, 'de', journey=True)

    def test_dry_run_counts_by_language_and_sends_nothing(self):
        with mock.patch('telegram.Bot') as bot:
            code, out = self._run(['--with', 'price,mappe'])
        self.assertEqual(code, 0)
        self.assertIn("'recipients': 3", out)
        self.assertIn("'uk': {'free': 1, 'paid': 0}", out)
        self.assertIn("'ru': {'free': 0, 'paid': 1}", out)
        self.assertIn('сухий прогін', out)
        bot.assert_not_called()
        self.assertFalse(os.path.exists(news.DONE_FILE))

    def test_send_needs_an_explicit_with(self):
        with mock.patch('telegram.Bot') as bot:
            code, _ = self._run(['--send'])
        self.assertEqual(code, 2)
        bot.assert_not_called()

    def test_send_goes_out_in_each_language_once_and_a_second_run_refuses(self):
        with mock.patch('telegram.Bot') as bot, mock.patch.object(news, 'SEND_PAUSE_SECONDS', 0):
            send = bot.return_value.send_message
            code, out = self._run(['--with', 'price,mappe', '--send'])
            self.assertEqual(code, 0)
            self.assertEqual(send.call_count, 3)
            by_user = {call.kwargs['chat_id']: call.kwargs for call in send.call_args_list}
            self.assertIn('Папка заявника', by_user[10]['text'])
            self.assertNotIn('Папка заявника', by_user[11]['text'].replace('Папка заявителя', ''))
            self.assertIn('Bewerbungsmappe', by_user[12]['text'])
            free_buttons = [b.callback_data for row in by_user[10]['reply_markup'].inline_keyboard for b in row]
            paid_buttons = [b.callback_data for row in by_user[11]['reply_markup'].inline_keyboard for b in row]
            self.assertEqual(free_buttons, ['housing:access_request', 'housing:menu'])
            self.assertEqual(paid_buttons, ['housing:menu'])
            self.assertIn('У вас уже всё работает', by_user[11]['text'])
            self.assertNotIn('уже всё работает', by_user[12]['text'])
            self.assertTrue(os.path.exists(news.DONE_FILE))
            with open(news.DONE_FILE + '.log', encoding='utf-8') as handle:
                self.assertEqual(len(handle.readlines()), 3)
            code, out = self._run(['--with', 'price', '--send'])
            self.assertEqual(code, 3)
            self.assertEqual(send.call_count, 3)

    def test_an_interrupted_run_continues_without_duplicates(self):
        with open(news.DONE_FILE + '.sent', 'w', encoding='utf-8') as handle:
            handle.write('10\n11\n')
        with mock.patch('telegram.Bot') as bot, mock.patch.object(news, 'SEND_PAUSE_SECONDS', 0):
            code, _ = self._run(['--with', 'price', '--send'])
            self.assertEqual([call.kwargs['chat_id'] for call in bot.return_value.send_message.call_args_list], [12])

    def test_a_failure_for_one_person_does_not_stop_the_rest(self):
        with mock.patch('telegram.Bot') as bot, mock.patch.object(news, 'SEND_PAUSE_SECONDS', 0):
            send = bot.return_value.send_message
            send.side_effect = [RuntimeError('blocked'), None, None]
            code, out = self._run(['--with', 'price', '--send'])
            self.assertEqual(send.call_count, 3)
            self.assertIn("'sent': 2, 'failed': 1", out)


if __name__ == '__main__':
    unittest.main()
