# -*- coding: utf-8 -*-
"""Column-migration helper: adding a column to an already-populated table.

Run from the project root:

    python -m unittest discover -s tests -t . -v
"""
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import db


class EnsureColumns(unittest.TestCase):
    def _conn(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE widgets (id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute("INSERT INTO widgets (name) VALUES ('existing row')")
        return conn

    def _cols(self, conn, table):
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}

    def test_adds_missing_column_and_keeps_rows(self):
        conn = self._conn()
        db._ensure_columns(conn, [("widgets", "colour", "TEXT DEFAULT ''")])
        self.assertIn("colour", self._cols(conn, "widgets"))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM widgets").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT colour FROM widgets").fetchone()[0], "")

    def test_is_idempotent(self):
        conn = self._conn()
        spec = [("widgets", "colour", "TEXT DEFAULT ''")]
        db._ensure_columns(conn, spec)
        conn.execute("UPDATE widgets SET colour = 'red'")
        db._ensure_columns(conn, spec)          # second run must not reset anything
        self.assertEqual(conn.execute("SELECT colour FROM widgets").fetchone()[0], "red")

    def test_skips_missing_table_without_raising(self):
        conn = self._conn()
        db._ensure_columns(conn, [("nonexistent", "colour", "TEXT DEFAULT ''")])
        self.assertNotIn("nonexistent", {
            r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        })


class AddedColumnsRegistry(unittest.TestCase):
    def test_invoice_date_iso_is_registered(self):
        self.assertIn(
            ("invoices", "invoice_date_iso", "TEXT DEFAULT ''"),
            db._ADDED_COLUMNS,
        )

    def test_invoice_date_iso_is_in_the_column_list(self):
        # INVOICE_COLUMNS filters both insert_invoice and update_invoice; a column
        # missing from it is silently never written.
        self.assertIn("invoice_date_iso", db.INVOICE_COLUMNS)


if __name__ == "__main__":
    unittest.main()
