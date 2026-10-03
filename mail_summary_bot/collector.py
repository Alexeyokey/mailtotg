"""Persist incoming mail without constructing Telegram or AI clients."""

from dataclasses import replace
import hashlib
import json
import logging
import math
import time

from .config import Config
from .mail import MailReader
from .store import Store


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
        self.readers = list(readers) if readers is not None else [MailReader(a, config.service) for a in config.accounts]
        if len(self.readers) != len(config.accounts):
            raise ValueError("Each mailbox requires one reader")
        self.store = store if store is not None else Store(config.service.database)
        self._closed = False
        try:
            for account in config.accounts:
                self.store.checkpoint(account.id, account_binding(account))
        except Exception:
            if store is None:
                self.close()
            raise

    def poll(self) -> bool:
        if self._closed:
            raise RuntimeError("Collector is closed")
        all_ok = True
        for account, reader in zip(self.config.accounts, self.readers):
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
                self.store.save_poll(account.id, binding, result)
                updates = {
                    f"health:{account.id}": "ok",
                    f"last_poll:{account.id}": int(time.time()),
                }
                if result.epoch_changed:
                    updates[f"epoch_notice:{account.id}"] = "1"
                    LOG.warning("Mailbox %s: identity epoch changed; recovering recent mail", account.id)
                self.store.set_many(updates)
                if result.messages:
                    LOG.info("Mailbox %s: processed %d messages", account.id, len(result.messages))
            except Exception as error:
                all_ok = False
                try:
                    self.store.set(f"health:{account.id}", "error")
                except Exception as state_error:
                    LOG.warning("Mailbox %s status persistence failed (%s)", account.id, type(state_error).__name__)
                # Never log exception text: it may contain credentials or mail.
                LOG.warning("Mailbox %s unavailable (%s)", account.id, type(error).__name__)
            finally:
                if adjusted:
                    reader.settings = original_settings
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
