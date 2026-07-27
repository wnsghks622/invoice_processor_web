# -*- coding: utf-8 -*-
"""Guards the branch's headline security rule: once an API key is saved, it is never
displayed back to the user. Drives the real /settings route with Flask's test client,
against a temp .env holding a FAKE key - never the real one."""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
import state
import app as flask_app_module

FAKE_KEY = "sk-ant-test-settings-page-should-not-echo"


class SettingsPageNeverShowsTheKey(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ipw_settings_"))
        self._real_env_file = config.ENV_FILE
        config.ENV_FILE = self.tmp / ".env"
        # save_api_key() also sets os.environ - keep that isolated from the real key too.
        self._had_env_var = os.environ.pop("ANTHROPIC_API_KEY", None)
        state.save_api_key(FAKE_KEY)
        self.client = flask_app_module.app.test_client()

    def tearDown(self):
        config.ENV_FILE = self._real_env_file
        os.environ.pop("ANTHROPIC_API_KEY", None)
        if self._had_env_var is not None:
            os.environ["ANTHROPIC_API_KEY"] = self._had_env_var
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_saved_key_does_not_appear_on_settings_page(self):
        resp = self.client.get("/settings")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertNotIn(FAKE_KEY, body)
        # Sanity check: the fixture actually exercises the "key is saved" path (status
        # shows Saved), not a coincidentally key-free page (e.g. a blank/error response).
        self.assertIn("Saved", body)


if __name__ == "__main__":
    unittest.main()
