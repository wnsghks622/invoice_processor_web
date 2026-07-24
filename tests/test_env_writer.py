# -*- coding: utf-8 -*-
"""Unit tests for the .env API-key writer. Uses a temp .env - never touches the real one."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
import state

FAKE_KEY = "sk-ant-test-0000000000000000"
OTHER_KEY = "sk-ant-test-1111111111111111"


class SaveApiKey(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ipw_env_"))
        self._real_env_file = config.ENV_FILE
        config.ENV_FILE = self.tmp / ".env"

    def tearDown(self):
        config.ENV_FILE = self._real_env_file

    def test_creates_env_file_when_absent(self):
        self.assertFalse(config.ENV_FILE.exists())
        state.save_api_key(FAKE_KEY)
        self.assertIn(f"ANTHROPIC_API_KEY={FAKE_KEY}",
                      config.ENV_FILE.read_text(encoding="utf-8"))

    def test_replaces_existing_key_without_duplicating(self):
        config.ENV_FILE.write_text(f"ANTHROPIC_API_KEY={FAKE_KEY}\n", encoding="utf-8")
        state.save_api_key(OTHER_KEY)
        text = config.ENV_FILE.read_text(encoding="utf-8")
        self.assertEqual(text.count("ANTHROPIC_API_KEY="), 1)
        self.assertIn(OTHER_KEY, text)
        self.assertNotIn(FAKE_KEY, text)

    def test_preserves_other_lines(self):
        config.ENV_FILE.write_text(
            "# my config\nCLAUDE_MODEL=claude-sonnet-5\n"
            f"ANTHROPIC_API_KEY={FAKE_KEY}\n", encoding="utf-8")
        state.save_api_key(OTHER_KEY)
        text = config.ENV_FILE.read_text(encoding="utf-8")
        self.assertIn("# my config", text)
        self.assertIn("CLAUDE_MODEL=claude-sonnet-5", text)
        self.assertIn(OTHER_KEY, text)

    def test_appends_when_file_exists_without_the_key(self):
        config.ENV_FILE.write_text("CLAUDE_MODEL=claude-sonnet-5\n", encoding="utf-8")
        state.save_api_key(FAKE_KEY)
        text = config.ENV_FILE.read_text(encoding="utf-8")
        self.assertIn("CLAUDE_MODEL=claude-sonnet-5", text)
        self.assertIn(f"ANTHROPIC_API_KEY={FAKE_KEY}", text)

    def test_file_ends_with_newline(self):
        state.save_api_key(FAKE_KEY)
        self.assertTrue(config.ENV_FILE.read_text(encoding="utf-8").endswith("\n"))

    def test_rejects_empty(self):
        for bad in ("", "   ", "\t"):
            with self.assertRaises(ValueError):
                state.save_api_key(bad)

    def test_rejects_newline_injection(self):
        # Without this, a pasted value could inject arbitrary extra .env lines.
        for bad in (f"{FAKE_KEY}\nEVIL=1", f"{FAKE_KEY}\r\nEVIL=1", f"{FAKE_KEY}\rEVIL=1"):
            with self.assertRaises(ValueError):
                state.save_api_key(bad)
        self.assertFalse(config.ENV_FILE.exists())

    def test_rejects_control_characters(self):
        with self.assertRaises(ValueError):
            state.save_api_key("sk-ant-\x00-null")

    def test_strips_surrounding_whitespace(self):
        state.save_api_key(f"  {FAKE_KEY}  ")
        self.assertIn(f"ANTHROPIC_API_KEY={FAKE_KEY}\n",
                      config.ENV_FILE.read_text(encoding="utf-8"))

    def test_does_not_leave_temp_files_behind(self):
        state.save_api_key(FAKE_KEY)
        leftovers = [p.name for p in self.tmp.iterdir() if p.name != ".env"]
        self.assertEqual(leftovers, [])

    @unittest.skipIf(os.name == "nt", "POSIX file modes only")
    def test_permissions_are_owner_only_on_posix(self):
        state.save_api_key(FAKE_KEY)
        self.assertEqual(config.ENV_FILE.stat().st_mode & 0o777, 0o600)

    def test_saved_key_is_then_reported_present(self):
        state.save_api_key(FAKE_KEY)
        self.assertTrue(state.api_key_present())

    def test_updates_os_environ_so_the_next_job_uses_it(self):
        # runner.py passes env={**os.environ} to job subprocesses, and load_dotenv() does NOT
        # override an existing variable - so without this the saved key is shadowed by the
        # stale one until the app restarts.
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-stale-9999999999999999"
        try:
            state.save_api_key(FAKE_KEY)
            self.assertEqual(os.environ["ANTHROPIC_API_KEY"], FAKE_KEY)
        finally:
            os.environ.pop("ANTHROPIC_API_KEY", None)


class ApiKeyFromEnvVar(unittest.TestCase):
    """The override check must COMPARE env against file. Importing state imports
    core.processor, which calls load_dotenv() - so .env's key is always in os.environ and a
    bare os.environ check would cry 'override' every time."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ipw_envvar_"))
        self._real_env_file = config.ENV_FILE
        config.ENV_FILE = self.tmp / ".env"
        self._had = os.environ.pop("ANTHROPIC_API_KEY", None)

    def tearDown(self):
        config.ENV_FILE = self._real_env_file
        os.environ.pop("ANTHROPIC_API_KEY", None)
        if self._had is not None:
            os.environ["ANTHROPIC_API_KEY"] = self._had

    def test_false_when_environment_has_no_key(self):
        config.ENV_FILE.write_text(f"ANTHROPIC_API_KEY={FAKE_KEY}\n", encoding="utf-8")
        self.assertFalse(state.api_key_from_env_var())

    def test_false_when_environment_merely_mirrors_the_file(self):
        # This is what load_dotenv() does on every import - it is NOT an override.
        config.ENV_FILE.write_text(f"ANTHROPIC_API_KEY={FAKE_KEY}\n", encoding="utf-8")
        os.environ["ANTHROPIC_API_KEY"] = FAKE_KEY
        self.assertFalse(state.api_key_from_env_var())

    def test_true_when_environment_supplies_a_different_key(self):
        config.ENV_FILE.write_text(f"ANTHROPIC_API_KEY={FAKE_KEY}\n", encoding="utf-8")
        os.environ["ANTHROPIC_API_KEY"] = OTHER_KEY
        self.assertTrue(state.api_key_from_env_var())

    def test_true_when_env_has_a_key_and_no_file_exists(self):
        os.environ["ANTHROPIC_API_KEY"] = OTHER_KEY
        self.assertTrue(state.api_key_from_env_var())


if __name__ == "__main__":
    unittest.main()
