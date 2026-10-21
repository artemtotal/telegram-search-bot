"""Вечірня самоперевірка відправок (аудит 13.10.2026)."""

import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database import Base, HousingSendLog, ImmoweltListing, KarlmarxListing, KleinanzeigenListing
from user_handlers import housing_monitor, housing_send_audit
from user_jobs import karlmarx_store, kleinanzeigen_store


class ListingProblemsTests(unittest.TestCase):
    def test_photo_counter_read_as_the_rent(self):
        problems = housing_send_audit.listing_problems({'rooms': 3, 'area_m2': 84.6, 'price_eur': 60})
        self.assertTrue(any('€/м²' in p for p in problems))

    def test_a_typo_in_the_area(self):
        problems = housing_send_audit.listing_problems({'rooms': 2, 'area_m2': 520, 'price_warm_eur': 520})
        self.assertTrue(any('площа 520' in p for p in problems))

    def test_a_wg_room(self):
        problems = housing_send_audit.listing_problems({'title': 'WG Zimmer in Golm', 'rooms': 1, 'area_m2': 15, 'price_eur': 450})
        self.assertIn('за заголовком — не квартира', problems)

    def test_an_ordinary_flat_is_fine(self):
        self.assertEqual(housing_send_audit.listing_problems(
            {'title': 'Helle 3-Zimmer Wohnung', 'rooms': 3, 'area_m2': 59.5, 'price_eur': 507, 'price_warm_eur': 757},
        ), [])


class AuditJobTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self._saved = [(m, m.DBSession) for m in (housing_send_audit, karlmarx_store, kleinanzeigen_store)]
        for module, _ in self._saved:
            module.DBSession = sessionmaker(bind=self.engine)

    def tearDown(self):
        for module, session in self._saved:
            module.DBSession = session
        self.engine.dispose()

    def _add(self, *rows):
        session = sessionmaker(bind=self.engine)()
        session.add_all(rows)
        session.commit()
        session.close()

    def test_suspicious_sends_reach_the_admin_grouped_by_flat(self):
        now = datetime.utcnow()
        filter_id = karlmarx_store.create_filter(user_id=1, title='t', max_price_warm_eur=800)
        self._add(
            ImmoweltListing(listing_key='iw', title='Wohnung zur Miete', rooms=3, area_m2=84.6, price_eur=60,
                            detail_url='https://www.immowelt.de/expose/iw', first_seen_at=now),
            KarlmarxListing(listing_key='km', title='Neubau', rooms=4, area_m2=98, price_warm_eur=1723.23,
                            first_seen_at=now, last_seen_at=now, is_active=True),
            HousingSendLog(user_id=1, source='immowelt', listing_key='iw', delayed=False, sent_at=now - timedelta(hours=2)),
            HousingSendLog(user_id=2, source='immowelt', listing_key='iw', delayed=False, sent_at=now - timedelta(hours=2)),
            HousingSendLog(user_id=1, source='karlmarx', listing_key='km', filter_id=filter_id,
                           delayed=False, sent_at=now - timedelta(hours=1)),
            # Учорашнє не рахується.
            HousingSendLog(user_id=3, source='immowelt', listing_key='iw', delayed=False, sent_at=now - timedelta(days=2)),
        )
        bot = mock.Mock()
        with mock.patch.object(housing_monitor, 'ADMIN_ID', 312029534), \
             mock.patch.object(housing_monitor, '_all_immowelt_filters', return_value=[]):
            housing_send_audit.daily_send_audit_job(SimpleNamespace(bot=bot))

        text = bot.send_message.call_args.kwargs['text']
        self.assertIn('Підозрілих квартир: <b>2</b>, відправок: <b>3</b>', text)
        self.assertIn('(людей: 2)', text)
        self.assertIn('не підходить під фільтр', text)

    def test_an_immowelt_flat_above_every_filter_of_the_person(self):
        """2950 € пішли фільтру «до 680 €»: приймач прочитав ціну як 14 €."""
        now = datetime.utcnow()
        self._add(
            ImmoweltListing(listing_key='ev', title='Wohnung zur Miete', rooms=2.5, area_m2=120, price_eur=2950,
                            first_seen_at=now),
            ImmoweltListing(listing_key='ok', title='Wohnung zur Miete', rooms=2, area_m2=60, price_eur=900,
                            first_seen_at=now),
            HousingSendLog(user_id=1, source='immowelt', listing_key='ev', delayed=False, sent_at=now - timedelta(hours=1)),
            # 900 € — у межах оцінки повної з холодної 680 € (680 + 4 × 60).
            HousingSendLog(user_id=1, source='immowelt', listing_key='ok', delayed=False, sent_at=now - timedelta(hours=1)),
        )
        filters = [{'filter_id': 5, 'user_id': 1, 'active': True, 'min_rooms': 1, 'max_price_eur': 680.9}]
        with mock.patch.object(housing_monitor, '_all_immowelt_filters', return_value=filters):
            findings = housing_send_audit.collect_findings(now - timedelta(days=1), now)

        self.assertEqual([key for _source, key in findings], ['ev'])
        self.assertIn('не підходить під жоден Immowelt-фільтр людини', findings[('immowelt', 'ev')]['problems'])

    def test_a_kleinanzeigen_near_miss_hint_is_not_a_wrong_send(self):
        """Підказка «дорожче лише на N €» теж у журналі відправок (20.10.2026:
        1050 € за 63 м² фільтру «до 811 € холодна»)."""
        now = datetime.utcnow()
        filter_id = kleinanzeigen_store.create_filter(user_id=1, title='t', max_price_eur=810.7)
        self._add(
            KleinanzeigenListing(listing_key='ka', title='EG-Wohnung in Potsdam Eiche', rooms=3, area_m2=63,
                                 price_eur=1050, first_seen_at=now, last_seen_at=now, is_active=True),
            HousingSendLog(user_id=1, source='kleinanzeigen', listing_key='ka', filter_id=filter_id,
                           delayed=True, sent_at=now - timedelta(hours=1)),
        )
        with mock.patch.object(housing_monitor, '_all_immowelt_filters', return_value=[]):
            findings = housing_send_audit.collect_findings(now - timedelta(days=1), now)

        self.assertEqual(dict(findings), {})

    def test_a_quiet_day_sends_nothing(self):
        bot = mock.Mock()
        with mock.patch.object(housing_monitor, 'ADMIN_ID', 312029534), \
             mock.patch.object(housing_monitor, '_all_immowelt_filters', return_value=[]):
            housing_send_audit.daily_send_audit_job(SimpleNamespace(bot=bot))
        bot.send_message.assert_not_called()


if __name__ == '__main__':
    unittest.main()
