"""Interactive blacklist and message lookups remain durable and account-scoped."""
from dataclasses import replace
from pathlib import Path
import sqlite3
import tempfile
import unittest

from mail_summary_bot.models import MailMessage, PollResult
from mail_summary_bot.store import Store


def mail(account='first', uid=1, sender='opros.perekrestok@x5.ru'):
    return MailMessage(account, 41, uid, f'<{account}-{uid}>', sender,
                       'Тема', '2026-10-15', 'Содержимое письма')


class BlacklistStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / 'state.sqlite3')
        self.store = Store(self.path)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def reopen(self):
        self.store.close()
        self.store = Store(self.path)

    def save(self, messages, *, parts=None):
        account = messages[0].account_id
        self.store.save_poll(account, account + '-binding',
                             PollResult(41, max(message.uid for message in messages), messages),
                             notification_parts=(lambda message: parts) if parts else None,
                             exclude_mail=lambda message: self.store.blacklist_matches(message.sender))
        return self.store.browse_mail(False, (account,))[1]

    def test_rule_canonicalization_stable_ids_disable_and_seed_survive_restart(self):
        self.store.seed_blacklist((' OZON.RU. ', 'perekrestok.ru'))
        ozon = next(rule for rule in self.store.blacklist_page()[1] if rule['value'] == 'ozon.ru')
        self.assertTrue(self.store.blacklist_remove(ozon['id']))
        self.assertTrue(self.store.blacklist_remove(ozon['id']))
        self.assertFalse(self.store.blacklist_remove(999))
        exact = self.store.blacklist_add('address', ' OPROS.Perekrestok@X5.RU ')
        self.assertEqual(self.store.blacklist_add('address', 'opros.perekrestok@x5.ru'), exact)
        self.reopen()
        self.store.seed_blacklist(('ozon.ru', 'perekrestok.ru'))
        self.assertFalse(self.store.blacklist_matches('sale@ozon.ru'))
        self.assertTrue(self.store.blacklist_matches('opros.perekrestok@x5.ru'))
        self.assertEqual(self.store.blacklist_page()[0], 2)
        self.assertEqual(self.store.blacklist_add('domain', 'OZON.RU'), ozon['id'])
        self.assertTrue(self.store.blacklist_matches('sale@ozon.ru'))
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM blacklist_rules').fetchone()[0], 3)

    def test_page_contains_active_rules_newest_first_with_bounded_offsets(self):
        ids = [self.store.blacklist_add('address', f'sender{number}@x5.ru') for number in range(10)]
        self.store.blacklist_remove(ids[5])
        total, page = self.store.blacklist_page(limit=3)
        self.assertEqual(total, 9)
        self.assertEqual([rule['id'] for rule in page], list(reversed(ids[-3:])))
        self.assertEqual([rule['id'] for rule in self.store.blacklist_page(offset=8)[1]], [ids[0]])
        self.assertEqual(self.store.blacklist_page(offset=100), (9, []))
        for kwargs in ({'offset': -1}, {'limit': 0}, {'offset': True}, {'limit': '8'}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.store.blacklist_page(**kwargs)

    def test_invalid_rule_or_seed_does_not_partially_modify_database(self):
        for kind, value in (('sender', 'a@x5.ru'), ('address', 'a@x5.ru,b@x5.ru'), ('domain', '*.x5.ru')):
            with self.subTest(kind=kind, value=value), self.assertRaises(ValueError):
                self.store.blacklist_add(kind, value)
        with self.assertRaises(ValueError):
            self.store.seed_blacklist(('ozon.ru', 'broken domain'))
        self.assertEqual(self.store.blacklist_page(), (0, []))

    def test_matching_returns_all_active_rules_without_broadening_exact_x5_rule(self):
        exact = self.store.blacklist_add('address', 'opros.perekrestok@x5.ru')
        self.assertEqual([rule['id'] for rule in self.store.blacklist_matching('OPROS.PEREKRESTOK@X5.RU')], [exact])
        for sender in ('staff@x5.ru', 'opros.perekrestok@sub.x5.ru',
                       '"opros.perekrestok@x5.ru" <staff@x5.ru>', 'a@x5.ru, opros.perekrestok@x5.ru'):
            with self.subTest(sender=sender):
                self.assertFalse(self.store.blacklist_matches(sender))
        domain = self.store.blacklist_add('domain', 'x5.ru')
        self.assertEqual({rule['id'] for rule in self.store.blacklist_matching('opros.perekrestok@x5.ru')}, {exact, domain})
        self.store.blacklist_remove(domain)
        self.assertEqual([rule['id'] for rule in self.store.blacklist_matching('opros.perekrestok@x5.ru')], [exact])

    def test_browse_and_get_only_configured_streams(self):
        first = self.save([mail(uid=1), mail(uid=2), mail(uid=3)])
        other = self.save([mail(account='removed')])[0]
        self.store.exclude_pending(lambda message: message.uid == 2, ('first',))
        self.assertEqual(self.store.browse_mail(False, ('first',), limit=1), (2, [first[0]]))
        self.assertEqual(self.store.browse_mail(False, ('first',), offset=1), (2, [first[-1]]))
        self.assertEqual(self.store.browse_mail(True, ('first',)), (1, [first[1]]))
        self.assertEqual(self.store.get_message(first[1][0], ('first',)), first[1][1])
        self.assertIsNone(self.store.get_message(other[0], ('first',)))
        self.assertEqual(self.store.browse_mail(True, ()), (0, []))
        self.assertEqual(self.store.browse_mail(False, ()), (0, []))
        self.assertIsNone(self.store.get_message(first[0][0], ()))

    def test_block_cancels_all_folder_notices_and_mixed_digest_keeps_delivery_history(self):
        streams = ('first', 'first:newsletters', 'first:social')
        original = {}
        for stream in (*streams, 'removed'):
            original[stream] = self.save([mail(account=stream)], parts=['Part one', 'Part two'])[0]
        allowed = self.save([mail(uid=2, sender='staff@x5.ru')], parts=['Allowed'])[0]
        self.store.notification_part_sent(original['first'][0], telegram_message_id=101, chat_id=1)
        digest = self.store.queue_digest([original['first'][0], allowed[0]], ['Already delivered', 'Pending'])
        self.store.part_sent(digest)
        rule = self.store.blacklist_add('address', 'opros.perekrestok@x5.ru')
        self.assertEqual(self.store.exclude_pending(lambda message: self.store.blacklist_matches(message.sender), streams, include_sent=True), 3)
        self.reopen()
        self.assertIsNone(self.store.outbox())
        self.assertEqual(tuple(self.store.db.execute('SELECT status,sent_parts FROM digests WHERE id=?', (digest,)).fetchone()), ('cancelled', 1))
        self.assertEqual(tuple(self.store.db.execute('SELECT status,sent_parts FROM notifications WHERE message_id=?', (original['first'][0],)).fetchone()), ('excluded', 1))
        self.assertEqual(self.store.pending(10, streams), [allowed])
        self.assertEqual(self.store.browse_mail(True, streams)[0], 3)
        self.assertEqual(self.store.telegram_mail('1', 101, streams), original['first'])
        self.assertEqual(self.store.notification_outbox()['message_id'], original['removed'][0])
        self.store.blacklist_remove(rule)
        for stream in streams:
            self.save([original[stream][1]], parts=['Should never replay'])
        self.reopen()
        self.assertEqual(self.store.browse_mail(True, streams)[0], 3)
        self.assertEqual(self.store.pending(10, streams), [allowed])
        self.assertEqual(self.store.notification_outbox()['message_id'], original['removed'][0])
        self.save([mail(uid=3)], parts=['New mail after unblock'])
        self.assertEqual([message.uid for _, message in self.store.pending(10, streams)], [2, 3])

    def test_include_sent_moves_archive_without_rewriting_acknowledged_notices(self):
        source = self.save([mail()], parts=['Delivered'])[0]
        self.store.notification_part_sent(source[0], telegram_message_id=11, chat_id=1)
        digest = self.store.queue_digest([source[0]], ['Delivered digest'])
        self.store.part_sent(digest)
        self.assertEqual(self.store.exclude_pending(lambda message: True, ('first',), include_sent=True), 1)
        self.assertEqual(self.store.exclude_pending(lambda message: True, ('first',), include_sent=True), 0)
        self.reopen()
        self.assertEqual(self.store.browse_mail(True, ('first',)), (1, [source]))
        self.assertEqual(tuple(self.store.db.execute('SELECT status,sent_parts FROM notifications').fetchone()), ('sent', 1))
        self.assertEqual(self.store.db.execute('SELECT status FROM digests WHERE id=?', (digest,)).fetchone()[0], 'sent')
        self.assertEqual(self.store.telegram_mail(1, 11, ('first',)), source)

    def test_unblocking_newly_filtered_mail_never_replays_after_folder_move_or_epoch(self):
        rule = self.store.blacklist_add('address', 'opros.perekrestok@x5.ru')
        original = mail()
        self.store.save_poll('first', 'binding', PollResult(41, 1, [original]), identity_account_id='first',
                             notification_parts=lambda message: ['Notice'],
                             exclude_mail=lambda message: self.store.blacklist_matches(message.sender))
        self.store.blacklist_remove(rule)
        self.reopen()
        moved = replace(original, account_id='first:newsletters', uidvalidity=42, uid=90)
        self.store.save_poll(moved.account_id, 'folder-binding', PollResult(42, 90, [moved]), identity_account_id='first',
                             notification_parts=lambda message: ['Must never replay'],
                             exclude_mail=lambda message: self.store.blacklist_matches(message.sender))
        self.assertEqual(self.store.browse_mail(True, ('first', 'first:newsletters'))[0], 1)
        self.assertIsNone(self.store.notification_outbox())
        self.assertEqual(self.store.checkpoint(moved.account_id, 'folder-binding'), (42, 90))

    def test_message_mapping_and_ack_are_atomic_and_durable_for_every_part(self):
        source = self.save([mail()], parts=['First', 'Second'])[0]
        self.store.notification_part_sent(source[0], telegram_message_id=10, chat_id=-123)
        self.reopen()
        self.assertEqual(self.store.notification_outbox()['sent_parts'], 1)
        self.assertEqual(self.store.telegram_mail('-123', 10, ('first',)), source)
        self.store.notification_part_sent(source[0], telegram_message_id=11, chat_id='-123')
        self.store.remember_telegram_mail(-123, 12, source[0])
        self.reopen()
        self.assertIsNone(self.store.notification_outbox())
        for telegram_id in (10, 11, 12):
            self.assertEqual(self.store.telegram_mail(-123, telegram_id, ('first',)), source)
            self.assertIsNone(self.store.telegram_mail(-123, telegram_id, ('removed',)))
            self.assertIsNone(self.store.telegram_mail(-124, telegram_id, ('first',)))

    def test_mapping_failure_rolls_back_delivery_ack(self):
        source = self.save([mail()], parts=['Notice'])[0]
        self.store.db.execute("CREATE TRIGGER mapping_failure BEFORE INSERT ON telegram_mail BEGIN SELECT RAISE(FAIL, 'disk write failed'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.notification_part_sent(source[0], telegram_message_id=10, chat_id=1)
        self.reopen()
        self.assertEqual(self.store.notification_outbox()['sent_parts'], 0)
        self.assertIsNone(self.store.telegram_mail(1, 10, ('first',)))

    def test_forget_mapping_only_clears_requested_chat_message_and_is_idempotent(self):
        source = self.save([mail()])[0]
        for chat_id, telegram_id in ((1, 10), (1, 11), (2, 10)):
            self.store.remember_telegram_mail(chat_id, telegram_id, source[0])
        self.store.forget_telegram_mail(1, 10)
        self.store.forget_telegram_mail(1, 10)
        self.reopen()
        self.assertIsNone(self.store.telegram_mail(1, 10, ('first',)))
        self.assertEqual(self.store.telegram_mail(1, 11, ('first',)), source)
        self.assertEqual(self.store.telegram_mail(2, 10, ('first',)), source)

    def test_mapping_validation_and_collision_do_not_ack_or_overwrite(self):
        sources = self.save([mail(uid=1), mail(uid=2)], parts=['Notice'])
        self.store.remember_telegram_mail(1, 10, sources[0][0])
        self.store.remember_telegram_mail(1, 10, sources[0][0])
        for kwargs in ({'telegram_message_id': 10}, {'chat_id': 1},
                       {'telegram_message_id': 0, 'chat_id': 1},
                       {'telegram_message_id': 10, 'chat_id': 1}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.store.notification_part_sent(sources[1][0], **kwargs)
        self.assertEqual(self.store.telegram_mail(1, 10, ('first',)), sources[0])
        self.assertEqual(self.store.notification_outbox()['sent_parts'], 0)
        with self.assertRaises(ValueError):
            self.store.remember_telegram_mail(1, 12, 999)

    def test_legacy_notification_lookup_requires_exact_unique_text_and_stream(self):
        first = self.save([mail()], parts=['Exact text\nSubject', 'Second part'])[0]
        self.assertEqual(self.store.notification_mail('Exact text\nSubject', ('first',)), first)
        self.assertEqual(self.store.notification_mail('Second part', ('first',)), first)
        for text in ('Exact text', 'Subject', 'exact text\nSubject', 'Exact text\nSubject ', ''):
            self.assertIsNone(self.store.notification_mail(text, ('first',)))
        self.assertIsNone(self.store.notification_mail('Second part', ('removed',)))
        self.assertIsNone(self.store.notification_mail('Second part', ()))
        self.save([mail(account='removed')], parts=['Second part'])
        self.assertEqual(self.store.notification_mail('Second part', ('first',)), first)
        self.assertIsNone(self.store.notification_mail('Second part', ('first', 'removed')))
        self.save([mail(uid=2)], parts=['Second part'])
        self.assertIsNone(self.store.notification_mail('Second part', ('first',)))

    def test_legacy_notice_matches_only_exact_digest_off_rendering(self):
        promise = ' Письмо также войдёт в сводку.'
        text = 'Письмо\nКраткая выдержка; полный текст — в почте.'
        source = self.save([mail()], parts=[text + promise])[0]
        self.reopen()
        self.assertEqual(self.store.notification_mail(text + promise, ('first',)), source)
        self.assertEqual(self.store.notification_mail(text, ('first',)), source)
        for candidate in (text[:-1], text + ' ', text + promise[:-1], 'Краткая выдержка; полный текст — в почте.'):
            with self.subTest(candidate=candidate):
                self.assertIsNone(self.store.notification_mail(candidate, ('first',)))
        self.save([mail(uid=2)], parts=['Another' + promise + ' Extra text'])
        self.assertIsNone(self.store.notification_mail('Another Extra text', ('first',)))
        self.assertIsNone(self.store.notification_mail('Another', ('first',)))

    def test_legacy_notice_digest_off_variant_must_be_unique_across_notices(self):
        promise = ' Письмо также войдёт в сводку.'
        source = self.save([mail()], parts=['Same notice' + promise])[0]
        self.save([mail(uid=2)], parts=['Same notice'])
        self.reopen()
        self.assertIsNone(self.store.notification_mail('Same notice', ('first',)))
        self.assertEqual(self.store.notification_mail('Same notice' + promise, ('first',)), source)

    def test_prune_removes_orphan_mappings_and_never_reuses_ids_or_rules(self):
        first = self.save([mail()], parts=['Notice'])[0]
        self.store.notification_part_sent(first[0], telegram_message_id=10, chat_id=1)
        self.store.exclude_pending(lambda message: True, ('first',), include_sent=True)
        rule = self.store.blacklist_add('domain', 'ozon.ru')
        self.store.blacklist_remove(rule)
        with self.store.db:
            self.store.db.execute('UPDATE messages SET created_at=0')
            self.store.db.execute('UPDATE notifications SET created_at=0')
        self.store.prune(1)
        self.reopen()
        self.assertIsNone(self.store.telegram_mail(1, 10, ('first',)))
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM telegram_mail').fetchone()[0], 0)
        self.store.seed_blacklist(('ozon.ru',))
        self.assertFalse(self.store.blacklist_matches('a@ozon.ru'))
        second = self.save([mail(uid=2)])[0]
        self.assertGreater(second[0], first[0])
        self.assertIsNone(self.store.get_message(first[0], ('first',)))
        self.assertGreater(self.store.blacklist_add('domain', 'example.org'), rule)

    def test_prune_keeps_mapping_while_source_mail_is_retained(self):
        source = self.save([mail()], parts=['Notice'])[0]
        self.store.notification_part_sent(source[0], telegram_message_id=10, chat_id=1)
        with self.store.db:
            self.store.db.execute('UPDATE messages SET created_at=0')
            self.store.db.execute('UPDATE notifications SET created_at=0')
        self.store.prune(1)
        self.assertIsNone(self.store.notification_mail('Notice', ('first',)))
        self.assertEqual(self.store.telegram_mail(1, 10, ('first',)), source)

    def test_additive_migration_preserves_old_mail_checkpoint_and_outbox(self):
        source = self.save([mail()], parts=['Notice'])[0]
        with self.store.db:
            self.store.db.execute('DROP TABLE blacklist_rules')
            self.store.db.execute('DROP TABLE telegram_mail')
            self.store.db.execute("DELETE FROM settings WHERE key='_mail_id_high_watermark'")
        self.reopen()
        self.assertEqual(self.store.get_message(source[0], ('first',)), source[1])
        self.assertEqual(self.store.notification_outbox()['parts'], ['Notice'])
        self.assertEqual(self.store.checkpoint('first', 'first-binding'), (41, 1))
        self.assertEqual(self.store.blacklist_page(), (0, []))
        self.store.exclude_pending(lambda message: True, ('first',))
        with self.store.db:
            self.store.db.execute('UPDATE messages SET created_at=0')
            self.store.db.execute('UPDATE notifications SET created_at=0')
        self.store.prune(1)
        self.assertGreater(self.save([mail(uid=2)])[0][0], source[0])


if __name__ == '__main__':
    unittest.main()
