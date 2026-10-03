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
        CREATE INDEX IF NOT EXISTS messages_status ON messages(status, id);
        CREATE INDEX IF NOT EXISTS notifications_status ON notifications(status, message_id);
        """)
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

    def checkpoint(self, account_id: str, binding: str):
        row = self.db.execute("SELECT * FROM checkpoints WHERE account_id=?", (account_id,)).fetchone()
        if row and row["binding"] != binding:
            raise ConfigError(f"Ящик {account_id} заменён: задайте новый id для нового адреса, сервера или папки")
        if not row:
            return None
        return row["uidvalidity"], row["last_uid"]

    def save_poll(self, account_id: str, binding: str, result: PollResult, *,
                  notification_parts: Callable[[MailMessage], list[str]] | None = None):
        with self.db:
            self.checkpoint(account_id, binding)
            if result.uidvalidity <= 0 or result.last_uid < 0:
                raise ValueError("Invalid IMAP cursor")
            for mail in result.messages:
                if mail.account_id != account_id or mail.uidvalidity != result.uidvalidity or not 0 < mail.uid <= result.last_uid:
                    raise ValueError("Inconsistent mailbox result")
                # A mailbox rebuild can give the same email a new UIDVALIDITY/UID.
                # Nonempty Message-ID plus content/headers avoids replaying it;
                # email without Message-ID remains deduplicated by IMAP identity.
                if mail.message_id:
                    identity = json.dumps([mail.message_id, mail.sender, mail.subject, mail.date, mail.body], ensure_ascii=False)
                    fingerprint = hashlib.sha256(identity.encode()).hexdigest()
                    known = self.db.execute("INSERT OR IGNORE INTO mail_identities VALUES (?,?,?)", (account_id, fingerprint, time.time()))
                    if not known.rowcount:
                        continue
                inserted = self.db.execute("INSERT OR IGNORE INTO messages(account_id,uidvalidity,uid,payload,created_at) VALUES (?,?,?,?,?)", (account_id, mail.uidvalidity, mail.uid, json.dumps(asdict(mail), ensure_ascii=False), time.time()))
                # Only a new insert gets a notice. Enabling notifications never
                # replays stored backlog, and formatter errors roll back the
                # mail, identity, outbox, and cursor as one transaction.
                if inserted.rowcount and notification_parts is not None:
                    parts = notification_parts(mail)
                    if not isinstance(parts, list) or not parts or not all(isinstance(part, str) and part for part in parts):
                        raise ValueError("Empty or invalid notification")
                    self.db.execute("INSERT INTO notifications(message_id,parts,created_at) VALUES (?,?,?)", (inserted.lastrowid, json.dumps(parts, ensure_ascii=False), time.time()))
            self.db.execute("INSERT INTO checkpoints VALUES (?,?,?,?) ON CONFLICT(account_id) DO UPDATE SET binding=excluded.binding,uidvalidity=excluded.uidvalidity,last_uid=excluded.last_uid", (account_id, binding, result.uidvalidity, result.last_uid))

    def pending(self, limit: int, account_ids: tuple[str, ...]):
        marks = ",".join("?" for _ in account_ids)
        rows = self.db.execute(f"SELECT id,payload FROM messages WHERE status='pending' AND account_id IN ({marks}) ORDER BY id LIMIT ?", (*account_ids, limit)).fetchall()
        return [(row["id"], MailMessage(**json.loads(row["payload"]))) for row in rows]

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

    def notification_part_sent(self, message_id: int):
        with self.db:
            row = self.db.execute("SELECT * FROM notifications WHERE message_id=? AND status='pending'", (message_id,)).fetchone()
            if row is None:
                raise ValueError("Unknown notification")
            sent_parts = row["sent_parts"] + 1
            if sent_parts > len(json.loads(row["parts"])):
                raise ValueError("Too many notification parts")
            status = "sent" if sent_parts == len(json.loads(row["parts"])) else "pending"
            self.db.execute("UPDATE notifications SET sent_parts=?,status=?,attempts=0,retry_at=0 WHERE message_id=?", (sent_parts, status, message_id))
            # A notice is independent of the morning digest: the source mail
            # remains pending (or queued) until its digest is acknowledged.

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

    def prune(self, days: int):
        cutoff = time.time() - days * 86400
        with self.db:
            self.db.execute("DELETE FROM notifications WHERE status='sent' AND created_at<?", (cutoff,))
            self.db.execute("DELETE FROM messages WHERE status='sent' AND created_at<? AND NOT EXISTS (SELECT 1 FROM notifications WHERE notifications.message_id=messages.id)", (cutoff,))
            self.db.execute("DELETE FROM digests WHERE status='sent' AND created_at<?", (cutoff,))
            # Keep identities while any unsent mail remains: recovery must never
            # lose a pending item just because its fingerprint was aged out.
            if (not self.db.execute("SELECT 1 FROM messages WHERE status!='sent'").fetchone()
                    and not self.db.execute("SELECT 1 FROM notifications WHERE status='pending'").fetchone()):
                self.db.execute("DELETE FROM mail_identities WHERE created_at<?", (cutoff,))
