from datetime import datetime, timedelta
from dataclasses import replace
from zoneinfo import ZoneInfo
import hashlib
import json
import logging
import time
import math
import re

from .config import Config
from .mail import MailReader
from .store import Store
from .summarizer import Summarizer
from .telegram import TelegramClient, split_message
from .filtering import sender_is_excluded

LOG = logging.getLogger(__name__)


def next_daily(now: float, clock: str, timezone: str) -> float:
    zone = ZoneInfo(timezone)
    local = datetime.fromtimestamp(now, zone)
    hour, minute = map(int, clock.split(":"))
    candidate = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate.timestamp() <= now:
        candidate += timedelta(days=1)
    return candidate.timestamp()


def account_binding(account) -> str:
    identity = json.dumps([account.host, account.port, account.username, account.mailbox])
    return hashlib.sha256(identity.encode()).hexdigest()


def notification_text(message) -> str:
    """Bounded plain-text notice; mail content remains data and never executes."""
    def excerpt(value, limit):
        value = re.sub(r"\s+", " ", value).strip()
        return value[:limit] + ("…" if len(value) > limit else "")

    return "\n".join([
        "✉️ Новое письмо · " + excerpt(message.account_id, 64),
        "От: " + (excerpt(message.sender, 256) or "(не указан)"),
        "Тема: " + (excerpt(message.subject, 512) or "(без темы)"),
        "Дата: " + (excerpt(message.date, 80) or "(не указана)"),
        "", excerpt(message.body, 450) or "(Текст письма отсутствует.)",
        "", "Краткая выдержка; полный текст — в почте. Письмо также войдёт в сводку.",
    ])


class Service:
    def __init__(self, config: Config, *, store=None, readers=None, telegram=None, summarizer=None):
        self.config = config
        self.store = store if store is not None else Store(config.service.database)
        self.readers = readers if readers is not None else [MailReader(a, config.service) for a in config.accounts]
        self.telegram = telegram if telegram is not None else TelegramClient(config.telegram)
        self.summarizer = summarizer if summarizer is not None else Summarizer(config.summary)
        self.next_mail_poll = 0.0
        self.next_summary_attempt = 0.0
        self.next_send_at = 0.0
        for account in config.accounts:
            self.store.checkpoint(account.id, account_binding(account))
        if config.service.excluded_sender_domains:
            self.store.exclude_pending(self.excluded, tuple(a.id for a in config.accounts))
        signature = json.dumps([config.service.schedule, config.service.digest_time, config.service.timezone, config.service.digest_interval_minutes])
        if self.store.get("schedule_config") != signature or self.store.get("next_due") is None:
            self.advance_schedule(time.time(), signature=signature)

    def close(self):
        self.telegram.close()
        self.summarizer.close()
        self.store.close()

    def excluded(self, message):
        return sender_is_excluded(message.sender, self.config.service.excluded_sender_domains)

    def advance_schedule(self, now: float, *, signature=None):
        settings = self.config.service
        if settings.schedule == "daily":
            next_due = next_daily(now, settings.digest_time, settings.timezone)
        elif settings.schedule == "interval":
            next_due = now + settings.digest_interval_minutes * 60
        else:
            next_due = 0
        updates = {"next_due": next_due}
        if signature is not None:
            updates["schedule_config"] = signature
        self.store.set_many(updates)

    def poll_mail(self):
        for account, reader in zip(self.config.accounts, self.readers):
            binding = account_binding(account)
            checkpoint = self.store.checkpoint(account.id, binding)
            try:
                previous_poll = self.store.get(f"last_poll:{account.id}")
                if previous_poll and hasattr(reader, "settings"):
                    days = math.ceil(max(0, time.time() - float(previous_poll)) / 86400) + 1
                    reader.settings = replace(self.config.service, lookback_days=max(self.config.service.lookback_days, days))
                result = reader.poll(checkpoint)
                self.store.save_poll(
                    account.id, binding, result,
                    notification_parts=(lambda mail: split_message(notification_text(mail)))
                    if self.config.service.notify_new_mail else None,
                    exclude_mail=self.excluded,
                )
                if result.epoch_changed:
                    self.store.set(f"epoch_notice:{account.id}", "1")
                    LOG.warning("Mailbox %s: identity epoch changed; recovering recent mail", account.id)
                self.store.set(f"health:{account.id}", "ok")
                self.store.set(f"last_poll:{account.id}", int(time.time()))
                if result.messages:
                    LOG.info("Mailbox %s: %d new messages", account.id, len(result.messages))
            except Exception as error:
                self.store.set(f"health:{account.id}", "error")
                LOG.warning("Mailbox %s unavailable (%s)", account.id, type(error).__name__)

    def request_digest(self):
        self.store.set("digest_requested", "1")
        self.next_summary_attempt = 0

    def build_digest(self, now: float):
        if self.store.get("digest_requested", "0") != "1" or self.store.outbox() or now < self.next_summary_attempt:
            return False
        rows = self.store.pending(self.config.service.max_digest_messages, tuple(a.id for a in self.config.accounts))
        if not rows:
            self.store.set("digest_requested", "0")
            return False
        try:
            result = self.summarizer.summarize([message for _, message in rows])
            if not isinstance(result, str) or not result.strip():
                raise ValueError("Empty summary")
            created = datetime.fromtimestamp(now, ZoneInfo(self.config.service.timezone)).strftime("%d.%m.%Y %H:%M")
            health = [a.id for a in self.config.accounts if self.store.get(f"health:{a.id}") != "ok"]
            header = f"Сводка почты · {created}\nПисем в этой части: {len(rows)}\n"
            if health:
                header += "Не удалось обновить ящики: " + ", ".join(health) + ". Сводка может быть неполной.\n"
            epochs = [a.id for a in self.config.accounts if self.store.get(f"epoch_notice:{a.id}") == "1"]
            if epochs:
                header += "Восстановление после изменения идентификаторов почты: " + ", ".join(epochs) + ". Возможны повторы писем без стабильного Message-ID.\n"
            self.store.queue_digest([mid for mid, _ in rows], split_message(header + "\n" + result))
            for account_id in epochs:
                self.store.set(f"epoch_notice:{account_id}", "0")
            return True
        except Exception as error:
            self.next_summary_attempt = now + 60
            LOG.warning("Summary deferred (%s)", type(error).__name__)
            return False

    def deliver(self, now: float):
        digest = self.store.outbox()
        if digest is None or now < max(digest["retry_at"], self.next_send_at):
            return False
        try:
            self.telegram.send_chunk(digest["parts"][digest["sent_parts"]])
            self.store.part_sent(digest["id"])
            self.next_send_at = time.time() + 1.1
            LOG.info("Telegram digest %s: delivered part %s/%s", digest["id"], digest["sent_parts"] + 1, len(digest["parts"]))
            return True
        except Exception as error:
            self.store.delivery_failed(digest["id"], getattr(error, "retry_after", None))
            LOG.warning("Telegram delivery deferred (%s)", type(error).__name__)
            return False

    def deliver_notifications(self, now: float):
        notice = self.store.notification_outbox()
        if notice is None or now < max(notice["retry_at"], self.next_send_at):
            return False
        try:
            self.telegram.send_chunk(notice["parts"][notice["sent_parts"]])
            self.store.notification_part_sent(notice["message_id"])
            self.next_send_at = time.time() + 1.1
            LOG.info("Telegram mail notification %s: delivered part %s/%s",
                     notice["message_id"], notice["sent_parts"] + 1, len(notice["parts"]))
            return True
        except Exception as error:
            self.store.notification_failed(notice["message_id"], getattr(error, "retry_after", None))
            LOG.warning("Mail notification deferred (%s)", type(error).__name__)
            return False

    def status_text(self):
        stats = self.store.stats()
        lines = ["Почтовая сводка", f"Ожидают сводки: {stats['pending']}", f"Ожидают доставки: {stats['queued']}", f"Режим: {self.config.summary.mode}"]
        lines.append("Уведомления о новых письмах: " + ("включены" if self.config.service.notify_new_mail else "выключены"))
        lines.append(f"Ожидают уведомления: {self.store.notification_stats()['pending']}")
        if self.config.service.excluded_sender_domains:
            lines.append("Исключённые отправители: " + ', '.join(self.config.service.excluded_sender_domains))
            lines.append(f"Исключено писем: {self.store.excluded_count()}")
        zone = ZoneInfo(self.config.service.timezone)
        for account in self.config.accounts:
            stamp = self.store.get(f"last_poll:{account.id}")
            date = datetime.fromtimestamp(int(stamp), zone).strftime("%d.%m %H:%M") if stamp else "ещё не проверен"
            lines.append(f"{account.id}: {self.store.get(f'health:{account.id}', 'ожидание')}; {date}")
            if self.store.get(f"epoch_notice:{account.id}") == "1":
                lines.append(f"{account.id}: восстановлен недавний период после смены идентификаторов; возможны повторы")
        return "\n".join(lines)

    def commands(self):
        offset = int(self.store.get("telegram_offset", 0))
        try:
            if self.store.notification_outbox():
                updates = self.telegram.get_updates(offset, timeout=0)
            else:
                updates = self.telegram.get_updates(offset)
        except Exception as error:
            LOG.warning("Telegram polling unavailable (%s)", type(error).__name__)
            return
        for update in updates:
            update_id = update.get("update_id")
            if not isinstance(update_id, int) or update_id < offset:
                continue
            message = update.get("message", {})
            chat = message.get("chat", {})
            text = message.get("text", "")
            if chat.get("type") == "private" and str(chat.get("id")) == str(self.telegram.chat_id) and isinstance(text, str):
                text = text.strip()
                command = text.split(maxsplit=1)[0].split("@", 1)[0] if text else ""
                answer = None
                if command == "/summary":
                    self.poll_mail()
                    rows = self.store.pending(1, tuple(a.id for a in self.config.accounts))
                    if rows or self.store.outbox():
                        self.request_digest()
                        answer = "Готовлю сводку новых писем. При недоступности сети доставка будет повторена."
                    else:
                        answer = "Новых писем для сводки нет.\n" + self.status_text()
                elif command == "/status":
                    answer = self.status_text()
                elif command in {"/start", "/help"}:
                    answer = f"Читаю настроенные ящики: {len(self.config.accounts)}.\n/summary — сводка новых писем\n/status — состояние подключений\nПисьма не помечаются прочитанными."
                if answer:
                    try:
                        self.telegram.send_text(answer)
                        self.next_send_at = time.time() + 1.1
                    except Exception as error:
                        LOG.warning("Telegram command reply unavailable (%s)", type(error).__name__)
            offset = update_id + 1
            self.store.set("telegram_offset", offset)

    def tick(self, *, poll_commands=True):
        now = time.time()
        if time.monotonic() >= self.next_mail_poll:
            self.poll_mail()
            self.next_mail_poll = time.monotonic() + self.config.service.poll_seconds
        due = float(self.store.get("next_due", 0))
        if self.config.service.schedule != "manual" and due and now >= due:
            self.request_digest()
            self.advance_schedule(now)
        self.deliver_notifications(time.time())
        if poll_commands:
            self.commands()
        now = time.time()
        self.build_digest(now)
        self.deliver(now)
        self.store.prune(self.config.service.retention_days)

    def run(self):
        LOG.info("Started: %d mailboxes, schedule=%s, summary=%s", len(self.config.accounts), self.config.service.schedule, self.config.summary.mode)
        while True:
            self.tick()
            time.sleep(1)
