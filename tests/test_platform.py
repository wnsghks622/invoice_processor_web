# -*- coding: utf-8 -*-
"""Unit tests for the platform seam in state.py. These must pass on any OS - the pure
helpers take the platform string as an argument so both branches are exercised from
whichever machine runs the suite."""
import os
import sys
import unittest
from pathlib import Path, PurePosixPath, PureWindowsPath

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


class TesseractDiscovery(unittest.TestCase):
    """Asserts on the exported fallback tuple, not on the function's source text - a source
    scan would pass on a path that only appears in a comment and break on any refactor."""

    def test_homebrew_paths_are_candidates(self):
        # Apple Silicon installs to /opt/homebrew, Intel to /usr/local. shutil.which()
        # covers the PATH case; these fallbacks cover a launcher with a minimal PATH.
        from core import bankrec
        self.assertIn("/opt/homebrew/bin/tesseract", bankrec._TESSERACT_FALLBACK_PATHS)
        self.assertIn("/usr/local/bin/tesseract", bankrec._TESSERACT_FALLBACK_PATHS)

    def test_windows_paths_still_present(self):
        from core import bankrec
        self.assertIn(r"C:\Program Files\Tesseract-OCR\tesseract.exe",
                      bankrec._TESSERACT_FALLBACK_PATHS)

    def test_fallbacks_are_absolute_paths(self):
        from core import bankrec
        for p in bankrec._TESSERACT_FALLBACK_PATHS:
            # Check absolute paths cross-platform: Unix paths use PurePosixPath, Windows use PureWindowsPath
            if p.startswith('/'):
                self.assertTrue(PurePosixPath(p).is_absolute(), p)
            else:
                self.assertTrue(PureWindowsPath(p).is_absolute(), p)

    def test_returns_none_or_a_real_path(self):
        from core import bankrec
        found = bankrec._find_tesseract()
        self.assertTrue(found is None or os.path.exists(found), found)


if __name__ == "__main__":
    unittest.main()
