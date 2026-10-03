"""Small synchronous Telegram client with explicit proxy configuration."""

import os
import re

import httpx

from .config import TelegramConfig


class TelegramError(RuntimeError):
    """Safe-to-display Telegram failure; no URL, token, or response content."""

    def __init__(
        self, message: str, *, retry_after: int | None = None, status_code: int | None = None
    ) -> None:
        super().__init__(message)
        self.retry_after = retry_after
        self.status_code = status_code


def split_message(text: str) -> list[str]:
    """Split losslessly at 4,000 UTF-16 units without splitting emoji."""
    chunks: list[str] = []
    start = 0
    units = 0
    for index, character in enumerate(text):
        character_units = 2 if ord(character) > 0xFFFF else 1
        if units + character_units > 4000:
            chunks.append(text[start:index])
            start = index
            units = 0
        units += character_units
    if start < len(text):
        chunks.append(text[start:])
    return chunks


class TelegramClient:
    def __init__(self, settings: TelegramConfig, client: httpx.Client | None = None) -> None:
        token = os.environ.get(settings.token_env, "").strip()
        chat_id = os.environ.get(settings.chat_id_env, "").strip()
        if not token or not chat_id:
            raise TelegramError("Не заданы токен Telegram или ID чата.")
        if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
            raise TelegramError("Некорректный формат токена Telegram.")
        self.chat_id: int | str = int(chat_id) if re.fullmatch(r"-?[0-9]+", chat_id) else chat_id
        self._base_url = f"https://api.telegram.org/bot{token}"
        self._poll_timeout = settings.poll_timeout_seconds
        self._timeout = max(10, self._poll_timeout + 5)
        proxy = os.environ.get(settings.proxy_env, "").strip() or None
        try:
            self._client = client if client is not None else httpx.Client(
                trust_env=False, proxy=proxy, timeout=self._timeout
            )
        except (ValueError, ImportError, httpx.InvalidURL):
            raise TelegramError("Не удалось настроить HTTP-клиент Telegram.") from None

    def _request(self, method: str, payload: dict) -> dict:
        try:
            response = self._client.post(
                f"{self._base_url}/{method}", json=payload, timeout=self._timeout
            )
        except (httpx.HTTPError, UnicodeError):
            raise TelegramError("Ошибка соединения с Telegram.") from None
        try:
            data = response.json()
        except (ValueError, UnicodeError):
            raise TelegramError(
                "Telegram вернул некорректный ответ.", status_code=response.status_code
            ) from None
        if not isinstance(data, dict):
            raise TelegramError("Telegram вернул некорректный ответ.")
        if not 200 <= response.status_code < 300 or data.get("ok") is not True:
            retry_after = None
            parameters = data.get("parameters")
            if isinstance(parameters, dict):
                value = parameters.get("retry_after")
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    retry_after = value
            code = data.get("error_code", response.status_code)
            if not isinstance(code, int) or isinstance(code, bool):
                code = response.status_code
            raise TelegramError(
                f"Ошибка Telegram (код {code}).",
                retry_after=retry_after,
                status_code=response.status_code,
            )
        return data

    def get_updates(self, offset: int) -> list[dict]:
        data = self._request(
            "getUpdates",
            {"offset": offset, "timeout": self._poll_timeout, "allowed_updates": ["message"]},
        )
        result = data.get("result")
        if not isinstance(result, list) or any(not isinstance(item, dict) for item in result):
            raise TelegramError("Telegram вернул некорректный список обновлений.")
        return result

    def send_chunk(self, text: str) -> None:
        if not text or sum(2 if ord(character) > 0xFFFF else 1 for character in text) > 4000:
            raise TelegramError("Недопустимый размер сообщения Telegram.")
        self._request(
            "sendMessage",
            {
                "chat_id": self.chat_id,
                "text": text,
                "link_preview_options": {"is_disabled": True},
            },
        )

    def send_text(self, text: str) -> None:
        for chunk in split_message(text):
            self.send_chunk(chunk)

    def close(self) -> None:
        self._client.close()
