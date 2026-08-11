# -*- coding: utf-8 -*-
"""The obligation ledger: the two tables, and the queries over them.

Every test builds a real schema in memory (db._SCHEMA + db._ensure_columns) and passes
that connection in explicitly, so nothing here touches a database file.

Run from the project root:

    python -m unittest discover -s tests -t . -v
"""
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import db


def make_db():
    """A real, empty schema in memory. Shared by every test class in this file."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db._SCHEMA)
    db._ensure_columns(conn)
    return conn


class LedgerSchema(unittest.TestCase):
    def test_obligation_table_has_the_expected_columns(self):
        conn = make_db()
        cols = {r[1] for r in conn.execute("PRAGMA table_info(obligation)")}
        self.assertEqual(cols, {
            "id", "kind", "title", "property_id", "vendor_id", "window_rule",
            "cadence", "anchor", "source", "confidence", "active", "notes",
        })

    def test_instance_table_has_the_expected_columns(self):
        conn = make_db()
        cols = {r[1] for r in conn.execute("PRAGMA table_info(obligation_instance)")}
        self.assertEqual(cols, {
            "id", "obligation_id", "period", "due_from", "due_to",
            "state", "satisfied_by", "done_at", "note",
        })

    def test_one_instance_per_obligation_per_period(self):
        # This constraint is the idempotence mechanism for rollover, not decoration.
        conn = make_db()
        conn.execute("INSERT INTO obligation (kind, title) VALUES ('ACTION', 'x')")
        oid = conn.execute("SELECT id FROM obligation").fetchone()["id"]
        conn.execute("INSERT INTO obligation_instance (obligation_id, period) VALUES (?, ?)",
                     (oid, "August 2026"))
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO obligation_instance (obligation_id, period) VALUES (?, ?)",
                         (oid, "August 2026"))

    def test_the_same_obligation_can_have_instances_in_different_periods(self):
        conn = make_db()
        conn.execute("INSERT INTO obligation (kind, title) VALUES ('ACTION', 'x')")
        oid = conn.execute("SELECT id FROM obligation").fetchone()["id"]
        for period in ("July 2026", "August 2026"):
            conn.execute("INSERT INTO obligation_instance (obligation_id, period) VALUES (?, ?)",
                         (oid, period))
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM obligation_instance").fetchone()[0], 2)

    def test_state_defaults_to_open(self):
        conn = make_db()
        conn.execute("INSERT INTO obligation (kind, title) VALUES ('ACTION', 'x')")
        oid = conn.execute("SELECT id FROM obligation").fetchone()["id"]
        conn.execute("INSERT INTO obligation_instance (obligation_id, period) VALUES (?, ?)",
                     (oid, "August 2026"))
        row = conn.execute("SELECT state FROM obligation_instance").fetchone()
        self.assertEqual(row["state"], "open")

    def test_active_defaults_to_one(self):
        conn = make_db()
        conn.execute("INSERT INTO obligation (kind, title) VALUES ('ACTION', 'x')")
        self.assertEqual(conn.execute("SELECT active FROM obligation").fetchone()["active"], 1)


if __name__ == "__main__":
    unittest.main()
