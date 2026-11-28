"""Одна анкета, два хранилища (28.11.2026): «Про мене» (лист) и анкета «Папки заявника» живуть узгоджено в обидва боки."""

import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database import Base
from user_handlers import housing_letter, housing_mappe
from user_jobs import housing_letter_store as ls, housing_mappe_store as ms, housing_profile_sync as sync

U = 4242


class _Db(unittest.TestCase):
    MODULES = (ls, ms, housing_mappe, housing_letter)

    def setUp(self):
        self.engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self._saved = [(m, m.DBSession) for m in self.MODULES if hasattr(m, 'DBSession')]
        for module, _ in self._saved:
            module.DBSession = sessionmaker(bind=self.engine)

    def tearDown(self):
        for module, session in self._saved:
            module.DBSession = session
        self.engine.dispose()


class PropagateTests(unittest.TestCase):
    def test_rules(self):
        self.assertIsNone(sync.propagate('', '', 'x'))                # пустым не затираем
        self.assertEqual(sync.propagate('', 'Артём', ''), 'Артём')    # в другом пусто: заполняем
        self.assertIsNone(sync.propagate('a', 'b', 'c'))              # там другое заполненное: не трогаем
        self.assertEqual(sync.propagate('a', 'b', 'a'), 'b')          # там было то же старое: обновляем
        self.assertIsNone(sync.propagate('a', 'a', 'c'))              # не менялось: не трогаем
        self.assertIsNone(sync.propagate('', 'b', 'b'))

    def test_household_roundtrip(self):
        text = sync.household_text('2', '1')
        self.assertEqual(sync.parse_household(text), {'adults': '2', 'children': '1'})
        self.assertEqual(sync.parse_household(sync.household_text('1', '0')), {'adults': '1', 'children': '0'})
        self.assertIsNone(sync.parse_household('я, жінка і кіт'))


class MappeToLetterTests(_Db):
    def test_name_only_fills_empty_letter_profile(self):
        ms.save_answers(U, {'full_name': 'Артём Т', 'birth_date': '', 'address': ''})
        profile = ls.get_profile(U)
        self.assertEqual(profile['full_name'], 'Артём Т')
        self.assertEqual(profile['household'], '')
        self.assertFalse(ls.profile_complete(profile))                # частичная анкета не считается готовой для письма

    def test_household_and_income_go_to_letter(self):
        ms.save_answers(U, {'full_name': 'A B', 'occupation': 'works', 'employer': 'Firma', 'net_income': '1800',
                            'adults': '2', 'children': '0', 'contact': '+49 1'})
        profile = ls.get_profile(U)
        self.assertEqual(profile['household'], sync.household_text('2', '0'))
        self.assertEqual(profile['income'], 'Firma, 1.800 € netto')
        self.assertEqual(profile['contact'], '+49 1')

    def test_filled_letter_value_is_not_overwritten_but_old_value_is_updated(self):
        ls.save_profile(U, {'full_name': 'Old Name', 'household': 'власний текст', 'income': 'x', 'move_in': 'зараз'})
        # нове значення в папці, у листі стояло інше (не те, що було в папці): не затираємо
        ms.save_answers(U, {'full_name': 'Old Name', 'adults': '3', 'children': '1'})
        self.assertEqual(ls.get_profile(U)['household'], 'власний текст')
        # правка імені там, де воно було підставлено з листа: лист оновлюється
        ms.save_answers(U, {'full_name': 'New Name', 'adults': '3', 'children': '1'})
        self.assertEqual(ls.get_profile(U)['full_name'], 'New Name')

    def test_empty_answers_never_clear(self):
        ls.save_profile(U, {'full_name': 'Keep', 'household': 'h', 'income': 'i', 'move_in': 'm'})
        ms.save_answers(U, {'full_name': '', 'move_in': '', 'adults': ''})
        profile = ls.get_profile(U)
        self.assertEqual((profile['full_name'], profile['move_in'], profile['household']), ('Keep', 'm', 'h'))


class LetterToMappeTests(_Db):
    def test_letter_profile_fills_empty_mappe_answers(self):
        ms.save_answers(U, {'full_name': '', 'contact': '', 'pets': 'no'})
        ls.save_profile(U, {'full_name': 'Іван', 'household': sync.household_text('2', '1'), 'income': 'i',
                            'move_in': 'зараз', 'contact': 'a@b.de'})
        answers = ms.get_answers(U)
        self.assertEqual((answers['full_name'], answers['contact']), ('Іван', 'a@b.de'))
        self.assertEqual((answers['adults'], answers['children'], answers['pets']), ('2', '1', 'no'))

    def test_different_mappe_value_is_kept_and_no_row_is_invented(self):
        ms.save_answers(U, {'full_name': 'Раніше'}, sync=False)
        ls.save_profile(U, {'full_name': 'Лист', 'household': 'h', 'income': 'i', 'move_in': 'm'}, sync=False)
        ms.save_answers(U, {'full_name': 'Своє імʼя'})            # у листі інше (не те, що було в папці): не затираємо
        self.assertEqual(ls.get_profile(U)['full_name'], 'Лист')
        ls.save_profile(U, {'full_name': 'Інше', 'household': 'h', 'income': 'i', 'move_in': 'm'})
        self.assertEqual(ms.get_answers(U)['full_name'], 'Своє імʼя')   # там було не те старе значення: не затираємо
        ls.save_profile(U + 1, {'full_name': 'Z', 'household': 'h', 'income': 'i', 'move_in': 'm'})
        self.assertIsNone(ms.get_answers(U + 1))          # папка і так бере імʼя з «Про мене» (merged)

    def test_no_loop_between_stores(self):
        ms.save_answers(U, {'full_name': 'A', 'adults': '1', 'children': '0'})
        ls.save_profile(U, ls.get_profile(U))
        ms.save_answers(U, ms.get_answers(U))
        self.assertEqual(ls.get_profile(U)['full_name'], 'A')


class ViewTests(_Db):
    def test_letter_view_shows_mappe_values_without_writing(self):
        ms.save_answers(U, {'full_name': 'A', 'move_in': 'зараз'}, sync=False)
        view = sync.letter_view(U)
        self.assertEqual((view['full_name'], view['move_in']), ('A', 'зараз'))
        self.assertIsNone(ls.get_profile(U))

    def test_mappe_view_takes_household_from_letter(self):
        letter = {'household': sync.household_text('2', '0')}
        out = sync.mappe_view(U, {'adults': '', 'children': ''}, letter)
        self.assertEqual((out['adults'], out['children']), ('2', '0'))


if __name__ == '__main__':
    unittest.main()
