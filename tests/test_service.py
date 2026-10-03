"""Service behaviour with fake readers, Telegram, and summarization."""

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from mail_summary_bot.config import AccountConfig, Config, ServiceConfig
from mail_summary_bot.models import MailMessage, PollResult
from mail_summary_bot.service import Service, account_binding, next_daily
from mail_summary_bot.store import Store
from mail_summary_bot.telegram import TelegramError


OWNER = 12345


def mail(account="first", uid=1, epoch=41):
    return MailMessage(account, epoch, uid, f"<{account}-{uid}>", "sender@example.org", "Консультация", "2026-10-03T09:00:00+03:00", "Консультация завтра в 15:00.")


def update(update_id, text, *, chat_id=OWNER, chat_type="private"):
    return {"update_id": update_id, "message": {"chat": {"id": chat_id, "type": chat_type}, "from": {"id": chat_id}, "text": text}}


class FakeReader:
    def __init__(self, result=None, error=None):
        self.result = result if result is not None else PollResult(41, 0, [])
        self.error = error
        self.calls = []

    def poll(self, checkpoint):
        self.calls.append(checkpoint)
        if self.error:
            raise self.error
        return self.result


class FakeTelegram:
    chat_id = OWNER

    def __init__(self, updates=None, outcomes=None):
        self.updates = updates or []
        self.outcomes = list(outcomes or [])
        self.offsets = []
        self.attempts = []
        self.sent = []
        self.replies = []
        self.timeouts = []
        self.events = []

    def get_updates(self, offset, *, timeout=None):
        self.offsets.append(offset)
        self.timeouts.append(timeout)
        self.events.append(("get_updates", offset, timeout))
        return self.updates

    def send_chunk(self, text):
        self.attempts.append(text)
        self.events.append(("send_chunk", text))
        outcome = self.outcomes.pop(0) if self.outcomes else None
        if outcome:
            raise outcome
        self.sent.append(text)

    def send_text(self, text):
        self.replies.append(text)

    def close(self):
        pass


class FakeSummarizer:
    def __init__(self, text="Краткая сводка", error=None):
        self.text = text
        self.error = error
        self.calls = []

    def summarize(self, messages):
        self.calls.append(messages)
        if self.error:
            raise self.error
        return self.text

    def close(self):
        pass


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / "state.sqlite3")
        self.accounts = (
            AccountConfig("first", "imap.first.example", "first@example.org", "FIRST_TEST_PASSWORD"),
            AccountConfig("second", "imap.second.example", "second@example.org", "SECOND_TEST_PASSWORD"),
        )
        self.config = Config(self.accounts, ServiceConfig(database=self.path, schedule="manual"))
        self.store = Store(self.path)
        self.readers = [FakeReader(), FakeReader()]
        self.telegram = FakeTelegram()
        self.summarizer = FakeSummarizer()
        self.service = self.make_service()

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def make_service(self):
        return Service(self.config, store=self.store, readers=self.readers, telegram=self.telegram, summarizer=self.summarizer)

    def save_mail(self, message):
        account = next(a for a in self.accounts if a.id == message.account_id)
        self.store.save_poll(account.id, account_binding(account), PollResult(message.uidvalidity, message.uid, [message]))

    def enable_notifications(self):
        self.config = replace(self.config, service=replace(self.config.service, notify_new_mail=True))
        self.service.config = self.config

    def reopen_service(self):
        self.service.close()
        self.store = Store(self.path)
        self.telegram = FakeTelegram()
        self.service = self.make_service()

    def test_discovered_mail_is_notified_and_remains_pending_for_digest(self):
        self.enable_notifications()
        discovered = mail(uid=7)
        self.readers[0].result = PollResult(41, 7, [discovered])
        with patch("mail_summary_bot.service.time.time", return_value=1000):
            self.service.poll_mail()
            self.assertTrue(self.service.deliver_notifications(1000))
        self.assertEqual(len(self.telegram.sent), 1)
        self.assertIn(discovered.sender, self.telegram.sent[0])
        self.assertIn(discovered.subject, self.telegram.sent[0])
        self.assertIn(discovered.body, self.telegram.sent[0])
        self.assertIsNone(self.store.notification_outbox())
        self.assertEqual(self.store.stats(), {"pending": 1, "queued": 0, "sent": 0})
        self.assertEqual(self.summarizer.calls, [])

    def test_notifications_are_disabled_by_default(self):
        self.readers[0].result = PollResult(41, 7, [mail(uid=7)])
        self.service.poll_mail()
        self.assertIsNone(self.store.notification_outbox())
        self.assertFalse(self.service.deliver_notifications(1000))
        self.assertEqual(self.telegram.sent, [])
        self.assertEqual(self.store.stats()["pending"], 1)

    def test_repeated_imap_results_do_not_replay_notification(self):
        self.enable_notifications()
        self.readers[0].result = PollResult(41, 7, [mail(uid=7)])
        with patch("mail_summary_bot.service.time.time", return_value=1000):
            self.service.poll_mail()
            self.service.poll_mail()
            self.assertTrue(self.service.deliver_notifications(1000))
            self.service.poll_mail()
        self.assertFalse(self.service.deliver_notifications(1002))
        self.assertEqual(len(self.telegram.sent), 1)
        self.assertEqual(self.store.stats()["pending"], 1)

    def test_enabling_notifications_does_not_replay_existing_digest_backlog(self):
        old = replace(mail(uid=1), subject="Уже сохранённое письмо")
        new = replace(mail(uid=2), subject="Впервые обнаруженное письмо")
        self.save_mail(old)
        self.enable_notifications()
        self.readers[0].result = PollResult(41, 2, [old, new])
        with patch("mail_summary_bot.service.time.time", return_value=1000):
            self.service.poll_mail()
            self.assertTrue(self.service.deliver_notifications(1000))
        self.assertIn(new.subject, self.telegram.sent[0])
        self.assertNotIn(old.subject, self.telegram.sent[0])
        self.assertIsNone(self.store.notification_outbox())
        self.assertEqual(self.store.stats()["pending"], 2)

    def test_successful_notification_does_not_remove_mail_from_daily_digest(self):
        self.enable_notifications()
        discovered = mail(uid=7)
        self.readers[0].result = PollResult(41, 7, [discovered])
        with patch("mail_summary_bot.service.time.time", return_value=1000):
            self.service.poll_mail()
            self.assertTrue(self.service.deliver_notifications(1000))
            self.service.request_digest()
            self.assertTrue(self.service.build_digest(1000))
        self.assertEqual(self.summarizer.calls, [[discovered]])
        self.assertFalse(self.service.deliver(1001))
        with patch("mail_summary_bot.service.time.time", return_value=1002):
            self.assertTrue(self.service.deliver(1002))
        self.assertEqual(len(self.telegram.sent), 2)
        self.assertIn("Сводка почты", self.telegram.sent[1])
        self.assertEqual(self.store.stats(), {"pending": 0, "queued": 0, "sent": 1})

    def test_six_am_moscow_digest_remains_scheduled_when_notifications_are_enabled(self):
        self.enable_notifications()
        self.config = replace(self.config, service=replace(self.config.service, schedule="daily", digest_time="06:00", timezone="Europe/Moscow"))
        self.service.config = self.config
        due = datetime(2026, 10, 4, 3, 0, tzinfo=timezone.utc).timestamp()
        tomorrow = datetime(2026, 10, 5, 3, 0, tzinfo=timezone.utc).timestamp()
        self.store.set("next_due", due)
        discovered = mail(uid=7)
        self.readers[0].result = PollResult(41, 7, [discovered])
        with patch("mail_summary_bot.service.time.time", return_value=due), patch("mail_summary_bot.service.time.monotonic", return_value=100):
            self.service.tick(poll_commands=False)
        self.assertEqual(float(self.store.get("next_due")), tomorrow)
        self.assertEqual(self.summarizer.calls, [[discovered]])
        self.assertEqual(len(self.telegram.sent), 1)
        self.assertIn("Новое письмо", self.telegram.sent[0])
        self.assertIsNotNone(self.store.outbox())
        with patch("mail_summary_bot.service.time.time", return_value=due + 2), patch("mail_summary_bot.service.time.monotonic", return_value=102):
            self.service.tick(poll_commands=False)
        self.assertEqual(len(self.telegram.sent), 2)
        self.assertIn("Сводка почты · 04.10.2026 06:00", self.telegram.sent[1])
        self.assertEqual(self.store.stats()["sent"], 1)

    def test_failed_notification_survives_restart_and_respects_retry_delay(self):
        self.enable_notifications()
        self.readers[0].result = PollResult(41, 7, [mail(uid=7)])
        self.telegram.outcomes = [TelegramError("private-notice-content", retry_after=30)]
        with patch("mail_summary_bot.service.time.time", return_value=1000):
            self.service.poll_mail()
            with self.assertLogs("mail_summary_bot.service", level="WARNING") as logs:
                self.assertFalse(self.service.deliver_notifications(1000))
        self.assertNotIn("private-notice-content", "\n".join(logs.output))
        self.assertEqual(self.store.stats()["pending"], 1)
        self.reopen_service()
        self.assertFalse(self.service.deliver_notifications(1029))
        self.assertEqual(self.telegram.attempts, [])
        with patch("mail_summary_bot.service.time.time", return_value=1031):
            self.assertTrue(self.service.deliver_notifications(1031))
        self.assertEqual(len(self.telegram.sent), 1)
        self.assertIsNone(self.store.notification_outbox())
        self.assertEqual(self.store.stats()["pending"], 1)

    def test_notification_retry_does_not_block_ready_digest(self):
        self.enable_notifications()
        self.readers[0].result = PollResult(41, 7, [mail(uid=7)])
        self.telegram.outcomes = [TelegramError("test failure", retry_after=30)]
        with patch("mail_summary_bot.service.time.time", return_value=1000):
            self.service.poll_mail()
            with self.assertLogs("mail_summary_bot.service", level="WARNING"):
                self.assertFalse(self.service.deliver_notifications(1000))
            self.service.request_digest()
            self.assertTrue(self.service.build_digest(1000))
        with patch("mail_summary_bot.service.time.time", return_value=1002):
            self.assertFalse(self.service.deliver_notifications(1002))
            self.assertTrue(self.service.deliver(1002))
        self.assertIsNotNone(self.store.notification_outbox())
        self.assertEqual(self.store.stats()["sent"], 1)

    def test_first_discovered_notice_is_sent_before_command_poll_and_remaining_queue_uses_zero_timeout(self):
        self.enable_notifications()
        self.readers[0].result = PollResult(41, 2, [mail(uid=1), mail(uid=2)])
        with patch("mail_summary_bot.service.time.time", return_value=1000), patch("mail_summary_bot.service.time.monotonic", return_value=100):
            self.service.tick()
        self.assertEqual([event[0] for event in self.telegram.events[:2]], ["send_chunk", "get_updates"])
        self.assertEqual(self.telegram.timeouts, [0])
        self.assertEqual(len(self.telegram.sent), 1)
        self.assertIsNotNone(self.store.notification_outbox())

    def test_command_poll_keeps_default_timeout_without_notification_queue(self):
        self.service.commands()
        self.assertEqual(self.telegram.timeouts, [None])

    def test_multiple_notices_resume_only_unsent_mail_after_restart(self):
        self.enable_notifications()
        first = replace(mail(uid=1), subject="Первое новое письмо")
        second = replace(mail(uid=2), subject="Второе новое письмо")
        self.readers[0].result = PollResult(41, 2, [first, second])
        self.telegram.outcomes = [None, TelegramError("test failure", retry_after=30)]
        with patch("mail_summary_bot.service.time.time", return_value=1000):
            self.service.poll_mail()
            self.assertTrue(self.service.deliver_notifications(1000))
        with patch("mail_summary_bot.service.time.time", return_value=1002):
            with self.assertLogs("mail_summary_bot.service", level="WARNING"):
                self.assertFalse(self.service.deliver_notifications(1002))
        self.assertEqual(len(self.telegram.sent), 1)
        self.assertIn(first.subject, self.telegram.sent[0])
        self.reopen_service()
        self.assertFalse(self.service.deliver_notifications(1031))
        with patch("mail_summary_bot.service.time.time", return_value=1033):
            self.assertTrue(self.service.deliver_notifications(1033))
            self.service.poll_mail()
        self.assertEqual(len(self.telegram.sent), 1)
        self.assertIn(second.subject, self.telegram.sent[0])
        self.assertNotIn(first.subject, self.telegram.sent[0])
        self.assertIsNone(self.store.notification_outbox())
        self.assertEqual(self.store.stats()["pending"], 2)

    def test_one_mailbox_failure_does_not_block_the_other(self):
        self.readers[0].error = RuntimeError("private-test-password")
        self.readers[1].result = PollResult(41, 3, [mail("second", uid=3)])
        with self.assertLogs("mail_summary_bot.service", level="WARNING") as logs:
            self.service.poll_mail()
        self.assertNotIn("private-test-password", "\n".join(logs.output))
        self.assertEqual(self.store.get("health:first"), "error")
        self.assertEqual(self.store.get("health:second"), "ok")
        self.assertIsNone(self.store.checkpoint("first", account_binding(self.accounts[0])))
        self.assertEqual(self.store.checkpoint("second", account_binding(self.accounts[1])), (41, 3))
        self.service.request_digest()
        self.assertTrue(self.service.build_digest(2000))
        self.assertEqual([m.account_id for m in self.summarizer.calls[0]], ["second"])
        self.assertIn("Не удалось обновить ящики: first", "".join(self.store.outbox()["parts"]))

    def test_repeated_poll_does_not_summarize_same_uid_twice(self):
        self.readers[0].result = PollResult(41, 7, [mail(uid=7)])
        self.service.poll_mail()
        self.service.poll_mail()
        self.assertEqual(self.readers[0].calls, [None, (41, 7)])
        self.assertEqual(self.store.stats()["pending"], 1)
        self.service.request_digest()
        self.service.build_digest(2000)
        self.assertEqual(len(self.summarizer.calls[0]), 1)

    def test_commands_accept_only_configured_private_chat(self):
        self.telegram.updates = [
            update(1, "/summary", chat_id=OWNER + 1),
            update(2, "/summary", chat_type="group"),
            update(3, "/status"),
        ]
        self.service.commands()
        self.assertEqual(len(self.telegram.replies), 1)
        self.assertIn("Почтовая сводка", self.telegram.replies[0])
        self.assertEqual([len(r.calls) for r in self.readers], [0, 0])
        self.assertEqual(self.store.get("digest_requested", "0"), "0")
        self.assertEqual(self.store.get("telegram_offset"), "4")

    def test_owner_summary_command_polls_and_persists_request(self):
        self.readers[0].result = PollResult(41, 7, [mail(uid=7)])
        self.telegram.updates = [update(10, "/summary")]
        self.service.commands()
        self.assertEqual([len(r.calls) for r in self.readers], [1, 1])
        self.assertEqual(self.store.get("digest_requested"), "1")
        self.assertEqual(self.store.get("telegram_offset"), "11")
        self.service.commands()
        self.assertEqual(len(self.telegram.replies), 1)

    def test_whitespace_message_does_not_stop_command_polling(self):
        self.telegram.updates = [update(1, "   \n\t"), update(2, "/status")]
        self.service.commands()
        self.assertEqual(len(self.telegram.replies), 1)
        self.assertEqual(self.store.get("telegram_offset"), "3")

    def test_ai_failure_keeps_mail_pending_and_retries_later(self):
        self.save_mail(mail())
        self.summarizer.error = RuntimeError("private-mail-content")
        self.service.request_digest()
        with self.assertLogs("mail_summary_bot.service", level="WARNING") as logs:
            self.assertFalse(self.service.build_digest(2000))
        self.assertNotIn("private-mail-content", "\n".join(logs.output))
        self.assertEqual(self.store.stats(), {"pending": 1, "queued": 0, "sent": 0})
        self.assertIsNone(self.store.outbox())
        self.assertEqual(self.store.get("digest_requested"), "1")
        self.assertFalse(self.service.build_digest(2059))
        self.assertEqual(len(self.summarizer.calls), 1)
        self.summarizer.error = None
        self.assertTrue(self.service.build_digest(2060))
        self.assertEqual(len(self.summarizer.calls), 2)
        self.assertEqual(self.store.stats()["queued"], 1)

    def test_empty_ai_output_is_not_treated_as_delivered_mail(self):
        self.save_mail(mail())
        self.summarizer.text = " \n"
        self.service.request_digest()
        with self.assertLogs("mail_summary_bot.service", level="WARNING"):
            self.assertFalse(self.service.build_digest(2000))
        self.assertEqual(self.store.stats()["pending"], 1)
        self.assertIsNone(self.store.outbox())

    def test_epoch_recovery_notice_is_included_and_cleared_only_after_queueing(self):
        self.save_mail(mail())
        self.readers[0].result = PollResult(42, 9, [mail(uid=9, epoch=42)], epoch_changed=True)
        with self.assertLogs("mail_summary_bot.service", level="WARNING"):
            self.service.poll_mail()
        self.assertEqual(self.store.get("epoch_notice:first"), "1")
        self.service.request_digest()
        self.summarizer.error = RuntimeError("test AI outage")
        with self.assertLogs("mail_summary_bot.service", level="WARNING"):
            self.assertFalse(self.service.build_digest(2000))
        self.assertEqual(self.store.get("epoch_notice:first"), "1")
        self.summarizer.error = None
        self.assertTrue(self.service.build_digest(2060))
        self.assertIn("Восстановление после изменения идентификаторов почты: first", "".join(self.store.outbox()["parts"]))
        self.assertEqual(self.store.get("epoch_notice:first"), "0")

    def test_multipart_delivery_resumes_at_first_unsent_part_after_restart(self):
        self.save_mail(mail())
        self.summarizer.text = "Я" * 8500
        self.telegram.outcomes = [None, TelegramError("test failure", retry_after=30)]
        self.service.request_digest()
        with patch("mail_summary_bot.service.time.time", return_value=1000):
            self.assertTrue(self.service.build_digest(1000))
            parts = self.store.outbox()["parts"]
            self.assertEqual(len(parts), 3)
            self.assertTrue(self.service.deliver(1000))
        with patch("mail_summary_bot.service.time.time", return_value=1002):
            with self.assertLogs("mail_summary_bot.service", level="WARNING"):
                self.assertFalse(self.service.deliver(1002))
        self.assertEqual(self.telegram.sent, parts[:1])
        self.assertEqual(self.store.stats(), {"pending": 0, "queued": 1, "sent": 0})
        self.service.close()
        self.store = Store(self.path)
        self.telegram = FakeTelegram()
        self.service = self.make_service()
        self.assertEqual(self.store.outbox()["sent_parts"], 1)
        self.assertFalse(self.service.deliver(1031))
        self.assertEqual(self.telegram.attempts, [])
        with patch("mail_summary_bot.service.time.time", return_value=1033):
            self.assertTrue(self.service.deliver(1033))
        with patch("mail_summary_bot.service.time.time", return_value=1035):
            self.assertTrue(self.service.deliver(1035))
        self.assertEqual(self.telegram.sent, parts[1:])
        self.assertIsNone(self.store.outbox())
        self.assertEqual(self.store.stats(), {"pending": 0, "queued": 0, "sent": 1})

    def test_request_drains_batches_without_losing_remaining_mail(self):
        self.config = replace(self.config, service=replace(self.config.service, max_digest_messages=1))
        self.service.config = self.config
        self.save_mail(mail(uid=1))
        self.save_mail(mail(uid=2))
        self.service.request_digest()
        with patch("mail_summary_bot.service.time.time", return_value=2000):
            self.assertTrue(self.service.build_digest(2000))
            self.assertTrue(self.service.deliver(2000))
        self.assertEqual(self.store.stats(), {"pending": 1, "queued": 0, "sent": 1})
        with patch("mail_summary_bot.service.time.time", return_value=2002):
            self.assertTrue(self.service.build_digest(2002))
            self.assertTrue(self.service.deliver(2002))
        self.assertFalse(self.service.build_digest(2004))
        self.assertEqual(self.store.get("digest_requested"), "0")
        self.assertEqual(self.store.stats()["sent"], 2)

    def test_daily_schedule_preserves_due_time_and_catches_up_after_restart(self):
        before = datetime(2026, 10, 3, 5, 59, tzinfo=timezone.utc).timestamp()
        due = datetime(2026, 10, 3, 6, 0, tzinfo=timezone.utc).timestamp()
        after = datetime(2026, 10, 3, 6, 1, tzinfo=timezone.utc).timestamp()
        tomorrow = datetime(2026, 10, 4, 6, 0, tzinfo=timezone.utc).timestamp()
        self.service.close()
        self.store = Store(self.path)
        self.config = replace(self.config, service=replace(self.config.service, schedule="daily", digest_time="09:00", timezone="Europe/Moscow"))
        with patch("mail_summary_bot.service.time.time", return_value=before):
            self.service = self.make_service()
            self.save_mail(mail())
        self.assertEqual(float(self.store.get("next_due")), due)
        self.service.close()
        self.store = Store(self.path)
        with patch("mail_summary_bot.service.time.time", return_value=after), patch("mail_summary_bot.service.time.monotonic", return_value=100):
            self.service = self.make_service()
            self.assertEqual(float(self.store.get("next_due")), due)
            self.service.tick(poll_commands=False)
            self.assertEqual(float(self.store.get("next_due")), tomorrow)
            self.assertEqual(len(self.summarizer.calls), 1)
            self.assertEqual(self.store.stats()["sent"], 1)
            self.service.tick(poll_commands=False)
        self.assertEqual(len(self.summarizer.calls), 1)

    def test_next_daily_uses_configured_timezone_and_excludes_elapsed_slot(self):
        before = datetime(2026, 10, 3, 5, 59, tzinfo=timezone.utc).timestamp()
        due = datetime(2026, 10, 3, 6, 0, tzinfo=timezone.utc).timestamp()
        tomorrow = datetime(2026, 10, 4, 6, 0, tzinfo=timezone.utc).timestamp()
        self.assertEqual(next_daily(before, "09:00", "Europe/Moscow"), due)
        self.assertEqual(next_daily(due, "09:00", "Europe/Moscow"), tomorrow)

    def test_restart_repairs_schedule_if_initial_due_write_was_interrupted(self):
        before = datetime(2026, 10, 3, 5, 59, tzinfo=timezone.utc).timestamp()
        due = datetime(2026, 10, 3, 6, 0, tzinfo=timezone.utc).timestamp()
        self.config = replace(self.config, service=replace(self.config.service, schedule="daily"))
        signature = json.dumps(["daily", "09:00", "Europe/Moscow", self.config.service.digest_interval_minutes])
        self.store.set("schedule_config", signature)
        with self.store.db:
            self.store.db.execute("DELETE FROM settings WHERE key='next_due'")
        self.service.close()
        self.store = Store(self.path)
        with patch("mail_summary_bot.service.time.time", return_value=before):
            self.service = self.make_service()
        self.assertEqual(float(self.store.get("next_due", 0)), due)


if __name__ == "__main__":
    unittest.main()
