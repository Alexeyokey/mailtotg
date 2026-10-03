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

    def test_relative_database_belongs_to_config_directory(self):
        config = load_config(self.config, secrets=False)
        self.assertEqual(config.service.database, str((self.root / "data/state.sqlite3").resolve()))
        self.assertEqual(len(config.accounts), 2)

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
