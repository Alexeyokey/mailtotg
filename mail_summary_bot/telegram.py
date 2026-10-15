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


class _MessageNotModified(TelegramError):
    """An edit already has the requested text and keyboard."""


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
        self.bot_id = int(token.split(":", 1)[0])
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
            description = data.get("description")
            unchanged = "Bad Request: message is not modified"
            if (
                method == "editMessageText"
                and response.status_code == 400
                and code == 400
                and isinstance(description, str)
                and (description == unchanged or description.startswith(unchanged + ":"))
            ):
                raise _MessageNotModified(
                    "Сообщение Telegram уже обновлено.", status_code=response.status_code
                )
            raise TelegramError(
                f"Ошибка Telegram (код {code}).",
                retry_after=retry_after,
                status_code=response.status_code,
            )
        return data

    def get_updates(self, offset: int, *, timeout: int | None = None) -> list[dict]:
        timeout = self._poll_timeout if timeout is None else timeout
        if type(timeout) is not int or not 0 <= timeout <= 50:
            raise TelegramError("Недопустимое время ожидания Telegram.")
        data = self._request(
            "getUpdates",
            {"offset": offset, "timeout": timeout, "allowed_updates": ["message", "callback_query"]},
        )
        result = data.get("result")
        if not isinstance(result, list) or any(not isinstance(item, dict) for item in result):
            raise TelegramError("Telegram вернул некорректный список обновлений.")
        return result

    def send_chunk(self, text: str, *, reply_markup: dict | None = None) -> int:
        if not text or sum(2 if ord(character) > 0xFFFF else 1 for character in text) > 4000:
            raise TelegramError("Недопустимый размер сообщения Telegram.")
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "link_preview_options": {"is_disabled": True},
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        data = self._request("sendMessage", payload)
        result = data.get("result")
        message_id = result.get("message_id") if isinstance(result, dict) else None
        if type(message_id) is not int or message_id <= 0:
            raise TelegramError("Telegram не подтвердил отправку сообщения.")
        return message_id

    def send_text(self, text: str, *, reply_markup: dict | None = None) -> None:
        chunks = split_message(text)
        for index, chunk in enumerate(chunks):
            if reply_markup is not None and index == len(chunks) - 1:
                self.send_chunk(chunk, reply_markup=reply_markup)
            else:
                self.send_chunk(chunk)

    def edit_text(self, message_id: int, text: str, *, reply_markup: dict | None = None) -> None:
        if type(message_id) is not int or message_id <= 0:
            raise TelegramError("Недопустимый ID сообщения Telegram.")
        if not text or sum(2 if ord(character) > 0xFFFF else 1 for character in text) > 4000:
            raise TelegramError("Недопустимый размер сообщения Telegram.")
        payload = {
            "chat_id": self.chat_id,
            "message_id": message_id,
            "text": text,
            "link_preview_options": {"is_disabled": True},
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        try:
            data = self._request("editMessageText", payload)
        except _MessageNotModified:
            return
        result = data.get("result")
        edited_id = result.get("message_id") if isinstance(result, dict) else None
        if type(edited_id) is not int or edited_id != message_id:
            raise TelegramError("Telegram не подтвердил изменение сообщения.")

    def answer_callback(self, callback_id: str, text: str = "", *, show_alert: bool = False) -> None:
        data = self._request("answerCallbackQuery", {
            "callback_query_id": callback_id,
            "text": text,
            "show_alert": show_alert,
        })
        if data.get("result") is not True:
            raise TelegramError("Telegram не подтвердил ответ на нажатие кнопки.")

    def set_commands(self, commands: list[dict]) -> None:
        data = self._request("setMyCommands", {"commands": commands})
        if data.get("result") is not True:
            raise TelegramError("Telegram не подтвердил обновление меню команд.")

    def close(self) -> None:
        self._client.close()
