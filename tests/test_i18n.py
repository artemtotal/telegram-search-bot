import unittest
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import i18n
from database import Base
from user_jobs import user_settings_store


class TranslationCoverageTests(unittest.TestCase):
    """uk.json is the source of truth; ru/de must never silently drift out
    of sync with it - a missing key should fail a test, not fall back
    quietly in production forever."""

    def test_all_languages_define_the_same_keys(self):
        uk_keys = set(i18n._TRANSLATIONS['uk'].keys())
        for lang in ('ru', 'de'):
            lang_keys = set(i18n._TRANSLATIONS[lang].keys())
            missing = uk_keys - lang_keys
            extra = lang_keys - uk_keys
            self.assertEqual(
                (missing, extra), (set(), set()),
                f"{lang}.json is out of sync with uk.json - missing={missing} extra={extra}",
            )


class TranslateFunctionTests(unittest.TestCase):
    def setUp(self):
        self.original = dict(i18n._TRANSLATIONS)
        i18n._TRANSLATIONS = {
            'uk': {'greeting': 'Привіт, {name}!', 'only_in_uk': 'Тільки укр.'},
            'ru': {'greeting': 'Привет, {name}!'},
            'de': {'greeting': 'Hallo, {name}!'},
        }

    def tearDown(self):
        i18n._TRANSLATIONS = self.original

    def test_returns_the_requested_language(self):
        self.assertEqual(i18n.t('greeting', 'de', name='Артем'), 'Hallo, Артем!')

    def test_falls_back_to_ukrainian_when_key_missing_in_language(self):
        self.assertEqual(i18n.t('only_in_uk', 'ru'), 'Тільки укр.')

    def test_falls_back_to_ukrainian_for_an_unsupported_language(self):
        self.assertEqual(i18n.t('greeting', 'fr', name='X'), 'Привіт, X!')

    def test_missing_everywhere_returns_the_bracketed_key_instead_of_raising(self):
        self.assertEqual(i18n.t('nonexistent.key', 'uk'), '[[nonexistent.key]]')

    def test_a_missing_format_placeholder_does_not_raise(self):
        self.assertEqual(i18n.t('greeting', 'uk'), 'Привіт, {name}!')


class GetLangTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            'sqlite://',
            connect_args={'check_same_thread': False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.original_session = user_settings_store.DBSession
        user_settings_store.DBSession = sessionmaker(bind=self.engine)

    def tearDown(self):
        user_settings_store.DBSession = self.original_session
        self.engine.dispose()

    def test_defaults_to_ukrainian_for_an_unknown_user(self):
        self.assertEqual(i18n.get_lang(999), 'uk')

    def test_reflects_a_stored_preference(self):
        user_settings_store.set_language(777, 'de')

        self.assertEqual(i18n.get_lang(777), 'de')

    def test_an_unsupported_stored_value_falls_back_to_ukrainian(self):
        with mock.patch.object(user_settings_store, 'get_language', return_value='fr'):
            self.assertEqual(i18n.get_lang(777), 'uk')


if __name__ == '__main__':
    unittest.main()
