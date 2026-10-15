"""Durable IMAP cursors and an outbox; checkpoint and mail insert are atomic."""
from dataclasses import asdict
from pathlib import Path
from collections.abc import Callable
import json
import fcntl
import hashlib
import os
import sqlite3
import time

from .models import MailMessage, PollResult
from .config import ConfigError
from .filtering import normalize_address, normalize_domain, sender_address


class Store:
    def __init__(self, path: str):
        self.lock = None
        if path != ":memory:":
            parent = Path(path).parent
            parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.lock = open(path + ".lock", "a")
            os.chmod(path + ".lock", 0o600)
            try:
                fcntl.flock(self.lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                self.lock.close()
                raise RuntimeError("С этой базой уже работает другой процесс") from None
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS checkpoints (
            account_id TEXT PRIMARY KEY, binding TEXT NOT NULL,
            uidvalidity INTEGER NOT NULL, last_uid INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY, account_id TEXT NOT NULL,
            uidvalidity INTEGER NOT NULL, uid INTEGER NOT NULL,
            payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
            created_at REAL NOT NULL,
            UNIQUE(account_id, uidvalidity, uid)
        );
        CREATE TABLE IF NOT EXISTS digests (
            id INTEGER PRIMARY KEY, parts TEXT NOT NULL, message_ids TEXT NOT NULL,
            sent_parts INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0,
            created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS notifications (
            message_id INTEGER PRIMARY KEY, parts TEXT NOT NULL,
            sent_parts INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0,
            created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS mail_identities (
            account_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
            created_at REAL NOT NULL, PRIMARY KEY(account_id, fingerprint)
        );
        CREATE TABLE IF NOT EXISTS blacklist_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL CHECK(kind IN ('address','domain')),
            value TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1
                CHECK(enabled IN (0,1)), created_at REAL NOT NULL,
            UNIQUE(kind,value)
        );
        CREATE TABLE IF NOT EXISTS telegram_mail (
            chat_id TEXT NOT NULL, telegram_message_id INTEGER NOT NULL,
            mail_id INTEGER NOT NULL,
            PRIMARY KEY(chat_id,telegram_message_id)
        );
        CREATE INDEX IF NOT EXISTS messages_status ON messages(status, id);
        CREATE INDEX IF NOT EXISTS notifications_status ON notifications(status, message_id);
        CREATE INDEX IF NOT EXISTS telegram_mail_source ON telegram_mail(mail_id);
        """)
        with self.db:
            last_id = max(int(self.get('_mail_id_high_watermark', '0')),
                          self.db.execute("SELECT COALESCE(MAX(id),0) FROM messages").fetchone()[0])
            self.db.execute("INSERT INTO settings VALUES ('_mail_id_high_watermark',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(last_id),))
        if path != ":memory:":
            os.chmod(path, 0o600)

    def close(self):
        self.db.close()
        if self.lock is not None:
            self.lock.close()

    def get(self, key: str, default=None):
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set(self, key: str, value):
        self.set_many({key: value})

    def set_many(self, values: dict):
        with self.db:
            for key, value in values.items():
                self.db.execute("INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))

    def seed_blacklist(self, domains):
        """Import configuration once per domain; disabled tombstones win."""
        values = [normalize_domain(domain) for domain in domains]
        with self.db:
            for value in values:
                self.db.execute("INSERT OR IGNORE INTO blacklist_rules(kind,value,created_at) VALUES ('domain',?,?)", (value, time.time()))

    def blacklist_add(self, kind: str, value: str) -> int:
        if kind not in ('address', 'domain'):
            raise ValueError("Invalid blacklist rule kind")
        value = normalize_address(value) if kind == 'address' else normalize_domain(value)
        with self.db:
            self.db.execute("""INSERT INTO blacklist_rules(kind,value,created_at) VALUES (?,?,?)
                ON CONFLICT(kind,value) DO UPDATE SET enabled=1""", (kind, value, time.time()))
            return self.db.execute("SELECT id FROM blacklist_rules WHERE kind=? AND value=?", (kind, value)).fetchone()[0]

    def blacklist_remove(self, rule_id: int) -> bool:
        """Disable a known rule, including an already-disabled one, without replay."""
        with self.db:
            result = self.db.execute("UPDATE blacklist_rules SET enabled=0 WHERE id=?", (rule_id,))
            return result.rowcount > 0

    @staticmethod
    def _check_page(offset: int, limit: int):
        if type(offset) is not int or offset < 0 or type(limit) is not int or limit < 1:
            raise ValueError("Invalid page")

    def blacklist_page(self, offset: int = 0, limit: int = 8) -> tuple[int, list[dict]]:
        self._check_page(offset, limit)
        total = self.db.execute("SELECT COUNT(*) FROM blacklist_rules WHERE enabled=1").fetchone()[0]
        rows = self.db.execute("SELECT * FROM blacklist_rules WHERE enabled=1 ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset)).fetchall()
        return total, [dict(row) for row in rows]

    def blacklist_matching(self, sender: str) -> list[dict]:
        address = sender_address(sender)
        if address is None:
            return []
        domain = address.rsplit('@', 1)[1]
        rows = self.db.execute("SELECT * FROM blacklist_rules WHERE enabled=1 ORDER BY id DESC").fetchall()
        return [dict(row) for row in rows if (
            row['kind'] == 'address' and address == row['value']
        ) or (
            row['kind'] == 'domain' and (domain == row['value'] or domain.endswith('.' + row['value']))
        )]

    def blacklist_matches(self, sender: str) -> bool:
        return bool(self.blacklist_matching(sender))

    def get_message(self, message_id: int, account_ids: tuple[str, ...]) -> MailMessage | None:
        if not account_ids:
            return None
        marks = ','.join('?' for _ in account_ids)
        row = self.db.execute(f"SELECT payload FROM messages WHERE id=? AND account_id IN ({marks})", (message_id, *account_ids)).fetchone()
        return MailMessage(**json.loads(row['payload'])) if row else None

    def browse_mail(self, excluded: bool, account_ids: tuple[str, ...], offset: int = 0,
                    limit: int = 8) -> tuple[int, list[tuple[int, MailMessage]]]:
        self._check_page(offset, limit)
        if not account_ids:
            return 0, []
        marks = ','.join('?' for _ in account_ids)
        comparison = '=' if excluded else '!='
        clause = f"status {comparison} 'excluded' AND account_id IN ({marks})"
        total = self.db.execute(f"SELECT COUNT(*) FROM messages WHERE {clause}", account_ids).fetchone()[0]
        rows = self.db.execute(f"SELECT id,payload FROM messages WHERE {clause} ORDER BY id DESC LIMIT ? OFFSET ?", (*account_ids, limit, offset)).fetchall()
        return total, [(row['id'], MailMessage(**json.loads(row['payload']))) for row in rows]

    def checkpoint(self, account_id: str, binding: str):
        row = self.db.execute("SELECT * FROM checkpoints WHERE account_id=?", (account_id,)).fetchone()
        if row and row["binding"] != binding:
            raise ConfigError(f"Ящик {account_id} заменён: задайте новый id для нового адреса, сервера или папки")
        if not row:
            return None
        return row["uidvalidity"], row["last_uid"]

    def save_poll(self, account_id: str, binding: str, result: PollResult, *,
                  notification_parts: Callable[[MailMessage], list[str]] | None = None,
                  exclude_mail: Callable[[MailMessage], bool] | None = None,
                  identity_account_id: str | None = None):
        identity_scope = account_id if identity_account_id is None else identity_account_id
        if not isinstance(identity_scope, str) or not identity_scope:
            raise ValueError("Invalid mail identity account")
        with self.db:
            self.checkpoint(account_id, binding)
            if result.uidvalidity <= 0 or result.last_uid < 0:
                raise ValueError("Invalid IMAP cursor")
            # Callback buttons keep source IDs. Never reuse an ID after pruning
            # an old message, including on databases created before this feature.
            last_id = max(int(self.get('_mail_id_high_watermark', '0')),
                          self.db.execute("SELECT COALESCE(MAX(id),0) FROM messages").fetchone()[0])
            for mail in result.messages:
                if mail.account_id != account_id or mail.uidvalidity != result.uidvalidity or not 0 < mail.uid <= result.last_uid:
                    raise ValueError("Inconsistent mailbox result")
                # A mailbox rebuild or folder move can give mail new IMAP IDs.
                # Nonempty Message-ID plus content/headers avoids replaying it;
                # email without Message-ID remains deduplicated by IMAP identity.
                if mail.message_id:
                    identity = json.dumps([mail.message_id, mail.sender, mail.subject, mail.date, mail.body], ensure_ascii=False)
                    fingerprint = hashlib.sha256(identity.encode()).hexdigest()
                    known = self.db.execute("INSERT OR IGNORE INTO mail_identities VALUES (?,?,?)", (identity_scope, fingerprint, time.time()))
                    if not known.rowcount:
                        continue
                inserted = self.db.execute("INSERT OR IGNORE INTO messages(id,account_id,uidvalidity,uid,payload,created_at) VALUES (?,?,?,?,?,?)", (last_id + 1, account_id, mail.uidvalidity, mail.uid, json.dumps(asdict(mail), ensure_ascii=False), time.time()))
                if inserted.rowcount:
                    last_id = inserted.lastrowid
                if inserted.rowcount and exclude_mail is not None and exclude_mail(mail):
                    # Retain the IMAP identity and cursor even for filtered
                    # mail: it must not be replayed at the next poll/rebuild.
                    self.db.execute("UPDATE messages SET status='excluded' WHERE id=?", (inserted.lastrowid,))
                    continue
                # Only a new insert gets a notice. Enabling notifications never
                # replays stored backlog, and formatter errors roll back the
                # mail, identity, outbox, and cursor as one transaction.
                if inserted.rowcount and notification_parts is not None:
                    parts = notification_parts(mail)
                    if not isinstance(parts, list) or not parts or not all(isinstance(part, str) and part for part in parts):
                        raise ValueError("Empty or invalid notification")
                    self.db.execute("INSERT INTO notifications(message_id,parts,created_at) VALUES (?,?,?)", (inserted.lastrowid, json.dumps(parts, ensure_ascii=False), time.time()))
            self.db.execute("INSERT INTO settings VALUES ('_mail_id_high_watermark',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(last_id),))
            self.db.execute("INSERT INTO checkpoints VALUES (?,?,?,?) ON CONFLICT(account_id) DO UPDATE SET binding=excluded.binding,uidvalidity=excluded.uidvalidity,last_uid=excluded.last_uid", (account_id, binding, result.uidvalidity, result.last_uid))

    def exclude_pending(self, predicate: Callable[[MailMessage], bool],
                        account_ids: tuple[str, ...], *, include_sent: bool = False) -> int:
        """Exclude backlog atomically; return newly excluded source-mail count.

        A pending notice may outlive its already-sent digest. Cancel that notice
        without rewriting delivery history. A mixed pending digest is immutable,
        so cancel it and let the remaining queued mail form a clean new digest.
        Its acknowledged chunks remain recorded and cannot be recalled.
        With include_sent, also move delivered source mail to the excluded archive;
        completed notifications and digests retain their delivery history.
        """
        if not account_ids:
            return 0
        marks = ",".join("?" for _ in account_ids)
        excluded = 0
        with self.db:
            rows = self.db.execute(f"""
                SELECT id,payload,status FROM messages
                WHERE account_id IN ({marks}) AND (
                    status IN ('pending','queued') OR (? AND status='sent') OR EXISTS (
                        SELECT 1 FROM notifications
                        WHERE notifications.message_id=messages.id
                            AND notifications.status='pending'
                    )
                ) ORDER BY id
                """, (*account_ids, include_sent)).fetchall()
            for row in rows:
                if not predicate(MailMessage(**json.loads(row["payload"]))):
                    continue
                if row["status"] in ("pending", "queued") or (include_sent and row['status'] == 'sent'):
                    self.db.execute("UPDATE messages SET status='excluded' WHERE id=?", (row["id"],))
                    excluded += 1
                self.db.execute("UPDATE notifications SET status='excluded',retry_at=0 WHERE message_id=? AND status='pending'", (row["id"],))

            for digest in self.db.execute("SELECT id,message_ids FROM digests WHERE status='pending'").fetchall():
                message_ids = json.loads(digest["message_ids"])
                statuses = [self.db.execute("SELECT status FROM messages WHERE id=?", (mid,)).fetchone()
                            for mid in message_ids]
                if not any(row is not None and row["status"] == "excluded" for row in statuses):
                    continue
                self.db.execute("UPDATE digests SET status='cancelled',retry_at=0 WHERE id=?", (digest["id"],))
                for mid in message_ids:
                    self.db.execute("UPDATE messages SET status='pending' WHERE id=? AND status='queued'", (mid,))
        return excluded

    def pending(self, limit: int, account_ids: tuple[str, ...]):
        marks = ",".join("?" for _ in account_ids)
        rows = self.db.execute(f"SELECT id,payload FROM messages WHERE status='pending' AND account_id IN ({marks}) ORDER BY id LIMIT ?", (*account_ids, limit)).fetchall()
        return [(row["id"], MailMessage(**json.loads(row["payload"]))) for row in rows]

    def cancel_pending_digests(self) -> int:
        """Cancel digest delivery and requeue source mail as one transaction.

        Already acknowledged chunks remain recorded. Notifications and mail
        that is already sent or excluded have independent state and are kept.
        """
        with self.db:
            rows = self.db.execute("SELECT id,message_ids FROM digests WHERE status='pending' ORDER BY id").fetchall()
            for row in rows:
                self.db.execute("UPDATE digests SET status='cancelled',retry_at=0 WHERE id=?", (row["id"],))
                for message_id in json.loads(row["message_ids"]):
                    self.db.execute("UPDATE messages SET status='pending' WHERE id=? AND status='queued'", (message_id,))
        return len(rows)

    def queue_digest(self, message_ids: list[int], parts: list[str]):
        if not message_ids or not parts:
            raise ValueError("Empty digest")
        with self.db:
            if self.db.execute("SELECT 1 FROM digests WHERE status='pending'").fetchone():
                raise ValueError("An outbox digest is already pending")
            cursor = self.db.execute("INSERT INTO digests(parts,message_ids,created_at) VALUES (?,?,?)", (json.dumps(parts, ensure_ascii=False), json.dumps(message_ids), time.time()))
            for message_id in message_ids:
                updated = self.db.execute("UPDATE messages SET status='queued' WHERE id=? AND status='pending'", (message_id,))
                if updated.rowcount != 1:
                    raise ValueError("Message no longer pending")
            return cursor.lastrowid

    def outbox(self):
        row = self.db.execute("SELECT * FROM digests WHERE status='pending' ORDER BY id LIMIT 1").fetchone()
        if row is None:
            return None
        result = dict(row)
        result["parts"] = json.loads(result["parts"])
        result["message_ids"] = json.loads(result["message_ids"])
        return result

    def part_sent(self, digest_id: int):
        with self.db:
            row = self.db.execute("SELECT * FROM digests WHERE id=? AND status='pending'", (digest_id,)).fetchone()
            if not row:
                raise ValueError("Unknown digest")
            sent_parts = row["sent_parts"] + 1
            parts = json.loads(row["parts"])
            if sent_parts > len(parts):
                raise ValueError("Too many parts")
            status = "sent" if sent_parts == len(parts) else "pending"
            self.db.execute("UPDATE digests SET sent_parts=?,status=?,attempts=0,retry_at=0 WHERE id=?", (sent_parts, status, digest_id))
            if status == "sent":
                for mid in json.loads(row["message_ids"]):
                    self.db.execute("UPDATE messages SET status='sent' WHERE id=?", (mid,))

    def delivery_failed(self, digest_id: int, retry_after: int | None = None):
        with self.db:
            row = self.db.execute("SELECT attempts FROM digests WHERE id=?", (digest_id,)).fetchone()
            attempts = row[0] + 1
            wait = max(2, retry_after or min(3600, 2 ** min(attempts, 11)))
            self.db.execute("UPDATE digests SET attempts=?,retry_at=? WHERE id=?", (attempts, time.time() + wait, digest_id))

    def notification_outbox(self):
        row = self.db.execute("SELECT * FROM notifications WHERE status='pending' ORDER BY message_id LIMIT 1").fetchone()
        if row is None:
            return None
        result = dict(row)
        result["parts"] = json.loads(result["parts"])
        return result

    def notification_part_sent(self, message_id: int, *, telegram_message_id: int | None = None,
                               chat_id: str | int | None = None):
        if (telegram_message_id is None) != (chat_id is None):
            raise ValueError("Telegram chat and message ID must be provided together")
        with self.db:
            row = self.db.execute("SELECT * FROM notifications WHERE message_id=? AND status='pending'", (message_id,)).fetchone()
            if row is None:
                raise ValueError("Unknown notification")
            sent_parts = row["sent_parts"] + 1
            if sent_parts > len(json.loads(row["parts"])):
                raise ValueError("Too many notification parts")
            status = "sent" if sent_parts == len(json.loads(row["parts"])) else "pending"
            self.db.execute("UPDATE notifications SET sent_parts=?,status=?,attempts=0,retry_at=0 WHERE message_id=?", (sent_parts, status, message_id))
            if telegram_message_id is not None:
                self._remember_telegram_mail(chat_id, telegram_message_id, message_id)
            # A notice is independent of the morning digest: the source mail
            # remains pending (or queued) until its digest is acknowledged.

    @staticmethod
    def _telegram_key(chat_id: str | int, telegram_message_id: int) -> tuple[str, int]:
        if type(chat_id) not in (str, int) or not str(chat_id).strip():
            raise ValueError("Invalid Telegram chat")
        if type(telegram_message_id) is not int or telegram_message_id <= 0:
            raise ValueError("Invalid Telegram message ID")
        return str(chat_id), telegram_message_id

    def _remember_telegram_mail(self, chat_id: str | int, telegram_message_id: int, mail_id: int):
        key = self._telegram_key(chat_id, telegram_message_id)
        if self.db.execute("SELECT 1 FROM messages WHERE id=?", (mail_id,)).fetchone() is None:
            raise ValueError("Unknown mail")
        previous = self.db.execute("SELECT mail_id FROM telegram_mail WHERE chat_id=? AND telegram_message_id=?", key).fetchone()
        if previous is not None and previous['mail_id'] != mail_id:
            raise ValueError("Telegram message is already associated with another mail")
        self.db.execute("INSERT OR IGNORE INTO telegram_mail(chat_id,telegram_message_id,mail_id) VALUES (?,?,?)", (*key, mail_id))

    def remember_telegram_mail(self, chat_id: str | int, tg_id: int, mail_id: int):
        with self.db:
            self._remember_telegram_mail(chat_id, tg_id, mail_id)

    def forget_telegram_mail(self, chat_id: str | int, tg_id: int):
        key = self._telegram_key(chat_id, tg_id)
        with self.db:
            self.db.execute("DELETE FROM telegram_mail WHERE chat_id=? AND telegram_message_id=?", key)

    def telegram_mail(self, chat_id: str | int, telegram_message_id: int,
                      account_ids: tuple[str, ...]) -> tuple[int, MailMessage] | None:
        key = self._telegram_key(chat_id, telegram_message_id)
        if not account_ids:
            return None
        marks = ','.join('?' for _ in account_ids)
        row = self.db.execute(f"""SELECT messages.id,messages.payload FROM telegram_mail
            JOIN messages ON messages.id=telegram_mail.mail_id
            WHERE telegram_mail.chat_id=? AND telegram_mail.telegram_message_id=?
                AND messages.account_id IN ({marks})""", (*key, *account_ids)).fetchone()
        return (row['id'], MailMessage(**json.loads(row['payload']))) if row else None

    def notification_mail(self, text: str, account_ids: tuple[str, ...]) -> tuple[int, MailMessage] | None:
        """Resolve an exact unique notice, including its known digest-off rendering."""
        if not isinstance(text, str) or not text or not account_ids:
            return None
        marks = ','.join('?' for _ in account_ids)
        rows = self.db.execute(f"""SELECT messages.id,messages.payload,notifications.parts
            FROM notifications JOIN messages ON messages.id=notifications.message_id
            WHERE messages.account_id IN ({marks})""", account_ids).fetchall()
        # Delivery removes this trailing promise when summaries are disabled.
        # Recognize only that exact rendering, never a general prefix/substring.
        promise = ' Письмо также войдёт в сводку.'
        matches = [row for row in rows if any(
            text == part or (part.endswith(promise) and text == part[:-len(promise)])
            for part in json.loads(row['parts'])
        )]
        if len(matches) != 1:
            return None
        return matches[0]['id'], MailMessage(**json.loads(matches[0]['payload']))

    def notification_failed(self, message_id: int, retry_after: int | None = None):
        with self.db:
            row = self.db.execute("SELECT attempts FROM notifications WHERE message_id=? AND status='pending'", (message_id,)).fetchone()
            if row is None:
                raise ValueError("Unknown notification")
            attempts = row[0] + 1
            wait = max(2, retry_after or min(3600, 2 ** min(attempts, 11)))
            self.db.execute("UPDATE notifications SET attempts=?,retry_at=? WHERE message_id=?", (attempts, time.time() + wait, message_id))

    def notification_stats(self):
        counts = {row[0]: row[1] for row in self.db.execute("SELECT status,COUNT(*) FROM notifications GROUP BY status")}
        return {status: counts.get(status, 0) for status in ("pending", "sent")}

    def stats(self):
        counts = {row[0]: row[1] for row in self.db.execute("SELECT status,COUNT(*) FROM messages GROUP BY status")}
        return {status: counts.get(status, 0) for status in ("pending", "queued", "sent")}

    def excluded_count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM messages WHERE status='excluded'").fetchone()[0]

    def prune(self, days: int):
        cutoff = time.time() - days * 86400
        with self.db:
            self.db.execute("DELETE FROM notifications WHERE status IN ('sent','excluded') AND created_at<?", (cutoff,))
            self.db.execute("DELETE FROM messages WHERE status IN ('sent','excluded') AND created_at<? AND NOT EXISTS (SELECT 1 FROM notifications WHERE notifications.message_id=messages.id)", (cutoff,))
            self.db.execute("DELETE FROM telegram_mail WHERE NOT EXISTS (SELECT 1 FROM messages WHERE messages.id=telegram_mail.mail_id)")
            self.db.execute("DELETE FROM digests WHERE status IN ('sent','cancelled') AND created_at<?", (cutoff,))
            # Keep identities while any unsent mail remains: recovery must never
            # lose a pending item just because its fingerprint was aged out.
            if (not self.db.execute("SELECT 1 FROM messages WHERE status IN ('pending','queued')").fetchone()
                    and not self.db.execute("SELECT 1 FROM notifications WHERE status='pending'").fetchone()):
                self.db.execute("DELETE FROM mail_identities WHERE created_at<?", (cutoff,))
