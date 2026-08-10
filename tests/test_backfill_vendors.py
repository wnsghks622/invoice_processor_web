# -*- coding: utf-8 -*-
"""scripts/backfill_vendors.py - the step that actually binds invoices.vendor_id.

Why this file exists: the whole-branch review rebuilt a pre-branch database, ran the
documented runbook end to end, and got `vendors: 86` with `invoices with vendor_id bound: 0`
and `queued for vendor review: 0`. Every individual piece worked; nothing joined them. The
failure was invisible precisely because unbound-and-unqueued reads as "nothing to do" from
every screen, so the two properties pinned hardest here are the ones that were missing:
a confident match must actually write vendor_id, and everything else must actually reach
the queue.

`decide()` is pure, so most of this needs nothing but a vendor list. `main()` is exercised
against a shared in-memory connection using the same `db._connect` patch documented in
tests/test_app.py - the script's dry-run-writes-nothing and re-run-is-idempotent guarantees
are behavioural, and cannot be checked on the pure helper alone.

Run from the project root:

    python -m unittest discover -s tests -t . -v
"""
import io
import sqlite3
import sys
import unittest
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import db
from scripts import backfill_vendors as bf


VENDORS = [
    {"id": 1, "canonical_name": "Athens Services", "short_name": "Athens",
     "aliases": "ATHENS SERVICES; Athens Svcs"},
    {"id": 2, "canonical_name": "Los Angeles Department of Water and Power",
     "short_name": "LADWP", "aliases": "LADWP; L.A. Dept of Water & Power"},
]


class Decide(unittest.TestCase):
    """The per-row decision: skip, bind, or flag. No database."""

    def test_a_row_that_already_has_a_vendor_id_is_skipped(self):
        # Idempotency, and the reason it matters: this row may have been bound by a human
        # on the Fixer page, which also taught the vendor that spelling. Re-deciding it
        # would re-litigate a decision a person already made.
        action, result = bf.decide({"id": 1, "vendor_name": "Athens Svcs", "vendor_id": 7},
                                   VENDORS)
        self.assertEqual(action, bf.SKIP)
        self.assertIsNone(result)

    def test_a_confident_match_binds(self):
        action, result = bf.decide({"id": 1, "vendor_name": "ATHENS SERVICES",
                                    "vendor_id": None}, VENDORS)
        self.assertEqual(action, bf.BIND)
        self.assertEqual(result.vendor_id, 1)

    def test_a_suggest_band_match_is_flagged_not_bound(self):
        # 'Athen Services' scores 0.9091 against vendor 1 - inside the suggest band
        # (SUGGEST_THRESHOLD <= score < BIND_THRESHOLD). A suggestion that bound itself
        # would make the review queue pointless.
        action, result = bf.decide({"id": 1, "vendor_name": "Athen Services",
                                    "vendor_id": None}, VENDORS)
        self.assertEqual(action, bf.FLAG)
        self.assertEqual(result.outcome, "suggest")

    def test_an_unknown_vendor_is_flagged(self):
        action, result = bf.decide({"id": 1, "vendor_name": "Rolling Greens Nursery",
                                    "vendor_id": None}, VENDORS)
        self.assertEqual(action, bf.FLAG)
        self.assertEqual(result.outcome, "new")

    def test_an_empty_vendor_list_flags_rather_than_crashing(self):
        # The order-of-operations hazard: running this before bootstrap_vendors.py must
        # queue every row for review, not fail and not silently leave them unqueued.
        action, _ = bf.decide({"id": 1, "vendor_name": "ATHENS SERVICES", "vendor_id": None}, [])
        self.assertEqual(action, bf.FLAG)

    def test_a_blank_vendor_name_is_flagged(self):
        for raw in ("", "   ", None):
            with self.subTest(raw=raw):
                action, _ = bf.decide({"id": 1, "vendor_name": raw, "vendor_id": None}, VENDORS)
                self.assertEqual(action, bf.FLAG)


class MainAgainstADatabase(unittest.TestCase):
    """main() end to end against an in-memory database: dry run writes nothing, --apply
    writes both outcomes, and a second --apply changes nothing."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(db._SCHEMA)
        db._ensure_columns(self.conn)

        @contextmanager
        def fake_connect():
            with self.conn:
                yield self.conn

        patcher = mock.patch.object(db, "_connect", fake_connect)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.conn.execute(
            "INSERT INTO vendors (id, short_name, canonical_name, aliases) "
            "VALUES (1, 'Athens', 'Athens Services', 'ATHENS SERVICES; Athens Svcs')")
        # 1: exact hit -> binds. 2: nothing like it -> queued. 3: already bound -> skipped.
        self.conn.execute("INSERT INTO invoices (id, vendor_name) VALUES (1, 'ATHENS SERVICES')")
        self.conn.execute("INSERT INTO invoices (id, vendor_name) "
                          "VALUES (2, 'Rolling Greens Nursery')")
        self.conn.execute("INSERT INTO invoices (id, vendor_name, vendor_id, "
                          "vendor_needs_review) VALUES (3, 'Athens Svcs', 1, 0)")
        self.conn.commit()

    def _run(self, *argv):
        buf = io.StringIO()
        with mock.patch.object(sys, "argv", ["backfill_vendors.py", *argv]):
            with redirect_stdout(buf):
                rc = bf.main()
        self.assertEqual(rc, 0)
        return buf.getvalue()

    def _state(self):
        return {r["id"]: (r["vendor_id"], r["vendor_needs_review"])
                for r in self.conn.execute(
                    "SELECT id, vendor_id, vendor_needs_review FROM invoices")}

    def test_dry_run_reports_the_work_but_writes_nothing(self):
        before = self._state()

        out = self._run()

        self.assertIn("would bind 1", out)
        self.assertIn("would queue for review 1", out)
        self.assertIn("already bound 1", out)
        self.assertIn("Dry run", out)
        self.assertEqual(self._state(), before)

    def test_apply_binds_confident_rows_and_queues_the_rest(self):
        out = self._run("--apply")

        self.assertIn("bound 1", out)
        self.assertIn("queued for review 1", out)
        self.assertEqual(self._state(), {
            1: (1, 0),      # bound, flag clear
            2: (None, 1),   # unbound but queued - never unbound *and* unqueued
            3: (1, 0),      # untouched
        })

    def test_binding_never_rewrites_provenance_or_the_filed_pdf(self):
        self.conn.execute("UPDATE invoices SET vendor_name='ATHENS SERVICES', "
                          "stored_file='Athens_06_2026.pdf' WHERE id=1")
        self.conn.commit()

        self._run("--apply")

        row = self.conn.execute("SELECT * FROM invoices WHERE id=1").fetchone()
        self.assertEqual(row["vendor_id"], 1)                       # sanity: it did bind
        self.assertEqual(row["vendor_name"], "ATHENS SERVICES")
        self.assertEqual(row["stored_file"], "Athens_06_2026.pdf")

    def test_re_applying_changes_nothing(self):
        self._run("--apply")
        after_first = self._state()

        out = self._run("--apply")

        self.assertEqual(self._state(), after_first)
        self.assertIn("bound 0", out)          # nothing left to bind
        self.assertIn("already bound 2", out)  # the newly-bound row is now skipped too


if __name__ == "__main__":
    unittest.main()
