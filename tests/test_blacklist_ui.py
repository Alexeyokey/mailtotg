"""Owner-only mailbox browsing and durable sender blocking, without network I/O."""

from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mail_summary_bot.config import AccountConfig, Config, ServiceConfig
from mail_summary_bot.models import MailMessage, PollResult
from mail_summary_bot.service import Service, account_binding, notification_text
from mail_summary_bot.store import Store
from mail_summary_bot.telegram import TelegramError


OWNER = 12345
BOT = 987654


def mail(uid=1, *, account="first", sender="offers@x5.ru", subject=None, body=None):
    return MailMessage(
        account, 41, uid, f"<{account}-{uid}@example.test>", sender,
        subject or f"Synthetic subject {account}-{uid}", "2026-10-15T09:00:00+03:00",
        body or f"Synthetic body {account}-{uid}",
    )


class Reader:
    def __init__(self):
        self.result = PollResult(41, 0, [])

    def poll(self, checkpoint):
        return self.result


class Telegram:
    chat_id = OWNER
    bot_id = BOT
    next_message_id = 500

    def __init__(self):
        self.updates = []
        self.sent = []
        self.edited = []
        self.answered = []
        self.registered_commands = []

    def get_updates(self, offset, *, timeout=None):
        return [update for update in self.updates if update["update_id"] >= offset]

    def send_chunk(self, text, *, reply_markup=None):
        type(self).next_message_id += 1
        self.sent.append({"message_id": type(self).next_message_id, "text": text,
                          "reply_markup": reply_markup})
        return type(self).next_message_id

    def send_text(self, text, *, reply_markup=None):
        self.send_chunk(text, reply_markup=reply_markup)

    def edit_text(self, message_id, text, *, reply_markup=None):
        self.edited.append({"message_id": message_id, "text": text,
                            "reply_markup": reply_markup})

    def answer_callback(self, callback_id, text="", *, show_alert=False):
        self.answered.append((callback_id, text, show_alert))

    def set_commands(self, commands):
        self.registered_commands.append(commands)

    def close(self):
        pass


class Summarizer:
    def summarize(self, messages):
        return "Synthetic digest"

    def close(self):
        pass


class BlacklistUITests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.path = str(Path(self.temp.name) / "state.sqlite3")
        self.accounts = (AccountConfig("first", "imap.example.test", "first@example.test", "TEST_PASSWORD"),)
        self.config = Config(self.accounts, ServiceConfig(
            database=self.path, notify_new_mail=True, schedule="manual"))
        self.store = Store(self.path)
        self.telegram = Telegram()
        self.reader = Reader()
        self.service = self.make_service()
        self.update_id = 0

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def make_service(self):
        return Service(self.config, store=self.store, readers=[self.reader],
                       telegram=self.telegram, summarizer=Summarizer())

    def reopen(self):
        self.service.close()
        self.store = Store(self.path)
        self.telegram = Telegram()
        self.service = self.make_service()

    def dispatch(self, payload):
        self.update_id += 1
        self.telegram.updates = [{"update_id": self.update_id, **payload}]
        self.service.commands()

    def command(self, text, *, sender=OWNER, chat=OWNER, chat_type="private", reply=None):
        message = {"message_id": 100 + self.update_id, "text": text,
                   "from": {"id": sender, "is_bot": False},
                   "chat": {"id": chat, "type": chat_type}}
        if reply is not None:
            message["reply_to_message"] = reply
        self.dispatch({"message": message})

    def callback(self, data, *, sender=OWNER, chat=OWNER, chat_type="private"):
        query = {"id": f"query-{self.update_id + 1}", "from": {"id": sender}, "data": data,
                 "message": {"message_id": 80, "chat": {"id": chat, "type": chat_type},
                             "from": {"id": BOT, "is_bot": True}}}
        self.dispatch({"callback_query": query})

    def save(self, message, *, notice=True):
        binding = account_binding(self.accounts[0]) if message.account_id == "first" else "foreign-binding"
        self.store.save_poll(
            message.account_id, binding, PollResult(41, message.uid, [message]),
            notification_parts=(lambda value: [notification_text(value)]) if notice else None,
            exclude_mail=self.service.excluded,
        )
        return self.store.db.execute(
            "SELECT id FROM messages WHERE account_id=? AND uid=?", (message.account_id, message.uid)
        ).fetchone()[0]

    def rules(self):
        return self.store.blacklist_page(0, 100)[1]

    @staticmethod
    def actions(render):
        markup = render["reply_markup"] or {}
        return [button["callback_data"] for row in markup.get("inline_keyboard", [])
                for button in row if "callback_data" in button]

    def last_render(self):
        return self.telegram.edited[-1] if self.telegram.edited else self.telegram.sent[-1]

    def test_commands_require_both_owner_sender_and_private_owner_chat(self):
        secret = mail(subject="Private mailbox subject")
        self.save(secret)
        for kwargs in ({"sender": OWNER + 1}, {"chat": OWNER + 1},
                       {"chat_type": "group"}, {"sender": None}):
            with self.subTest(kwargs=kwargs):
                self.command("/block offers@x5.ru", **kwargs)
                self.command("/mail", **kwargs)
        self.assertEqual(self.rules(), [])
        self.assertEqual(self.telegram.sent, [])
        self.command("/mail")
        self.assertIn(secret.subject, self.telegram.sent[-1]["text"])
        self.assertEqual(int(self.store.get("telegram_offset")), self.update_id + 1)

    def test_callbacks_require_both_owner_sender_and_private_owner_chat(self):
        message_id = self.save(mail())
        for kwargs in ({"sender": OWNER + 1}, {"chat": OWNER + 1},
                       {"chat_type": "supergroup"}, {"sender": None}):
            with self.subTest(kwargs=kwargs):
                self.callback(f"mb:block:{message_id}", **kwargs)
                self.callback(f"mb:v:{message_id}:i:0:0", **kwargs)
        self.assertEqual(self.rules(), [])
        self.assertEqual(self.telegram.sent, [])
        self.assertEqual(self.telegram.edited, [])
        self.assertEqual(self.store.excluded_count(), 0)
        self.assertEqual(int(self.store.get("telegram_offset")), self.update_id + 1)

    def test_notice_has_read_block_and_blocked_mail_actions_and_durable_mapping(self):
        message = mail()
        message_id = self.save(message)
        with patch("mail_summary_bot.service.time.time", return_value=1000):
            self.assertTrue(self.service.deliver_notifications(1000))
        notice = self.telegram.sent[-1]
        actions = self.actions(notice)
        self.assertIn(f"mb:block:{message_id}", actions)
        self.assertIn("mb:b:0", actions)
        self.assertTrue(any(action.startswith(f"mb:v:{message_id}:") for action in actions))
        self.assertEqual(self.store.telegram_mail(OWNER, notice["message_id"], self.service.account_ids),
                         (message_id, message))
        self.reopen()
        self.assertEqual(self.store.telegram_mail(OWNER, notice["message_id"], self.service.account_ids),
                         (message_id, message))

    def test_block_button_blocks_exact_address_and_preserves_other_x5_senders(self):
        unwanted = mail(sender="X5 Offers <offers@x5.ru>")
        legitimate = mail(2, sender="support@x5.ru")
        message_id = self.save(unwanted)
        self.save(legitimate)
        self.callback(f"mb:block:{message_id}")
        self.assertTrue(self.store.blacklist_matches("offers@x5.ru"))
        self.assertFalse(self.store.blacklist_matches("support@x5.ru"))
        self.assertFalse(self.store.blacklist_matches("offers@news.x5.ru"))
        self.assertEqual([(rule["kind"], rule["value"]) for rule in self.rules()],
                         [("address", "offers@x5.ru")])
        self.assertEqual([message for _, message in self.store.pending(10, self.service.account_ids)],
                         [legitimate])
        self.assertEqual(self.store.excluded_count(), 1)

    def test_block_cancels_pending_notice_and_mixed_digest_then_rebuilds_allowed_mail(self):
        first = self.save(mail())
        second = self.save(mail(2, sender="teacher@example.test"))
        self.store.queue_digest([first, second], ["Old digest includes blocked sender"])
        self.command("/block offers@x5.ru")
        self.assertIsNone(self.store.outbox())
        self.assertEqual(self.store.notification_outbox()["message_id"], second)
        self.assertEqual([mid for mid, _ in self.store.pending(10, self.service.account_ids)], [second])
        self.assertTrue(self.service.request_digest())
        self.assertTrue(self.service.build_digest(1000))
        self.assertNotIn("Old digest", "".join(self.store.outbox()["parts"]))

    def test_unblock_delivers_next_new_mail_without_replaying_excluded_backlog(self):
        self.command("/block offers@x5.ru")
        old = mail(subject="Old blocked mail")
        self.reader.result = PollResult(41, 1, [old])
        self.service.poll_mail()
        self.assertIsNone(self.store.notification_outbox())
        self.command("/unblock offers@x5.ru")
        self.assertFalse(self.store.blacklist_matches(old.sender))
        new = mail(2, subject="New allowed mail")
        self.reader.result = PollResult(41, 2, [old, new])
        self.service.poll_mail()
        self.service.next_send_at = 0
        with patch("mail_summary_bot.service.time.time", return_value=1000):
            self.assertTrue(self.service.deliver_notifications(1000))
        self.assertIn(new.subject, self.telegram.sent[-1]["text"])
        self.assertNotIn(old.subject, self.telegram.sent[-1]["text"])
        self.assertIsNone(self.store.notification_outbox())
        self.assertEqual(self.store.excluded_count(), 1)
        self.assertEqual(self.store.browse_mail(True, self.service.account_ids, 0, 10)[0], 1)

    def test_disabled_seeded_domain_stays_disabled_after_restart(self):
        self.config = replace(self.config, service=replace(
            self.config.service, excluded_sender_domains=("ozon.ru",)))
        self.reopen()
        self.assertTrue(self.service.excluded(mail(sender="offers@news.ozon.ru")))
        self.command("/unblock ozon.ru")
        self.assertFalse(self.service.excluded(mail(sender="offers@news.ozon.ru")))
        self.reopen()
        self.assertFalse(self.service.excluded(mail(sender="offers@news.ozon.ru")))
        self.assertEqual(self.rules(), [])
        self.reader.result = PollResult(41, 1, [mail(sender="offers@news.ozon.ru")])
        self.service.poll_mail()
        self.assertIsNotNone(self.store.notification_outbox())

    def test_add_rule_prompt_accepts_owner_input_and_cancel_clears_it(self):
        self.callback("mb:add")
        self.command("attacker@example.test", sender=OWNER + 1)
        self.assertEqual(self.rules(), [])
        self.command("offers@x5.ru")
        self.assertTrue(self.store.blacklist_matches("offers@x5.ru"))
        self.callback("mb:add")
        self.command("/cancel")
        self.command("teacher@example.test")
        self.assertFalse(self.store.blacklist_matches("teacher@example.test"))
        self.assertEqual(len(self.rules()), 1)

    def test_rule_list_remove_button_disables_rule(self):
        self.command("/block offers@x5.ru")
        rule_id = self.rules()[0]["id"]
        self.command("/blacklist")
        self.assertIn("offers@x5.ru", self.telegram.sent[-1]["text"])
        self.assertIn(f"mb:remove:{rule_id}:0", self.actions(self.telegram.sent[-1]))
        self.callback(f"mb:remove:{rule_id}:0")
        self.assertEqual(self.rules(), [])
        self.assertFalse(self.service.excluded(mail()))

    def test_invalid_rule_input_cannot_create_broad_or_partial_match(self):
        for value in ("X5 <offers@x5.ru>", "https://x5.ru", "*.x5.ru",
                      "offers@x5.ru teacher@example.test", "@x5.ru"):
            with self.subTest(value=value):
                self.command("/block " + value)
        self.assertEqual(self.rules(), [])
        self.assertFalse(self.service.excluded(mail()))

    def test_domain_rule_matches_subdomains_but_not_display_name_or_mail_content(self):
        self.command("/block ozon.ru")
        self.assertTrue(self.service.excluded(mail(sender="Offers <offers@news.ozon.ru>")))
        self.assertFalse(self.service.excluded(mail(sender='"Ozon" <teacher@example.test>')))
        self.assertFalse(self.service.excluded(mail(sender="offers@fakeozon.ru")))
        self.assertFalse(self.service.excluded(mail(sender="teacher@example.test", body="Read ozon.ru")))

    def test_blocked_mail_reading_is_paginated_and_never_reenqueues_notice(self):
        self.command("/block offers@x5.ru")
        message = mail(body="START " + "📨" * 5000 + " END-OF-ARCHIVE")
        message_id = self.save(message)
        self.command("/blocked")
        self.assertIn(message.subject, self.telegram.sent[-1]["text"])
        self.assertIn(f"mb:v:{message_id}:b:0:0", self.actions(self.telegram.sent[-1]))
        page = 0
        texts = []
        for _ in range(12):
            self.callback(f"mb:v:{message_id}:b:0:{page}")
            rendered = self.last_render()
            texts.append(rendered["text"])
            self.assertLessEqual(len(rendered["text"].encode("utf-16-le")) // 2, 4000)
            candidates = [action for action in self.actions(rendered)
                          if action.startswith(f"mb:v:{message_id}:b:0:")
                          and int(action.rsplit(":", 1)[1]) > page]
            if not candidates:
                break
            page = min(int(action.rsplit(":", 1)[1]) for action in candidates)
        self.assertGreater(len(texts), 1)
        self.assertIn("START", texts[0])
        self.assertIn("END-OF-ARCHIVE", texts[-1])
        self.assertIsNone(self.store.notification_outbox())
        self.assertEqual(self.store.excluded_count(), 1)

    def test_mail_command_includes_already_sent_mail_and_hides_excluded_mail(self):
        sent = mail(sender="teacher@example.test", subject="Already delivered letter")
        message_id = self.save(sent, notice=False)
        self.store.queue_digest([message_id], ["Sent digest"])
        self.store.part_sent(self.store.outbox()["id"])
        self.command("/block offers@x5.ru")
        blocked = mail(2, subject="Hidden blocked letter")
        self.save(blocked)
        for command in ("/mail", "/inbox"):
            self.command(command)
            self.assertIn(sent.subject, self.telegram.sent[-1]["text"])
            self.assertNotIn(blocked.subject, self.telegram.sent[-1]["text"])

    def test_blocking_already_delivered_mail_retains_it_in_archive(self):
        message = mail()
        message_id = self.save(message, notice=False)
        self.store.queue_digest([message_id], ["Sent digest"])
        self.store.part_sent(self.store.outbox()["id"])
        self.callback(f"mb:block:{message_id}")
        total, archived = self.store.browse_mail(True, self.service.account_ids, 0, 10)
        self.assertEqual((total, archived), (1, [(message_id, message)]))
        self.assertEqual(self.store.browse_mail(False, self.service.account_ids, 0, 10)[0], 0)
        self.assertIsNone(self.store.notification_outbox())
        self.assertIsNone(self.store.outbox())

    def test_archive_list_pagination_reaches_all_retained_messages_once(self):
        self.command("/block offers@x5.ru")
        expected = {self.save(mail(uid)) for uid in range(1, 15)}
        offset = 0
        visited = []
        for _ in range(10):
            self.callback(f"mb:b:{offset}")
            rendered = self.last_render()
            self.assertLessEqual(len(rendered["text"].encode("utf-16-le")) // 2, 4000)
            actions = self.actions(rendered)
            visited.extend(int(action.split(":")[2]) for action in actions
                           if action.startswith("mb:v:"))
            candidates = [int(action.rsplit(":", 1)[1]) for action in actions
                          if action.startswith("mb:b:") and int(action.rsplit(":", 1)[1]) > offset]
            if not candidates:
                break
            offset = min(candidates)
        self.assertEqual(set(visited), expected)
        self.assertEqual(len(visited), len(expected))
        self.assertGreater(offset, 0)
        self.assertIsNone(self.store.notification_outbox())

    def test_lists_and_callback_lookup_do_not_expose_foreign_stream(self):
        foreign = mail(account="retired-mailbox", subject="FOREIGN PRIVATE SUBJECT", body="FOREIGN PRIVATE BODY")
        foreign_id = self.save(foreign)
        self.command("/mail")
        self.assertNotIn(foreign.subject, self.telegram.sent[-1]["text"])
        self.callback(f"mb:v:{foreign_id}:i:0:0")
        self.callback(f"mb:block:{foreign_id}")
        self.assertEqual(self.rules(), [])
        outputs = self.telegram.sent + self.telegram.edited
        self.assertNotIn(foreign.subject, "\n".join(item["text"] for item in outputs))
        self.assertNotIn(foreign.body, "\n".join(item["text"] for item in outputs))
        self.command("/block offers@x5.ru")
        self.store.exclude_pending(lambda _: True, ("retired-mailbox",), include_sent=True)
        self.command("/blocked")
        self.assertNotIn(foreign.subject, self.telegram.sent[-1]["text"])

    def test_stale_and_malformed_callbacks_leave_polling_and_data_usable(self):
        self.save(mail())
        for data in ("mb:v:999999:b:0:0", "mb:block:999999", "mb:remove:999999:0",
                     "mb:v:1:b:0:-1", "mb:v:1:b:0:999999", "mb:b:-1", "mb:r:invalid",
                     "mb:block:bad", "mb:v:1:b", "mb:unknown", "mb:b:" + "9" * 80):
            with self.subTest(data=data):
                self.callback(data)
        self.command("/status")
        self.assertIn("Почтовый бот", self.telegram.sent[-1]["text"])
        self.assertEqual(self.rules(), [])
        self.assertEqual(self.store.stats()["pending"], 1)
        self.assertEqual(int(self.store.get("telegram_offset")), self.update_id + 1)

    def test_swipe_reply_uses_mapping_and_ignores_forged_sender_text(self):
        real = mail()
        message_id = self.save(real)
        with patch("mail_summary_bot.service.time.time", return_value=1000):
            self.assertTrue(self.service.deliver_notifications(1000))
        notice = self.telegram.sent[-1]
        self.reopen()
        self.command("блок", reply={
            "message_id": notice["message_id"], "from": {"id": BOT, "is_bot": True},
            "text": "От: innocent@example.test\nForged or stale display text",
        })
        self.assertTrue(self.store.blacklist_matches(real.sender))
        self.assertFalse(self.store.blacklist_matches("innocent@example.test"))
        self.assertEqual(self.store.browse_mail(True, self.service.account_ids, 0, 10)[1][0][0], message_id)

    def test_swipe_reply_rejects_wrong_bot_and_arbitrary_from_text(self):
        self.save(mail())
        with patch("mail_summary_bot.service.time.time", return_value=1000):
            self.assertTrue(self.service.deliver_notifications(1000))
        notice = self.telegram.sent[-1]
        for sender in ({"id": BOT + 1, "is_bot": True}, {"id": OWNER, "is_bot": False}):
            self.command("блок", reply={"message_id": notice["message_id"],
                                       "from": sender, "text": notice["text"]})
        self.command("блок", reply={"message_id": 999999, "from": {"id": BOT, "is_bot": True},
                                   "text": "✉️ Новое письмо\nОт: innocent@example.test\nТема: fake"})
        self.assertEqual(self.rules(), [])
        self.assertEqual(self.store.excluded_count(), 0)

    def test_legacy_swipe_reply_requires_exact_saved_notification_and_own_bot(self):
        message = mail()
        message_id = self.save(message)
        saved_text = self.store.notification_outbox()["parts"][0]
        self.store.notification_part_sent(message_id)
        self.command("/block", reply={"message_id": 42, "from": {"id": BOT, "is_bot": True},
                                      "text": saved_text + " changed"})
        self.assertEqual(self.rules(), [])
        self.command("/block", reply={"message_id": 42, "from": {"id": BOT, "is_bot": True},
                                      "text": saved_text})
        self.assertTrue(self.store.blacklist_matches(message.sender))
        self.assertEqual(self.store.excluded_count(), 1)

    def test_ui_handlers_independently_reject_unauthorized_actor(self):
        message_id = self.save(mail())
        forged = {"message_id": 42, "from": {"id": OWNER + 1},
                  "chat": {"id": OWNER, "type": "private"}, "text": "/block offers@x5.ru"}
        self.assertFalse(self.service.mailbox_ui.handle_message(forged))
        self.assertFalse(self.service.mailbox_ui.handle_callback({
            "id": "forged", "from": {"id": OWNER + 1}, "message": forged,
            "data": f"mb:block:{message_id}",
        }))
        self.assertEqual(self.rules(), [])
        self.assertEqual(self.telegram.sent, [])
        self.assertEqual(self.telegram.edited, [])

    def test_callback_rate_limit_retains_update_and_retries_action_after_deadline(self):
        message_id = self.save(mail(), notice=False)
        limited = TelegramError("Rate limited", status_code=429, retry_after=30)
        with patch.object(self.telegram, "answer_callback", side_effect=[limited, None]) as answer:
            with patch("mail_summary_bot.service.time.time", return_value=1000):
                with self.assertLogs("mail_summary_bot.service", level="WARNING"):
                    self.callback(f"mb:block:{message_id}")
            self.assertEqual(self.rules(), [])
            self.assertEqual(int(self.store.get("telegram_offset", "0")), 0)
            retry_at = float(self.store.get("telegram_command_retry_at", "0"))
            self.assertGreaterEqual(retry_at, 1030)
            with patch("mail_summary_bot.service.time.time", return_value=1029):
                self.service.commands()
            self.assertEqual(answer.call_count, 1)
            self.assertEqual(self.rules(), [])
            with patch("mail_summary_bot.service.time.time", return_value=retry_at):
                self.service.commands()
            self.assertEqual(answer.call_count, 2)
        self.assertTrue(self.store.blacklist_matches("offers@x5.ru"))
        self.assertEqual(int(self.store.get("telegram_offset")), self.update_id + 1)
        self.assertLessEqual(float(self.store.get("telegram_command_retry_at", "0")), retry_at)

    def test_callback_retry_deadline_survives_restart(self):
        message_id = self.save(mail(), notice=False)
        with patch.object(self.telegram, "answer_callback", side_effect=TelegramError(
                "Rate limited", status_code=429, retry_after=45)):
            with patch("mail_summary_bot.service.time.time", return_value=2000):
                with self.assertLogs("mail_summary_bot.service", level="WARNING"):
                    self.callback(f"mb:block:{message_id}")
        updates = self.telegram.updates
        retry_at = float(self.store.get("telegram_command_retry_at", "0"))
        self.assertGreaterEqual(retry_at, 2045)
        self.reopen()
        self.telegram.updates = updates
        with patch.object(self.telegram, "get_updates", wraps=self.telegram.get_updates) as poll:
            with patch("mail_summary_bot.service.time.time", return_value=2044):
                self.service.commands()
            poll.assert_not_called()
            self.assertEqual(self.rules(), [])
            with patch("mail_summary_bot.service.time.time", return_value=retry_at):
                self.service.commands()
            poll.assert_called_once()
        self.assertTrue(self.store.blacklist_matches("offers@x5.ru"))
        self.assertEqual(int(self.store.get("telegram_offset")), self.update_id + 1)

    def test_add_dialog_confirmation_retry_preserves_input_and_creates_one_rule(self):
        with patch("mail_summary_bot.service.time.time", return_value=1000):
            self.callback("mb:add")
        input_until = self.store.get("blacklist_input_until")
        self.assertGreater(float(input_until), 1001)
        offset_before_input = self.store.get("telegram_offset")
        with patch.object(self.telegram, "send_chunk", side_effect=TelegramError(
                "Rate limited", status_code=429, retry_after=30)):
            with patch("mail_summary_bot.service.time.time", return_value=1001):
                with self.assertLogs("mail_summary_bot.service", level="WARNING"):
                    self.command("offers@x5.ru")
        self.assertEqual(self.store.get("blacklist_input_until"), input_until)
        self.assertEqual(self.store.get("telegram_offset"), offset_before_input)
        self.assertEqual(len(self.rules()), 1)
        self.assertTrue(self.store.blacklist_matches("offers@x5.ru"))
        self.assertEqual(self.telegram.sent, [])
        retry_at = float(self.store.get("telegram_command_retry_at", "0"))
        self.assertGreaterEqual(retry_at, 1031)
        with patch("mail_summary_bot.service.time.time", return_value=1030):
            self.service.commands()
        self.assertEqual(self.telegram.sent, [])
        self.assertEqual(self.store.get("blacklist_input_until"), input_until)
        with patch("mail_summary_bot.service.time.time", return_value=retry_at):
            self.service.commands()
        self.assertEqual(len(self.telegram.sent), 1)
        self.assertIn("offers@x5.ru", self.telegram.sent[0]["text"])
        self.assertEqual(self.store.get("blacklist_input_until"), "0")
        self.assertEqual(int(self.store.get("telegram_offset")), self.update_id + 1)
        self.assertEqual(len(self.rules()), 1)

    def test_expired_callback_acknowledgement_does_not_discard_block_action(self):
        message_id = self.save(mail(), notice=False)
        with patch.object(self.telegram, "answer_callback", side_effect=TelegramError(
                "Query is too old", status_code=400)) as answer:
            self.callback(f"mb:block:{message_id}")
        answer.assert_called_once()
        self.assertTrue(self.store.blacklist_matches("offers@x5.ru"))
        self.assertEqual(self.store.excluded_count(), 1)
        self.assertEqual(int(self.store.get("telegram_offset")), self.update_id + 1)

    def test_menu_registration_failure_retries_in_tick_only_after_deadline(self):
        self.service.next_mail_poll = float("inf")
        limited = TelegramError("Rate limited", status_code=429, retry_after=30)
        with patch.object(self.telegram, "set_commands", side_effect=[limited, None]) as register:
            with patch("mail_summary_bot.service.time.time", return_value=1000), \
                    patch("mail_summary_bot.service.time.monotonic", return_value=100):
                with self.assertLogs("mail_summary_bot.service", level="WARNING"):
                    self.service.setup_menu()
            self.assertTrue(self.service.menu_setup_pending)
            self.assertGreaterEqual(self.service.next_menu_attempt, 130)
            self.assertIsNone(self.store.get("mailbox_ui_version"))
            self.assertEqual(self.telegram.sent, [])
            with patch("mail_summary_bot.service.time.time", return_value=1029), \
                    patch("mail_summary_bot.service.time.monotonic", return_value=129):
                self.service.tick(poll_commands=False)
            self.assertEqual(register.call_count, 1)
            retry_at = self.service.next_menu_attempt
            with patch("mail_summary_bot.service.time.time", return_value=retry_at + 900), \
                    patch("mail_summary_bot.service.time.monotonic", return_value=retry_at):
                self.service.tick(poll_commands=False)
            self.assertEqual(register.call_count, 2)
            self.assertFalse(self.service.menu_setup_pending)
            self.assertEqual(self.store.get("mailbox_ui_version"), "1")
            self.assertEqual(len(self.telegram.sent), 1)
            with patch("mail_summary_bot.service.time.time", return_value=retry_at + 960), \
                    patch("mail_summary_bot.service.time.monotonic", return_value=retry_at + 60):
                self.service.tick(poll_commands=False)
            self.assertEqual(register.call_count, 2)
            self.assertEqual(len(self.telegram.sent), 1)

    def test_failed_welcome_does_not_mark_menu_setup_complete(self):
        self.service.next_mail_poll = float("inf")
        with patch.object(self.telegram, "send_text", side_effect=TelegramError(
                "Temporary outage", status_code=503)):
            with patch("mail_summary_bot.service.time.time", return_value=1000), \
                    patch("mail_summary_bot.service.time.monotonic", return_value=100):
                with self.assertLogs("mail_summary_bot.service", level="WARNING"):
                    self.service.setup_menu()
        self.assertTrue(self.service.menu_setup_pending)
        self.assertIsNone(self.store.get("mailbox_ui_version"))
        self.assertEqual(len(self.telegram.registered_commands), 1)
        with patch("mail_summary_bot.service.time.time", return_value=self.service.next_menu_attempt + 900), \
                patch("mail_summary_bot.service.time.monotonic", return_value=self.service.next_menu_attempt):
            self.service.tick(poll_commands=False)
        self.assertFalse(self.service.menu_setup_pending)
        self.assertEqual(self.store.get("mailbox_ui_version"), "1")
        self.assertEqual(len(self.telegram.sent), 1)
        self.assertEqual(len(self.telegram.registered_commands), 2)

    def test_successful_menu_setup_registers_on_restart_without_repeating_welcome(self):
        self.service.setup_menu()
        self.assertEqual(self.store.get("mailbox_ui_version"), "1")
        self.assertEqual(len(self.telegram.registered_commands), 1)
        self.assertEqual(len(self.telegram.sent), 1)
        self.assertIn("keyboard", self.telegram.sent[0]["reply_markup"])
        self.reopen()
        self.service.setup_menu()
        self.assertEqual(len(self.telegram.registered_commands), 1)
        self.assertEqual(self.telegram.sent, [])
        self.assertFalse(self.service.menu_setup_pending)

    def test_editing_detail_switches_mapping_to_new_letter_and_menu_clears_it(self):
        first, second = mail(), mail(2, sender="teacher@example.test")
        first_id, second_id = self.save(first, notice=False), self.save(second, notice=False)
        self.callback(f"mb:v:{first_id}:i:0:0")
        self.assertEqual(self.store.telegram_mail(OWNER, 80, self.service.account_ids), (first_id, first))
        self.callback(f"mb:v:{second_id}:i:0:0")
        self.assertEqual(self.store.telegram_mail(OWNER, 80, self.service.account_ids), (second_id, second))
        self.callback("mb:i:0")
        self.assertIsNone(self.store.telegram_mail(OWNER, 80, self.service.account_ids))
        self.command("блок", reply={"message_id": 80, "from": {"id": BOT, "is_bot": True},
                                   "text": self.telegram.edited[-1]["text"]})
        self.assertEqual(self.rules(), [])

    def test_failed_detail_edit_invalidates_old_mapping_before_network_call(self):
        first, second = self.save(mail(), notice=False), self.save(mail(2), notice=False)
        self.callback(f"mb:v:{first}:i:0:0")

        def fail_edit(*args, **kwargs):
            self.assertIsNone(self.store.telegram_mail(OWNER, 80, self.service.account_ids))
            raise TelegramError("Rate limited", status_code=429, retry_after=30)

        with patch.object(self.telegram, "edit_text", side_effect=fail_edit):
            with self.assertLogs("mail_summary_bot.service", level="WARNING"):
                self.callback(f"mb:v:{second}:i:0:0")
        self.assertIsNone(self.store.telegram_mail(OWNER, 80, self.service.account_ids))

    def test_uneditable_detail_fallback_maps_only_replacement_message(self):
        first, second = mail(), mail(2)
        first_id, second_id = self.save(first, notice=False), self.save(second, notice=False)
        self.callback(f"mb:v:{first_id}:i:0:0")
        with patch.object(self.telegram, "edit_text", side_effect=TelegramError(
                "Message is unavailable", status_code=400)):
            self.callback(f"mb:v:{second_id}:i:0:0")
        replacement_id = self.telegram.sent[-1]["message_id"]
        self.assertIsNone(self.store.telegram_mail(OWNER, 80, self.service.account_ids))
        self.assertEqual(self.store.telegram_mail(OWNER, replacement_id, self.service.account_ids),
                         (second_id, second))


if __name__ == "__main__":
    unittest.main()
