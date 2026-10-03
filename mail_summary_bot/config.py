from dataclasses import dataclass, field
from dataclasses import fields, replace
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import os
import re
import shlex
import tomllib


@dataclass(frozen=True)
class AccountConfig:
    id: str
    host: str
    username: str
    password_env: str
    port: int = 993
    mailbox: str = "INBOX"
    timeout_seconds: int = 30


@dataclass(frozen=True)
class ServiceConfig:
    database: str = "data/state.sqlite3"
    poll_seconds: int = 60
    schedule: str = "daily"
    digest_time: str = "09:00"
    timezone: str = "Europe/Moscow"
    digest_interval_minutes: int = 60
    bootstrap: str = "new"
    lookback_days: int = 1
    max_messages_per_poll: int = 100
    max_body_chars: int = 12000
    max_email_bytes: int = 2000000
    max_digest_messages: int = 30
    retention_days: int = 30


@dataclass(frozen=True)
class TelegramConfig:
    token_env: str = "TELEGRAM_BOT_TOKEN"
    chat_id_env: str = "TELEGRAM_CHAT_ID"
    proxy_env: str = "TELEGRAM_PROXY_URL"
    poll_timeout_seconds: int = 10


@dataclass(frozen=True)
class SummaryConfig:
    mode: str = "extractive"
    model_env: str = "SUMMARY_MODEL"
    endpoint_env: str = "SUMMARY_API_URL"
    api_key_env: str = "SUMMARY_API_KEY"
    proxy_env: str = "SUMMARY_PROXY_URL"
    timeout_seconds: int = 120
    max_input_chars: int = 60000


@dataclass(frozen=True)
class Config:
    accounts: tuple[AccountConfig, ...]
    service: ServiceConfig = field(default_factory=ServiceConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    summary: SummaryConfig = field(default_factory=SummaryConfig)


class ConfigError(ValueError):
    pass


def load_env(path: str | Path) -> None:
    """Read literal .env values; never execute or expand their contents."""
    file = Path(path)
    if not file.exists():
        return
    for line_number, line in enumerate(file.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:]
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", key):
            raise ConfigError(f"Некорректная строка .env: {line_number}")
        value = value.strip()
        if value.startswith(("'", '"')):
            try:
                values = shlex.split(value, comments=False)
            except ValueError:
                raise ConfigError(f"Некорректные кавычки .env: {line_number}") from None
            if len(values) != 1:
                raise ConfigError(f"Некорректная строка .env: {line_number}")
            value = values[0]
        os.environ.setdefault(key, value)


def _section(cls, data):
    if not isinstance(data, dict):
        raise ConfigError(f"Раздел {cls.__name__} должен быть таблицей TOML")
    unknown = data.keys() - {item.name for item in fields(cls)}
    if unknown:
        raise ConfigError(f"Неизвестные настройки {cls.__name__}: {', '.join(sorted(unknown))}")
    try:
        return cls(**data)
    except TypeError:
        raise ConfigError(f"Отсутствуют обязательные настройки {cls.__name__}") from None


def load_config(path: str | Path, *, secrets: bool = True, mail_only: bool = False) -> Config:
    file = Path(path).resolve()
    try:
        data = tomllib.loads(file.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        raise ConfigError("Не удалось прочитать корректный config.toml") from None
    if data.keys() - {"accounts", "service", "telegram", "summary"}:
        raise ConfigError("Неизвестный раздел config.toml")
    entries = data.get("accounts", [])
    if not isinstance(entries, list) or not 1 <= len(entries) <= 2:
        raise ConfigError("Нужно настроить один или два [[accounts]]")
    accounts = tuple(_section(AccountConfig, item) for item in entries)
    for a in accounts:
        if not isinstance(a.id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", a.id):
            raise ConfigError("id ящика: 1–64 латинские буквы, цифры, _ или -")
        if not all(isinstance(v, str) and v.strip() for v in (a.host, a.username, a.password_env, a.mailbox)):
            raise ConfigError(f"Заполните адрес и авторизацию ящика {a.id}")
        if type(a.port) is not int or not 1 <= a.port <= 65535:
            raise ConfigError(f"Некорректный IMAP port для {a.id}")
        if type(a.timeout_seconds) is not int or a.timeout_seconds <= 0:
            raise ConfigError(f"Некорректный IMAP timeout для {a.id}")
    if len({a.id for a in accounts}) != len(accounts):
        raise ConfigError("У ящиков должны быть разные id")
    if len({(a.host, a.username, a.mailbox) for a in accounts}) != len(accounts):
        raise ConfigError("Подключения должны указывать на разные ящики")
    service = _section(ServiceConfig, data.get("service", {}))
    telegram = _section(TelegramConfig, data.get("telegram", {}))
    summary = _section(SummaryConfig, data.get("summary", {}))
    if service.schedule not in {"daily", "interval", "manual"}:
        raise ConfigError("schedule: daily, interval или manual")
    if service.bootstrap not in {"new", "lookback"}:
        raise ConfigError("bootstrap: new или lookback")
    if not isinstance(service.digest_time, str) or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", service.digest_time):
        raise ConfigError("digest_time должен быть в формате HH:MM")
    try:
        ZoneInfo(service.timezone)
    except (ZoneInfoNotFoundError, TypeError):
        raise ConfigError("Неизвестная timezone") from None
    for name in ("poll_seconds", "digest_interval_minutes", "lookback_days", "max_messages_per_poll", "max_body_chars", "max_email_bytes", "max_digest_messages", "retention_days"):
        value = getattr(service, name)
        if type(value) is not int or value <= 0:
            raise ConfigError(f"{name} должен быть положительным целым")
    if type(telegram.poll_timeout_seconds) is not int or not 0 <= telegram.poll_timeout_seconds <= 50:
        raise ConfigError("Telegram poll timeout: 0–50 секунд")
    if summary.mode not in {"extractive", "openai", "ollama"}:
        raise ConfigError("summary.mode: extractive, openai или ollama")
    if type(summary.max_input_chars) is not int or summary.max_input_chars < 1000:
        raise ConfigError("max_input_chars должен быть не меньше 1000")
    if type(summary.timeout_seconds) is not int or summary.timeout_seconds <= 0:
        raise ConfigError("Некорректный summary timeout")
    database = Path(service.database)
    if not database.is_absolute():
        database = file.parent / database
    service = replace(service, database=str(database))
    if secrets:
        required = [a.password_env for a in accounts]
        if not mail_only:
            required.extend([telegram.token_env, telegram.chat_id_env])
            if summary.mode != "extractive":
                required.append(summary.model_env)
            if summary.mode == "openai":
                required.append(summary.api_key_env)
        missing = [name for name in required if not os.environ.get(name, "").strip()]
        if missing:
            raise ConfigError("Заполните переменные: " + ", ".join(missing))
        if not mail_only:
            try:
                if int(os.environ[telegram.chat_id_env]) <= 0:
                    raise ValueError
            except ValueError:
                raise ConfigError("Нужен положительный TELEGRAM_CHAT_ID личного чата") from None
    return Config(accounts, service, telegram, summary)
