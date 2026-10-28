"""Configuration env-expansion tests (regressions for ``%`` handling)."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config


class EnvExpansionTests(unittest.TestCase):
    def test_command_with_percent_is_left_alone(self):
        self.assertEqual("date +%Y", config._expand_env("date +%Y"))
        self.assertEqual("grep -E '^a%'", config._expand_env("grep -E '^a%'"))

    @unittest.skipUnless(os.name == "nt", "Windows %VAR% expansion")
    def test_percent_variable_is_expanded_without_rewriting_separators(self):
        with patch.dict(os.environ, {"QIZHI_TEST_DIR": r"C:\Temp\qizhi"}):
            expanded = config._expand_env(r"%QIZHI_TEST_DIR%\logs\bridge.log")
        self.assertEqual(r"C:\Temp\qizhi\logs\bridge.log", expanded)

    def test_nested_values_are_kept_as_strings(self):
        source = {"paths": ["plain", "x"], "command": "date +%Y", "nested": {"pattern": "%Y-%m"}}
        expanded = config._expand_env(source)
        self.assertEqual(source, expanded)
        self.assertIsInstance(expanded["paths"][0], str)

    def test_load_config_accepts_percent_in_any_string(self):
        temp_dir = Path(tempfile.mkdtemp())
        path = temp_dir / "config.json"
        path.write_text(
            json.dumps(
                {
                    "keepalive_command": "date +%Y",
                    "api_host": "127.0.0.1",
                    "dangerous_command_guard_patterns": ["%Y-%m-%d"],
                    "log_level": "INFO",
                }
            ),
            encoding="utf-8",
        )

        loaded = config.load_config(str(path))

        self.assertEqual("date +%Y", loaded["keepalive_command"])
        self.assertEqual("127.0.0.1", loaded["api_host"])
        self.assertEqual(["%Y-%m-%d"], loaded["dangerous_command_guard_patterns"])
        self.assertEqual("INFO", loaded["log_level"])

    def test_default_config_prefers_shell_channel(self):
        self.assertIs(True, config.DEFAULT_CONFIG["ssh_prefer_shell"])


if __name__ == "__main__":
    unittest.main()
