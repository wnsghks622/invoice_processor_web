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


from core import ledger


class ObligationCrud(unittest.TestCase):
    def test_add_returns_an_id_and_round_trips(self):
        conn = make_db()
        oid = ledger.add_obligation(conn=conn, kind="ACTION", title="Call Michelle",
                                    window_rule="day:12", cadence="monthly")
        row = ledger.get_obligation(oid, conn=conn)
        self.assertEqual((row["kind"], row["title"], row["window_rule"], row["cadence"]),
                         ("ACTION", "Call Michelle", "day:12", "monthly"))

    def test_unknown_fields_are_ignored_rather_than_crashing(self):
        conn = make_db()
        oid = ledger.add_obligation(conn=conn, kind="ACTION", title="x", nonsense="y")
        self.assertIsNotNone(ledger.get_obligation(oid, conn=conn))

    def test_update_changes_only_what_is_passed(self):
        conn = make_db()
        oid = ledger.add_obligation(conn=conn, kind="EXPECT", title="LADWP",
                                    cadence="monthly", source="learned")
        ledger.update_obligation(oid, conn=conn, cadence="even-months", anchor=0)
        row = ledger.get_obligation(oid, conn=conn)
        self.assertEqual((row["cadence"], row["anchor"]), ("even-months", 0))
        self.assertEqual(row["title"], "LADWP")        # untouched
        self.assertEqual(row["source"], "learned")     # untouched

    def test_get_returns_none_for_a_missing_id(self):
        conn = make_db()
        self.assertIsNone(ledger.get_obligation(999, conn=conn))

    def test_active_obligations_excludes_inactive_ones(self):
        conn = make_db()
        ledger.add_obligation(conn=conn, kind="ACTION", title="live")
        dead = ledger.add_obligation(conn=conn, kind="ACTION", title="retired")
        ledger.update_obligation(dead, conn=conn, active=0)
        self.assertEqual([o["title"] for o in ledger.active_obligations(conn=conn)], ["live"])


class InstanceQueries(unittest.TestCase):
    def _seed(self, conn):
        oid = ledger.add_obligation(conn=conn, kind="EXPECT", title="Athens",
                                    window_rule="day:5", cadence="monthly")
        conn.execute(
            "INSERT INTO obligation_instance (obligation_id, period, due_from, due_to) "
            "VALUES (?, ?, ?, ?)", (oid, "August 2026", "2026-08-05", "2026-08-05"))
        return oid

    def test_instances_for_period_carries_the_obligation_fields(self):
        conn = make_db()
        oid = self._seed(conn)
        rows = ledger.instances_for_period("August 2026", conn=conn)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "Athens")
        self.assertEqual(rows[0]["cadence"], "monthly")
        self.assertEqual(rows[0]["obligation_id"], oid)

    def test_instances_for_period_is_scoped_to_that_period(self):
        conn = make_db()
        self._seed(conn)
        self.assertEqual(ledger.instances_for_period("July 2026", conn=conn), [])

    def test_set_instance_state_records_state_note_and_satisfier(self):
        conn = make_db()
        self._seed(conn)
        inst = ledger.instances_for_period("August 2026", conn=conn)[0]
        ledger.set_instance_state(inst["id"], "skipped", note="LADWP skips odd months",
                                  satisfied_by="", conn=conn)
        after = ledger.instances_for_period("August 2026", conn=conn)[0]
        self.assertEqual(after["state"], "skipped")
        self.assertEqual(after["note"], "LADWP skips odd months")

    def test_marking_done_stamps_done_at(self):
        conn = make_db()
        self._seed(conn)
        inst = ledger.instances_for_period("August 2026", conn=conn)[0]
        ledger.set_instance_state(inst["id"], "done", conn=conn)
        after = ledger.instances_for_period("August 2026", conn=conn)[0]
        self.assertEqual(after["state"], "done")
        self.assertTrue(after["done_at"])


if __name__ == "__main__":
    unittest.main()
