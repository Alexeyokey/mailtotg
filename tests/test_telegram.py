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
        text = "<b>Не HTML</b> 😀\n" * 400
        client.send_text(text)
        payloads = [json.loads(request.content) for request in requests]
        self.assertEqual("".join(payload["text"] for payload in payloads), text)
        for request, payload in zip(requests, payloads):
            self.assertEqual(request.url.path.split("/")[-1], "sendMessage")
            self.assertEqual(payload["chat_id"], 12345)
            self.assertNotIn("parse_mode", payload)
            self.assertEqual(payload["link_preview_options"], {"is_disabled": True})
            self.assertLessEqual(len(payload["text"].encode("utf-16-le")) // 2, 4000)

    def test_get_updates_does_not_modify_webhooks(self):
        requests = []
        updates = [{"update_id": 17, "message": {"text": "/summary"}}]

        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={"ok": True, "result": updates})

        client = self.client(handler)
        self.assertEqual(client.get_updates(18), updates)
        self.assertEqual(len(requests), 1)
        self.assertTrue(str(requests[0].url).endswith("/getUpdates"))
        self.assertEqual(json.loads(requests[0].content), {
            "offset": 18, "timeout": 10, "allowed_updates": ["message"],
        })

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
