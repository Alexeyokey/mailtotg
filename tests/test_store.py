"""Durable state tests; no email, Telegram, or AI connections are made."""

from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

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

    def test_notification_migration_and_enabling_never_replay_backlog(self):
        self.store.save_poll("first", "binding", PollResult(41, 1, [mail()]))
        # Simulate a deployed database created before notifications existed.
        with self.store.db:
            self.store.db.execute("DROP TABLE notifications")
        self.reopen()
        self.assertEqual(self.store.notification_stats(), {"pending": 0, "sent": 0})
        formatted = []

        def notice(message):
            formatted.append(message.uid)
            return [f"Новое письмо {message.uid}"]

        self.store.save_poll("first", "binding", PollResult(41, 2, [mail(), mail(uid=2)]), notification_parts=notice)
        self.assertEqual(formatted, [2])
        self.assertEqual(self.store.stats(), {"pending": 2, "queued": 0, "sent": 0})
        self.assertEqual(self.store.notification_stats(), {"pending": 1, "sent": 0})
        self.assertEqual(self.store.notification_outbox()["parts"], ["Новое письмо 2"])

    def test_formatter_failure_rolls_back_new_mail_notices_identities_and_cursor(self):
        self.store.save_poll("first", "binding", PollResult(41, 1, [mail()]))

        def notice(message):
            if message.uid == 3:
                raise RuntimeError("Formatter failed")
            return ["Уведомление"]

        with self.assertRaises(RuntimeError):
            self.store.save_poll("first", "binding", PollResult(41, 3, [mail(uid=2), mail(uid=3)]), notification_parts=notice)
        self.reopen()
        self.assertEqual(self.store.checkpoint("first", "binding"), (41, 1))
        self.assertEqual([message.uid for _, message in self.store.pending(10, ("first",))], [1])
        self.assertIsNone(self.store.notification_outbox())
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM mail_identities").fetchone()[0], 1)
        self.store.save_poll("first", "binding", PollResult(41, 3, [mail(uid=2), mail(uid=3)]), notification_parts=lambda message: ["Уведомление"])
        self.assertEqual(self.store.notification_stats()["pending"], 2)

    def test_invalid_notification_parts_roll_back_mail_and_checkpoint(self):
        for parts in ([], [""], [42], "Уведомление", None):
            with self.subTest(parts=parts):
                with self.assertRaises(ValueError):
                    self.store.save_poll("first", "binding", PollResult(41, 1, [mail()]), notification_parts=lambda message: parts)
                self.assertIsNone(self.store.checkpoint("first", "binding"))
                self.assertEqual(self.store.stats()["pending"], 0)
                self.assertEqual(self.store.notification_stats()["pending"], 0)

    def test_notification_dedup_by_stable_identity_across_epoch_change(self):
        original = mail()
        formatted = []

        def notice(message):
            formatted.append(message.uid)
            return ["Уведомление"]

        self.store.save_poll("first", "binding", PollResult(41, 1, [original]), notification_parts=notice)
        self.store.save_poll("first", "binding", PollResult(41, 1, [original]), notification_parts=notice)
        replay = replace(original, uidvalidity=42, uid=9)
        self.store.save_poll("first", "binding", PollResult(42, 9, [replay]), notification_parts=notice)
        self.reopen()
        self.assertEqual(formatted, [1])
        self.assertEqual(self.store.notification_stats(), {"pending": 1, "sent": 0})
        self.assertEqual(self.store.checkpoint("first", "binding"), (42, 9))

    def test_notification_dedup_by_uid_without_message_id(self):
        original = replace(mail(), message_id="")
        formatted = []

        def notice(message):
            formatted.append(message.body)
            return ["Уведомление"]

        self.store.save_poll("first", "binding", PollResult(41, 1, [original]), notification_parts=notice)
        self.store.save_poll("first", "binding", PollResult(41, 1, [replace(original, body="Повтор UID")]), notification_parts=notice)
        self.assertEqual(formatted, [original.body])
        self.assertEqual(self.store.notification_stats()["pending"], 1)

    def test_notifications_are_independent_for_two_accounts(self):
        original = mail()
        notice = lambda message: [f"Письмо из {message.account_id}"]
        self.store.save_poll("first", "first-binding", PollResult(41, 1, [original]), notification_parts=notice)
        self.store.save_poll("second", "second-binding", PollResult(41, 1, [replace(original, account_id="second")]), notification_parts=notice)
        first = self.store.notification_outbox()
        self.store.notification_part_sent(first["message_id"])
        second = self.store.notification_outbox()
        self.assertNotEqual(first["message_id"], second["message_id"])
        self.assertEqual(second["parts"], ["Письмо из second"])
        self.assertEqual(self.store.notification_stats(), {"pending": 1, "sent": 1})

    def test_partial_notification_and_retry_survive_reopen_without_affecting_digest(self):
        self.store.save_poll("first", "binding", PollResult(41, 1, [mail()]), notification_parts=lambda message: ["Часть 1", "Часть 2"])
        notice = self.store.notification_outbox()
        self.store.notification_part_sent(notice["message_id"])
        with patch("mail_summary_bot.store.time.time", return_value=1000):
            self.store.notification_failed(notice["message_id"], retry_after=90)
        self.reopen()
        notice = self.store.notification_outbox()
        self.assertEqual(notice["sent_parts"], 1)
        self.assertEqual(notice["parts"], ["Часть 1", "Часть 2"])
        self.assertEqual(notice["attempts"], 1)
        self.assertEqual(notice["retry_at"], 1090)
        self.assertEqual(self.store.stats(), {"pending": 1, "queued": 0, "sent": 0})
        self.store.notification_part_sent(notice["message_id"])
        self.reopen()
        self.assertIsNone(self.store.notification_outbox())
        self.assertEqual(self.store.notification_stats(), {"pending": 0, "sent": 1})
        self.assertEqual(self.store.stats(), {"pending": 1, "queued": 0, "sent": 0})
        row = self.store.db.execute("SELECT attempts,retry_at FROM notifications").fetchone()
        self.assertEqual(tuple(row), (0, 0))
        digest_id = self.store.queue_digest([notice["message_id"]], ["Утренняя сводка"])
        self.store.part_sent(digest_id)
        self.assertEqual(self.store.stats(), {"pending": 0, "queued": 0, "sent": 1})

    def test_notification_backoff_and_unknown_or_completed_notice(self):
        self.store.save_poll("first", "binding", PollResult(41, 1, [mail()]), notification_parts=lambda message: ["Уведомление"])
        notice_id = self.store.notification_outbox()["message_id"]
        with patch("mail_summary_bot.store.time.time", return_value=1000):
            self.store.notification_failed(notice_id)
            self.assertEqual(self.store.notification_outbox()["retry_at"], 1002)
            self.store.notification_failed(notice_id)
            self.assertEqual(self.store.notification_outbox()["retry_at"], 1004)
            for _ in range(20):
                self.store.notification_failed(notice_id)
            self.assertEqual(self.store.notification_outbox()["retry_at"], 3048)
        with self.assertRaises(ValueError):
            self.store.notification_failed(notice_id + 1000)
        with self.assertRaises(ValueError):
            self.store.notification_part_sent(notice_id + 1000)
        self.store.notification_part_sent(notice_id)
        with self.assertRaises(ValueError):
            self.store.notification_failed(notice_id)
        with self.assertRaises(ValueError):
            self.store.notification_part_sent(notice_id)

    def test_prune_keeps_digest_sent_mail_and_identity_with_pending_notification(self):
        self.store.save_poll("first", "binding", PollResult(41, 1, [mail()]), notification_parts=lambda message: ["Уведомление"])
        notice_id = self.store.notification_outbox()["message_id"]
        digest_id = self.store.queue_digest([notice_id], ["Утренняя сводка"])
        self.store.part_sent(digest_id)
        with self.store.db:
            self.store.db.execute("UPDATE messages SET created_at=0")
            self.store.db.execute("UPDATE digests SET created_at=0")
            self.store.db.execute("UPDATE notifications SET created_at=0")
            self.store.db.execute("UPDATE mail_identities SET created_at=0")
        self.store.prune(1)
        self.reopen()
        self.assertEqual(self.store.stats(), {"pending": 0, "queued": 0, "sent": 1})
        self.assertEqual(self.store.notification_stats(), {"pending": 1, "sent": 0})
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM mail_identities").fetchone()[0], 1)
        self.store.notification_part_sent(notice_id)
        self.assertEqual(self.store.stats()["sent"], 1)
        self.store.prune(1)
        self.reopen()
        self.assertEqual(self.store.stats(), {"pending": 0, "queued": 0, "sent": 0})
        self.assertEqual(self.store.notification_stats(), {"pending": 0, "sent": 0})
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM mail_identities").fetchone()[0], 0)

    def test_prune_completed_notice_does_not_discard_pending_digest_mail(self):
        self.store.save_poll("first", "binding", PollResult(41, 1, [mail()]), notification_parts=lambda message: ["Уведомление"])
        notice_id = self.store.notification_outbox()["message_id"]
        self.store.notification_part_sent(notice_id)
        with self.store.db:
            self.store.db.execute("UPDATE messages SET created_at=0")
            self.store.db.execute("UPDATE notifications SET created_at=0")
        self.store.prune(1)
        self.reopen()
        self.assertEqual(self.store.stats(), {"pending": 1, "queued": 0, "sent": 0})
        self.assertEqual(self.store.notification_stats(), {"pending": 0, "sent": 0})
        self.assertEqual(self.store.pending(1, ("first",))[0][0], notice_id)

    def test_filtered_new_mail_keeps_cursor_and_identity_without_notice_or_digest(self):
        blocked = replace(mail(uid=1), sender="alerts@excluded.example")
        ordinary = mail(uid=2)
        formatted = []

        def notice(message):
            formatted.append(message.uid)
            return ["Уведомление"]

        predicate = lambda message: message.sender.endswith("@excluded.example")
        self.store.save_poll("first", "binding", PollResult(41, 2, [blocked, ordinary]),
                             notification_parts=notice, exclude_mail=predicate)
        self.reopen()
        self.assertEqual(self.store.checkpoint("first", "binding"), (41, 2))
        self.assertEqual(self.store.stats(), {"pending": 1, "queued": 0, "sent": 0})
        self.assertEqual(self.store.excluded_count(), 1)
        self.assertEqual([message.uid for _, message in self.store.pending(10, ("first",))], [2])
        self.assertEqual(formatted, [2])
        self.assertEqual(self.store.notification_stats(), {"pending": 1, "sent": 0})
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM mail_identities").fetchone()[0], 2)

        replay = replace(blocked, uidvalidity=42, uid=9)
        self.store.save_poll("first", "binding", PollResult(42, 9, [replay]),
                             notification_parts=notice, exclude_mail=predicate)
        self.assertEqual(self.store.checkpoint("first", "binding"), (42, 9))
        self.assertEqual(self.store.excluded_count(), 1)
        self.assertEqual(formatted, [2])

    def test_filter_failure_rolls_back_mail_notices_identity_and_cursor(self):
        self.store.save_poll("first", "binding", PollResult(41, 1, [mail()]))

        def predicate(message):
            if message.uid == 3:
                raise RuntimeError("Invalid exclusion rule")
            return True

        with self.assertRaises(RuntimeError):
            self.store.save_poll("first", "binding", PollResult(41, 3, [mail(uid=2), mail(uid=3)]),
                                 notification_parts=lambda message: ["Уведомление"], exclude_mail=predicate)
        self.reopen()
        self.assertEqual(self.store.checkpoint("first", "binding"), (41, 1))
        self.assertEqual(self.store.stats(), {"pending": 1, "queued": 0, "sent": 0})
        self.assertEqual(self.store.excluded_count(), 0)
        self.assertIsNone(self.store.notification_outbox())
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM mail_identities").fetchone()[0], 1)

    def test_backlog_exclusion_is_scoped_to_configured_accounts_and_idempotent(self):
        self.store.save_poll("first", "first-binding", PollResult(41, 1, [mail()]),
                             notification_parts=lambda message: ["Первый ящик"])
        self.store.save_poll("second", "second-binding", PollResult(41, 1, [mail(account="second")]),
                             notification_parts=lambda message: ["Второй ящик"])
        self.assertEqual(self.store.exclude_pending(lambda message: True, ()), 0)
        self.assertEqual(self.store.exclude_pending(lambda message: True, ("first",)), 1)
        self.assertEqual(self.store.exclude_pending(lambda message: True, ("first",)), 0)
        self.reopen()
        self.assertEqual(self.store.excluded_count(), 1)
        self.assertEqual(self.store.stats(), {"pending": 1, "queued": 0, "sent": 0})
        self.assertEqual(self.store.notification_outbox()["parts"], ["Второй ящик"])
        self.assertEqual(self.store.db.execute("SELECT status FROM notifications WHERE message_id=1").fetchone()[0], "excluded")

    def test_excluding_partly_sent_mixed_digest_requeues_remaining_mail(self):
        self.store.save_poll("first", "binding", PollResult(41, 2, [mail(uid=1), mail(uid=2)]),
                             notification_parts=lambda message: ["Уведомление"])
        ids = [mid for mid, _ in self.store.pending(10, ("first",))]
        digest_id = self.store.queue_digest(ids, ["Уже подтверждённая часть", "Ожидающая часть"])
        self.store.part_sent(digest_id)
        self.assertEqual(self.store.exclude_pending(lambda message: message.uid == 1, ("first",)), 1)
        self.reopen()
        self.assertIsNone(self.store.outbox())
        self.assertEqual(self.store.stats(), {"pending": 1, "queued": 0, "sent": 0})
        self.assertEqual(self.store.excluded_count(), 1)
        self.assertEqual([message.uid for _, message in self.store.pending(10, ("first",))], [2])
        cancelled = self.store.db.execute("SELECT status,sent_parts FROM digests WHERE id=?", (digest_id,)).fetchone()
        self.assertEqual(tuple(cancelled), ("cancelled", 1))
        self.assertEqual(self.store.notification_outbox()["message_id"], ids[1])
        clean_id = self.store.queue_digest([ids[1]], ["Чистая сводка"])
        self.store.part_sent(clean_id)
        self.assertEqual(self.store.stats(), {"pending": 0, "queued": 0, "sent": 1})
        self.assertEqual(self.store.excluded_count(), 1)

    def test_backlog_predicate_failure_rolls_back_exclusions_and_notice_cancellation(self):
        self.store.save_poll("first", "binding", PollResult(41, 2, [mail(uid=1), mail(uid=2)]),
                             notification_parts=lambda message: ["Уведомление"])
        ids = [mid for mid, _ in self.store.pending(10, ("first",))]
        self.store.queue_digest(ids, ["Сводка"])

        def predicate(message):
            if message.uid == 2:
                raise RuntimeError("Invalid exclusion rule")
            return True

        with self.assertRaises(RuntimeError):
            self.store.exclude_pending(predicate, ("first",))
        self.reopen()
        self.assertEqual(self.store.stats(), {"pending": 0, "queued": 2, "sent": 0})
        self.assertEqual(self.store.excluded_count(), 0)
        self.assertEqual(self.store.notification_stats(), {"pending": 2, "sent": 0})
        self.assertEqual(self.store.outbox()["message_ids"], ids)

    def test_filter_cancels_pending_notice_on_sent_mail_without_rewriting_sent_digest(self):
        self.store.save_poll("first", "binding", PollResult(41, 1, [mail()]),
                             notification_parts=lambda message: ["Первая часть", "Вторая часть"])
        notice_id = self.store.notification_outbox()["message_id"]
        self.store.notification_part_sent(notice_id)
        digest_id = self.store.queue_digest([notice_id], ["Отправленная сводка"])
        self.store.part_sent(digest_id)
        self.assertEqual(self.store.exclude_pending(lambda message: True, ("first",)), 0)
        self.reopen()
        self.assertEqual(self.store.stats(), {"pending": 0, "queued": 0, "sent": 1})
        self.assertEqual(self.store.excluded_count(), 0)
        self.assertIsNone(self.store.notification_outbox())
        self.assertEqual(tuple(self.store.db.execute("SELECT status,sent_parts FROM notifications").fetchone()), ("excluded", 1))
        self.assertEqual(self.store.db.execute("SELECT status FROM digests WHERE id=?", (digest_id,)).fetchone()[0], "sent")

    def test_prune_ages_excluded_mail_notice_and_cancelled_digest_but_retains_pending(self):
        self.store.save_poll("first", "binding", PollResult(41, 2, [mail(uid=1), mail(uid=2)]),
                             notification_parts=lambda message: ["Уведомление"])
        ids = [mid for mid, _ in self.store.pending(10, ("first",))]
        digest_id = self.store.queue_digest(ids, ["Смешанная сводка"])
        self.store.exclude_pending(lambda message: message.uid == 1, ("first",))
        with self.store.db:
            self.store.db.execute("UPDATE messages SET created_at=0")
            self.store.db.execute("UPDATE notifications SET created_at=0")
            self.store.db.execute("UPDATE digests SET created_at=0")
            self.store.db.execute("UPDATE mail_identities SET created_at=0")
        self.store.prune(1)
        self.reopen()
        self.assertEqual(self.store.excluded_count(), 0)
        self.assertEqual(self.store.stats(), {"pending": 1, "queued": 0, "sent": 0})
        self.assertEqual(self.store.notification_outbox()["message_id"], ids[1])
        self.assertIsNone(self.store.db.execute("SELECT 1 FROM digests WHERE id=?", (digest_id,)).fetchone())
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM mail_identities").fetchone()[0], 2)

        self.store.notification_part_sent(ids[1])
        clean_id = self.store.queue_digest([ids[1]], ["Чистая сводка"])
        self.store.part_sent(clean_id)
        self.store.prune(1)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM mail_identities").fetchone()[0], 0)

    def test_prune_excluded_mail_does_not_pin_old_fingerprints(self):
        self.store.save_poll("first", "binding", PollResult(41, 1, [mail()]), exclude_mail=lambda message: True)
        with self.store.db:
            self.store.db.execute("UPDATE messages SET created_at=0")
            self.store.db.execute("UPDATE mail_identities SET created_at=0")
        self.store.prune(1)
        self.assertEqual(self.store.stats(), {"pending": 0, "queued": 0, "sent": 0})
        self.assertEqual(self.store.excluded_count(), 0)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM mail_identities").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
