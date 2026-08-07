# -*- coding: utf-8 -*-
"""Column-migration helper: adding a column to an already-populated table.

Run from the project root:

    python -m unittest discover -s tests -t . -v
"""
import sqlite3
import sys
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

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

    def test_vendor_identity_columns_are_registered(self):
        for entry in (
            ("vendors",  "canonical_name",      "TEXT DEFAULT ''"),
            ("vendors",  "active",              "INTEGER DEFAULT 1"),
            ("invoices", "vendor_id",           "INTEGER"),
            ("invoices", "vendor_needs_review", "INTEGER DEFAULT 0"),
        ):
            with self.subTest(entry=entry):
                self.assertIn(entry, db._ADDED_COLUMNS)

    def test_vendor_columns_are_in_the_invoice_column_list(self):
        self.assertIn("vendor_id", db.INVOICE_COLUMNS)
        self.assertIn("vendor_needs_review", db.INVOICE_COLUMNS)


class VendorIdentityMigrationRegression(unittest.TestCase):
    """Regression tests for the vendor identity schema migration.

    Task 6 added vendor_id and vendor_needs_review to invoices, and canonical_name
    and active to vendors. These tests verify that db.init() properly handles:
    1. Migrating existing databases whose tables predate the new columns
    2. The self-healing property: columns in _ADDED_COLUMNS are applied to fresh
       databases even if not in _SCHEMA_TABLES

    Both tests patch db._connect to exercise the real db.init() function.
    """

    def test_upgrade_existing_database_without_vendor_columns(self):
        """Verify db.init() successfully migrates a pre-Task-6 database.

        Pre-Task-6 invoices and vendors tables lack the new columns. The correct
        ordering (tables→migrate→indexes) ensures indexes can reference newly-added
        columns. Under the pre-round-1 ordering (executescript(_SCHEMA) first,
        _ensure_columns second), the index creation fails because vendor_id doesn't
        exist yet on an existing table.
        """
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row

        # Create pre-Task-6 invoices and vendors tables
        pre_task6_schema = """
        CREATE TABLE IF NOT EXISTS invoices (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            status           TEXT DEFAULT 'OK',
            vendor_name      TEXT DEFAULT '',
            invoice_number   TEXT DEFAULT '',
            unit             TEXT DEFAULT '',
            invoice_date     TEXT DEFAULT '',
            invoice_date_iso TEXT DEFAULT '',
            due_date         TEXT DEFAULT '',
            amount           REAL,
            amount_text      TEXT DEFAULT '',
            description      TEXT DEFAULT '',
            line_items       TEXT DEFAULT '',
            property         TEXT DEFAULT '',
            source_file      TEXT DEFAULT '',
            date_processed   TEXT DEFAULT '',
            entered_in_yardi INTEGER DEFAULT 0,
            stored_file      TEXT DEFAULT '',
            reconciled       TEXT DEFAULT '',
            check_number     TEXT DEFAULT '',
            carried_forward  TEXT DEFAULT '',
            needs_review     INTEGER DEFAULT 0,
            origin           TEXT DEFAULT 'processor'
        );
        CREATE TABLE IF NOT EXISTS vendors (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            short_name TEXT NOT NULL UNIQUE,
            aliases    TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS properties (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            canonical_name TEXT NOT NULL UNIQUE,
            property_code  TEXT DEFAULT '',
            aliases        TEXT DEFAULT '',
            sort_order     INTEGER DEFAULT 0
        );
        """
        conn.executescript(pre_task6_schema)
        conn.execute("INSERT INTO invoices (vendor_name) VALUES ('Test Vendor')")
        conn.execute("INSERT INTO vendors (short_name) VALUES ('TestV')")

        @contextmanager
        def fake_connect():
            with conn:
                yield conn

        # Patch _connect so db.init() uses our pre-Task-6 connection
        with mock.patch.object(db, "_connect", fake_connect):
            # Call the real db.init()
            db.init()

        # Verify the columns were added
        cols = {r[1] for r in conn.execute("PRAGMA table_info(invoices)")}
        self.assertIn("vendor_id", cols)
        self.assertIn("vendor_needs_review", cols)

        cols = {r[1] for r in conn.execute("PRAGMA table_info(vendors)")}
        self.assertIn("canonical_name", cols)
        self.assertIn("active", cols)

        # Verify the index was created
        indices = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_invoices_vendor_id'"
        )}
        self.assertIn("idx_invoices_vendor_id", indices)

        # Verify data was preserved
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM invoices").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM vendors").fetchone()[0], 1)

        conn.close()

    def test_self_healing_property_columns_in_added_columns_only(self):
        """Verify columns in _ADDED_COLUMNS are applied to fresh databases.

        This property requires tables to be created before _ensure_columns runs.
        Under round-1's ordering (_ensure_columns before executescript),
        _ensure_columns runs on a fresh database where no tables exist, skips them
        (per its design), and columns registered only in _ADDED_COLUMNS are never
        added. The correct ordering (tables→migrate→indexes) ensures tables exist
        before migration, so the fake column is successfully added.
        """
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row

        # Create a fake column that is in _ADDED_COLUMNS but NOT in _SCHEMA_TABLES
        fake_added_columns = [
            ("invoices", "fake_regression_test_column", "TEXT DEFAULT 'regression_test'"),
        ]

        @contextmanager
        def fake_connect():
            with conn:
                yield conn

        # Patch both _connect and _ADDED_COLUMNS so db.init() sees the fake column
        with mock.patch.object(db, "_connect", fake_connect):
            with mock.patch.object(db, "_ADDED_COLUMNS", fake_added_columns):
                # Call the real db.init() which will create tables, add columns, create indexes
                db.init()

        # Verify the fake column was added to the invoices table
        cols = {r[1] for r in conn.execute("PRAGMA table_info(invoices)")}
        self.assertIn("fake_regression_test_column", cols)

        # Verify the default value works
        conn.execute("INSERT INTO invoices (vendor_name) VALUES ('Test')")
        row = conn.execute("SELECT fake_regression_test_column FROM invoices WHERE id=1").fetchone()
        self.assertEqual(row[0], "regression_test")

        conn.close()


class InvoiceDateQueries(unittest.TestCase):
    """Behavioural tests for the date queries, against a real in-memory schema.

    Also the first test to exercise _ensure_columns' spec=None path, which is the
    one db.init() uses in production.
    """

    def _db(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(db._SCHEMA)
        db._ensure_columns(conn)  # spec=None path, as init() calls it
        return conn

    def test_empty_iso_is_unresolved(self):
        """A row with invoice_date_iso = '' is returned by unresolved_date_invoices."""
        conn = self._db()
        conn.execute(
            "INSERT INTO invoices (id, vendor_name, invoice_date, invoice_date_iso) "
            "VALUES (1, 'Test Vendor', '2024-01-15', '')"
        )
        result = db.unresolved_date_invoices(conn=conn)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["id"], 1)

    def test_iso_value_is_resolved(self):
        """A row with a real ISO value is not returned by unresolved_date_invoices."""
        conn = self._db()
        conn.execute(
            "INSERT INTO invoices (id, vendor_name, invoice_date, invoice_date_iso) "
            "VALUES (1, 'Test Vendor', '2024-01-15', '2024-01-15')"
        )
        result = db.unresolved_date_invoices(conn=conn)
        self.assertEqual(len(result), 0)

    def test_null_iso_is_unresolved(self):
        """A row where invoice_date_iso is SQL NULL is also returned - this is what
        the COALESCE is for, and a plain = '' comparison would miss it."""
        conn = self._db()
        conn.execute(
            "INSERT INTO invoices (id, vendor_name, invoice_date, invoice_date_iso) "
            "VALUES (1, 'Test Vendor', '2024-01-15', NULL)"
        )
        result = db.unresolved_date_invoices(conn=conn)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["id"], 1)

    def test_set_invoice_date_writes_both_columns(self):
        """set_invoice_date writes both invoice_date and invoice_date_iso in one call,
        and the row then disappears from unresolved_date_invoices."""
        conn = self._db()
        conn.execute(
            "INSERT INTO invoices (id, vendor_name, invoice_date, invoice_date_iso) "
            "VALUES (1, 'Test Vendor', '', '')"
        )
        # Verify the row is unresolved before
        result_before = db.unresolved_date_invoices(conn=conn)
        self.assertEqual(len(result_before), 1)

        # Call set_invoice_date
        db.set_invoice_date(1, "2024-01-15", "2024-01-15", conn=conn)

        # Verify both columns were written
        row = conn.execute("SELECT invoice_date, invoice_date_iso FROM invoices WHERE id=1").fetchone()
        self.assertEqual(row["invoice_date"], "2024-01-15")
        self.assertEqual(row["invoice_date_iso"], "2024-01-15")

        # Verify the row is no longer unresolved
        result_after = db.unresolved_date_invoices(conn=conn)
        self.assertEqual(len(result_after), 0)

    def test_results_are_newest_first(self):
        """Results come back newest-first (ORDER BY id DESC)."""
        conn = self._db()
        conn.execute(
            "INSERT INTO invoices (id, vendor_name, invoice_date, invoice_date_iso) "
            "VALUES (1, 'Vendor A', '2024-01-15', '')"
        )
        conn.execute(
            "INSERT INTO invoices (id, vendor_name, invoice_date, invoice_date_iso) "
            "VALUES (2, 'Vendor B', '2024-01-16', '')"
        )
        conn.execute(
            "INSERT INTO invoices (id, vendor_name, invoice_date, invoice_date_iso) "
            "VALUES (3, 'Vendor C', '2024-01-17', '')"
        )
        result = db.unresolved_date_invoices(conn=conn)
        self.assertEqual(len(result), 3)
        self.assertEqual(result[0]["id"], 3)
        self.assertEqual(result[1]["id"], 2)
        self.assertEqual(result[2]["id"], 1)


if __name__ == "__main__":
    unittest.main()
