"""本地设置：保存、保留密钥、清除、优先级。"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autocode.llm import settings
from autocode.llm.client import load_config


class TestSettings(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "settings.json"
        self.env = patch.dict(os.environ, {"AUTOCODE_SETTINGS_FILE": str(self.path)}, clear=False)
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.tmp.cleanup()

    def test_save_and_load(self) -> None:
        settings.update_fields({"base_url": "https://api.deepseek.com/v1", "model": "deepseek-v4-pro"})
        data = settings.load_settings()
        self.assertEqual(data["model"], "deepseek-v4-pro")
        self.assertTrue(self.path.exists())

    def test_empty_key_keeps_existing(self) -> None:
        settings.update_fields({"api_key": "sk-test-1234567890"})
        settings.update_fields({"model": "deepseek-v4-flash"})
        self.assertEqual(settings.load_settings()["api_key"], "sk-test-1234567890")

    def test_clear_key(self) -> None:
        settings.update_fields({"api_key": "sk-test-1234567890"})
        settings.update_fields({"clear_key": True})
        self.assertNotIn("api_key", settings.load_settings())

    def test_model_from_settings_overrides_env(self) -> None:
        settings.update_fields({"model": "deepseek-v4-pro"})
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "sk-env", "DEEPSEEK_MODEL": "deepseek-chat"}):
            cfg = load_config()
        self.assertEqual(cfg.model, "deepseek-v4-pro")

    def test_mask(self) -> None:
        self.assertEqual(settings.mask("sk-abcdefgh1234"), "sk-a…1234")
        self.assertEqual(settings.mask(""), "")


if __name__ == "__main__":
    unittest.main()
