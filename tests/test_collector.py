from dataclasses import replace
from email.message import EmailMessage
from pathlib import Path
from tempfile import TemporaryDirectory
import os
import unittest
from unittest.mock import Mock, patch

from mail_summary_bot.collector import Collector, account_binding
from mail_summary_bot.config import AccountConfig, Config, ServiceConfig, SummaryConfig
from mail_summary_bot.models import MailMessage, PollResult


def mail(account="first", uid=1, epoch=41):
    return MailMessage(
        account, epoch, uid, f"<{account}-{epoch}-{uid}@example.test>",
        "sender@example.test", "Private test subject", "2026-10-03T10:00:00+00:00", "Private test body",
    )


class FakeReader:
    def __init__(self, settings, result=None, error=None):
        self.settings = settings
        self.result = result if result is not None else PollResult(41, 0, [])
        self.error = error
        self.calls = []
        self.windows = []

    def poll(self, checkpoint):
        self.calls.append(checkpoint)
        self.windows.append(self.settings.lookback_days)
        if self.error:
            raise self.error
        return self.result


class BootstrapIMAP:
    """A two-message mailbox with only UID 20 inside the lookback window."""

    def __init__(self):
        message = EmailMessage()
        message["Subject"] = "Test subject"
        message.set_content("Test content")
        self.raw = message.as_bytes()
        self.fetched = []

    def login(self, username, password):
        return "OK", []

    def select(self, mailbox, readonly=False):
        if not readonly:
            raise AssertionError("Mailbox must be read-only")
        return "OK", [b"2"]

    def response(self, name):
        return name, [b"41"]

    def uid(self, command, *args):
        if command == "search":
            return "OK", [b"20" if "SINCE" in args else b"10 20"]
        uid = int(args[0])
        self.fetched.append(uid)
        header = f'{uid} (UID {uid} RFC822.SIZE {len(self.raw)} INTERNALDATE "03-Oct-2026 10:00:00 +0000"'.encode()
        if "BODY.PEEK" in args[1]:
            return "OK", [(header + b" BODY[]", self.raw), b")"]
        return "OK", [header + b")"]

    def logout(self):
        return "BYE", []


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.path = str(Path(self.temp.name) / "state.sqlite3")
        self.accounts = (
            AccountConfig("first", "imap.first.example.test", "first@example.test", "FIRST_TEST_PASSWORD"),
            AccountConfig("second", "imap.second.example.test", "second@example.test", "SECOND_TEST_PASSWORD"),
        )
        self.config = Config(self.accounts[:1], ServiceConfig(database=self.path, poll_seconds=17))
        self.readers = [FakeReader(self.config.service)]
        self.collector = Collector(self.config, readers=self.readers)

    def tearDown(self):
        self.collector.close()
        self.temp.cleanup()

    def replace_collector(self, config, readers=None):
        self.collector.close()
        self.config = config
        self.collector = Collector(config, readers=readers)

    def test_first_bootstrap_new_saves_boundary_without_existing_mail(self):
        self.replace_collector(self.config)
        fake = BootstrapIMAP()
        with patch.dict(os.environ, {"FIRST_TEST_PASSWORD": "test-only"}, clear=True), patch("mail_summary_bot.mail.imaplib.IMAP4_SSL", return_value=fake):
            self.assertTrue(self.collector.poll())
        self.assertEqual(self.collector.store.checkpoint("first", account_binding(self.accounts[0])), (41, 20))
        self.assertEqual(self.collector.store.stats()["pending"], 0)
        self.assertEqual(fake.fetched, [])

    def test_first_bootstrap_lookback_saves_only_recent_mail(self):
        config = replace(self.config, service=replace(self.config.service, bootstrap="lookback"))
        self.replace_collector(config)
        fake = BootstrapIMAP()
        with patch.dict(os.environ, {"FIRST_TEST_PASSWORD": "test-only"}, clear=True), patch("mail_summary_bot.mail.imaplib.IMAP4_SSL", return_value=fake):
            self.assertTrue(self.collector.poll())
        pending = self.collector.store.pending(10, ("first",))
        self.assertEqual([message.uid for _, message in pending], [20])
        self.assertNotIn(10, fake.fetched)

    def test_messages_cursor_and_health_survive_restart(self):
        self.readers[0].result = PollResult(41, 7, [mail(uid=7)])
        with patch("mail_summary_bot.collector.time.time", return_value=1000):
            self.assertTrue(self.collector.poll())
        reader = FakeReader(self.config.service, PollResult(41, 7, []))
        self.replace_collector(self.config, [reader])
        self.assertEqual(self.collector.store.get("health:first"), "ok")
        self.assertEqual(self.collector.store.get("last_poll:first"), "1000")
        self.assertEqual(self.collector.store.stats()["pending"], 1)
        self.assertTrue(self.collector.poll())
        self.assertEqual(reader.calls, [(41, 7)])
        self.assertEqual(self.collector.store.stats()["pending"], 1)

    def test_two_account_failure_is_isolated_and_no_content_is_logged(self):
        config = replace(self.config, accounts=self.accounts)
        readers = [
            FakeReader(config.service, error=RuntimeError("Private test password")),
            FakeReader(config.service, PollResult(41, 3, [mail("second", uid=3)])),
        ]
        self.replace_collector(config, readers)
        with self.assertLogs("mail_summary_bot.collector", level="INFO") as captured:
            self.assertFalse(self.collector.poll())
        logs = "\n".join(captured.output)
        for value in ("Private test password", "Private test subject", "Private test body", "sender@example.test"):
            self.assertNotIn(value, logs)
        self.assertEqual(self.collector.store.get("health:first"), "error")
        self.assertEqual(self.collector.store.get("health:second"), "ok")
        self.assertIsNone(self.collector.store.checkpoint("first", account_binding(self.accounts[0])))
        self.assertEqual(self.collector.store.checkpoint("second", account_binding(self.accounts[1])), (41, 3))
        self.assertEqual(self.collector.store.stats()["pending"], 1)

    def test_failed_poll_keeps_last_success_and_persistent_error_health(self):
        self.readers[0].result = PollResult(41, 4, [mail(uid=4)])
        with patch("mail_summary_bot.collector.time.time", return_value=1000):
            self.assertTrue(self.collector.poll())
        self.readers[0].error = RuntimeError("test failure")
        with self.assertLogs("mail_summary_bot.collector", level="WARNING"):
            self.assertFalse(self.collector.poll())
        self.replace_collector(self.config, [FakeReader(self.config.service)])
        self.assertEqual(self.collector.store.get("last_poll:first"), "1000")
        self.assertEqual(self.collector.store.get("health:first"), "error")
        self.assertEqual(self.collector.store.checkpoint("first", account_binding(self.accounts[0])), (41, 4))

    def test_epoch_recovery_window_covers_downtime_and_restores_settings(self):
        self.collector.store.save_poll("first", account_binding(self.accounts[0]), PollResult(41, 30, []))
        self.collector.store.set("last_poll:first", 1000)
        original = self.readers[0].settings
        self.readers[0].result = PollResult(42, 35, [mail(uid=35, epoch=42)], epoch_changed=True)
        now = 1000 + 5 * 86400 + 3600
        with patch("mail_summary_bot.collector.time.time", return_value=now), self.assertLogs("mail_summary_bot.collector", level="WARNING"):
            self.assertTrue(self.collector.poll())
        self.assertEqual(self.readers[0].calls, [(41, 30)])
        self.assertEqual(self.readers[0].windows, [7])
        self.assertIs(self.readers[0].settings, original)
        self.assertEqual(self.collector.store.get("epoch_notice:first"), "1")
        self.assertEqual(self.collector.store.checkpoint("first", account_binding(self.accounts[0])), (42, 35))

    def test_recovery_window_keeps_larger_configured_lookback(self):
        config = replace(self.config, service=replace(self.config.service, lookback_days=14))
        reader = FakeReader(config.service)
        self.replace_collector(config, [reader])
        self.collector.store.set("last_poll:first", 1000)
        with patch("mail_summary_bot.collector.time.time", return_value=1001):
            self.assertTrue(self.collector.poll())
        self.assertEqual(reader.windows, [14])

    def test_failure_restores_temporary_recovery_settings(self):
        original = self.readers[0].settings
        self.collector.store.set("last_poll:first", 1000)
        self.readers[0].error = RuntimeError("test outage")
        with patch("mail_summary_bot.collector.time.time", return_value=1000 + 8 * 86400), self.assertLogs("mail_summary_bot.collector", level="WARNING"):
            self.assertFalse(self.collector.poll())
        self.assertIs(self.readers[0].settings, original)
        self.assertEqual(self.readers[0].windows, [9])

    def test_collector_does_not_prune_unsent_or_create_outbox(self):
        self.readers[0].result = PollResult(41, 1, [mail()])
        self.collector.store.prune = Mock(side_effect=AssertionError("No pruning in collector"))
        self.assertTrue(self.collector.poll())
        with self.collector.store.db:
            self.collector.store.db.execute("UPDATE messages SET created_at=0")
        self.assertTrue(self.collector.poll())
        self.collector.store.prune.assert_not_called()
        self.assertEqual(self.collector.store.stats()["pending"], 1)
        self.assertIsNone(self.collector.store.outbox())

    def test_collector_works_without_telegram_or_ai_credentials(self):
        config = replace(self.config, summary=SummaryConfig(mode="openai"))
        with patch.dict(os.environ, {}, clear=True):
            self.replace_collector(config, [FakeReader(config.service)])
            self.assertTrue(self.collector.poll())

    def test_run_uses_configured_poll_interval(self):
        with patch.object(self.collector, "poll", return_value=True) as poll, patch("mail_summary_bot.collector.time.sleep", side_effect=KeyboardInterrupt) as sleep:
            with self.assertRaises(KeyboardInterrupt):
                self.collector.run()
        poll.assert_called_once_with()
        sleep.assert_called_once_with(17)

    def test_reader_count_must_match_account_count(self):
        with self.assertRaisesRegex(ValueError, "one reader"):
            Collector(self.config, readers=[])

    def test_close_is_idempotent(self):
        self.collector.close()
        self.collector.close()
        with self.assertRaisesRegex(RuntimeError, "closed"):
            self.collector.poll()


if __name__ == "__main__":
    unittest.main()
