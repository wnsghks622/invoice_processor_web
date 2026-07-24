# -*- coding: utf-8 -*-
"""Unit tests for the platform seam in state.py. These must pass on any OS - the pure
helpers take the platform string as an argument so both branches are exercised from
whichever machine runs the suite."""
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


if __name__ == "__main__":
    unittest.main()
