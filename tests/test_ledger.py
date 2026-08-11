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

    def test_a_state_change_touches_only_the_named_instance(self):
        # Two instances, because a single-row fixture cannot tell "UPDATE ... WHERE id=?"
        # apart from "UPDATE every row" - and a dropped WHERE on this particular statement
        # would silently mark a whole month done. This also pins satisfied_by, which the
        # test above claims to cover in its name but never asserts: it is the column that
        # records WHY a row is closed, and Task 8 writes 'invoice:<id>' into it through a
        # different statement, so nothing else exercises it here.
        conn = make_db()
        self._seed(conn)
        other = ledger.add_obligation(conn=conn, kind="EXPECT", title="Frontier",
                                      window_rule="day:9", cadence="monthly")
        conn.execute(
            "INSERT INTO obligation_instance (obligation_id, period, due_from, due_to) "
            "VALUES (?, ?, ?, ?)", (other, "August 2026", "2026-08-09", "2026-08-09"))
        before = {r["title"]: r for r in
                  ledger.instances_for_period("August 2026", conn=conn)}
        ledger.set_instance_state(before["Athens"]["id"], "done",
                                  satisfied_by="invoice:42", conn=conn)
        after = {r["title"]: r for r in
                 ledger.instances_for_period("August 2026", conn=conn)}
        self.assertEqual(after["Athens"]["state"], "done")
        self.assertEqual(after["Athens"]["satisfied_by"], "invoice:42")
        self.assertEqual(after["Frontier"]["state"], "open")
        self.assertEqual(after["Frontier"]["satisfied_by"], "")


from core import periods


class OpenPeriod(unittest.TestCase):
    def test_creates_one_instance_per_applicable_obligation(self):
        conn = make_db()
        ledger.add_obligation(conn=conn, kind="ACTION", title="a", window_rule="day:1",
                              cadence="monthly")
        ledger.add_obligation(conn=conn, kind="ACTION", title="b", window_rule="week:2",
                              cadence="monthly")
        result = ledger.open_period("August 2026", conn=conn)
        self.assertEqual(result["created"], 2)
        self.assertEqual(len(ledger.instances_for_period("August 2026", conn=conn)), 2)

    def test_is_idempotent_and_preserves_state(self):
        conn = make_db()
        ledger.add_obligation(conn=conn, kind="ACTION", title="a", window_rule="day:1",
                              cadence="monthly")
        ledger.open_period("August 2026", conn=conn)
        inst = ledger.instances_for_period("August 2026", conn=conn)[0]
        ledger.set_instance_state(inst["id"], "done", conn=conn)

        again = ledger.open_period("August 2026", conn=conn)
        self.assertEqual((again["created"], again["existing"]), (0, 1))
        after = ledger.instances_for_period("August 2026", conn=conn)[0]
        self.assertEqual(after["state"], "done")     # not reset

    def test_resolves_the_window_onto_the_period(self):
        conn = make_db()
        ledger.add_obligation(conn=conn, kind="ACTION", title="a", window_rule="day:28-30",
                              cadence="monthly")
        ledger.open_period("February 2026", conn=conn)
        inst = ledger.instances_for_period("February 2026", conn=conn)[0]
        self.assertEqual((inst["due_from"], inst["due_to"]), ("2026-02-28", "2026-02-28"))

    def test_on_demand_obligations_never_get_an_instance(self):
        # The whole point: a work-order vendor cannot be "missing".
        conn = make_db()
        ledger.add_obligation(conn=conn, kind="EXPECT", title="plumber",
                              window_rule="learned", cadence="on-demand")
        result = ledger.open_period("August 2026", conn=conn)
        self.assertEqual(result["created"], 0)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(ledger.instances_for_period("August 2026", conn=conn), [])

    def test_off_anchor_periods_get_nothing(self):
        conn = make_db()
        ledger.add_obligation(conn=conn, kind="EXPECT", title="LADWP Monette",
                              window_rule="day:15", cadence="odd-months", anchor=1)
        self.assertEqual(ledger.open_period("August 2026", conn=conn)["created"], 0)
        self.assertEqual(ledger.open_period("September 2026", conn=conn)["created"], 1)

    def test_a_quarterly_obligation_lands_only_on_its_anchor(self):
        conn = make_db()
        ledger.add_obligation(conn=conn, kind="EXPECT", title="amtech",
                              window_rule="day:20", cadence="quarterly", anchor=1)
        for period, expected in (("July 2026", 1), ("August 2026", 0),
                                 ("September 2026", 0), ("October 2026", 1)):
            with self.subTest(period=period):
                self.assertEqual(
                    ledger.open_period(period, conn=conn)["created"], expected)

    def test_a_one_off_lands_in_exactly_one_period(self):
        conn = make_db()
        ledger.add_obligation(conn=conn, kind="ACTION", title="check the distribution",
                              window_rule="date:2026-08-18", cadence="once")
        self.assertEqual(ledger.open_period("July 2026", conn=conn)["created"], 0)
        self.assertEqual(ledger.open_period("August 2026", conn=conn)["created"], 1)
        self.assertEqual(ledger.open_period("September 2026", conn=conn)["created"], 0)

    def test_inactive_obligations_are_not_rolled(self):
        conn = make_db()
        oid = ledger.add_obligation(conn=conn, kind="ACTION", title="a",
                                    window_rule="day:1", cadence="monthly")
        ledger.update_obligation(oid, conn=conn, active=0)
        self.assertEqual(ledger.open_period("August 2026", conn=conn)["created"], 0)


import datetime


class IsMissing(unittest.TestCase):
    """A list that cries wolf is a list nobody opens, so an instance becomes 'missing'
    only once it is genuinely late for its own confidence level."""

    def _inst(self, **over):
        base = {
            "kind": "EXPECT", "state": "open", "satisfied_by": "",
            "period": "August 2026", "due_from": "2026-08-05", "due_to": "2026-08-05",
            "confidence": "high", "cadence": "monthly", "notes": "",
        }
        base.update(over)
        return base

    def test_not_missing_before_the_window_closes(self):
        self.assertFalse(ledger.is_missing(self._inst(), datetime.date(2026, 8, 4)))

    def test_not_missing_inside_the_slack(self):
        self.assertFalse(ledger.is_missing(self._inst(), datetime.date(2026, 8, 7)))

    def test_missing_once_high_confidence_slack_expires(self):
        self.assertTrue(ledger.is_missing(self._inst(), datetime.date(2026, 8, 8)))

    def test_medium_confidence_gets_a_week(self):
        inst = self._inst(confidence="medium")
        self.assertFalse(ledger.is_missing(inst, datetime.date(2026, 8, 12)))
        self.assertTrue(ledger.is_missing(inst, datetime.date(2026, 8, 13)))

    def test_low_confidence_waits_for_the_last_week(self):
        inst = self._inst(confidence="low")
        self.assertFalse(ledger.is_missing(inst, datetime.date(2026, 8, 24)))
        self.assertTrue(ledger.is_missing(inst, datetime.date(2026, 8, 25)))

    def test_irregular_cadence_also_waits_for_the_last_week(self):
        inst = self._inst(confidence="high", cadence="irregular")
        self.assertFalse(ledger.is_missing(inst, datetime.date(2026, 8, 10)))
        self.assertTrue(ledger.is_missing(inst, datetime.date(2026, 8, 25)))

    def test_a_satisfied_instance_is_never_missing(self):
        inst = self._inst(state="done", satisfied_by="invoice:5")
        self.assertFalse(ledger.is_missing(inst, datetime.date(2026, 8, 31)))

    def test_a_skipped_instance_is_never_missing(self):
        inst = self._inst(state="skipped")
        self.assertFalse(ledger.is_missing(inst, datetime.date(2026, 8, 31)))

    def test_an_unconfirmed_expectation_is_never_missing(self):
        # Promotion is a guess until you confirm it. A wrong guess must cost nothing.
        inst = self._inst(notes="unconfirmed")
        self.assertFalse(ledger.is_missing(inst, datetime.date(2026, 8, 31)))

    def test_an_instance_with_no_window_is_not_missing(self):
        # due_to defaults to '' in the schema and an instance can be created without one -
        # Task 1's schema tests insert exactly that shape, and instances_for_period returns
        # it like any other row. Without the guard, date.fromisoformat('') raises ValueError
        # and takes the entire month page down rather than skipping one row.
        self.assertFalse(
            ledger.is_missing(self._inst(due_to=""), datetime.date(2026, 8, 31)))

    def test_a_reminder_uses_its_window_with_no_slack(self):
        # You set the date yourself, so there is no learned uncertainty to allow for.
        inst = self._inst(kind="ACTION", confidence="high", due_to="2026-08-12")
        self.assertFalse(ledger.is_missing(inst, datetime.date(2026, 8, 12)))
        self.assertTrue(ledger.is_missing(inst, datetime.date(2026, 8, 13)))


if __name__ == "__main__":
    unittest.main()
