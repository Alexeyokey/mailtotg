import json
import os
import unittest
from unittest.mock import patch

import httpx

from mail_summary_bot.config import TelegramConfig
from mail_summary_bot.telegram import TelegramClient, TelegramError, split_message


ENV = {"TELEGRAM_BOT_TOKEN": "111:SECRET_abcdef", "TELEGRAM_CHAT_ID": "12345"}


class TelegramTests(unittest.TestCase):
    def client(self, handler):
        http_client = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
        with patch.dict(os.environ, ENV, clear=True):
            client = TelegramClient(TelegramConfig(), client=http_client)
        self.addCleanup(client.close)
        return client

    def test_unicode_chunking_is_lossless_and_within_utf16_limit(self):
        text = "А" * 3999 + "😀" + "x" * 5 + "🚀" * 4001 + "\nКонец"
        chunks = split_message(text)
        self.assertEqual("".join(chunks), text)
        self.assertTrue(all(0 < len(chunk.encode("utf-16-le")) // 2 <= 4000 for chunk in chunks))
        self.assertEqual(chunks[0], "А" * 3999)
        self.assertEqual(split_message(""), [])
        self.assertEqual(split_message("😀" * 2000), ["😀" * 2000])

    def test_send_uses_configured_chat_and_plain_text(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

        client = self.client(handler)
        self.assertEqual(client.bot_id, 111)
        text = "<b>Не HTML</b> 😀\n" * 400
        client.send_text(text)
        payloads = [json.loads(request.content) for request in requests]
        self.assertEqual("".join(payload["text"] for payload in payloads), text)
        for request, payload in zip(requests, payloads):
            self.assertEqual(request.url.path.split("/")[-1], "sendMessage")
            self.assertEqual(payload["chat_id"], 12345)
            self.assertNotIn("parse_mode", payload)
            self.assertNotIn("reply_markup", payload)
            self.assertEqual(payload["link_preview_options"], {"is_disabled": True})
            self.assertLessEqual(len(payload["text"].encode("utf-16-le")) // 2, 4000)

    def test_get_updates_does_not_modify_webhooks(self):
        requests = []
        updates = [
            {"update_id": 17, "message": {"text": "/summary"}},
            {"update_id": 18, "callback_query": {"id": "callback-18", "data": "blacklist"}},
        ]

        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={"ok": True, "result": updates})

        client = self.client(handler)
        self.assertEqual(client.get_updates(18), updates)
        self.assertEqual(len(requests), 1)
        self.assertTrue(str(requests[0].url).endswith("/getUpdates"))
        self.assertEqual(json.loads(requests[0].content), {
            "offset": 18, "timeout": 10, "allowed_updates": ["message", "callback_query"],
        })

    def test_send_chunk_accepts_inline_and_reply_keyboards(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 7}})

        client = self.client(handler)
        keyboards = [
            {"inline_keyboard": [[{"text": "Заблокировать", "callback_data": "block:1"}]]},
            {"keyboard": [[{"text": "Чёрный список"}]], "resize_keyboard": True},
        ]
        for keyboard in keyboards:
            self.assertEqual(client.send_chunk("Письмо", reply_markup=keyboard), 7)
            self.assertEqual(json.loads(requests[-1].content)["reply_markup"], keyboard)

    def test_send_text_attaches_keyboard_only_to_last_chunk(self):
        payloads = []

        def handler(request):
            payloads.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True, "result": {"message_id": len(payloads)}})

        client = self.client(handler)
        keyboard = {"inline_keyboard": [[{"text": "Список", "callback_data": "blacklist"}]]}
        text = "😀" * 4001
        self.assertIsNone(client.send_text(text, reply_markup=keyboard))
        self.assertEqual("".join(payload["text"] for payload in payloads), text)
        self.assertEqual(len(payloads), 3)
        self.assertTrue(all("reply_markup" not in payload for payload in payloads[:-1]))
        self.assertEqual(payloads[-1]["reply_markup"], keyboard)
        client.send_text("", reply_markup=keyboard)
        self.assertEqual(len(payloads), 3)

    def test_edit_sends_plain_text_and_optional_inline_keyboard(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 8}})

        client = self.client(handler)
        keyboard = {"inline_keyboard": []}
        for markup in (None, keyboard):
            self.assertIsNone(client.edit_text(8, "<b>Обновлено</b>", reply_markup=markup))
            self.assertTrue(str(requests[-1].url).endswith("/editMessageText"))
            expected = {
                "chat_id": 12345, "message_id": 8, "text": "<b>Обновлено</b>",
                "link_preview_options": {"is_disabled": True},
            }
            if markup is not None:
                expected["reply_markup"] = markup
            self.assertEqual(json.loads(requests[-1].content), expected)

    def test_repeated_edit_is_idempotent_without_exposing_description(self):
        for description in (
            "Bad Request: message is not modified",
            "Bad Request: message is not modified: private response 111:SECRET_abcdef",
        ):
            with self.subTest(description=description):
                client = self.client(lambda request: httpx.Response(400, json={
                    "ok": False, "error_code": 400, "description": description,
                }))
                self.assertIsNone(client.edit_text(8, "Уже обновлено"))
                with self.assertRaises(TelegramError) as caught:
                    client.send_chunk("Письмо")
                self.assertNotIn("private", str(caught.exception))
                self.assertNotIn("SECRET", str(caught.exception))

    def test_edit_does_not_hide_other_errors_or_false_acknowledgements(self):
        responses = [
            (400, {"ok": False, "error_code": 400, "description": "private response"}),
            (500, {"ok": False, "error_code": 500, "description": "Bad Request: message is not modified"}),
            (400, {"ok": False, "error_code": 400, "description": "Bad Request: message is not modified unexpectedly"}),
            (200, {"ok": True, "result": True}),
            (200, {"ok": True, "result": {"message_id": 9}}),
            (200, {"ok": True, "result": {"message_id": True}}),
            (200, {"ok": True, "result": None}),
        ]
        for status, body in responses:
            with self.subTest(status=status, body=body):
                client = self.client(lambda request: httpx.Response(status, json=body))
                with self.assertRaises(TelegramError) as caught:
                    client.edit_text(8, "Обновлено")
                self.assertNotIn("private response", str(caught.exception))

    def test_edit_validates_message_id_and_size_before_network(self):
        requests = []
        client = self.client(lambda request: requests.append(request))
        for message_id, text in ((0, "x"), (-1, "x"), (True, "x"), ("8", "x"), (8, ""), (8, "😀" * 2001)):
            with self.subTest(message_id=message_id), self.assertRaises(TelegramError):
                client.edit_text(message_id, text)
        self.assertEqual(requests, [])

    def test_callback_answers_and_commands_send_expected_payloads(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={"ok": True, "result": True})

        client = self.client(handler)
        client.answer_callback("callback-1")
        client.answer_callback("callback-2", "Отправитель заблокирован", show_alert=True)
        commands = [{"command": "blacklist", "description": "Чёрный список"}]
        client.set_commands(commands)
        self.assertEqual([request.url.path.rsplit("/", 1)[-1] for request in requests], [
            "answerCallbackQuery", "answerCallbackQuery", "setMyCommands",
        ])
        self.assertEqual([json.loads(request.content) for request in requests], [
            {"callback_query_id": "callback-1", "text": "", "show_alert": False},
            {"callback_query_id": "callback-2", "text": "Отправитель заблокирован", "show_alert": True},
            {"commands": commands},
        ])

    def test_callback_and_command_methods_require_true_acknowledgement(self):
        for result in (None, False, 1, {}, "true"):
            with self.subTest(result=result):
                client = self.client(lambda request: httpx.Response(200, json={"ok": True, "result": result}))
                with self.assertRaises(TelegramError):
                    client.answer_callback("callback-1")
                with self.assertRaises(TelegramError):
                    client.set_commands([])

    def test_interactive_methods_preserve_sanitized_rate_limit_errors(self):
        client = self.client(lambda request: httpx.Response(429, json={
            "ok": False, "error_code": 429,
            "description": "111:SECRET_abcdef https://secret.invalid private response",
            "parameters": {"retry_after": 12},
        }))
        for operation in (
            lambda: client.edit_text(8, "Обновлено"),
            lambda: client.answer_callback("callback-1"),
            lambda: client.set_commands([]),
        ):
            with self.assertRaises(TelegramError) as caught:
                operation()
            self.assertEqual(caught.exception.retry_after, 12)
            self.assertEqual(caught.exception.status_code, 429)
            self.assertEqual(str(caught.exception), "Ошибка Telegram (код 429).")

    def test_zero_timeout_does_not_wait_while_notifications_are_queued(self):
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={"ok": True, "result": []})
        client = self.client(handler)
        self.assertEqual(client.get_updates(3, timeout=0), [])
        self.assertEqual(json.loads(requests[0].content)['timeout'], 0)
        for value in (-1, 51, True, "0"):
            with self.subTest(value=value), self.assertRaises(TelegramError):
                client.get_updates(3, timeout=value)
        self.assertEqual(len(requests), 1)

    def test_send_requires_real_message_acknowledgement(self):
        for result in ({}, [], None, {'message_id': True}, {'message_id': 0}, {'message_id': '2'}):
            with self.subTest(result=result):
                client = self.client(lambda request: httpx.Response(200, json={"ok": True, "result": result}))
                with self.assertRaises(TelegramError):
                    client.send_chunk('Тест')
        client = self.client(lambda request: httpx.Response(200, json={"ok": True, "result": {'message_id': 2}}))
        self.assertEqual(client.send_chunk('Тест'), 2)

    def test_429_returns_retry_after_without_echoing_response(self):
        secret = "111:SECRET_abcdef https://secret.invalid private response"
        client = self.client(lambda request: httpx.Response(429, json={
            "ok": False, "error_code": 429, "description": secret,
            "parameters": {"retry_after": 123},
        }))
        with self.assertRaises(TelegramError) as caught:
            client.send_chunk("hello")
        self.assertEqual(caught.exception.retry_after, 123)
        self.assertEqual(caught.exception.status_code, 429)
        self.assertNotIn("SECRET", str(caught.exception))
        self.assertNotIn("secret.invalid", str(caught.exception))
        self.assertNotIn("private response", str(caught.exception))

    def test_transport_error_does_not_expose_token_or_url(self):
        def handler(request):
            raise httpx.ConnectError(f"Failure at {request.url}", request=request)

        client = self.client(handler)
        with self.assertRaises(TelegramError) as caught:
            client.get_updates(0)
        self.assertNotIn("SECRET", str(caught.exception))
        self.assertNotIn("https", str(caught.exception))

    def test_bad_update_shape_is_rejected(self):
        client = self.client(lambda request: httpx.Response(200, json={"ok": True, "result": ["bad"]}))
        with self.assertRaises(TelegramError):
            client.get_updates(0)

    def test_send_chunk_refuses_oversized_input_before_network(self):
        requests = []
        client = self.client(lambda request: requests.append(request))
        with self.assertRaises(TelegramError):
            client.send_chunk("😀" * 2001)
        self.assertEqual(requests, [])

    def test_only_explicit_proxy_is_used_and_environment_proxy_is_ignored(self):
        for explicit in ("", "socks5://127.0.0.1:1080"):
            with self.subTest(explicit=explicit), patch.dict(os.environ, {
                **ENV, "HTTP_PROXY": "http://unwanted.invalid", "HTTPS_PROXY": "http://unwanted.invalid",
                "TELEGRAM_PROXY_URL": explicit,
            }, clear=True), patch("mail_summary_bot.telegram.httpx.Client") as constructor:
                TelegramClient(TelegramConfig())
                self.assertFalse(constructor.call_args.kwargs["trust_env"])
                self.assertEqual(constructor.call_args.kwargs["proxy"], explicit or None)

    def test_missing_credentials_are_sanitized(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(TelegramError):
            TelegramClient(TelegramConfig())


if __name__ == "__main__":
    unittest.main()
