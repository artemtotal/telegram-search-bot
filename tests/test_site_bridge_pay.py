"""Мост сайта: оплата жилья и «Підтримати проєкт» (пакет H2, 28.11.2026).
Тот же сценарий, что в чате: запись оплаты, чек админу с теми же кнопками, день миттєвих, одобрение у админа. Админу и людям ничего не уходит:
Telegram подменён. Базовый класс: PayDbCase (база в памяти)."""

import base64
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from unittest import mock

import site_bridge
from site_bridge import Refuse, pay
from database import HousingPayment, HousingAccessUser
from pay_helpers import ADMIN, FRIEND, USER, PayDbCase, callback, photo

JPG = b"\xff\xd8\xff\xe0" + b"jpegdata" * 20
PNG = b"\x89PNG\r\n\x1a\n" + b"pngdata" * 20
PDF = b"%PDF-1.4\n" + b"pdfdata" * 20


def b64(raw):
    return base64.b64encode(raw).decode()


class PayBridgeCase(PayDbCase):
    def setUp(self):
        super().setUp()
        for name in ("_sent_hashes", "_last_receipt", "_creates", "_support_done", "_user_locks"):
            getattr(pay, name).clear()
        self.tg = mock.MagicMock()
        self.tg.get_chat.return_value = SimpleNamespace(first_name="Olena", last_name="", username="olena")
        p = mock.patch.object(pay, "bot", return_value=self.tg)
        p.start()
        self.patches.append(p)
        p2 = mock.patch.object(self.housing_monitor, "_notify_user_access_granted")
        p2.start()
        self.patches.append(p2)
        # без паузы между чеками (её проверяет отдельный тест)
        p3 = mock.patch.object(pay, "RECEIPT_GAP", 0.0)
        p3.start()
        self.patches.append(p3)
        self.context.bot = self.tg

    def payments(self):
        s = self.session()
        try:
            return [(p.user_id, p.package, p.status, p.amount_uah) for p in s.query(HousingPayment).order_by(HousingPayment.id)]
        finally:
            s.close()

    def pick(self, code="m1", user=USER):
        return pay.create(user, "uk", {"package": code})["payment"]

    def admin_calls(self):
        return self.tg.send_photo.call_args_list + self.tg.send_document.call_args_list + self.tg.send_message.call_args_list + self.tg.copy_message.call_args_list


class InfoTests(PayBridgeCase):
    def test_unknown_user_gets_packages_from_bot_functions(self):
        d = pay.info(424242, "uk", {})
        self.assertTrue(d["enabled"])
        self.assertFalse(d["premium"])
        self.assertIsNone(d["until"])
        self.assertIsNone(d["last"])
        self.assertIsNone(d["open"])
        from user_jobs import housing_packages as hpk
        self.assertEqual([p["code"] for p in d["packages"]], list(hpk.CODES))
        for p in d["packages"]:
            self.assertEqual(p["uah"], hpk.amount_uah(p["code"], 48.3))
            self.assertEqual(p["eur"], hpk.price_eur(p["code"]))
        self.assertEqual(d["rate"], "48,30")
        self.assertNotIn("card", json.dumps(d))          # реквизиты только после выбора пакета
        self.assertEqual(self.admin_calls(), [])
        self.assertEqual(self.payments(), [])            # info ничего не пишет

    def test_info_shows_subscription_and_last_payment_statuses(self):
        self.housing_monitor.grant_paid_months(USER, "Olena", 2)
        d = pay.info(USER, "uk", {})
        self.assertTrue(d["premium"])
        self.assertTrue(d["until"])
        self.pick("m3")
        pay.receipt(USER, "uk", {"payment_id": pay.info(USER, "uk", {})["open"]["id"], "data": b64(JPG)})
        d = pay.info(USER, "uk", {})
        self.assertEqual(d["last"]["status"], "sent")
        self.assertEqual(d["open"]["status"], "sent")
        pid = d["last"]["id"]
        self.housing_payment.handle_callback(callback("housing:pay:no:%d" % pid, ADMIN), self.context)
        d = pay.info(USER, "uk", {})
        self.assertEqual(d["last"]["status"], "rejected")
        self.assertIsNone(d["open"])

    def test_info_for_other_user_does_not_leak_open_payment(self):
        self.pick("m1", USER)
        d = pay.info(FRIEND, "uk", {})
        self.assertIsNone(d["open"])
        self.assertIsNone(d["last"])

    def test_params_user_id_is_ignored(self):
        pay.create(USER, "uk", {"package": "m1", "user_id": FRIEND})
        self.assertEqual([r[0] for r in self.payments()], [USER])

    def test_card_missing_means_disabled(self):
        with mock.patch.object(self.housing_payment, "PAY_CARD", ""):
            d = pay.info(USER, "uk", {})
            self.assertFalse(d["enabled"])
            self.assertEqual(d["packages"], [])
            with self.assertRaises(Refuse) as cm:
                pay.create(USER, "uk", {"package": "m1"})
            self.assertEqual(cm.exception.code, "unavailable")
            with self.assertRaises(Refuse):
                pay.receipt(USER, "uk", {"payment_id": 1, "data": b64(JPG)})

    def test_until_found_user_cannot_pay_again(self):
        self.pick("until_found")
        pid = pay.info(USER, "uk", {})["open"]["id"]
        pay.receipt(USER, "uk", {"payment_id": pid, "data": b64(JPG)})
        self.housing_payment.handle_callback(callback("housing:pay:ok:%d" % pid, ADMIN), self.context)
        d = pay.info(USER, "uk", {})
        self.assertTrue(d["until_found"])
        self.assertEqual(d["packages"], [])
        with self.assertRaises(Refuse) as cm:
            pay.create(USER, "uk", {"package": "m1"})
        self.assertEqual(cm.exception.code, "until_found")

    def test_texts_in_three_languages(self):
        for lang in ("uk", "ru", "de"):
            d = pay.info(USER, lang, {})
            self.assertNotIn("[[", json.dumps(d, ensure_ascii=False))
            self.assertTrue(d["cash_note"])
            c = pay.create(USER, lang, {"package": "m1"})
            self.assertTrue(c["payment"]["warn"])
            self.assertNotIn("[[", json.dumps(c, ensure_ascii=False))


class CreateTests(PayBridgeCase):
    def test_create_writes_awaiting_row_with_requisites_from_bot(self):
        c = pay.create(USER, "uk", {"package": "m1"})
        p = c["payment"]
        self.assertEqual(p["status"], "awaiting_receipt")
        self.assertEqual(p["uah"], 250)
        self.assertEqual(p["card_raw"], "4111111111111111")
        self.assertEqual(p["card"], "4111 1111 1111 1111")
        self.assertEqual(p["holder"], "Тарасюк")
        self.assertEqual(self.payments(), [(USER, "m1", "awaiting_receipt", 250)])
        s = self.session()
        self.assertEqual(s.query(HousingPayment).first().display_name, "Olena (@olena)")   # как в чате
        s.close()

    def test_double_tap_same_package_reuses_row(self):
        a = self.pick("m1")
        b = self.pick("m1")
        self.assertEqual(a["id"], b["id"])
        self.assertEqual(len(self.payments()), 1)

    def test_other_package_cancels_previous_choice(self):
        self.pick("m1")
        self.pick("m3")
        self.assertEqual([r[2] for r in self.payments()], ["cancelled", "awaiting_receipt"])
        self.assertEqual(self.payments()[-1][1], "m3")

    def test_bad_package_is_refused_and_writes_nothing(self):
        for bad in ("", "m2", "n1", None, 5, "m1; drop", "until_found "):
            with self.assertRaises(Refuse):
                pay.create(USER, "uk", {"package": bad})
        self.assertEqual(self.payments(), [])

    def test_cancel_closes_only_unsent_choice(self):
        self.pick("m1")
        pay.cancel(USER, "uk", {})
        self.assertEqual(self.payments()[-1][2], "cancelled")
        pid = self.pick("m1")["id"]
        pay.receipt(USER, "uk", {"payment_id": pid, "data": b64(JPG)})
        pay.cancel(USER, "uk", {})
        self.assertEqual(self.payments()[-1][2], "sent")

    def test_create_rate_limit(self):
        with mock.patch.object(pay, "CREATE_PER_HOUR", 2):
            self.pick("m1")
            self.pick("m3")
            with self.assertRaises(Refuse) as cm:
                self.pick("m1")
            self.assertEqual(cm.exception.code, "rate_limit")

    def test_legacy_months_mode(self):
        from user_jobs import housing_packages as hpk
        with mock.patch.object(hpk, "enabled", return_value=False):
            d = pay.info(USER, "uk", {})
            self.assertEqual([p["code"] for p in d["packages"]], ["n1", "n2", "n3", "n4", "n5", "n6"])
            self.assertEqual(d["packages"][0]["uah"], 250)
            p = pay.create(USER, "uk", {"package": "n3"})["payment"]
            self.assertEqual((p["months"], p["uah"]), (3, 730))
            with self.assertRaises(Refuse):
                pay.create(USER, "uk", {"package": "m1"})


class ReceiptTests(PayBridgeCase):
    def test_jpg_receipt_same_scenario_as_chat(self):
        pid = self.pick("m3")["id"]
        before = datetime.utcnow()
        out = pay.receipt(USER, "uk", {"payment_id": pid, "data": b64(JPG), "note": "оплатила <b>сьогодні</b>"})
        self.assertEqual(out["status"], "sent")
        self.assertTrue(out["provisional"])
        self.assertEqual(out["payment"]["status"], "sent")
        # админу: фото с карточкой и теми же кнопками, что в чате
        self.assertEqual(self.tg.send_photo.call_count, 1)
        kw = self.tg.send_photo.call_args.kwargs
        self.assertEqual(kw["chat_id"], ADMIN)
        self.assertIn("Оплата підписки", kw["caption"])
        self.assertIn("580 грн", kw["caption"])
        self.assertIn("🌐 з сайту", kw["caption"])
        self.assertIn("&lt;b&gt;сьогодні", kw["caption"])      # подпись экранируется
        self.assertIn("1 день", kw["caption"])
        data = [b.callback_data for row in kw["reply_markup"].inline_keyboard for b in row if b.callback_data]
        self.assertEqual(data, ["housing:pay:ok:%d" % pid, "housing:pay:no:%d" % pid])
        self.assertEqual(kw["reply_markup"].inline_keyboard[1][1].url, "tg://user?id=%d" % USER)
        # статус, день миттєвих, как в чате
        self.assertEqual(self.payments()[-1][2], "sent")
        s = self.session()
        row = s.query(HousingAccessUser).get(USER)
        self.assertGreater(row.expires_at, before + timedelta(hours=23))
        self.assertLess(row.expires_at, before + timedelta(hours=25))
        s.close()
        # людям в чат ничего не ушло, кроме единственной отправки админу
        self.tg.send_message.assert_not_called()
        self.assertEqual(len(self.admin_calls()), 1)

    def test_caption_matches_chat_caption_apart_from_marker(self):
        pid = self.pick("m1")["id"]
        pay.receipt(USER, "uk", {"payment_id": pid, "data": b64(JPG)})
        web = self.tg.send_photo.call_args.kwargs["caption"]
        # тот же шаблон, что и для чата: заголовок, пакет, сумма, Telegram ID
        for needle in ("💳 <b>Оплата підписки — перевірте квитанцію</b>", "Пакет: <b>1 міс.</b>", "Сума: <b>250 грн</b>", "Telegram ID: <code>777</code>"):
            self.assertIn(needle, web)

    def test_chat_and_web_receipts_share_the_same_submit_step(self):
        self.housing_payment.handle_callback(callback("housing:pay:p:m1"), self.context)
        self.housing_payment.handle_receipt(photo(USER, caption="чат"), self.context)
        chat = self.tg.copy_message.call_args.kwargs
        self.tg.reset_mock()
        pid = self.pick("m1", FRIEND)["id"]
        pay.receipt(FRIEND, "uk", {"payment_id": pid, "data": b64(JPG)})
        web = self.tg.send_photo.call_args.kwargs
        chat_keys = [b.text.split(" ")[0] for row in chat["reply_markup"].inline_keyboard for b in row]
        web_keys = [b.text.split(" ")[0] for row in web["reply_markup"].inline_keyboard for b in row]
        self.assertEqual(chat_keys, web_keys)
        self.assertEqual(chat["parse_mode"], web["parse_mode"])

    def test_png_goes_as_photo_pdf_as_document(self):
        pid = self.pick("m1")["id"]
        pay.receipt(USER, "uk", {"payment_id": pid, "data": b64(PNG)})
        self.assertEqual((self.tg.send_photo.call_count, self.tg.send_document.call_count), (1, 0))
        pid = self.pick("m3")["id"]
        pay.receipt(USER, "uk", {"payment_id": pid, "data": b64(PDF)})
        self.assertEqual((self.tg.send_photo.call_count, self.tg.send_document.call_count), (1, 1))
        kw = self.tg.send_document.call_args.kwargs
        self.assertEqual(kw["document"].name, "receipt.pdf")
        self.assertEqual(kw["document"].read(), PDF)

    def test_photo_refused_by_telegram_falls_back_to_document(self):
        self.tg.send_photo.side_effect = RuntimeError("PHOTO_INVALID_DIMENSIONS")
        pid = self.pick("m1")["id"]
        out = pay.receipt(USER, "uk", {"payment_id": pid, "data": b64(JPG)})
        self.assertEqual(out["status"], "sent")
        self.assertEqual(self.tg.send_document.call_count, 1)
        self.assertEqual(self.tg.send_document.call_args.kwargs["document"].read(), JPG)

    def test_admin_unreachable_gives_error_and_keeps_payment_open(self):
        self.tg.send_photo.side_effect = RuntimeError("down")
        self.tg.send_document.side_effect = RuntimeError("down")
        self.tg.send_message.side_effect = RuntimeError("down")
        pid = self.pick("m1")["id"]
        with self.assertRaises(Refuse) as cm:
            pay.receipt(USER, "uk", {"payment_id": pid, "data": b64(JPG)})
        self.assertEqual(cm.exception.code, "send_failed")
        self.assertEqual(self.payments()[-1][2], "awaiting_receipt")
        # после восстановления тот же файл можно отправить снова
        self.tg.send_photo.side_effect = None
        out = pay.receipt(USER, "uk", {"payment_id": pid, "data": b64(JPG)})
        self.assertEqual(out["status"], "sent")

    def test_card_fallback_text_when_media_fails_but_message_works(self):
        self.tg.send_photo.side_effect = RuntimeError("x")
        self.tg.send_document.side_effect = RuntimeError("x")
        pid = self.pick("m1")["id"]
        out = pay.receipt(USER, "uk", {"payment_id": pid, "data": b64(JPG)})
        self.assertEqual(out["status"], "sent")
        text = self.tg.send_message.call_args.kwargs
        self.assertEqual(text["chat_id"], ADMIN)
        self.assertIn("не вдалося переслати", text["text"])
        self.assertIn("housing:pay:ok:%d" % pid, text["reply_markup"].inline_keyboard[0][0].callback_data)

    def test_foreign_payment_is_refused(self):
        pid = self.pick("m1", FRIEND)["id"]
        with self.assertRaises(Refuse) as cm:
            pay.receipt(USER, "uk", {"payment_id": pid, "data": b64(JPG)})
        self.assertEqual(cm.exception.code, "not_found")
        self.assertEqual(self.admin_calls(), [])
        self.assertEqual(self.payments()[-1][2], "awaiting_receipt")
        for bad in (0, -1, 99999, "abc", None, 1.5 * 0 + 10 ** 12):
            with self.assertRaises(Refuse):
                pay.receipt(USER, "uk", {"payment_id": bad, "data": b64(JPG)})
        self.assertEqual(self.admin_calls(), [])

    def test_closed_old_or_decided_payments_are_refused(self):
        pid = self.pick("m1")["id"]
        pay.cancel(USER, "uk", {})
        with self.assertRaises(Refuse) as cm:            # отменена
            pay.receipt(USER, "uk", {"payment_id": pid, "data": b64(JPG)})
        self.assertEqual(cm.exception.code, "closed")
        first = self.pick("m1")["id"]
        second = self.pick("m3")["id"]
        with self.assertRaises(Refuse):                  # не последний выбор
            pay.receipt(USER, "uk", {"payment_id": first, "data": b64(JPG)})
        s = self.session()
        s.query(HousingPayment).get(second).created_at = datetime.utcnow() - timedelta(days=4)
        s.commit()
        s.close()
        with self.assertRaises(Refuse):                  # окно 3 дня как в чате
            pay.receipt(USER, "uk", {"payment_id": second, "data": b64(JPG)})
        self.assertEqual(self.admin_calls(), [])

    def test_decided_payment_cannot_receive_more(self):
        pid = self.pick("m1")["id"]
        pay.receipt(USER, "uk", {"payment_id": pid, "data": b64(JPG)})
        self.housing_payment.handle_callback(callback("housing:pay:ok:%d" % pid, ADMIN), self.context)
        self.tg.reset_mock()
        with self.assertRaises(Refuse) as cm:
            pay.receipt(USER, "uk", {"payment_id": pid, "data": b64(PNG)})
        self.assertEqual(cm.exception.code, "closed")
        self.assertEqual(self.admin_calls(), [])

    def test_bad_files(self):
        pid = self.pick("m1")["id"]
        cases = [
            ("", "bad_file"), (None, "bad_file"), (123, "bad_file"), ("!!!not base64!!!", "bad_file"),
            (b64(b""), "bad_file"), (b64(b"hello world, not a receipt"), "bad_type"), (b64(b"GIF89a" + b"x" * 30), "bad_type"),
            (b64(b"<html><script>alert(1)</script></html>"), "bad_type"), (b64(b"MZ\x90\x00" + b"x" * 40), "bad_type"),
        ]
        for data, code in cases:
            with self.assertRaises(Refuse, msg=repr(data)[:30]) as cm:
                pay.receipt(USER, "uk", {"payment_id": pid, "data": data})
            self.assertEqual(cm.exception.code, code, repr(data)[:30])
        self.assertEqual(self.admin_calls(), [])
        self.assertEqual(self.payments()[-1][2], "awaiting_receipt")

    def test_too_big_file(self):
        pid = self.pick("m1")["id"]
        with mock.patch.object(pay, "MAX_FILE", 100):
            with self.assertRaises(Refuse) as cm:
                pay.receipt(USER, "uk", {"payment_id": pid, "data": b64(JPG + b"x" * 200)})
        self.assertEqual(cm.exception.code, "too_big")
        self.assertEqual(self.admin_calls(), [])

    def test_max_size_file_fits_into_the_bridge_body_limit(self):
        raw = JPG + b"\0" * (pay.MAX_FILE - len(JPG))
        body = json.dumps({"user_id": 1, "lang": "uk", "params": {"payment_id": 1, "note": "x" * pay.MAX_NOTE, "data": b64(raw)}})
        self.assertLess(len(body.encode()), pay.MAX_BODY)

    def test_same_file_twice_is_refused(self):
        pid = self.pick("m1")["id"]
        pay.receipt(USER, "uk", {"payment_id": pid, "data": b64(JPG)})
        with self.assertRaises(Refuse) as cm:
            pay.receipt(USER, "uk", {"payment_id": pid, "data": b64(JPG)})
        self.assertEqual(cm.exception.code, "duplicate")
        self.assertEqual(len(self.admin_calls()), 1)

    def test_extra_pages_allowed_up_to_a_limit_and_no_second_free_day(self):
        pid = self.pick("m1")["id"]
        pay.receipt(USER, "uk", {"payment_id": pid, "data": b64(JPG)})
        s = self.session()
        until = s.query(HousingPayment).get(pid).provisional_until
        s.close()
        out = pay.receipt(USER, "uk", {"payment_id": pid, "data": b64(PNG)})
        self.assertFalse(out["provisional"])
        pay.receipt(USER, "uk", {"payment_id": pid, "data": b64(PDF)})
        with self.assertRaises(Refuse) as cm:
            pay.receipt(USER, "uk", {"payment_id": pid, "data": b64(PNG + b"1")})
        self.assertEqual(cm.exception.code, "too_many")
        self.assertEqual(len(self.admin_calls()), 3)
        s = self.session()
        self.assertEqual(s.query(HousingPayment).get(pid).provisional_until, until)
        s.close()

    def test_pause_between_receipts(self):
        pid = self.pick("m1")["id"]
        with mock.patch.object(pay, "RECEIPT_GAP", 60.0):
            pay.receipt(USER, "uk", {"payment_id": pid, "data": b64(JPG)})
            with self.assertRaises(Refuse) as cm:
                pay.receipt(USER, "uk", {"payment_id": pid, "data": b64(PNG)})
            self.assertEqual(cm.exception.code, "too_fast")

    def test_concurrent_double_tap_sends_once(self):
        pid = self.pick("m1")["id"]
        results = []

        def go():
            try:
                results.append(pay.receipt(USER, "uk", {"payment_id": pid, "data": b64(JPG)})["status"])
            except Refuse as e:
                results.append(e.code)

        threads = [threading.Thread(target=go) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(results), ["duplicate"] * 3 + ["sent"])
        self.assertEqual(len(self.admin_calls()), 1)

    def test_admin_approval_still_works_on_web_receipt(self):
        pid = self.pick("m3")["id"]
        pay.receipt(USER, "uk", {"payment_id": pid, "data": b64(JPG)})
        before = datetime.utcnow()
        self.housing_payment.handle_callback(callback("housing:pay:ok:%d" % pid, ADMIN), self.context)
        self.assertEqual(self.payments()[-1][2], "approved")
        s = self.session()
        self.assertGreater(s.query(HousingAccessUser).get(USER).expires_at, before + timedelta(days=88))
        s.close()
        self.housing_payment.handle_callback(callback("housing:pay:ok:%d" % pid, ADMIN), self.context)   # повторный тап ничего не добавляет
        s = self.session()
        self.assertLess(s.query(HousingAccessUser).get(USER).expires_at, before + timedelta(days=93))
        s.close()
        self.assertEqual(pay.info(USER, "uk", {})["last"]["status"], "approved")

    def test_user_is_not_messaged_by_the_receipt_step(self):
        pid = self.pick("m1")["id"]
        pay.receipt(USER, "uk", {"payment_id": pid, "data": b64(JPG)})
        for call in self.admin_calls():
            self.assertEqual(call.kwargs.get("chat_id"), ADMIN)


class SupportTests(PayBridgeCase):
    def test_support_info_matches_support_module(self):
        from user_handlers import support
        d = pay.support_info(USER, "uk", {})
        self.assertTrue(d["enabled"])
        self.assertEqual([a["eur"] for a in d["amounts"]], list(support.AMOUNTS_EUR))
        for a in d["amounts"]:
            self.assertEqual(a["uah"], support._uah(a["cents"]))
            self.assertEqual(a["cents"], a["eur"] * 100)
        self.assertEqual(d["custom_max"], support.MAX_CUSTOM_EUR)
        self.assertNotIn("<", d["intro"] + d["title"])
        self.assertIn("Підтримати", d["title"])
        self.assertEqual(d["rate"], "48,30")

    def test_support_info_three_languages_and_disabled(self):
        for lang in ("uk", "ru", "de"):
            self.assertNotIn("[[", json.dumps(pay.support_info(USER, lang, {}), ensure_ascii=False))
        with mock.patch.object(self.housing_payment, "ADMIN_ID", 0):
            d = pay.support_info(USER, "uk", {})
            self.assertFalse(d["enabled"])
            for fn in (pay.support_card, pay.support_done):
                with self.assertRaises(Refuse) as cm:
                    fn(USER, "uk", {"cents": 500})
                self.assertEqual(cm.exception.code, "unavailable")

    def test_support_card_for_button_and_custom_amount(self):
        c = pay.support_card(USER, "uk", {"cents": 500})
        self.assertEqual((c["cents"], c["eur_text"], c["uah"]), (500, "5", 242))      # 5 * 48.3 = 241.5 -> 242
        self.assertEqual(c["card"], "4111 1111 1111 1111")
        self.assertEqual(c["holder"], "Тарасюк")
        c = pay.support_card(USER, "uk", {"text": "4,5 €"})
        self.assertEqual((c["cents"], c["eur_text"]), (450, "4,50"))
        self.assertEqual(self.admin_calls(), [])

    def test_bad_amounts(self):
        for params in ({"cents": 99}, {"cents": 100001}, {"cents": True}, {"cents": "500"}, {"cents": -5}, {"text": "abc"}, {"text": "0,5"}, {"text": "1001"}, {"text": 5}, {}):
            with self.assertRaises(Refuse, msg=str(params)) as cm:
                pay.support_card(USER, "uk", params)
            self.assertEqual(cm.exception.code, "bad_amount")
            with self.assertRaises(Refuse):
                pay.support_done(USER, "uk", params)
        self.assertEqual(self.admin_calls(), [])

    def test_done_sends_the_same_admin_message_as_chat(self):
        out = pay.support_done(USER, "uk", {"cents": 500})
        self.assertIn("Дякуємо", out["thanks"])
        self.assertEqual(self.tg.send_message.call_count, 1)
        kw = self.tg.send_message.call_args.kwargs
        self.assertEqual(kw["chat_id"], ADMIN)
        self.assertIn("Підтримка проєкту", kw["text"])
        self.assertIn("5 € = 242 грн", kw["text"])
        self.assertIn("<code>777</code>", kw["text"])
        self.assertIn("Olena (@olena)", kw["text"])
        # тот же текст, что строит чат
        chat_bot = mock.MagicMock()
        from user_handlers import support
        support._notify_admin_done(chat_bot, SimpleNamespace(id=USER, first_name="Olena", last_name="", username="olena"), 500)
        self.assertEqual(chat_bot.send_message.call_args.kwargs["text"], kw["text"])

    def test_done_rate_limits(self):
        pay.support_done(USER, "uk", {"cents": 100})
        with self.assertRaises(Refuse) as cm:
            pay.support_done(USER, "uk", {"cents": 100})
        self.assertEqual(cm.exception.code, "rate_limit")
        self.assertEqual(self.tg.send_message.call_count, 1)
        with mock.patch.object(pay, "SUPPORT_DONE_GAP", 0.0), mock.patch.object(pay, "SUPPORT_DONE_PER_DAY", 3):
            pay.support_done(USER, "uk", {"cents": 100})
            pay.support_done(USER, "uk", {"cents": 100})
            with self.assertRaises(Refuse):
                pay.support_done(USER, "uk", {"cents": 100})
        self.assertEqual(self.tg.send_message.call_count, 3)
        pay.support_done(FRIEND, "uk", {"cents": 100})       # другой человек не задет
        self.assertEqual(self.tg.send_message.call_count, 4)


class HttpTests(unittest.TestCase):
    """Операции зарегистрированы, секрет и размеры тела как у остального моста."""

    KEY = "p" * 64

    @classmethod
    def setUpClass(cls):
        from user_handlers import housing_web
        site_bridge.load()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), housing_web.Handler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        fd, self.keyfile = tempfile.mkstemp()
        os.write(fd, (self.KEY + "\n").encode())
        os.close(fd)
        self.patch = mock.patch.object(site_bridge, "KEY_FILE", self.keyfile)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        os.remove(self.keyfile)

    def call(self, op, body, key=KEY, headers=None):
        req = urllib.request.Request("http://127.0.0.1:%d/int/pay/%s" % (self.port, op), data=json.dumps(body).encode(), method="POST")
        req.add_header("Content-Type", "application/json")
        if key:
            req.add_header("X-Bridge-Key", key)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def declared_length(self, op, length):
        """Ответ на заголовок с заявленной длиной (тело не шлём: сервер отвечает 413 до чтения)."""
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.putrequest("POST", "/int/pay/" + op)
        conn.putheader("X-Bridge-Key", self.KEY)
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", str(length))
        conn.endheaders()
        if length < 100000:
            conn.send(b" " * length)
        try:
            return conn.getresponse().status
        except Exception:
            return -1
        finally:
            conn.close()

    def test_registered(self):
        names = site_bridge.registered()
        for n in ("pay.info", "pay.create", "pay.cancel", "pay.receipt", "pay.support_info", "pay.support_card", "pay.support_done"):
            self.assertIn(n, names)

    def test_no_secret_wrong_secret_and_proxy_headers(self):
        for op in ("info", "receipt", "create"):
            self.assertEqual(self.call(op, {"user_id": 5}, key=None)[0], 403)
            self.assertEqual(self.call(op, {"user_id": 5}, key="x" * 64)[0], 403)
            self.assertEqual(self.call(op, {"user_id": 5}, headers={"X-Forwarded-For": "1.2.3.4"})[0], 404)

    def test_user_required(self):
        self.assertEqual(self.call("info", {"user_id": 0})[0], 400)
        self.assertEqual(self.call("receipt", {"user_id": 0, "params": {}})[0], 400)

    def test_body_limits(self):
        self.assertEqual(self.declared_length("info", 70000), 413)             # 64 КБ у обычных операций
        self.assertEqual(self.declared_length("receipt", pay.MAX_BODY + 10), 413)
        from user_handlers import housing_payment
        with mock.patch.object(housing_payment, "PAY_CARD", ""):
            status, body = self.call("receipt", {"user_id": 5, "params": {"data": "a" * 200000}})      # больше 64 КБ, но допустимо
        self.assertEqual(status, 422)

    def test_info_for_nonexistent_user_over_http_is_read_only(self):
        # не трогаем настоящую базу: заглушаем чтение, проверяем лишь что операция отвечает JSON
        from user_handlers import housing_payment
        with mock.patch.object(housing_payment, "PAY_CARD", ""), \
                mock.patch.object(housing_payment, "_current_expiry", return_value=None), \
                mock.patch.object(housing_payment, "until_found_expiry", return_value=None), \
                mock.patch("user_jobs.housing_tier.is_premium", return_value=False), \
                mock.patch.object(pay, "_last_payment", return_value=None):
            status, body = self.call("info", {"user_id": 9999999, "lang": "de", "params": {}})
        self.assertEqual(status, 200)
        self.assertFalse(body["data"]["enabled"])


if __name__ == "__main__":
    unittest.main()
