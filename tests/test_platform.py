# -*- coding: utf-8 -*-
"""Unit tests for the platform seam in state.py. These must pass on any OS - the pure
helpers take the platform string as an argument so both branches are exercised from
whichever machine runs the suite."""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import state


class FileManagerName(unittest.TestCase):
    def test_windows(self):
        self.assertEqual(state.file_manager_name("win32"), "File Explorer")

    def test_mac(self):
        self.assertEqual(state.file_manager_name("darwin"), "Finder")

    def test_other_platforms_get_a_generic_name(self):
        self.assertEqual(state.file_manager_name("linux"), "your file manager")

    def test_module_constants_match_this_machine(self):
        self.assertEqual(state.FILE_MANAGER, state.file_manager_name(sys.platform))
        self.assertEqual(state.IS_WINDOWS, sys.platform == "win32")
        self.assertEqual(state.IS_MAC, sys.platform == "darwin")


class BankRecExamplePath(unittest.TestCase):
    def test_includes_month_folder(self):
        p = state.bank_rec_example_path("July 2026")
        self.assertTrue(p.endswith("July 2026 Bank Rec"), p)

    def test_appends_property_folder(self):
        p = state.bank_rec_example_path("July 2026", "Solair")
        self.assertTrue(p.endswith("July 2026 Bank Rec" + os.sep + "Solair"), p)

    def test_uses_os_separator_not_hardcoded_backslash(self):
        p = state.bank_rec_example_path("July 2026", "Solair")
        self.assertIn(os.sep, p)
        if os.sep == "/":
            self.assertNotIn("\\", p)

    def test_property_name_is_made_folder_safe(self):
        # ':' is illegal in a Windows folder name and awkward in Finder; _safe_folder_name
        # strips it, and the hint must show the real folder the user will see on disk.
        p = state.bank_rec_example_path("July 2026", "Foo: Bar")
        self.assertNotIn(":", p.split("Bank Rec")[-1])

    def test_blank_month_does_not_crash(self):
        self.assertIsInstance(state.bank_rec_example_path("", ""), str)


if __name__ == "__main__":
    unittest.main()
