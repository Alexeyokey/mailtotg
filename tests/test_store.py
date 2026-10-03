"""Durable state tests; no email, Telegram, or AI connections are made."""

from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from mail_summary_bot.models import MailMessage, PollResult
from mail_summary_bot.store import Store
from mail_summary_bot.config import ConfigError


def mail(account="first", uid=1, epoch=41, body="Содержимое письма"):
    return MailMessage(account, epoch, uid, f"<{account}-{uid}>", "sender@example.org", "Тема", "2026-10-03T09:00:00+03:00", body)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / "state.sqlite3")
        self.store = Store(self.path)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def reopen(self):
        self.store.close()
        self.store = Store(self.path)

    def test_mail_and_checkpoint_survive_reopen(self):
        self.store.save_poll("first", "binding", PollResult(41, 7, [mail(uid=7)]))
        self.reopen()
        self.assertEqual(self.store.checkpoint("first", "binding"), (41, 7))
        with self.assertRaises(ConfigError):
            self.store.checkpoint("first", "other-binding")
        self.assertEqual(self.store.pending(10, ("first",))[0][1].uid, 7)

    def test_checkpoint_and_all_mail_inserts_roll_back_together(self):
        self.store.save_poll("first", "binding", PollResult(41, 1, [mail(uid=1)]))
        invalid_batch = PollResult(41, 3, [mail(uid=2), mail(account="second", uid=3)])
        with self.assertRaises(ValueError):
            self.store.save_poll("first", "binding", invalid_batch)
        self.reopen()
        self.assertEqual(self.store.checkpoint("first", "binding"), (41, 1))
        self.assertEqual([m.uid for _, m in self.store.pending(10, ("first", "second"))], [1])
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM mail_identities").fetchone()[0], 1)

    def test_dedup_is_scoped_to_account_and_uidvalidity(self):
        first = PollResult(41, 1, [mail()])
        self.store.save_poll("first", "first-binding", first)
        self.store.save_poll("first", "first-binding", first)
        self.store.save_poll("second", "second-binding", PollResult(41, 1, [mail(account="second")]))
        self.store.save_poll("first", "first-binding", PollResult(42, 1, [mail(epoch=42, body="Другое письмо после смены эпохи")]))
        self.assertEqual(self.store.stats(), {"pending": 3, "queued": 0, "sent": 0})

    def test_same_content_with_stable_message_id_is_not_replayed_after_epoch_change(self):
        original = mail()
        self.store.save_poll("first", "binding", PollResult(41, 1, [original]))
        replay = replace(original, uidvalidity=42, uid=9)
        self.store.save_poll("first", "binding", PollResult(42, 9, [replay]))
        self.reopen()
        self.assertEqual(self.store.stats()["pending"], 1)
        self.assertEqual(self.store.checkpoint("first", "binding"), (42, 9))

    def test_same_message_id_with_different_body_keeps_both_messages(self):
        original = mail()
        self.store.save_poll("first", "binding", PollResult(41, 1, [original]))
        changed = replace(original, uidvalidity=42, uid=9, body="Исправленное содержание")
        self.store.save_poll("first", "binding", PollResult(42, 9, [changed]))
        self.assertEqual([m.body for _, m in self.store.pending(10, ("first",))], [original.body, changed.body])

    def test_same_message_id_and_content_in_different_accounts_keeps_both(self):
        original = mail()
        self.store.save_poll("first", "first-binding", PollResult(41, 1, [original]))
        other_account = replace(original, account_id="second")
        self.store.save_poll("second", "second-binding", PollResult(41, 1, [other_account]))
        self.assertEqual([m.account_id for _, m in self.store.pending(10, ("first", "second"))], ["first", "second"])

    def test_mail_without_message_id_is_not_lost_on_epoch_change(self):
        original = replace(mail(), message_id="")
        self.store.save_poll("first", "binding", PollResult(41, 1, [original]))
        replay = replace(original, uidvalidity=42, uid=9)
        self.store.save_poll("first", "binding", PollResult(42, 9, [replay]))
        self.assertEqual(self.store.stats()["pending"], 2)

    def test_changed_account_binding_is_rejected_without_changing_mail_or_cursor(self):
        self.store.save_poll("first", "old-binding", PollResult(41, 1, [mail()]))
        with self.assertRaises(ConfigError):
            self.store.save_poll("first", "new-binding", PollResult(41, 1, [mail(body="Письмо другого ящика")]))
        self.reopen()
        self.assertEqual(self.store.checkpoint("first", "old-binding"), (41, 1))
        self.assertEqual(self.store.stats()["pending"], 1)
        self.assertEqual(self.store.pending(1, ("first",))[0][1].body, mail().body)

    def test_second_worker_cannot_open_active_database(self):
        with self.assertRaises(RuntimeError):
            Store(self.path)

    def test_pending_filters_configured_accounts(self):
        self.store.save_poll("first", "first-binding", PollResult(41, 1, [mail()]))
        self.store.save_poll("second", "second-binding", PollResult(41, 1, [mail(account="second")]))
        self.assertEqual([m.account_id for _, m in self.store.pending(10, ("second",))], ["second"])

    def test_queue_digest_is_atomic_when_one_message_is_missing(self):
        self.store.save_poll("first", "binding", PollResult(41, 1, [mail()]))
        message_id = self.store.pending(1, ("first",))[0][0]
        with self.assertRaises(ValueError):
            self.store.queue_digest([message_id, message_id + 1000], ["Сводка"])
        self.reopen()
        self.assertIsNone(self.store.outbox())
        self.assertEqual(self.store.stats(), {"pending": 1, "queued": 0, "sent": 0})

    def test_failure_and_partial_delivery_retain_mail_after_reopen(self):
        original = mail(body="Не терять исходное письмо")
        self.store.save_poll("first", "binding", PollResult(41, 1, [original]))
        message_id = self.store.pending(1, ("first",))[0][0]
        digest_id = self.store.queue_digest([message_id], ["Часть 1", "Часть 2"])
        self.store.part_sent(digest_id)
        self.store.delivery_failed(digest_id, retry_after=90)
        self.reopen()
        queued = self.store.outbox()
        self.assertEqual(queued["sent_parts"], 1)
        self.assertEqual(queued["attempts"], 1)
        self.assertGreater(queued["retry_at"], queued["created_at"])
        self.assertEqual(self.store.stats(), {"pending": 0, "queued": 1, "sent": 0})
        self.assertIn(original.body, self.store.db.execute("SELECT payload FROM messages").fetchone()[0])
        self.store.part_sent(digest_id)
        self.reopen()
        self.assertIsNone(self.store.outbox())
        self.assertEqual(self.store.stats(), {"pending": 0, "queued": 0, "sent": 1})

    def test_prune_never_discards_pending_or_queued_mail(self):
        self.store.save_poll("first", "binding", PollResult(41, 3, [mail(uid=1), mail(uid=2), mail(uid=3)]))
        ids = [mid for mid, _ in self.store.pending(10, ("first",))]
        digest_id = self.store.queue_digest([ids[0]], ["Отправлено"])
        self.store.part_sent(digest_id)
        self.store.queue_digest([ids[1]], ["Ожидает отправки"])
        with self.store.db:
            self.store.db.execute("UPDATE messages SET created_at=0")
            self.store.db.execute("UPDATE digests SET created_at=0")
        self.store.prune(1)
        self.reopen()
        self.assertEqual(self.store.stats(), {"pending": 1, "queued": 1, "sent": 0})
        self.assertIsNotNone(self.store.outbox())


if __name__ == "__main__":
    unittest.main()
