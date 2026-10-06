"""Independent folder cursors with one physical-account delivery identity."""

from dataclasses import replace
from email.message import EmailMessage
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import os
import unittest
from unittest.mock import patch

from mail_summary_bot.collector import Collector
from mail_summary_bot.config import (
    AccountConfig, Config, ConfigError, ServiceConfig, expand_accounts, load_config,
)
from mail_summary_bot.models import MailMessage, PollResult
from mail_summary_bot.service import Service, account_binding
from mail_summary_bot.store import Store


def message(account="first", *, uid=7, epoch=41, message_id="<sample@example.test>", body="Synthetic content"):
    return MailMessage(
        account, epoch, uid, message_id, "sender@example.test", "Synthetic subject",
        "2026-10-03T10:00:00+00:00", body,
    )


class Reader:
    def __init__(self, result=None, error=None):
        self.result = result or PollResult(41, 0, [])
        self.error = error
        self.calls = []

    def poll(self, checkpoint):
        self.calls.append(checkpoint)
        if self.error is not None:
            raise self.error
        return self.result


class Telegram:
    chat_id = 12345

    def __init__(self):
        self.sent = []

    def send_chunk(self, text):
        self.sent.append(text)

    def close(self):
        pass


class Summarizer:
    def __init__(self):
        self.calls = []

    def summarize(self, messages):
        self.calls.append(messages)
        return "Synthetic summary"

    def close(self):
        pass


class MultiFolderConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "config.toml"

    def load(self, additional=None, *, mailbox="INBOX", second=""):
        extra = "" if additional is None else "additional_mailboxes = " + additional + "\n"
        self.path.write_text(
            '[[accounts]]\nid="first"\nhost="imap.example.test"\n'
            'username="first@example.test"\npassword_env="TEST_PASSWORD"\n'
            + "mailbox = " + json.dumps(mailbox) + "\n" + extra + second,
            encoding="utf-8",
        )
        return load_config(self.path, secrets=False)

    def test_legacy_account_has_one_stream_with_unchanged_binding(self):
        account = self.load().accounts[0]
        legacy = AccountConfig("first", "imap.example.test", "first@example.test", "TEST_PASSWORD")
        self.assertEqual(account.additional_mailboxes, ())
        self.assertEqual(expand_accounts((account,)), (legacy,))
        self.assertEqual(account_binding(account), account_binding(legacy))

    def test_additional_folders_expand_in_declared_order_with_shared_credentials(self):
        account = self.load('["Newsletters", "Receipts", "Архив/Важное"]').accounts[0]
        self.assertEqual(account.additional_mailboxes, ("Newsletters", "Receipts", "Архив/Важное"))
        streams = expand_accounts((account,))
        self.assertEqual([s.id for s in streams],
                         ["first", "first/Newsletters", "first/Receipts", "first/Архив/Важное"])
        self.assertEqual([s.mailbox for s in streams], ["INBOX", "Newsletters", "Receipts", "Архив/Важное"])
        for stream in streams:
            self.assertEqual((stream.host, stream.username, stream.password_env, stream.port, stream.timeout_seconds),
                             (account.host, account.username, account.password_env, account.port, account.timeout_seconds))
        self.assertTrue(all(s.additional_mailboxes == () for s in streams[1:]))
        self.assertEqual(account_binding(streams[0]), account_binding(account))

    def test_additional_folders_reject_wrong_types_empty_names_and_control_characters(self):
        for value in ('"Newsletters"', '1', '[1]', '[true]', '[""]', '["  "]',
                      '["News\\nletters"]', '["News\\rletters"]', '["News\\u0000letters"]'):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                self.load(value)

    def test_additional_folders_reject_duplicates_including_primary_inbox(self):
        for value in ('["Newsletters", "Newsletters"]', '["INBOX"]', '["inbox"]', '["Inbox"]'):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                self.load(value)
        with self.assertRaises(ConfigError):
            self.load('["Receipts"]', mailbox="Receipts")

    def test_non_inbox_names_preserve_case_and_spaces(self):
        account = self.load('["Newsletters", "newsletters", "My Receipts"]').accounts[0]
        self.assertEqual([s.mailbox for s in expand_accounts((account,))],
                         ["INBOX", "Newsletters", "newsletters", "My Receipts"])

    def test_primary_folder_rejects_protocol_control_characters(self):
        for name in ("INBOX\rFETCH", "INBOX\nFETCH", "INBOX\0"):
            with self.subTest(name=repr(name)), self.assertRaises(ConfigError):
                self.load(mailbox=name)

    def test_overlapping_folder_across_accounts_is_rejected(self):
        second = ('\n[[accounts]]\nid="second"\nhost="imap.example.test"\n'
                  'username="first@example.test"\npassword_env="TEST_PASSWORD"\nmailbox="Newsletters"\n')
        with self.assertRaises(ConfigError):
            self.load('["Newsletters"]', second=second)


class MultiFolderStoreTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)

    def save(self, mail, *, parent="first", notification=True):
        self.store.save_poll(
            mail.account_id, "binding:" + mail.account_id,
            PollResult(mail.uidvalidity, mail.uid, [mail]),
            identity_account_id=parent,
            notification_parts=(lambda _: ["Synthetic notice"]) if notification else None,
        )

    def test_same_uid_and_epoch_in_two_folders_store_two_distinct_messages(self):
        primary = message(message_id="<primary@example.test>")
        extra = message("first/Newsletters", message_id="<newsletter@example.test>")
        self.save(primary)
        self.save(extra)
        self.assertEqual([m for _, m in self.store.pending(10, (primary.account_id, extra.account_id))], [primary, extra])
        self.assertEqual(self.store.notification_stats()["pending"], 2)

    def test_copied_or_moved_message_is_not_notified_twice_within_one_account(self):
        primary = message()
        copy = replace(primary, account_id="first/Newsletters", uid=80, uidvalidity=99)
        self.save(primary)
        notice = self.store.notification_outbox()
        self.store.notification_part_sent(notice["message_id"])
        self.save(copy)
        self.assertEqual(self.store.stats()["pending"], 1)
        self.assertEqual(self.store.notification_stats(), {"pending": 0, "sent": 1})
        self.assertEqual(self.store.checkpoint(copy.account_id, "binding:" + copy.account_id), (99, 80))

    def test_identical_message_in_separate_physical_accounts_remains_distinct(self):
        first = message("first/Newsletters")
        second = replace(first, account_id="second/Newsletters")
        self.save(first, parent="first")
        self.save(second, parent="second")
        self.assertEqual(self.store.stats()["pending"], 2)
        self.assertEqual(self.store.notification_stats()["pending"], 2)

    def test_missing_message_id_uses_each_folder_uid_identity_without_collision(self):
        first = message(message_id="")
        extra = replace(first, account_id="first/Newsletters")
        for mail in (first, extra, first, extra):
            self.save(mail)
        self.assertEqual(self.store.stats()["pending"], 2)
        self.assertEqual(self.store.notification_stats()["pending"], 2)

    def test_shared_identity_rolls_back_with_failed_notification_formatter(self):
        extra = message("first/Newsletters")
        def failing_formatter(_):
            raise RuntimeError("Synthetic formatter failure")
        with self.assertRaises(RuntimeError):
            self.store.save_poll(extra.account_id, "binding:" + extra.account_id,
                                 PollResult(41, 7, [extra]), identity_account_id="first",
                                 notification_parts=failing_formatter)
        self.assertIsNone(self.store.checkpoint(extra.account_id, "binding:" + extra.account_id))
        self.assertEqual(self.store.stats()["pending"], 0)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM mail_identities").fetchone()[0], 0)
        self.save(replace(extra, account_id="first"))
        self.assertEqual(self.store.notification_stats()["pending"], 1)


class MultiFolderServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / "state.sqlite3")
        self.account = AccountConfig("first", "imap.example.test", "first@example.test", "TEST_PASSWORD",
                                     additional_mailboxes=("Newsletters", "Receipts"))
        self.config = Config((self.account,), ServiceConfig(database=self.path, schedule="manual", notify_new_mail=True))
        self.streams = expand_accounts(self.config.accounts)
        self.readers = [Reader() for _ in self.streams]
        self.telegram = Telegram()
        self.summarizer = Summarizer()
        self.service = Service(self.config, readers=self.readers, telegram=self.telegram, summarizer=self.summarizer)
        self.addCleanup(lambda: self.service.close())

    def test_one_folder_failure_does_not_block_other_folders_or_advance_failed_cursor(self):
        self.readers[0].result = PollResult(41, 7, [message(message_id="<primary@example.test>")])
        self.readers[1].error = RuntimeError("Synthetic IMAP failure")
        self.readers[2].result = PollResult(41, 7, [message("first/Receipts", message_id="<receipt@example.test>")])
        with self.assertLogs("mail_summary_bot.service", level="WARNING"):
            self.service.poll_mail()
        for stream in (self.streams[0], self.streams[2]):
            self.assertEqual(self.service.store.checkpoint(stream.id, account_binding(stream)), (41, 7))
            self.assertEqual(self.service.store.get("folder_health:" + stream.id), "ok")
        self.assertIsNone(self.service.store.checkpoint(self.streams[1].id, account_binding(self.streams[1])))
        self.assertEqual(self.service.store.get("health:first/Newsletters"), "error")
        self.assertEqual(self.service.store.get("health:first"), "error")
        self.assertEqual(self.service.store.stats()["pending"], 2)
        self.assertEqual([r.calls for r in self.readers], [[None], [None], [None]])

    def test_newsletter_enters_notice_status_and_digest_with_parent_account_name(self):
        extra = message("first/Newsletters")
        self.readers[1].result = PollResult(41, 7, [extra])
        with patch("mail_summary_bot.service.time.time", return_value=1000):
            self.service.poll_mail()
            self.assertTrue(self.service.deliver_notifications(1000))
            self.service.request_digest()
            self.assertTrue(self.service.build_digest(1002))
        self.assertIn("Новое письмо · first", self.telegram.sent[0])
        self.assertNotIn("first/Newsletters", self.telegram.sent[0])
        self.assertEqual(self.summarizer.calls, [[replace(extra, account_id="first")]])
        self.assertEqual(self.service.store.stats(), {"pending": 0, "queued": 1, "sent": 0})
        self.assertIn("Ожидают доставки сводки: 1", self.service.status_text())
        self.assertIn("Newsletters", self.service.status_text())
        self.assertNotIn("first/Newsletters", "\n".join(self.service.store.outbox()["parts"]))

    def test_shared_message_in_primary_and_newsletter_has_one_notice_and_one_digest_item(self):
        primary = message()
        self.readers[0].result = PollResult(41, 7, [primary])
        self.readers[1].result = PollResult(41, 85, [replace(primary, account_id="first/Newsletters", uid=85)])
        self.service.poll_mail()
        self.assertEqual(self.service.store.stats()["pending"], 1)
        self.assertEqual(self.service.store.notification_stats()["pending"], 1)
        self.service.request_digest()
        self.assertTrue(self.service.build_digest(1000))
        self.assertEqual(self.summarizer.calls, [[primary]])

    def test_uidvalidity_reset_in_one_folder_keeps_other_folder_cursors(self):
        for stream in self.streams:
            self.service.store.save_poll(stream.id, account_binding(stream), PollResult(41, 700, []))
        self.readers[0].result = PollResult(41, 700, [])
        self.readers[1].result = PollResult(42, 3, [message("first/Newsletters", uid=3, epoch=42)], epoch_changed=True)
        self.readers[2].result = PollResult(41, 700, [])
        with self.assertLogs("mail_summary_bot.service", level="WARNING"):
            self.service.poll_mail()
        self.assertEqual([r.calls for r in self.readers], [[(41, 700)], [(41, 700)], [(41, 700)]])
        self.assertEqual([self.service.store.checkpoint(s.id, account_binding(s)) for s in self.streams],
                         [(41, 700), (42, 3), (41, 700)])
        self.assertEqual(self.service.store.get("epoch_notice:first"), "1")
        self.assertEqual(self.service.store.get("epoch_notice:first/Newsletters"), "1")
        self.assertIsNone(self.service.store.get("epoch_notice:first/Receipts"))
        self.service.request_digest()
        self.assertTrue(self.service.build_digest(1000))
        self.assertIn("изменения идентификаторов почты: first", "\n".join(self.service.store.outbox()["parts"]))
        self.assertEqual(self.service.store.get("epoch_notice:first/Newsletters"), "0")

    def test_existing_primary_cursor_digest_and_notice_survive_adding_folders(self):
        self.service.close()
        primary = replace(self.account, additional_mailboxes=())
        old_store = Store(self.path)
        old_store.save_poll(primary.id, account_binding(primary), PollResult(41, 700, [message(uid=700)]),
                            notification_parts=lambda _: ["Legacy notice part one", "Legacy notice part two"])
        notice = old_store.notification_outbox()
        old_store.notification_part_sent(notice["message_id"])
        source_id = old_store.pending(10, (primary.id,))[0][0]
        old_store.queue_digest([source_id], ["Legacy digest part one", "Legacy digest part two"])
        digest = old_store.outbox()
        old_store.part_sent(digest["id"])
        expected_digest = old_store.outbox()
        expected_notice = old_store.notification_outbox()
        old_store.close()
        self.service = Service(self.config, readers=self.readers, telegram=self.telegram, summarizer=self.summarizer)
        self.assertEqual(self.service.store.checkpoint("first", account_binding(primary)), (41, 700))
        self.assertEqual(self.service.store.outbox(), expected_digest)
        self.assertEqual(self.service.store.notification_outbox(), expected_notice)
        self.readers[0].result = PollResult(41, 700, [])
        self.service.poll_mail()
        self.assertEqual(self.readers[0].calls, [(41, 700)])
        self.assertEqual(self.readers[1].calls, [None])
        self.assertEqual(self.service.store.stats()["queued"], 1)

    def test_sender_exclusions_apply_to_new_and_existing_extra_folder_mail(self):
        extra = replace(message("first/Newsletters"), sender="offers@news.ozon.ru")
        self.service.store.save_poll(extra.account_id, account_binding(self.streams[1]), PollResult(41, 7, [extra]),
                                     identity_account_id="first", notification_parts=lambda _: ["Synthetic notice"])
        self.service.close()
        filtered_config = replace(self.config, service=replace(self.config.service, excluded_sender_domains=("ozon.ru",)))
        self.service = Service(filtered_config, readers=self.readers, telegram=self.telegram, summarizer=self.summarizer)
        self.assertEqual(self.service.store.excluded_count(), 1)
        self.assertEqual(self.service.store.stats()["pending"], 0)
        self.assertIsNone(self.service.store.notification_outbox())
        self.readers[1].result = PollResult(41, 8, [replace(extra, uid=8, message_id="<second@example.test>")])
        self.service.poll_mail()
        self.assertEqual(self.service.store.excluded_count(), 2)
        self.assertEqual(self.service.store.checkpoint(extra.account_id, account_binding(self.streams[1])), (41, 8))


class ReadOnlyIMAP:
    def __init__(self, number):
        mail = EmailMessage()
        mail["Message-ID"] = f"<folder-{number}@example.test>"
        mail["Subject"] = "Synthetic subject"
        mail.set_content("Synthetic body")
        self.raw = mail.as_bytes()
        self.calls = []

    def login(self, username, password):
        return "OK", []

    def select(self, mailbox, readonly=False):
        self.calls.append(("select", mailbox, readonly))
        if readonly is not True:
            raise AssertionError("A folder was opened with write access")
        return "OK", [b"1"]

    def response(self, name):
        return name, [b"41"]

    def uid(self, command, *args):
        self.calls.append((command, *args))
        if command == "search":
            return "OK", [b"7"]
        if command != "fetch":
            raise AssertionError("An unexpected write operation was requested")
        header = f"7 (UID 7 RFC822.SIZE {len(self.raw)}".encode()
        if "BODY.PEEK" in args[1]:
            return "OK", [(header + b" BODY[]", self.raw), b")"]
        if "BODY" in args[1]:
            raise AssertionError("Message fetch can modify seen flags")
        return "OK", [header + b")"]

    def logout(self):
        return "BYE", []


class MultiFolderCollectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        account = AccountConfig("first", "imap.example.test", "first@example.test", "TEST_PASSWORD",
                                additional_mailboxes=("Newsletters", "Receipts"))
        self.config = Config((account,), ServiceConfig(database=str(Path(self.temp.name) / "state.sqlite3"), bootstrap="lookback"))
        self.streams = expand_accounts(self.config.accounts)

    def test_default_collector_reads_every_folder_readonly_without_delivery_clients(self):
        clients = [ReadOnlyIMAP(index) for index in range(3)]
        with patch.dict(os.environ, {"TEST_PASSWORD": "synthetic-password"}, clear=True), \
                patch("mail_summary_bot.mail.imaplib.IMAP4_SSL", side_effect=clients):
            collector = Collector(self.config)
            self.addCleanup(collector.close)
            self.assertTrue(collector.poll())
        self.assertEqual(len(collector.readers), 3)
        self.assertEqual([len([c for c in client.calls if c[0] == "select"]) for client in clients], [1, 1, 1])
        self.assertTrue(all(c[2] is True for client in clients for c in client.calls if c[0] == "select"))
        self.assertTrue(all("BODY.PEEK" in c[2] for client in clients for c in client.calls if c[0] == "fetch" and "BODY" in c[2]))
        for stream in self.streams:
            self.assertEqual(collector.store.checkpoint(stream.id, account_binding(stream)), (41, 7))
            self.assertEqual(collector.store.get("health:" + stream.id), "ok")
        self.assertEqual(collector.store.stats()["pending"], 3)
        self.assertIsNone(collector.store.outbox())
        self.assertIsNone(collector.store.notification_outbox())

    def test_collector_folder_failure_is_isolated_and_other_cursors_survive_restart(self):
        readers = [Reader(error=RuntimeError("Synthetic IMAP outage")),
                   Reader(PollResult(41, 7, [message("first/Newsletters")])),
                   Reader(PollResult(41, 9, [message("first/Receipts", uid=9, message_id="<receipt@example.test>")]))]
        collector = Collector(self.config, readers=readers)
        with self.assertLogs("mail_summary_bot.collector", level="WARNING"):
            self.assertFalse(collector.poll())
        collector.close()
        fresh = [Reader(), Reader(PollResult(41, 7, [])), Reader(PollResult(41, 9, []))]
        collector = Collector(self.config, readers=fresh)
        self.addCleanup(collector.close)
        self.assertEqual(collector.store.get("health:first"), "error")
        self.assertTrue(collector.poll())
        self.assertEqual([r.calls for r in fresh], [[None], [(41, 7)], [(41, 9)]])
        self.assertEqual(collector.store.stats()["pending"], 2)

    def test_injected_readers_must_cover_each_expanded_stream(self):
        with self.assertRaisesRegex(ValueError, "one reader"):
            Collector(self.config, readers=[Reader()])


if __name__ == "__main__":
    unittest.main()
