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
    and active to vendors. These tests verify the migration path and the self-healing
    property that separating table DDL from index DDL restores.
    """

    def test_migration_of_old_invoices_table_without_vendor_columns(self):
        """Regression: db.init() must not crash when vendor_id index references
        a non-existent column in an existing table. This is the core bug that
        motivated separating table DDL from index DDL."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row

        # Create invoices table with the OLD schema (without vendor_id and vendor_needs_review)
        old_invoices_schema = """
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
        )
        """
        conn.executescript(old_invoices_schema)

        # Insert a row to simulate an existing database
        conn.execute("INSERT INTO invoices (vendor_name) VALUES ('Test Vendor')")

        # Now run the three-step init sequence that db.init() uses
        conn.executescript(db._SCHEMA_TABLES)      # Tables: no-op since they exist
        db._ensure_columns(conn)                    # Add missing columns
        conn.executescript(db._SCHEMA_INDEXES)      # Indexes: now vendor_id column exists

        # Verify the columns were added
        cols = {r[1] for r in conn.execute("PRAGMA table_info(invoices)")}
        self.assertIn("vendor_id", cols)
        self.assertIn("vendor_needs_review", cols)

        # Verify the index was created
        indices = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_invoices_vendor_id'"
        )}
        self.assertIn("idx_invoices_vendor_id", indices)

        # Verify data was preserved
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM invoices").fetchone()[0], 1)

        conn.close()

    def test_self_healing_property_on_fresh_database(self):
        """Regression: columns registered only in _ADDED_COLUMNS must still be
        applied to fresh databases. This property is only preserved when tables are
        created before _ensure_columns runs."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row

        # Create a temporary fake column that is in _ADDED_COLUMNS but NOT in _SCHEMA
        fake_spec = [
            ("invoices", "fake_regression_test_column", "TEXT DEFAULT 'regression_test'"),
        ]

        # Run the three-step sequence with a fake column
        conn.executescript(db._SCHEMA_TABLES)      # Tables are created empty
        db._ensure_columns(conn, spec=fake_spec)    # Fake column added to existing table
        conn.executescript(db._SCHEMA_INDEXES)      # Indexes created

        # Verify the fake column was added to the existing table
        cols = {r[1] for r in conn.execute("PRAGMA table_info(invoices)")}
        self.assertIn("fake_regression_test_column", cols)

        # Verify the default value is set
        row = conn.execute(
            "INSERT INTO invoices (vendor_name) VALUES ('Test') RETURNING *"
        ).fetchone()
        self.assertEqual(row["fake_regression_test_column"], "regression_test")

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
