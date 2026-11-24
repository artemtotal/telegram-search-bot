"""«📁 Папка заявника» (25.11.2026): questionnaire, German PDF, subscription lock."""

import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database import Base, HousingAccessUser, HousingMappeLog, KleinanzeigenListing
from user_handlers import housing_letter, housing_mappe, housing_monitor
from user_jobs import housing_letter_store, housing_mappe_pdf, housing_mappe_store, housing_tier, user_settings_store


class _SyncThread:
    def __init__(self, target, **_):
        self.target = target

    def start(self):
        self.target()


class _Db(unittest.TestCase):
    MODULES = (housing_letter_store, housing_mappe_store, housing_mappe, housing_letter, housing_tier,
               user_settings_store, housing_monitor)

    def setUp(self):
        self.engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self._saved = [(m, m.DBSession) for m in self.MODULES if hasattr(m, 'DBSession')]
        for module, _ in self._saved:
            module.DBSession = sessionmaker(bind=self.engine)
        housing_mappe._busy.clear()

    def tearDown(self):
        for module, session in self._saved:
            module.DBSession = session
        self.engine.dispose()

    def _session(self):
        return sessionmaker(bind=self.engine)()

    def _subscribe(self, user_id=777):
        session = self._session()
        session.add(HousingAccessUser(
            user_id=user_id, display_name='x', active=True, is_trial=False,
            created_at=datetime.utcnow(), updated_at=datetime.utcnow(),
        ))
        session.commit()
        session.close()


class HelperTests(unittest.TestCase):
    def test_income_is_formatted_only_when_it_is_a_plain_amount(self):
        self.assertEqual(housing_mappe.format_income('1800'), '1.800 € netto')
        self.assertEqual(housing_mappe.format_income('2 400 €'), '2.400 € netto')
        self.assertEqual(housing_mappe.format_income('1850,50'), '1.850,50 € netto')
        self.assertEqual(housing_mappe.format_income('близько двох тисяч'), 'близько двох тисяч')
        self.assertEqual(housing_mappe.format_income(''), '')

    def test_questions_follow_the_answers(self):
        letter = {'full_name': 'Іван Петренко', 'move_in': 'з 01.12', 'contact': ''}
        self.assertEqual(housing_mappe.next_field({}, letter), 'birth_date')  # the name is already known
        self.assertEqual(housing_mappe.next_field({}, {}), 'full_name')
        # forced re-edit asks even what «✍️ Листи» knows
        self.assertEqual(housing_mappe.next_field({}, letter, force=True), 'full_name')
        base = {'full_name': '', 'birth_date': '', 'address': '', 'move_in': '', 'contact': ''}
        self.assertEqual(housing_mappe.next_field(dict(base), {}), 'occupation')
        self.assertEqual(housing_mappe.next_field({**base, 'occupation': 'works'}, {}), 'employer')
        self.assertEqual(housing_mappe.next_field({**base, 'occupation': 'works', 'employer': 'x'}, {}), 'net_income')
        jobcenter = {**base, 'occupation': 'jobcenter'}
        self.assertEqual(housing_mappe.next_field(jobcenter, {}), 'adults')  # nothing to ask about work
        done = {**jobcenter, 'adults': '2', 'children': '1', 'pets': 'yes'}
        self.assertEqual(housing_mappe.next_field(done, {}), 'pet_kind')
        done.update(pet_kind='cat', smoking='no', debts='no', insolvency='no')
        self.assertIsNone(housing_mappe.next_field(done, {}))

    def test_a_skipped_question_counts_as_asked(self):
        answers = {'full_name': '', 'birth_date': ''}
        self.assertEqual(housing_mappe.next_field(answers, {}), 'address')

    def test_merge_prefers_the_answer_here_then_the_letter_profile(self):
        values = housing_mappe.merged({'contact': ''}, {'full_name': 'А Б', 'contact': '+49 1', 'move_in': 'зараз'})
        self.assertEqual(values['full_name'], 'А Б')
        self.assertEqual(values['move_in'], 'зараз')
        self.assertEqual(values['contact'], '')  # skipped here on purpose

    def test_documents_depend_on_what_the_person_does(self):
        works = housing_mappe_pdf.documents_list('works')
        self.assertIn('Gehaltsabrechnungen der letzten 3 Monate', works)
        self.assertFalse(any('Jobcenter' in doc for doc in works))
        jobcenter = housing_mappe_pdf.documents_list('jobcenter')
        self.assertIn('Mietübernahmebescheinigung des Jobcenters', jobcenter)
        for docs in (works, jobcenter, housing_mappe_pdf.documents_list('')):
            self.assertTrue(any('SCHUFA' in doc for doc in docs))
            self.assertTrue(any('Mietschuldenfreiheit' in doc for doc in docs))
            self.assertTrue(any('Ausweis' in doc for doc in docs))

    def test_persons_line_never_invents_numbers(self):
        self.assertEqual(housing_mappe_pdf.persons_line('', ''), '')
        self.assertEqual(housing_mappe_pdf.persons_line('2', '0'), '2 Erwachsene, keine Kinder')
        self.assertEqual(housing_mappe_pdf.persons_line('1', '1'), '1 Erwachsener, 1 Kind')
        self.assertEqual(housing_mappe_pdf.persons_line('', '2'), '2 Kinder')

    def test_translation_falls_back_to_the_original_words(self):
        with mock.patch.object(housing_letter, '_call_ai', side_effect=RuntimeError('down')):
            self.assertEqual(housing_mappe.translate({'employer': 'логіст', 'pet_kind': ''}), {'employer': 'логіст'})
        with mock.patch.object(housing_letter, '_call_ai', return_value='не json'):
            self.assertEqual(housing_mappe.translate({'employer': 'логіст'}), {'employer': 'логіст'})
        with mock.patch.object(housing_letter, '_call_ai', return_value='```json\n{"employer": "Logistiker"}\n```'):
            self.assertEqual(housing_mappe.translate({'employer': 'логіст'}), {'employer': 'Logistiker'})
        with mock.patch.object(housing_letter, '_call_ai') as ai:
            self.assertEqual(housing_mappe.translate({'employer': ''}), {})
            ai.assert_not_called()

    def test_letter_profile_only_carries_given_facts(self):
        values = housing_mappe.merged({'occupation': 'jobcenter', 'adults': '2', 'children': '1', 'smoking': 'no',
                                       'pets': 'no'}, {})
        profile = housing_mappe.letter_profile(values, {'about': 'німецька B1'})
        self.assertEqual(profile['household'], '2 Erwachsene, 1 Kind')
        self.assertIn('Jobcenter', profile['income'])
        self.assertEqual(profile['about'], 'німецька B1, Nichtraucher, keine Haustiere')
        empty = housing_mappe.letter_profile(housing_mappe.merged({}, {}), {})
        self.assertEqual(set(empty.values()), {''})


@unittest.skipUnless(housing_mappe_pdf.available(), 'fpdf2 is not installed')
class PdfTests(unittest.TestCase):
    def _data(self, **extra):
        data = {
            'name': 'Олена Коваленко', 'birth_date': '15.03.1988', 'address': 'вул. Шевченка 1, Potsdam',
            'contact': '+49 170 0000000', 'move_in': 'ab 01.12.2026', 'occupation': 'works',
            'employer': 'Logistik GmbH, Logistikerin', 'net_income': '2.400 € netto', 'adults': '1',
            'children': '2', 'pets': 'no', 'pet_kind': '', 'smoking': 'no', 'debts': 'no', 'insolvency': 'no',
            'letter': 'Sehr geehrte Damen und Herren,\n\nich bewerbe mich um die Wohnung.\n\nMit freundlichen Grüßen\nÖlena',
            'listing': {'title': '3-Zimmer-Wohnung', 'address': 'Babelsberg'},
            'docs': housing_mappe_pdf.documents_list('works'), 'today': '25.11.2026',
        }
        data.update(extra)
        return data

    def test_a_full_pdf_is_built(self):
        pdf = housing_mappe_pdf.build_pdf(self._data())
        self.assertTrue(pdf.startswith(b'%PDF'))
        self.assertGreater(len(pdf), 5000)

    def test_an_empty_questionnaire_still_gives_a_pdf_with_blank_lines(self):
        pdf = housing_mappe_pdf.build_pdf({'letter': '', 'today': '25.11.2026'})
        self.assertTrue(pdf.startswith(b'%PDF'))

    def test_jobcenter_and_pets_variants(self):
        pdf = housing_mappe_pdf.build_pdf(self._data(
            occupation='jobcenter', employer='', net_income='', pets='yes', pet_kind='Katze', listing=None,
            docs=housing_mappe_pdf.documents_list('jobcenter'),
        ))
        self.assertTrue(pdf.startswith(b'%PDF'))

    def test_a_very_long_letter_runs_onto_more_pages(self):
        short = housing_mappe_pdf.build_pdf(self._data())
        long = housing_mappe_pdf.build_pdf(self._data(letter='Zeile mit Text. ' * 700))
        self.assertGreater(len(long), len(short))


class FlowTests(_Db):
    def _ctx(self):
        bot = mock.Mock()
        bot.send_message.return_value = SimpleNamespace(message_id=5)
        return bot, SimpleNamespace(bot=bot, user_data={})

    def _tap(self, context, data, user_id=777):
        query = SimpleNamespace(data=data, answer=mock.Mock())
        update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=user_id))
        housing_mappe.handle_callback(update, context)
        return query

    def _text(self, context, text, user_id=777):
        update = SimpleNamespace(message=SimpleNamespace(text=text), effective_user=SimpleNamespace(id=user_id))
        return housing_mappe.handle_text(update, context)

    def _texts(self, bot):
        return [call.kwargs.get('text') or '' for call in bot.send_message.call_args_list]

    def test_a_free_person_sees_the_lock_and_the_subscription_button(self):
        bot, context = self._ctx()
        self._tap(context, 'housing:mappe:menu')
        [call] = bot.send_message.call_args_list
        self.assertIn('функція підписки', call.kwargs['text'])
        callbacks = [b.callback_data for row in call.kwargs['reply_markup'].inline_keyboard for b in row]
        self.assertIn('housing:access_request', callbacks)
        query = self._tap(context, 'housing:mappe:go')
        self.assertTrue(query.answer.call_args.kwargs.get('show_alert'))
        self.assertEqual(housing_mappe_store.get_answers(777), None)

    def test_the_whole_flow_for_a_person_on_jobcenter(self):
        self._subscribe()
        housing_letter_store.save_profile(777, {'full_name': 'Олена К', 'household': 'x', 'income': 'y', 'move_in': 'з 01.12'})
        bot, context = self._ctx()
        pdf_calls = []
        with mock.patch.object(housing_mappe.threading, 'Thread', _SyncThread), \
                mock.patch.object(housing_letter, '_call_ai', side_effect=lambda p: (
                    '{"move_in": "ab 01.12."}' if 'JSON' in p else 'Sehr geehrte Damen und Herren,\nich bewerbe mich.')), \
                mock.patch.object(housing_mappe_pdf, 'build_pdf', side_effect=lambda d: pdf_calls.append(d) or b'%PDF-1.4 x'):
            self._tap(context, 'housing:mappe:go')
            # name and move-in come from «✍️ Листи»: the first question is the birth date
            self.assertIn('Дата народження', self._texts(bot)[-1])
            self._text(context, '15.03.1988')
            self._tap(context, 'housing:mappe:skip')            # address
            self._tap(context, 'housing:mappe:skip')            # contact
            self._tap(context, 'housing:mappe:ans:jobcenter')   # occupation: no work questions follow
            self.assertIn('дорослих', self._texts(bot)[-1])
            self._text(context, '2')
            self._tap(context, 'housing:mappe:ans:1')
            self._tap(context, 'housing:mappe:ans:no')          # pets
            self._tap(context, 'housing:mappe:ans:no')          # smoking
            self._tap(context, 'housing:mappe:skip')            # debts - skipped
            self._tap(context, 'housing:mappe:ans:no')          # insolvency
        answers = housing_mappe_store.get_answers(777)
        self.assertEqual(answers['adults'], '2')
        self.assertEqual(answers['debts'], '')
        self.assertEqual(answers['occupation'], 'jobcenter')
        self.assertNotIn('employer', answers)
        [data] = pdf_calls
        self.assertEqual(data['name'], 'Олена К')
        self.assertEqual(data['move_in'], 'ab 01.12.')
        self.assertEqual(data['birth_date'], '15.03.1988')
        self.assertEqual(data['debts'], '')
        self.assertIn('Sehr geehrte', data['letter'])
        bot.send_document.assert_called_once()
        self.assertEqual(bot.send_document.call_args.kwargs['filename'], 'Bewerbungsmappe.pdf')
        checklist = self._texts(bot)[-1]
        self.assertIn('Mietübernahmebescheinigung', checklist)
        self.assertIn('SCHUFA', checklist)
        self.assertNotIn('Gehaltsabrechnung', checklist)
        self.assertEqual(self._session().query(HousingMappeLog).filter_by(user_id=777).count(), 1)

    def test_a_second_run_asks_nothing_and_a_flat_makes_the_letter_specific(self):
        self._subscribe()
        housing_letter_store.save_profile(777, {'full_name': 'А Б', 'household': 'x', 'income': 'y', 'move_in': 'z'})
        full = {f: '' for f in housing_mappe.FIELDS}
        full.update(occupation='works', employer='Fa', net_income='2000')
        housing_mappe_store.save_answers(777, full)
        session = self._session()
        session.add(KleinanzeigenListing(
            listing_key='k1', title='Helle 3-Zimmer-Wohnung', address='Babelsberg', rooms=3, area_m2=75, price_eur=850,
            detail_url='https://x/k1', first_seen_at=datetime.utcnow(), last_seen_at=datetime.utcnow(), is_active=True))
        session.commit()
        session.close()
        ref = housing_letter_store.ref_for('kleinanzeigen', 'k1')
        bot, context = self._ctx()
        prompts, pdf_calls = [], []
        with mock.patch.object(housing_mappe.threading, 'Thread', _SyncThread), \
                mock.patch.object(housing_letter, '_call_ai', side_effect=lambda p: prompts.append(p) or 'Brief'), \
                mock.patch.object(housing_mappe_pdf, 'build_pdf', side_effect=lambda d: pdf_calls.append(d) or b'%PDF'):
            self._tap(context, f'housing:mappe:go:{ref}')
        self.assertEqual(len(pdf_calls), 1)
        self.assertEqual(pdf_calls[0]['listing']['title'], 'Helle 3-Zimmer-Wohnung')
        self.assertTrue(any('Helle 3-Zimmer-Wohnung' in p for p in prompts))
        self.assertIn('2.000 € netto', pdf_calls[0]['net_income'])
        self.assertIn('Gehaltsabrechnung', self._texts(bot)[-1])
        self.assertEqual(self._session().query(HousingMappeLog).one().listing_key, 'k1')

    def test_the_daily_limit_stops_the_eleventh_folder(self):
        self._subscribe()
        session = self._session()
        for _ in range(housing_mappe.DAILY_LIMIT):
            session.add(HousingMappeLog(user_id=777, created_at=datetime.utcnow() - timedelta(hours=3)))
        session.add(HousingMappeLog(user_id=888, created_at=datetime.utcnow()))
        session.commit()
        session.close()
        housing_mappe_store.save_answers(777, {f: '' for f in housing_mappe.FIELDS if f not in ('employer', 'net_income', 'pet_kind')})
        bot, context = self._ctx()
        with mock.patch.object(housing_mappe.threading, 'Thread', _SyncThread), \
                mock.patch.object(housing_letter, '_call_ai') as ai:
            self._tap(context, 'housing:mappe:go')
        ai.assert_not_called()
        bot.send_document.assert_not_called()
        self.assertIn(str(housing_mappe.DAILY_LIMIT), self._texts(bot)[-1])
        self.assertEqual(housing_mappe_store.count_today(888), 1)

    def test_a_failed_letter_says_so_and_sends_no_pdf(self):
        self._subscribe()
        housing_mappe_store.save_answers(777, {f: '' for f in housing_mappe.FIELDS if f not in ('employer', 'net_income', 'pet_kind')})
        bot, context = self._ctx()
        with mock.patch.object(housing_mappe.threading, 'Thread', _SyncThread), \
                mock.patch.object(housing_letter, '_call_ai', return_value=''), \
                mock.patch.object(housing_mappe_pdf, 'build_pdf', return_value=b'%PDF'):
            self._tap(context, 'housing:mappe:go')
        bot.send_document.assert_not_called()
        bot.edit_message_text.assert_called_once()
        self.assertEqual(self._session().query(HousingMappeLog).count(), 0)
        self.assertFalse(housing_mappe._busy)

    def test_delete_my_data_erases_both_questionnaires(self):
        self._subscribe()
        housing_letter_store.save_profile(777, {'full_name': 'А Б'})
        housing_mappe_store.save_answers(777, {'full_name': 'А Б'})
        housing_mappe_store.save_answers(888, {'full_name': 'Інший'})
        bot, context = self._ctx()
        self._tap(context, 'housing:mappe:del')
        self.assertEqual(housing_mappe_store.get_answers(777), {'full_name': 'А Б'})  # only asked, not yet done
        self._tap(context, 'housing:mappe:delok')
        self.assertIsNone(housing_mappe_store.get_answers(777))
        self.assertIsNone(housing_letter_store.get_profile(777))
        self.assertEqual(housing_mappe_store.get_answers(888), {'full_name': 'Інший'})

    def test_buttons_only_questions_ignore_free_text_and_numbers_are_checked(self):
        self._subscribe()
        bot, context = self._ctx()
        self._tap(context, 'housing:mappe:edit')
        for _ in range(4):
            self._tap(context, 'housing:mappe:skip')  # name, birth date, address, move-in
        self._tap(context, 'housing:mappe:skip')      # contact
        self.assertEqual(context.user_data[housing_mappe.STATE_KEY]['field'], 'occupation')
        self.assertTrue(self._text(context, 'працюю'))
        self.assertEqual(context.user_data[housing_mappe.STATE_KEY]['field'], 'occupation')
        self._tap(context, 'housing:mappe:ans:works')
        self._text(context, 'Firma')
        self._text(context, '2000')
        self.assertEqual(context.user_data[housing_mappe.STATE_KEY]['field'], 'adults')
        self._text(context, 'двоє')
        self.assertEqual(context.user_data[housing_mappe.STATE_KEY]['field'], 'adults')
        self._text(context, '0')
        self.assertEqual(context.user_data[housing_mappe.STATE_KEY]['field'], 'adults')
        self._text(context, '3')
        self.assertEqual(context.user_data[housing_mappe.STATE_KEY]['answers']['adults'], '3')

    def test_text_outside_the_questionnaire_is_not_taken(self):
        bot, context = self._ctx()
        self.assertFalse(self._text(context, 'привіт'))

    def test_off_switch(self):
        self._subscribe()
        bot, context = self._ctx()
        with mock.patch.object(housing_mappe, 'ENABLED', False):
            query = self._tap(context, 'housing:mappe:menu')
        self.assertTrue(query.answer.call_args.kwargs.get('show_alert'))
        bot.send_message.assert_not_called()

    def test_the_menu_has_the_button_and_the_letter_offers_a_folder_for_that_flat(self):
        keyboard = housing_monitor._menu_keyboard(777, 'uk')
        buttons = {b.callback_data: b.text for row in keyboard.inline_keyboard for b in row}
        # only people with access see the monitoring block; the free text is still translated
        self.assertIn('housing:faq', buttons)
        from i18n import t
        for lang in ('uk', 'ru', 'de'):
            self.assertNotIn('[[', t('housing.menu.btn.mappe', lang))
            self.assertNotIn('[[', t('housing.mappe.btn.for_flat', lang))


if __name__ == '__main__':
    unittest.main()
