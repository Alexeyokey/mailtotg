import imaplib
import os
import re
import ssl
import unittest
from dataclasses import replace
from email.message import EmailMessage
from email import policy
from unittest.mock import patch

from mail_summary_bot.config import AccountConfig, ServiceConfig
from mail_summary_bot.mail import (
    BODY_TRUNCATION_NOTICE,
    RAW_TRUNCATION_NOTICE,
    MailReadError,
    MailReader,
    parse_message,
)


def raw_email(text="Обычное письмо", *, html=None):
    message = EmailMessage()
    message["From"] = "Иван <ivan@example.test>"
    message["Subject"] = "Русская тема"
    message["Message-ID"] = "<unique@example.test>"
    message["Date"] = "Thu, 01 Oct 2026 12:00:00 +0300"
    message.set_content(text, charset="utf-8")
    if html is not None:
        message.add_alternative(html, subtype="html", charset="utf-8")
    return message


def parsed(message, **kwargs):
    if isinstance(message, EmailMessage):
        message = message.as_bytes(policy=policy.SMTP)
    return parse_message(
        message, account_id="personal", uidvalidity=7, uid=11,
        max_body_chars=kwargs.pop("max_body_chars", 12000), **kwargs,
    )


class MIMEParsingTests(unittest.TestCase):
    def test_russian_headers_koi8r_body_and_plain_alternative(self):
        message = raw_email()
        message.set_content("Привет, мир!", charset="koi8-r")
        message.add_alternative("<p>HTML вместо текста</p>", subtype="html")
        message.add_attachment("Секрет вложения", filename="note.txt", charset="utf-8")
        result = parsed(message)
        self.assertEqual(result.subject, "Русская тема")
        self.assertEqual(result.sender, "Иван <ivan@example.test>")
        self.assertEqual(result.body, "Привет, мир!")
        self.assertEqual(result.date, "2026-10-01T12:00:00+03:00")
        self.assertEqual(result.message_id, "<unique@example.test>")

    def test_html_strips_scripts_styles_remote_images_and_keeps_visible_text(self):
        message = EmailMessage()
        message.set_content(
            '<html><head><title>Hidden title</title><style>body { secret: 1 }</style></head>'
            '<body><p>Привет &amp; мир</p><script>stealPassword()</script>'
            '<img src="https://tracker.test/pixel" alt="tracking secret">'
            '<p><a href="https://site.test/">Видимая ссылка</a><br>Конец</p></body></html>',
            subtype="html", charset="utf-8",
        )
        result = parsed(message)
        self.assertIn("Привет & мир", result.body)
        self.assertIn("Видимая ссылка", result.body)
        self.assertIn("Конец", result.body)
        for excluded in ("stealPassword", "Hidden title", "secret", "https://", "tracker"):
            self.assertNotIn(excluded, result.body)

    def test_inline_text_file_and_attached_message_are_excluded(self):
        message = raw_email("Основной текст")
        message.add_attachment("Не включать", filename="inline.txt", disposition="inline")
        message.add_attachment(raw_email("Вложенное письмо"))
        self.assertEqual(parsed(message).body, "Основной текст")

    def test_empty_plain_alternative_falls_back_to_html(self):
        self.assertEqual(parsed(raw_email("", html="<p>Полезный HTML</p>")).body, "Полезный HTML")

    def test_plain_alternative_is_preferred_inside_a_nested_multipart(self):
        message = EmailMessage()
        message.make_alternative()
        html = EmailMessage()
        html.set_content("<p>HTML вариант</p>", subtype="html")
        nested = EmailMessage()
        nested.make_related()
        plain = EmailMessage()
        plain.set_content("Русский текстовый вариант")
        nested.attach(plain)
        message.attach(html)
        message.attach(nested)
        self.assertEqual(parsed(message).body, "Русский текстовый вариант")

    def test_unknown_charset_and_invalid_date_are_handled(self):
        message = (
            b"Subject: =?utf-8?b?0KLQtdGB0YI=?=\r\n"
            b"Date: invalid\r\nContent-Type: text/plain; charset=unknown-codec\r\n\r\n"
            + "Содержимое".encode("utf-8")
        )
        result = parsed(message, internaldate="01-Oct-2026 10:00:00 +0000")
        self.assertEqual(result.subject, "Тест")
        self.assertEqual(result.body, "Содержимое")
        self.assertEqual(result.date, "2026-10-01T10:00:00+00:00")

    def test_imap_internaldate_with_hyphenated_english_month(self):
        message = raw_email()
        del message["Date"]
        result = parsed(message, internaldate="03-Oct-2026 10:00:00 +0000")
        self.assertEqual(result.date, "2026-10-03T10:00:00+00:00")

    def test_body_limit_includes_explicit_truncation_notice(self):
        result = parsed(raw_email("А" * 500), max_body_chars=100)
        self.assertLessEqual(len(result.body), 100)
        self.assertIn(BODY_TRUNCATION_NOTICE, result.body)
        self.assertIn("А", result.body)

    def test_raw_limit_notice_is_visible_even_without_text(self):
        self.assertIn(RAW_TRUNCATION_NOTICE, parsed(b"", truncated=True).body)


class FakeIMAP:
    def __init__(self, *, epoch=7, messages=None, recent=None, failed_uid=None, auth_error=False):
        self.epoch = epoch
        self.messages = messages or {}
        self.recent = list(self.messages) if recent is None else recent
        self.failed_uid = failed_uid
        self.auth_error = auth_error
        self.calls = []
        self.logged_out = False

    def login(self, username, password):
        self.calls.append(("login", username))
        if self.auth_error:
            raise imaplib.IMAP4.error("Server leaked password: PRIVATE_SECRET")
        return "OK", [b"success"]

    def select(self, mailbox, readonly=False):
        self.calls.append(("select", mailbox, readonly))
        return "OK", [str(len(self.messages)).encode()]

    def response(self, name):
        self.calls.append(("response", name))
        return name, [str(self.epoch).encode()]

    def uid(self, command, *args):
        self.calls.append((command, *args))
        if command == "search":
            start = int(args[2].split(":")[0])
            pool = self.recent if "SINCE" in args else list(self.messages)
            values = [uid for uid in pool if uid >= start]
            # Model RFC 3501 reversed N:* range behavior on an empty new range.
            if not values and pool and start > max(pool):
                values = [max(pool)]
            return "OK", [" ".join(map(str, reversed(values))).encode()]
        uid = int(args[0])
        if uid == self.failed_uid:
            return "NO", [b"failure PRIVATE_SECRET"]
        raw = self.messages[uid]
        header = (
            f'{uid} (UID {uid} RFC822.SIZE {len(raw)} '
            'INTERNALDATE "01-Oct-2026 10:00:00 +0000"'
        ).encode()
        if "BODY.PEEK" not in args[1]:
            return "OK", [header + b")"]
        count = int(re.search(r"<0\.(\d+)>", args[1]).group(1))
        data = raw[:count]
        return "OK", [(header + f" BODY[]<0> {{{len(data)}}}".encode(), data), b")"]

    def logout(self):
        self.logged_out = True
        return "BYE", []


class IMAPPollingTests(unittest.TestCase):
    def setUp(self):
        self.account = AccountConfig("personal", "imap.example.test", "user", "TEST_MAIL_PASSWORD")
        self.settings = ServiceConfig()
        self.raw = raw_email().as_bytes(policy=policy.SMTP)
        self.environment = patch.dict(os.environ, {"TEST_MAIL_PASSWORD": "PRIVATE_SECRET"})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def poll(self, fake, checkpoint=None, settings=None):
        with patch("mail_summary_bot.mail.imaplib.IMAP4_SSL", return_value=fake) as factory:
            result = MailReader(self.account, settings or self.settings).poll(checkpoint)
        context = factory.call_args.kwargs["ssl_context"]
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        self.assertEqual(factory.call_args.kwargs["timeout"], 30)
        self.assertTrue(fake.logged_out)
        self.assertIn(("select", "INBOX", True), fake.calls)
        return result

    def test_new_bootstrap_captures_max_uid_without_fetching(self):
        fake = FakeIMAP(messages={2: self.raw, 100: self.raw})
        result = self.poll(fake)
        self.assertEqual((result.uidvalidity, result.last_uid, result.messages), (7, 100, []))
        self.assertFalse(result.epoch_changed)
        self.assertFalse(any(call[0] == "fetch" for call in fake.calls))

    def test_empty_mailbox_bootstrap(self):
        self.assertEqual(self.poll(FakeIMAP()).last_uid, 0)

    def test_incremental_pagination_is_sorted_and_does_not_skip_backlog(self):
        settings = replace(self.settings, max_messages_per_poll=2)
        fake = FakeIMAP(messages={10: self.raw, 13: self.raw, 18: self.raw, 21: self.raw})
        first = self.poll(fake, (7, 10), settings)
        self.assertEqual([message.uid for message in first.messages], [13, 18])
        self.assertEqual(first.last_uid, 18)
        self.assertFalse(first.epoch_changed)
        second_fake = FakeIMAP(messages=fake.messages)
        second = self.poll(second_fake, (first.uidvalidity, first.last_uid), settings)
        self.assertEqual([message.uid for message in second.messages], [21])
        self.assertIn(("search", None, "UID", "19:*"), second_fake.calls)
        body_requests = [call for call in fake.calls if call[0] == "fetch" and "BODY" in call[2]]
        self.assertTrue(body_requests)
        self.assertTrue(all("BODY.PEEK[]<0." in call[2] for call in body_requests))

    def test_uid_star_edge_case_does_not_repeat_last_message(self):
        fake = FakeIMAP(messages={22: self.raw})
        result = self.poll(fake, (7, 22))
        self.assertEqual(result.messages, [])
        self.assertEqual(result.last_uid, 22)
        self.assertFalse(any(call[0] == "fetch" for call in fake.calls))

    def test_lookback_first_page_retains_remaining_uids(self):
        settings = replace(self.settings, bootstrap="lookback", max_messages_per_poll=1)
        fake = FakeIMAP(messages={3: self.raw, 10: self.raw, 20: self.raw}, recent=[10, 20])
        first = self.poll(fake, settings=settings)
        self.assertEqual([message.uid for message in first.messages], [10])
        self.assertEqual(first.last_uid, 10)
        searches = [call for call in fake.calls if call[0] == "search"]
        self.assertTrue(any("SINCE" in call for call in searches))
        second = self.poll(FakeIMAP(messages=fake.messages), (7, first.last_uid), settings)
        self.assertEqual([message.uid for message in second.messages], [20])

    def test_empty_lookback_does_not_import_old_mail_on_second_poll(self):
        settings = replace(self.settings, bootstrap="lookback")
        fake = FakeIMAP(messages={100: self.raw}, recent=[])
        first = self.poll(fake, settings=settings)
        self.assertEqual((first.last_uid, first.messages), (100, []))
        second = self.poll(FakeIMAP(messages=fake.messages), (7, first.last_uid), settings)
        self.assertEqual(second.messages, [])

    def test_empty_lookback_boundary_is_captured_before_new_arrival(self):
        settings = replace(self.settings, bootstrap="lookback")
        fake = FakeIMAP(messages={100: self.raw}, recent=[])
        original = fake.uid

        def arriving_message(command, *args):
            response = original(command, *args)
            if command == "search" and "SINCE" not in args:
                fake.messages[101] = self.raw
            return response

        fake.uid = arriving_message
        first = self.poll(fake, settings=settings)
        self.assertEqual(first.last_uid, 100)
        second = self.poll(FakeIMAP(messages=fake.messages), (7, 100), settings)
        self.assertEqual([message.uid for message in second.messages], [101])

    def test_uidvalidity_reset_recovers_lookback_despite_new_bootstrap_setting(self):
        fake = FakeIMAP(epoch=8, messages={2: self.raw, 5: self.raw}, recent=[5])
        result = self.poll(fake, (7, 900))
        self.assertEqual((result.uidvalidity, result.last_uid), (8, 5))
        self.assertEqual([message.uid for message in result.messages], [5])
        self.assertTrue(result.epoch_changed)
        self.assertTrue(any(call[0] == "search" and "SINCE" in call for call in fake.calls))

    def test_uidvalidity_reset_applies_lookback_bootstrap(self):
        settings = replace(self.settings, bootstrap="lookback")
        result = self.poll(FakeIMAP(epoch=8, messages={2: self.raw}), (7, 900), settings)
        self.assertEqual([message.uid for message in result.messages], [2])
        self.assertEqual(result.uidvalidity, 8)
        self.assertTrue(result.epoch_changed)

    def test_uidvalidity_reset_recovery_preserves_paginated_backlog(self):
        settings = replace(self.settings, max_messages_per_poll=1)
        fake = FakeIMAP(epoch=8, messages={2: self.raw, 5: self.raw}, recent=[2, 5])
        first = self.poll(fake, (7, 900), settings)
        self.assertEqual([message.uid for message in first.messages], [2])
        self.assertEqual(first.last_uid, 2)
        self.assertTrue(first.epoch_changed)
        second = self.poll(FakeIMAP(epoch=8, messages=fake.messages), (8, 2), settings)
        self.assertEqual([message.uid for message in second.messages], [5])
        self.assertFalse(second.epoch_changed)

    def test_empty_uidvalidity_reset_recovery_establishes_new_boundary(self):
        result = self.poll(FakeIMAP(epoch=8, messages={5: self.raw}, recent=[]), (7, 900))
        self.assertEqual((result.uidvalidity, result.last_uid, result.messages), (8, 5, []))
        self.assertTrue(result.epoch_changed)

    def test_fetch_failure_raises_without_skipping_later_uids(self):
        fake = FakeIMAP(messages={11: self.raw, 12: self.raw, 13: self.raw}, failed_uid=12)
        with self.assertRaises(MailReadError) as error:
            self.poll(fake, (7, 10))
        self.assertNotIn("PRIVATE_SECRET", str(error.exception))
        self.assertFalse(any(call[0] == "fetch" and call[1] == "13" for call in fake.calls))
        self.assertTrue(fake.logged_out)
        # Retrying the unchanged persisted cursor imports the failed UID as well.
        result = self.poll(FakeIMAP(messages=fake.messages), (7, 10))
        self.assertEqual([message.uid for message in result.messages], [11, 12, 13])

    def test_oversized_email_uses_partial_fetch_and_explicit_notice(self):
        settings = replace(self.settings, max_email_bytes=400)
        message = raw_email("Небольшой полезный текст")
        message.add_attachment(b"a" * 100000, maintype="application", subtype="octet-stream", filename="big.bin")
        fake = FakeIMAP(messages={11: message.as_bytes(policy=policy.SMTP)})
        result = self.poll(fake, (7, 10), settings)
        self.assertIn(RAW_TRUNCATION_NOTICE, result.messages[0].body)
        self.assertIn(("fetch", "11", "(UID INTERNALDATE RFC822.SIZE BODY.PEEK[]<0.400>)"), fake.calls)

    def test_short_body_response_does_not_advance_checkpoint(self):
        fake = FakeIMAP(messages={11: self.raw})
        original = fake.uid

        def short_response(command, *args):
            status, data = original(command, *args)
            if command == "fetch" and "BODY.PEEK" in args[1]:
                data[0] = (data[0][0], data[0][1][:-10])
            return status, data

        fake.uid = short_response
        with self.assertRaises(MailReadError):
            self.poll(fake, (7, 10))

    def test_body_fetch_current_size_overrides_stale_larger_metadata_size(self):
        fake = FakeIMAP(messages={11: self.raw})
        original = fake.uid

        def stale_metadata(command, *args):
            status, data = original(command, *args)
            if command == "fetch" and "BODY.PEEK" not in args[1]:
                data[0] = data[0].replace(
                    f"RFC822.SIZE {len(self.raw)}".encode(),
                    f"RFC822.SIZE {len(self.raw) + 100}".encode(),
                )
            return status, data

        fake.uid = stale_metadata
        result = self.poll(fake, (7, 10))
        self.assertEqual(result.last_uid, 11)
        self.assertEqual(result.messages[0].body, "Обычное письмо")
        self.assertNotIn(RAW_TRUNCATION_NOTICE, result.messages[0].body)

    def test_body_fetch_current_larger_size_still_rejects_incomplete_content(self):
        fake = FakeIMAP(messages={11: self.raw})
        original = fake.uid

        def larger_current_size(command, *args):
            status, data = original(command, *args)
            if command == "fetch" and "BODY.PEEK" in args[1]:
                header, payload = data[0]
                header = header.replace(
                    f"RFC822.SIZE {len(self.raw)}".encode(),
                    f"RFC822.SIZE {len(self.raw) + 10}".encode(),
                )
                data[0] = (header, payload)
            return status, data

        fake.uid = larger_current_size
        with self.assertRaisesRegex(MailReadError, "content was incomplete"):
            self.poll(fake, (7, 10))

    def test_body_without_current_size_uses_metadata_for_completeness(self):
        for shortened in (False, True):
            with self.subTest(shortened=shortened):
                fake = FakeIMAP(messages={11: self.raw})
                original = fake.uid

                def missing_current_size(command, *args):
                    status, data = original(command, *args)
                    if command == "fetch" and "BODY.PEEK" in args[1]:
                        header, payload = data[0]
                        header = re.sub(rb"RFC822\.SIZE \d+ ?", b"", header)
                        data[0] = (header, payload[:-10] if shortened else payload)
                    return status, data

                fake.uid = missing_current_size
                if shortened:
                    with self.assertRaisesRegex(MailReadError, "content was incomplete"):
                        self.poll(fake, (7, 10))
                else:
                    self.assertEqual(self.poll(fake, (7, 10)).last_uid, 11)

    def test_body_literal_can_precede_uid_and_size_in_same_fetch_response(self):
        fake = FakeIMAP(messages={11: self.raw})
        original = fake.uid

        def trailing_metadata(command, *args):
            status, data = original(command, *args)
            if command == "fetch" and "BODY.PEEK" in args[1]:
                payload = data[0][1]
                data = [
                    (f"11 (BODY[]<0> {{{len(payload)}}}".encode(), payload),
                    f" UID 11 RFC822.SIZE {len(payload)})".encode(),
                ]
            return status, data

        fake.uid = trailing_metadata
        result = self.poll(fake, (7, 10))
        self.assertEqual(result.messages[0].body, "Обычное письмо")
        self.assertEqual(result.last_uid, 11)

    def test_unsolicited_other_uid_body_is_not_assigned_to_requested_uid(self):
        fake = FakeIMAP(messages={11: self.raw})
        original = fake.uid

        def unrelated_body(command, *args):
            status, data = original(command, *args)
            if command == "fetch" and "BODY.PEEK" in args[1]:
                data = [
                    (f"12 (BODY[]<0> {{{len(self.raw)}}}".encode(), self.raw),
                    f" UID 12 RFC822.SIZE {len(self.raw)})".encode(),
                    f"11 (UID 11 RFC822.SIZE {len(self.raw)})".encode(),
                ]
            return status, data

        fake.uid = unrelated_body
        with self.assertRaisesRegex(MailReadError, "content was not returned"):
            self.poll(fake, (7, 10))

    def test_unsolicited_other_uid_size_does_not_override_requested_body_size(self):
        fake = FakeIMAP(messages={11: self.raw})
        original = fake.uid
        other = raw_email("Другое письмо").as_bytes(policy=policy.SMTP)

        def other_response_before_and_after(command, *args):
            status, data = original(command, *args)
            if command == "fetch" and "BODY.PEEK" in args[1]:
                data = [
                    (f"12 (BODY[]<0> {{{len(other)}}}".encode(), other),
                    f" UID 12 RFC822.SIZE {len(other) + 100})".encode(),
                    (f"11 (BODY[]<0> {{{len(self.raw)}}}".encode(), self.raw),
                    f" UID 11 RFC822.SIZE {len(self.raw)})".encode(),
                    b"13 (UID 13 RFC822.SIZE 99999 FLAGS ())",
                ]
            return status, data

        fake.uid = other_response_before_and_after
        result = self.poll(fake, (7, 10))
        self.assertEqual(result.messages[0].body, "Обычное письмо")
        self.assertNotIn(RAW_TRUNCATION_NOTICE, result.messages[0].body)

    def test_authentication_errors_never_expose_server_reply(self):
        fake = FakeIMAP(auth_error=True)
        with self.assertRaises(MailReadError) as error:
            self.poll(fake)
        self.assertNotIn("PRIVATE_SECRET", str(error.exception))
        self.assertTrue(fake.logged_out)

    def test_missing_password_uses_safe_error(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(MailReadError, "environment variable is missing"):
                MailReader(self.account, self.settings).poll(None)


class MailboxSelectionTests(unittest.TestCase):
    def test_configured_folder_is_encoded_quoted_and_opened_readonly(self):
        cases = (
            ("INBOX", "INBOX"),
            ("inbox", "inbox"),
            ("Newsletters", '"Newsletters"'),
            ("My Receipts", '"My Receipts"'),
            ('Team "Receipts"\\Archive', r'"Team \"Receipts\"\\Archive"'),
            ("Рассылки", '"&BCAEMARBBEEESwQ7BDoEOA-"'),
            ("台北", '"&U,BTFw-"'),
            ("INBOX/台北 & Archive", '"INBOX/&U,BTFw- &- Archive"'),
            ("&U,BTFw-", '"&U,BTFw-"'),
            ("A&-B", '"A&-B"'),
        )
        for mailbox, expected in cases:
            with self.subTest(mailbox=mailbox):
                account = AccountConfig("personal", "imap.example.test", "user", "TEST_MAIL_PASSWORD", mailbox=mailbox)
                fake = FakeIMAP()
                with patch.dict(os.environ, {"TEST_MAIL_PASSWORD": "synthetic-password"}, clear=True), \
                        patch("mail_summary_bot.mail.imaplib.IMAP4_SSL", return_value=fake):
                    MailReader(account, ServiceConfig()).poll(None)
                self.assertIn(("select", expected, True), fake.calls)
                self.assertTrue(fake.logged_out)
                expected.encode("ascii")

    def test_invalid_folder_never_reaches_the_imap_server(self):
        for mailbox in ("", " ", None, 7, "INBOX\rFETCH", "INBOX\nFETCH", "INBOX\0", "INBOX\t", "INBOX\x7f"):
            with self.subTest(mailbox=repr(mailbox)):
                account = AccountConfig("personal", "imap.example.test", "user", "TEST_MAIL_PASSWORD", mailbox=mailbox)
                with patch.dict(os.environ, {"TEST_MAIL_PASSWORD": "synthetic-password"}, clear=True), \
                        patch("mail_summary_bot.mail.imaplib.IMAP4_SSL") as factory:
                    with self.assertRaises(MailReadError):
                        MailReader(account, ServiceConfig()).poll(None)
                factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
