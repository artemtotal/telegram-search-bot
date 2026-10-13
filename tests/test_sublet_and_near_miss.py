"""Суборенда лише тим, хто шукає кімнату, і підказка «трохи дорожче» з
Kleinanzeigen не частіше разу на 4 дні (рішення Артема 13.10.2026)."""

import json
import unittest
from datetime import datetime, timedelta
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database import Base, HousingSearch, HousingSendLog, KleinanzeigenListing
from user_handlers import housing_receiver
from user_jobs import (
    housing_journey_store, housing_tier, kleinanzeigen_matching, kleinanzeigen_monitor, kleinanzeigen_store, sublet,
)


class _InMemoryDb(unittest.TestCase):
    MODULES = (housing_tier, housing_journey_store, kleinanzeigen_store, sublet, housing_receiver)

    def setUp(self):
        self.engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self._saved = [(module, module.DBSession) for module in self.MODULES]
        for module in self.MODULES:
            module.DBSession = sessionmaker(bind=self.engine)
        patcher = mock.patch.object(housing_tier, 'premium_user_ids', return_value={777, 888})
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        for module, session in self._saved:
            module.DBSession = session
        self.engine.dispose()

    def _add(self, *rows):
        session = sessionmaker(bind=self.engine)()
        session.add_all(rows)
        session.commit()
        session.close()

    def _search(self, user_id, property_types=None):
        criteria = {'min_rooms': 2}
        if property_types is not None:
            criteria['property_types'] = property_types
        self._add(HousingSearch(user_id=user_id, criteria=json.dumps(criteria), members='[]', coops='[]',
                                created_at=datetime.utcnow()))


class SubletTitlesTests(unittest.TestCase):
    def test_sublets_are_recognised(self):
        for title in (
            'Mietwhg zur Untermiete in Potsdam Eiche für ein Jahr -unmöbiliert',
            'Teilmöblierte 2ZKB in Potsdam zur befristeten Untervermierung',
            'Potsdam Wohnung zur Zwischenmiete am Heiligen See',
            'Möblierte 2-Zimmer-Wohnung in Potsdam zur zwischenmietung',
            'neu sanierte 3-Zimmer in Potsdam, nahe Sanssouci - saniert befristet zu vermieten',
            'WG-Zimmer zur Miete auf Zeit',
            'Möbliertes Studio in Potsdam 01.09.2026 – 14.09.2026',
            'Wohnung in Potsdam Untervermietung 1 Jahr',
        ):
            self.assertTrue(sublet.is_sublet(title), title)

    def test_ordinary_and_open_ended_lets_are_not(self):
        for title in (
            '1 Zimmer Wohnung Möbeliert unbefristet Potsdam',
            'Hell, Groß, Höhe decken und mit Balkon! Unbefristete 4 Zimmer Wohnung im DG !',
            'Stilvolle möblierte Wohnung in Potsdam',
            'Nachmieter für 3 Zimmer-Wohnung in Potsdam gesucht!',
            'Helle 3-Zimmer Wohnung mitten in Potsdam Waldstadt',
        ):
            self.assertFalse(sublet.is_sublet(title), title)


class SubletDeliveryTests(_InMemoryDb):
    SUBLET = {'listing_key': 's', 'title': '2-Zimmer-Wohnung zur Untermiete'}

    def test_only_someone_who_chose_rooms_gets_a_sublet(self):
        self._search(777, ['apartment', 'room'])
        self._search(888)  # типовий вибір «усе» — кімнатою не вважається
        gate = housing_tier.DeliveryGate('kleinanzeigen')
        self.assertTrue(gate.due(777, self.SUBLET))
        self.assertFalse(gate.due(888, self.SUBLET))
        self.assertTrue(gate.due(888, {'listing_key': 'f', 'title': '2-Zimmer-Wohnung in Potsdam'}))

    def test_immowelt_sublet_is_skipped_for_a_flat_seeker(self):
        self._search(888)
        payload = {
            'source': 'immowelt', 'user_id': 888, 'filter_title': 'f',
            'listing': {'url': 'https://www.immowelt.de/expose/x', 'listing_id': 'x',
                        'title': 'WG-Zimmer zur Miete auf Zeit'},
        }
        bot = mock.Mock()
        result = housing_receiver.handle_immowelt_result(bot, payload)
        self.assertEqual(result, {'ok': True, 'skipped': 'sublet'})
        bot.send_message.assert_not_called()
        bot.send_photo.assert_not_called()


class NearMissMatchingTests(unittest.TestCase):
    FILT = {'min_rooms': 2, 'max_price_eur': 700, 'max_price_warm_eur': 850}

    def test_a_price_that_may_be_the_full_rent(self):
        listing = {'rooms': 2, 'area_m2': 55, 'price_eur': 750, 'price_warm_eur': 750}
        self.assertEqual(kleinanzeigen_matching.near_miss_over(listing, self.FILT), 50)
        # Повної немає взагалі — так само неоднозначно.
        self.assertEqual(kleinanzeigen_matching.near_miss_over({**listing, 'price_warm_eur': None}, self.FILT), 50)

    def test_not_a_near_miss(self):
        base = {'rooms': 2, 'area_m2': 55, 'price_eur': 750}
        # Портал назвав окрему повну — холодна справжня.
        self.assertIsNone(kleinanzeigen_matching.near_miss_over({**base, 'price_warm_eur': 900}, self.FILT))
        # І як повна не вкладається.
        self.assertIsNone(kleinanzeigen_matching.near_miss_over({**base, 'price_eur': 900}, self.FILT))
        # Не підходить ще й за кімнатами.
        self.assertIsNone(kleinanzeigen_matching.near_miss_over({**base, 'rooms': 1}, self.FILT))
        # Підходить і так.
        self.assertIsNone(kleinanzeigen_matching.near_miss_over({**base, 'price_eur': 650}, self.FILT))


class NearMissDeliveryTests(_InMemoryDb):
    def _listing(self, key, price, age=timedelta(hours=5)):
        now = datetime.utcnow()
        self._add(KleinanzeigenListing(
            listing_key=key, title='2-Zimmer-Wohnung', address='Potsdam', rooms=2, area_m2=55, price_eur=price,
            price_warm_eur=price, first_seen_at=now - age, last_seen_at=now, is_active=True,
        ))

    def _run(self):
        bot = mock.Mock()
        filters = kleinanzeigen_store.list_filters(active_only=True)
        gate = housing_tier.DeliveryGate('kleinanzeigen', KleinanzeigenListing)
        return bot, kleinanzeigen_monitor._send_near_misses(bot, filters, gate)

    def test_one_hint_with_the_smallest_overrun_then_quiet_for_four_days(self):
        kleinanzeigen_store.create_filter(user_id=777, title='t', min_rooms=2, max_price_eur=700, max_price_warm_eur=850)
        self._listing('a', 800)
        self._listing('b', 740)
        self._listing('old', 720, age=timedelta(days=5))

        bot, sent = self._run()

        self.assertEqual(sent, 1)
        text = bot.send_message.call_args.kwargs['text']
        self.assertIn('40', text)
        self.assertIn('700', text)
        session = sessionmaker(bind=self.engine)()
        self.assertEqual([r.listing_key for r in session.query(HousingSendLog)], ['b'])
        session.close()

        # Наступний обхід у межах 4 днів — нічого.
        bot, sent = self._run()
        self.assertEqual(sent, 0)
        bot.send_message.assert_not_called()

        # Через 4 дні — наступна, але не та сама.
        with mock.patch.object(housing_journey_store, 'utc_now', return_value=datetime.utcnow() - timedelta(days=5)):
            housing_journey_store.mark_near_miss_sent(777)
        bot, sent = self._run()
        self.assertEqual(sent, 1)
        self.assertIn('100', bot.send_message.call_args.kwargs['text'])


if __name__ == '__main__':
    unittest.main()
