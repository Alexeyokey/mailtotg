"""Persist incoming mail without constructing Telegram or AI clients."""

from dataclasses import replace
import hashlib
import json
import logging
import math
import time

from .config import Config, expand_accounts
from .mail import MailReader
from .store import Store
from .filtering import sender_is_excluded


LOG = logging.getLogger(__name__)


def account_binding(account) -> str:
    # Keep the same binding as Service so both modes can share a durable cursor.
    identity = json.dumps([account.host, account.port, account.username, account.mailbox])
    return hashlib.sha256(identity.encode()).hexdigest()


class Collector:
    def __init__(self, config: Config, *, store=None, readers=None):
        self.config = config
        if not 1 <= len(config.accounts) <= 2:
            raise ValueError("Collector requires one or two mailboxes")
        self.accounts = expand_accounts(config.accounts)
        self.account_ids = tuple(account.id for account in self.accounts)
        self.account_parents = {account.id: account.id.split("/", 1)[0] for account in self.accounts}
        self.readers = list(readers) if readers is not None else [MailReader(a, config.service) for a in self.accounts]
        if len(self.readers) != len(self.accounts):
            raise ValueError("Each mailbox requires one reader")
        self.store = store if store is not None else Store(config.service.database)
        self._closed = False
        try:
            for account in self.accounts:
                self.store.checkpoint(account.id, account_binding(account))
            if config.service.excluded_sender_domains:
                self.store.exclude_pending(self.excluded, self.account_ids)
        except Exception:
            if store is None:
                self.close()
            raise

    def excluded(self, message):
        return sender_is_excluded(message.sender, self.config.service.excluded_sender_domains)

    def poll(self) -> bool:
        if self._closed:
            raise RuntimeError("Collector is closed")
        if len(self.readers) != len(self.accounts):
            raise ValueError("Each mailbox requires one reader")
        all_ok = True
        parent_health = {account.id: True for account in self.config.accounts}
        for account, reader in zip(self.accounts, self.readers):
            parent_id = self.account_parents[account.id]
            adjusted = False
            original_settings = None
            try:
                binding = account_binding(account)
                checkpoint = self.store.checkpoint(account.id, binding)
                previous_poll = self.store.get(f"last_poll:{account.id}")
                if previous_poll and hasattr(reader, "settings"):
                    original_settings = reader.settings
                    days = math.ceil(max(0, time.time() - float(previous_poll)) / 86400) + 1
                    reader.settings = replace(
                        self.config.service,
                        lookback_days=max(self.config.service.lookback_days, days),
                    )
                    adjusted = True
                result = reader.poll(checkpoint)
                self.store.save_poll(account.id, binding, result, exclude_mail=self.excluded,
                                     identity_account_id=parent_id)
                updates = {
                    f"health:{account.id}": "ok",
                    f"folder_health:{account.id}": "ok",
                    f"last_poll:{account.id}": int(time.time()),
                }
                if result.epoch_changed:
                    updates[f"epoch_notice:{account.id}"] = "1"
                    updates[f"epoch_notice:{parent_id}"] = "1"
                    LOG.warning("Mailbox %s: identity epoch changed; recovering recent mail", account.id)
                self.store.set_many(updates)
                if result.messages:
                    LOG.info("Mailbox %s: processed %d messages", account.id, len(result.messages))
            except Exception as error:
                all_ok = False
                parent_health[parent_id] = False
                try:
                    self.store.set_many({f"health:{account.id}": "error",
                                         f"folder_health:{account.id}": "error"})
                except Exception as state_error:
                    LOG.warning("Mailbox %s status persistence failed (%s)", account.id, type(state_error).__name__)
                # Never log exception text: it may contain credentials or mail.
                LOG.warning("Mailbox %s unavailable (%s)", account.id, type(error).__name__)
            finally:
                if adjusted:
                    reader.settings = original_settings
        try:
            self.store.set_many({f"health:{parent_id}": "ok" if healthy else "error"
                                 for parent_id, healthy in parent_health.items()})
        except Exception as error:
            all_ok = False
            LOG.warning("Mailbox status persistence failed (%s)", type(error).__name__)
        return all_ok

    def run(self):
        LOG.info("Collector started: %d mailboxes; poll=%ss", len(self.config.accounts), self.config.service.poll_seconds)
        while True:
            self.poll()
            time.sleep(self.config.service.poll_seconds)

    def close(self):
        if not self._closed:
            self.store.close()
            self._closed = True
