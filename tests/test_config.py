from pathlib import Path
from tempfile import TemporaryDirectory
import os
import unittest
from unittest.mock import patch

from mail_summary_bot.config import ConfigError, load_config, load_env

BASE = Path(__file__).parents[1] / "config.example.toml"


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = self.root / "config.toml"
        self.config.write_text(BASE.read_text(), encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def one_account(self):
        self.config.write_text(BASE.read_text().split('[[accounts]]')[0] + '[[accounts]]' + BASE.read_text().split('[[accounts]]')[1], encoding="utf-8")

    def test_relative_database_belongs_to_config_directory(self):
        config = load_config(self.config, secrets=False)
        self.assertEqual(config.service.database, str((self.root / "data/state.sqlite3").resolve()))
        self.assertEqual(len(config.accounts), 2)

    def test_one_account_is_accepted(self):
        self.one_account()
        self.assertEqual(len(load_config(self.config, secrets=False).accounts), 1)

    def test_notifications_default_disabled_and_accept_boolean(self):
        self.assertFalse(load_config(self.config, secrets=False).service.notify_new_mail)
        self.config.write_text(BASE.read_text().replace('notify_new_mail = false', 'notify_new_mail = true'))
        self.assertTrue(load_config(self.config, secrets=False).service.notify_new_mail)

    def test_notifications_reject_string_and_integer_values(self):
        for value in ('"true"', '1'):
            with self.subTest(value=value):
                self.config.write_text(BASE.read_text().replace('notify_new_mail = false', 'notify_new_mail = ' + value))
                with self.assertRaisesRegex(ConfigError, 'notify_new_mail'):
                    load_config(self.config, secrets=False)

    def test_zero_accounts_are_rejected(self):
        self.config.write_text(BASE.read_text().split('[[accounts]]')[0], encoding="utf-8")
        with self.assertRaisesRegex(ConfigError, "один или два"):
            load_config(self.config, secrets=False)

    def test_three_accounts_are_rejected(self):
        self.config.write_text(BASE.read_text() + '\n[[accounts]]\nid="third"\nhost="imap.example.test"\nusername="third@example.test"\npassword_env="MAIL_3_PASSWORD"\n', encoding="utf-8")
        with self.assertRaisesRegex(ConfigError, "один или два"):
            load_config(self.config, secrets=False)

    def test_one_account_mail_only_requires_only_its_imap_secret(self):
        self.one_account()
        with patch.dict(os.environ, {"MAIL_1_PASSWORD": "test-only"}, clear=True):
            config = load_config(self.config, mail_only=True)
        self.assertEqual(len(config.accounts), 1)

    def test_mail_only_skips_telegram_and_model_secret_validation(self):
        self.config.write_text(BASE.read_text().replace('mode = "extractive"', 'mode = "openai"'), encoding="utf-8")
        env = {"MAIL_1_PASSWORD": "test-first", "MAIL_2_PASSWORD": "test-second", "TELEGRAM_CHAT_ID": "invalid-unused"}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(load_config(self.config, mail_only=True).summary.mode, "openai")

    def test_mail_only_still_requires_imap_secrets(self):
        self.one_account()
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ConfigError) as error:
                load_config(self.config, mail_only=True)
        self.assertIn("MAIL_1_PASSWORD", str(error.exception))
        self.assertNotIn("TELEGRAM", str(error.exception))

    def test_default_mode_still_requires_telegram_for_one_account(self):
        self.one_account()
        with patch.dict(os.environ, {"MAIL_1_PASSWORD": "test-only"}, clear=True):
            with self.assertRaisesRegex(ConfigError, "TELEGRAM_BOT_TOKEN"):
                load_config(self.config)

    def test_one_account_default_mode_works_with_telegram(self):
        self.one_account()
        env = {"MAIL_1_PASSWORD": "test-only", "TELEGRAM_BOT_TOKEN": "123:test", "TELEGRAM_CHAT_ID": "12345"}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(len(load_config(self.config).accounts), 1)

    def test_secrets_false_remains_compatible_with_mail_only(self):
        self.one_account()
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(len(load_config(self.config, secrets=False, mail_only=True).accounts), 1)

    def test_missing_secrets_reports_names_without_values(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ConfigError, "MAIL_1_PASSWORD"):
                load_config(self.config)

    def test_only_private_positive_chat_ids_are_accepted(self):
        env = {"MAIL_1_PASSWORD": "a", "MAIL_2_PASSWORD": "b", "TELEGRAM_BOT_TOKEN": "123:test", "TELEGRAM_CHAT_ID": "-100123"}
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(ConfigError, "личного чата"):
                load_config(self.config)

    def test_duplicate_account_id_is_rejected(self):
        self.config.write_text(BASE.read_text().replace('id = "work"', 'id = "personal"'))
        with self.assertRaises(ConfigError):
            load_config(self.config, secrets=False)

    def test_duplicate_mailbox_is_rejected(self):
        text = BASE.read_text()
        first = text.split('[[accounts]]')[1]
        self.config.write_text(text.split('[[accounts]]')[0] + '[[accounts]]' + first + '[[accounts]]' + first.replace('id = "personal"', 'id = "other"'), encoding="utf-8")
        with self.assertRaisesRegex(ConfigError, "разные ящики"):
            load_config(self.config, secrets=False)

    def test_unknown_configuration_is_rejected(self):
        self.config.write_text(BASE.read_text().replace('poll_seconds = 60', 'poll_second = 60'))
        with self.assertRaisesRegex(ConfigError, "Неизвестные"):
            load_config(self.config, secrets=False)

    def test_env_values_are_literal_and_existing_environment_wins(self):
        env = self.root / ".env"
        env.write_text("# comment\nTASK_SECRET='$(touch sentinel) # literal'\nEMPTY=''\nPRESERVE=file\n", encoding="utf-8")
        with patch.dict(os.environ, {"PRESERVE": "original"}, clear=True):
            load_env(env)
            self.assertEqual(os.environ["TASK_SECRET"], "$(touch sentinel) # literal")
            self.assertEqual(os.environ["EMPTY"], "")
            self.assertEqual(os.environ["PRESERVE"], "original")
            self.assertFalse((self.root / "sentinel").exists())

    def test_invalid_timezone_is_rejected(self):
        self.config.write_text(BASE.read_text().replace('Europe/Moscow', 'invalid/example'))
        with self.assertRaisesRegex(ConfigError, "timezone"):
            load_config(self.config, secrets=False)


if __name__ == "__main__":
    unittest.main()
