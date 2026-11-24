"""«🎁 Запроси друга»: привязка нового человека и награда за ПЕРВУЮ оплату друга
(25.11.2026)."""

import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import mock

import i18n
from database import HousingAccessUser, HousingPayment, HousingSearch, HousingUserJourney, UserSettings
from user_jobs.housing_pay_store import HousingReferral
from pay_helpers import ADMIN, FRIEND, USER, PayDbCase, callback, photo, tg_user
from user_handlers import chat_start, housing_referral
from user_jobs import housing_access_store, housing_letter_store, housing_pay_store, user_settings_store

INVITER = USER
OTHER = 4242


class ReferralBase(PayDbCase):
    def setUp(self):
        super().setUp()
        patch = mock.patch.object(housing_letter_store, "_bot_username", "PotsdamTestBot")
        patch.start()
        self.patches.append(patch)

    def invite(self, inviter=INVITER, friend=FRIEND):
        code = housing_pay_store.referral_code(inviter)
        update = SimpleNamespace(effective_user=tg_user(friend))
        return housing_referral.handle_start(update, self.context, f"rf{code}")

    def referral(self, friend=FRIEND):
        return self.session().query(HousingReferral).get(friend)

    def friend_pays(self, package="m1", friend=FRIEND, approve=True):
        self.housing_payment.handle_callback(callback(f"housing:pay:p:{package}", friend), self.context)
        self.housing_payment.handle_receipt(photo(friend, caption="оплата"), self.context)
        payment_id = self.session().query(HousingPayment).filter(HousingPayment.user_id == friend) \
            .order_by(HousingPayment.id.desc()).first().id
        if approve:
            update = callback(f"housing:pay:ok:{payment_id}", ADMIN)
            self.housing_payment.handle_callback(update, self.context)
            return payment_id, update
        return payment_id, None

    def expiry(self, user_id):
        row = self.session().query(HousingAccessUser).get(user_id)
        return row.expires_at if row else None


class LinkTest(ReferralBase):
    def test_code_is_stable_unique_and_short(self):
        first = housing_pay_store.referral_code(USER)
        self.assertEqual(housing_pay_store.referral_code(USER), first)
        self.assertNotEqual(housing_pay_store.referral_code(OTHER), first)
        self.assertEqual(len(first), 8)
        self.assertEqual(housing_pay_store.inviter_by_code(first), USER)
        self.assertIsNone(housing_pay_store.inviter_by_code("nonexist"))

    def test_link_fits_the_start_parameter_and_does_not_clash_with_letters(self):
        link = housing_referral.link_for(USER)
        self.assertTrue(link.startswith("https://t.me/PotsdamTestBot?start=rf"))
        param = link.split("start=", 1)[1]
        self.assertLessEqual(len(param), 64)
        self.assertFalse(param.startswith("lt"))
        self.assertEqual(housing_referral.START_PREFIX, "rf")

    def test_no_link_without_the_bot_username(self):
        with mock.patch.object(housing_letter_store, "_bot_username", None):
            self.assertIsNone(housing_referral.link_for(USER))
            text, markup = housing_referral.menu_text_and_keyboard(USER)
        self.assertIn("тимчасово недоступне", text)


class BindTest(ReferralBase):
    def test_a_brand_new_person_is_bound(self):
        self.assertTrue(self.invite())
        row = self.referral()
        self.assertEqual((row.inviter_id, row.rewarded_at), (INVITER, None))

    def test_nothing_is_promised_or_sent_to_the_friend_on_the_link(self):
        self.invite()
        self.bot.send_message.assert_not_called()

    def test_cannot_invite_yourself(self):
        self.assertFalse(self.invite(inviter=FRIEND, friend=FRIEND))
        self.assertIsNone(self.referral())

    def test_only_one_inviter_per_person(self):
        self.assertTrue(self.invite(inviter=INVITER))
        self.assertFalse(self.invite(inviter=OTHER))
        self.assertEqual(self.referral().inviter_id, INVITER)

    def test_a_person_the_bot_already_knows_is_not_bound(self):
        for make in (
            lambda s: s.add(UserSettings(user_id=FRIEND, language="uk", news_subscribed=True, updated_at=datetime.utcnow())),
            lambda s: s.add(HousingUserJourney(user_id=FRIEND, first_menu_at=datetime.utcnow())),
            lambda s: s.add(HousingSearch(user_id=FRIEND, criteria="{}", members="[]", coops="[]", created_at=datetime.utcnow())),
            lambda s: s.add(HousingAccessUser(user_id=FRIEND, display_name="x", active=False, created_at=datetime.utcnow(), updated_at=datetime.utcnow())),
        ):
            with self.subTest(make=make):
                session = self.session()
                make(session)
                session.commit()
                self.assertFalse(self.invite())
                self.assertIsNone(self.referral())
                session.query(UserSettings).delete()
                session.query(HousingUserJourney).delete()
                session.query(HousingSearch).delete()
                session.query(HousingAccessUser).delete()
                session.commit()

    def test_a_person_who_opened_the_bot_before_is_not_new(self):
        i18n.get_lang(FRIEND)       # так бот узнаёт человека на первом же экране
        self.assertFalse(self.invite())

    def test_unknown_code_does_nothing(self):
        update = SimpleNamespace(effective_user=tg_user(FRIEND))
        self.assertFalse(housing_referral.handle_start(update, self.context, "rfdeadbeef"))
        self.assertIsNone(self.referral())

    def test_off_switch_ignores_links(self):
        with mock.patch.dict("os.environ", {"HOUSING_REFERRAL_ENABLED": "0"}):
            self.assertFalse(self.invite())

    def test_start_binds_before_the_home_screen_creates_records(self):
        code = housing_pay_store.referral_code(INVITER)

        def fake_home(update, context, edit=False):
            i18n.get_lang(FRIEND)       # главный экран создаёт запись о человеке

        update = SimpleNamespace(
            message=SimpleNamespace(from_user=SimpleNamespace(id=FRIEND)),
            effective_chat=SimpleNamespace(type="private"), effective_user=tg_user(FRIEND),
        )
        self.context.args = [f"rf{code}"]
        with mock.patch("user_handlers.anonymous_posts.show_home", side_effect=fake_home) as home:
            chat_start.start(update, self.context)
        home.assert_called_once()
        self.assertEqual(self.referral().inviter_id, INVITER)

    def test_letter_links_still_go_to_letters(self):
        update = SimpleNamespace(
            message=SimpleNamespace(from_user=SimpleNamespace(id=FRIEND)),
            effective_chat=SimpleNamespace(type="private"), effective_user=tg_user(FRIEND),
        )
        self.context.args = ["lt12"]
        with mock.patch("user_handlers.housing_letter.handle_start") as letter, \
                mock.patch("user_handlers.anonymous_posts.show_home") as home:
            chat_start.start(update, self.context)
        letter.assert_called_once()
        home.assert_not_called()
        self.assertIsNone(self.referral())


class RewardTest(ReferralBase):
    def test_inviter_gets_a_month_when_the_friend_pays(self):
        self.invite()
        before = datetime.utcnow()
        self.friend_pays("m1")
        expected = self.housing_monitor._add_months(before, 1)
        self.assertLess(abs(self.expiry(INVITER) - expected), timedelta(minutes=2))
        row = self.referral()
        self.assertIsNotNone(row.rewarded_at)
        self.assertEqual(row.reward_kind, "month")

    def test_every_package_of_the_friend_triggers_exactly_one_month(self):
        for package in ("m1", "m3", "until_found"):
            with self.subTest(package=package):
                session = self.session()
                session.query(HousingReferral).delete()
                session.query(HousingAccessUser).delete()
                session.query(HousingPayment).delete()
                session.query(UserSettings).delete()      # человек снова «новый для бота»
                session.commit()
                self.invite()
                before = datetime.utcnow()
                self.friend_pays(package)
                self.assertLess(
                    abs(self.expiry(INVITER) - self.housing_monitor._add_months(before, 1)), timedelta(minutes=2),
                )

    def test_month_is_added_on_top_of_a_running_subscription(self):
        end = datetime.utcnow() + timedelta(days=10)
        housing_access_store.grant_access(INVITER, "Inviter", expires_at=end)
        self.invite()
        self.friend_pays("m3")
        self.assertLess(abs(self.expiry(INVITER) - self.housing_monitor._add_months(end, 1)), timedelta(seconds=5))

    def test_no_reward_until_the_friend_actually_pays(self):
        self.invite()
        self.friend_pays("m1", approve=False)       # квитанция есть, админ ещё не решил
        self.assertIsNone(self.expiry(INVITER))
        self.assertIsNone(self.referral().rewarded_at)

    def test_rejected_payment_gives_nothing(self):
        self.invite()
        payment_id, _ = self.friend_pays("m1", approve=False)
        self.housing_payment.handle_callback(callback(f"housing:pay:no:{payment_id}", ADMIN), self.context)
        self.assertIsNone(self.expiry(INVITER))
        self.assertIsNone(self.referral().rewarded_at)

    def test_reward_is_given_once_per_friend(self):
        self.invite()
        payment_id, _ = self.friend_pays("m1")
        first = self.expiry(INVITER)
        # повторный клик админа по той же оплате
        self.housing_payment.handle_callback(callback(f"housing:pay:ok:{payment_id}", ADMIN), self.context)
        self.assertEqual(self.expiry(INVITER), first)
        # вторая оплата друга (продление) - уже не первая
        self.friend_pays("m1")
        self.assertEqual(self.expiry(INVITER), first)

    def test_two_friends_two_months(self):
        self.invite(friend=FRIEND)
        self.invite(friend=OTHER)
        before = datetime.utcnow()
        self.friend_pays("m1", friend=FRIEND)
        self.friend_pays("m1", friend=OTHER)
        self.assertLess(abs(self.expiry(INVITER) - self.housing_monitor._add_months(
            self.housing_monitor._add_months(before, 1), 1)), timedelta(minutes=2))

    def test_both_get_a_message_and_the_admin_card_gets_a_line(self):
        self.invite()
        self.bot.reset_mock()
        _payment_id, update = self.friend_pays("m1")
        inviter_text = " ".join(self.sent_texts(INVITER))
        friend_texts = self.sent_texts(FRIEND)
        self.assertIn("Ваш друг оплатив підписку — вам додано +1 місяць", inviter_text)
        self.assertIn(self.expiry(INVITER).strftime("%d.%m.%Y"), inviter_text)
        self.assertTrue(any("Ви прийшли за запрошенням" in t for t in friend_texts), friend_texts)
        caption = update.callback_query.edit_message_caption.call_args.kwargs["caption"]
        self.assertIn(f"Запрошувачу <code>{INVITER}</code> додано +1 міс.", caption)

    def test_friend_is_not_promised_anything_extra(self):
        self.invite()
        _payment_id, _update = self.friend_pays("m1")
        friend_row = self.session().query(HousingAccessUser).get(FRIEND)
        self.assertLess(
            abs(friend_row.expires_at - self.housing_monitor._add_months(datetime.utcnow(), 1)), timedelta(minutes=2),
        )

    def test_receipt_card_tells_the_admin_the_friend_was_invited(self):
        self.invite()
        self.housing_payment.handle_callback(callback("housing:pay:p:m1", FRIEND), self.context)
        self.housing_payment.handle_receipt(photo(FRIEND), self.context)
        caption = self.bot.copy_message.call_args.kwargs["caption"]
        self.assertIn(f"Прийшов за запрошенням від <code>{INVITER}</code>", caption)
        self.assertIn("запрошувачу +1 міс.", caption)

    def test_receipt_card_without_an_invitation_has_no_extra_line(self):
        self.housing_payment.handle_callback(callback("housing:pay:p:m1", FRIEND), self.context)
        self.housing_payment.handle_receipt(photo(FRIEND), self.context)
        self.assertNotIn("запрошенням", self.bot.copy_message.call_args.kwargs["caption"])

    def test_inviter_on_until_found_is_only_thanked(self):
        self.housing_payment.handle_callback(callback("housing:pay:p:until_found", INVITER), self.context)
        self.housing_payment.handle_receipt(photo(INVITER), self.context)
        pid = self.session().query(HousingPayment).order_by(HousingPayment.id.desc()).first().id
        self.housing_payment.handle_callback(callback(f"housing:pay:ok:{pid}", ADMIN), self.context)
        end = self.expiry(INVITER)
        self.invite()
        self.bot.reset_mock()
        _payment_id, update = self.friend_pays("m1")
        self.assertEqual(self.expiry(INVITER), end)
        self.assertEqual(self.referral().reward_kind, "thanks")
        self.assertIn("Ваш пакет уже діє", " ".join(self.sent_texts(INVITER)))
        caption = update.callback_query.edit_message_caption.call_args.kwargs["caption"]
        self.assertIn("лише подяку", caption)
        self.assertTrue(any("нашу подяку" in t for t in self.sent_texts(FRIEND)))

    def test_inviter_with_unlimited_access_is_only_thanked(self):
        housing_access_store.grant_access(INVITER, "Inviter", expires_at=None)
        self.invite()
        self.friend_pays("m1")
        self.assertIsNone(self.expiry(INVITER))
        self.assertEqual(self.referral().reward_kind, "thanks")

    def test_admin_as_inviter_is_only_thanked(self):
        self.invite(inviter=ADMIN)
        self.friend_pays("m1")
        self.assertIsNone(self.expiry(ADMIN))
        self.assertEqual(self.referral().reward_kind, "thanks")

    def test_failed_extension_is_reported_and_can_be_redone(self):
        self.invite()
        with mock.patch.object(self.housing_monitor, "grant_paid_months", side_effect=[
            datetime.utcnow() + timedelta(days=30), RuntimeError("db locked"),
        ]):
            # первый вызов - оплата друга (обычная выдача), второй - продление запрошувачу падает
            _payment_id, update = self.friend_pays("m1")
        caption = update.callback_query.edit_message_caption.call_args.kwargs["caption"]
        self.assertIn("Не вдалося додати місяць", caption)
        self.assertIsNone(self.referral().rewarded_at)       # отметку сняли - награду не потеряли

    def test_reward_survives_a_blocked_inviter(self):
        self.invite()

        def send(**kwargs):
            if kwargs["chat_id"] == INVITER:
                raise RuntimeError("Forbidden: bot was blocked by the user")

        self.bot.send_message.side_effect = send
        self.friend_pays("m1")
        self.assertIsNotNone(self.expiry(INVITER))
        self.assertIsNotNone(self.referral().rewarded_at)

    def test_reward_does_not_switch_on_the_paused_searches_of_someone_who_found_a_flat(self):
        self.invite()
        self.housing_monitor._set_all_filters_active_for_user.reset_mock()
        before = self.housing_monitor._set_all_filters_active_for_user.call_args_list
        self.friend_pays("m1")
        # продление запрошувачу - без включения его фильтров; фильтры друга (он платит сам) включаются
        resumed_for = [c.args[0] for c in self.housing_monitor._set_all_filters_active_for_user.call_args_list]
        self.assertNotIn(INVITER, resumed_for)
        self.assertIn(FRIEND, resumed_for)

    def test_off_switch_gives_no_reward(self):
        self.invite()
        with mock.patch.dict("os.environ", {"HOUSING_REFERRAL_ENABLED": "0"}):
            self.friend_pays("m1")
        self.assertIsNone(self.expiry(INVITER))

    def test_messages_in_every_language(self):
        for lang in i18n.SUPPORTED_LANGS:
            with self.subTest(lang=lang):
                session = self.session()
                session.query(HousingReferral).delete()
                session.query(HousingPayment).delete()
                session.query(HousingAccessUser).delete()
                session.commit()
                user_settings_store.set_language(INVITER, lang)
                user_settings_store.set_language(FRIEND, lang)
                session.close()
                # у друга уже есть запись о языке - привязываем напрямую
                session = self.session()
                session.add(HousingReferral(invited_id=FRIEND, inviter_id=INVITER, created_at=datetime.utcnow()))
                session.commit()
                self.bot.reset_mock()
                self.friend_pays("m1")
                for text in self.sent_texts(INVITER) + self.sent_texts(FRIEND):
                    self.assertNotIn("{", text)
                    self.assertNotIn("[[", text)


class CountersTest(ReferralBase):
    def test_menu_shows_invited_and_paid_only_when_nonzero(self):
        text, _markup = housing_referral.menu_text_and_keyboard(INVITER)
        self.assertNotIn("Запрошено", text)
        self.assertNotIn("Оплатили", text)
        self.assertIn("https://t.me/PotsdamTestBot?start=rf", text)
        self.invite(friend=FRIEND)
        self.invite(friend=OTHER)
        text, _markup = housing_referral.menu_text_and_keyboard(INVITER)
        self.assertIn("Запрошено: <b>2</b>", text)
        self.assertNotIn("Оплатили", text)
        self.friend_pays("m1", friend=FRIEND)
        text, _markup = housing_referral.menu_text_and_keyboard(INVITER)
        self.assertIn("Запрошено: <b>2</b>", text)
        self.assertIn("Оплатили: <b>1</b>", text)
        self.assertEqual(housing_pay_store.referral_counts(INVITER), (2, 1))

    def test_paid_counts_only_approved_payments(self):
        self.invite()
        self.friend_pays("m1", approve=False)
        self.assertEqual(housing_pay_store.referral_counts(INVITER), (1, 0))

    def test_menu_has_share_button_and_back(self):
        _text, markup = housing_referral.menu_text_and_keyboard(INVITER)
        buttons = self.buttons(markup)
        self.assertTrue(buttons[0].url.startswith("https://t.me/share/url?url="))
        self.assertEqual(buttons[-1].callback_data, "housing:menu")

    def test_menu_callback_opens_the_screen(self):
        update = callback(housing_referral.CALLBACK, INVITER)
        self.housing_monitor._handle_callback(update, self.context)
        text = update.callback_query.edit_message_text.call_args.args[0]
        self.assertIn("Запроси друга", text)

    def test_menu_button_is_in_the_housing_menu(self):
        markup = self.housing_monitor._menu_keyboard(INVITER, "uk")
        self.assertIn(housing_referral.CALLBACK, [b.callback_data for b in self.buttons(markup)])
        with mock.patch.dict("os.environ", {"HOUSING_REFERRAL_ENABLED": "0"}):
            markup = self.housing_monitor._menu_keyboard(INVITER, "uk")
        self.assertNotIn(housing_referral.CALLBACK, [b.callback_data for b in self.buttons(markup)])

    def test_menu_screen_in_every_language(self):
        for lang in i18n.SUPPORTED_LANGS:
            user_settings_store.set_language(INVITER, lang)
            text, markup = housing_referral.menu_text_and_keyboard(INVITER)
            self.assertNotIn("{", text)
            self.assertNotIn("[[", text)
            for button in self.buttons(markup):
                self.assertNotIn("[[", button.text)


if __name__ == "__main__":
    unittest.main()
