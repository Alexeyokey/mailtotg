import argparse
from datetime import datetime, timezone
import json
import logging
import os
import sys
import time

from .config import ConfigError, SummaryConfig, load_config, load_env
from .models import MailMessage


def main():
    parser = argparse.ArgumentParser(description="Сводки одного или двух почтовых ящиков в Telegram")
    parser.add_argument("command", choices=["run", "once", "check", "collect", "demo", "chat-id"])
    parser.add_argument("--config", default="config.toml")
    parser.add_argument("--env", default=".env")
    args = parser.parse_args()
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    try:
        if args.command == "demo":
            from .summarizer import Summarizer
            mails = [
                MailMessage("work", 1, 1, "<demo1>", "Преподаватель <teacher@example.org>", "Перенос консультации", datetime.now(timezone.utc).isoformat(), "Консультация состоится завтра в 15:00 в аудитории 410. Пришлите вопросы до 12:00."),
                MailMessage("personal", 1, 2, "<demo2>", "Сервис <service@example.org>", "Отчёт готов", datetime.now(timezone.utc).isoformat(), "Ваш отчёт сформирован. Его можно скачать в личном кабинете. Ответ не требуется."),
            ]
            renderer = Summarizer(SummaryConfig(mode="extractive"))
            try:
                print(renderer.summarize(mails))
            finally:
                renderer.close()
            return 0
        load_env(args.env)
        if args.command == "chat-id":
            # Only discover ids; no outbound messages and no mail/AI requests.
            from .telegram import TelegramClient
            from .config import TelegramConfig
            if not os.environ.get("TELEGRAM_BOT_TOKEN"):
                raise ConfigError("Заполните TELEGRAM_BOT_TOKEN в .env")
            saved = os.environ.get("TELEGRAM_CHAT_ID")
            os.environ["TELEGRAM_CHAT_ID"] = "1"
            client = TelegramClient(TelegramConfig(poll_timeout_seconds=0))
            try:
                ids = {u["message"]["chat"]["id"] for u in client.get_updates(0) if u.get("message", {}).get("chat", {}).get("type") == "private"}
                print(json.dumps({"private_chat_ids": sorted(ids)}, ensure_ascii=False))
                if not ids:
                    print("Сначала отправьте /start своему боту и повторите команду.")
            finally:
                client.close()
                if saved is None:
                    os.environ.pop("TELEGRAM_CHAT_ID", None)
                else:
                    os.environ["TELEGRAM_CHAT_ID"] = saved
            return 0
        config = load_config(args.config, mail_only=args.command == "collect")
        if args.command == "collect":
            from .collector import Collector
            collector = Collector(config)
            try:
                collector.run()
            finally:
                collector.close()
            return 0
        if args.command == "check":
            print("Конфигурация и необходимые переменные заполнены. Это не проверка реальных подключений.")
            print(f"Ящиков: {len(config.accounts)}; schedule={config.service.schedule}; summary={config.summary.mode}; timezone={config.service.timezone}")
            return 0
        from .service import Service
        service = Service(config)
        try:
            if args.command == "once":
                service.poll_mail()
                while service.store.notification_outbox():
                    time.sleep(1.1)
                    if not service.deliver_notifications(time.time()):
                        print("Уведомление сохранено для повторной доставки.")
                        return 2
                service.request_digest()
                while True:
                    built = service.build_digest(time.time())
                    if not built and not service.store.outbox():
                        if service.store.stats()["pending"]:
                            print("Сводку не удалось создать. Письма сохранены для повторной обработки.")
                            return 2
                        break
                    # Deliver all chunks and queued batches; keep failures durable.
                    while service.store.outbox():
                        time.sleep(1.1)
                        if not service.deliver(time.time()):
                            print("Сводка сохранена для повторной доставки.")
                            return 2
                print(service.status_text())
                if any(service.store.get(f"health:{a.id}") != "ok" for a in config.accounts):
                    return 2
            else:
                service.run()
        finally:
            service.close()
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as error:
        if isinstance(error, ConfigError):
            print(f"Ошибка настройки: {error}", file=sys.stderr)
        else:
            print(f"Операция не завершена ({type(error).__name__}). Проверьте настройки и доступность сети.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
