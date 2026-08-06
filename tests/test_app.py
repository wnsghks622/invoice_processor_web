# -*- coding: utf-8 -*-
"""Route-level coverage for the Fixer page's date review queue: `GET /fixer`'s undated-invoice
panel, and the `POST /fixer/<id>/date` handler (`fixer_set_date`).

Why this file exists: `fixer_set_date` has four branches (empty-reject, unparseable-reject,
missing-invoice-reject, validate-then-write), and the ordering between validation and the
single write is precisely the guarantee that route exists to provide. No Flask route anywhere
in this codebase had automated coverage before this file, so a future edit that reordered those
checks would regress silently - `python -m unittest discover` would stay green while the
route quietly started writing bad data, or writing before validating.

Database approach
------------------
Every route here calls `db.*` functions with no `conn=` argument (e.g. `fixer_set_date` calls
`db.get_invoice(invoice_id)` and `db.set_invoice_date(invoice_id, iso, iso)` with no connection
threaded through), so they always resolve through `core.db._connect()` -> `config.DB_PATH`,
i.e. the real `data/invoices.db`. The suite's rule is that no test may read or write that file.
`db._conn_or`'s injectable-`conn` parameter (what `tests/test_migration.py`'s
`InvoiceDateQueries` uses) doesn't reach far enough here, because the *routes themselves* don't
accept or forward a connection - the patch has to happen one level down, at `_connect` itself.

So: build one `sqlite3.connect(":memory:")` with the real schema (`db._SCHEMA` +
`db._ensure_columns`, the same fixture shape `test_migration.py` already uses), and monkeypatch
`core.db._connect` to a context manager that yields that *same* connection every time instead of
opening `DB_PATH`. Nothing is ever written to disk, so there is nothing to clean up.

Ordering matters: `app.py` calls `db.init()` at import time (module-level, unconditional) - so
merely `import app` touches whatever `db._connect` currently points at. The patch is installed
in `setUpModule`, *before* `app` is imported, so that first `db.init()` call also lands on the
in-memory connection rather than the real file. Importing `app` first and patching afterward
would already be too late.

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

app = None      # set by setUpModule, once the DB patch is active
_conn = None    # the one shared in-memory connection every patched _connect() call reuses
_patcher = None


@contextmanager
def _fake_connect():
    """Stand-in for core.db._connect: same commit-on-success/rollback-on-error contract (via
    `with _conn:`), but reuses the shared in-memory connection instead of opening DB_PATH, and
    never closes it - production closes after every single operation, but this connection has
    to survive many _connect() calls across one test (and across many tests)."""
    with _conn:
        yield _conn


def setUpModule():
    global app, _conn, _patcher
    _conn = sqlite3.connect(":memory:")
    _conn.row_factory = sqlite3.Row
    _conn.executescript(db._SCHEMA)
    db._ensure_columns(_conn)          # spec=None path, as init() calls it

    _patcher = mock.patch.object(db, "_connect", _fake_connect)
    _patcher.start()
    try:
        import app as _app_module      # db.init() runs here - must land on the in-memory conn
    except BaseException:
        _patcher.stop()
        raise
    app = _app_module
    app.app.testing = True


def tearDownModule():
    _patcher.stop()


class FixerDateQueuePanel(unittest.TestCase):
    """GET /fixer: the undated-invoice panel is the other half of 'visible, not dropped'."""

    def setUp(self):
        _conn.execute("DELETE FROM invoices")   # every test starts from an empty table
        _conn.commit()
        self.client = app.app.test_client()

    def _insert_undated(self, invoice_id, vendor="Test Vendor", property_="Test Property"):
        _conn.execute(
            "INSERT INTO invoices (id, vendor_name, property, invoice_date, invoice_date_iso) "
            "VALUES (?, ?, ?, '', '')",
            (invoice_id, vendor, property_),
        )
        _conn.commit()

    def test_panel_renders_and_lists_unresolved_rows(self):
        self._insert_undated(1, vendor="Acme Plumbing", property_="Maple Court")
        self._insert_undated(2, vendor="Beta Electric", property_="Oak Ridge")

        resp = self.client.get("/fixer")

        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("Dates that could not be read", html)
        self.assertIn("2 invoices", html)
        self.assertIn("Acme Plumbing", html)
        self.assertIn("Beta Electric", html)
        self.assertIn('action="/fixer/1/date"', html)
        self.assertIn('action="/fixer/2/date"', html)

    def test_panel_is_absent_when_the_queue_is_empty(self):
        """The converse of the above: the {% if undated %} guard actually guards. Not on the
        coordinator's list verbatim, but it is the other side of the same template line, and
        costs nothing to lock in alongside it - see the fix-round report for this call-out."""
        resp = self.client.get("/fixer")
        self.assertNotIn("Dates that could not be read", resp.get_data(as_text=True))


class FixerSetDateRoute(unittest.TestCase):
    """POST /fixer/<id>/date (fixer_set_date): empty-reject, unparseable-reject,
    missing-invoice-reject, and validate-then-write."""

    def setUp(self):
        _conn.execute("DELETE FROM invoices")
        _conn.commit()
        self.client = app.app.test_client()

    def _insert_undated(self, invoice_id, vendor="Test Vendor", property_="Test Property"):
        _conn.execute(
            "INSERT INTO invoices (id, vendor_name, property, invoice_date, invoice_date_iso) "
            "VALUES (?, ?, ?, '', '')",
            (invoice_id, vendor, property_),
        )
        _conn.commit()

    def test_empty_input_is_rejected_and_writes_nothing(self):
        self._insert_undated(1)

        resp = self.client.post("/fixer/1/date", data={"invoice_date": ""})
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.headers.get("Location", "").endswith("/fixer"))

        row = db.get_invoice(1)
        self.assertEqual(row["invoice_date"], "")
        self.assertEqual(row["invoice_date_iso"], "")
        self.assertEqual(len(db.unresolved_date_invoices()), 1)

        flashed = self.client.get("/fixer").get_data(as_text=True)
        self.assertIn("Pick a date.", flashed)

    def test_unparseable_input_is_rejected_and_writes_nothing(self):
        self._insert_undated(1)

        resp = self.client.post("/fixer/1/date", data={"invoice_date": "banana"})
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.headers.get("Location", "").endswith("/fixer"))

        row = db.get_invoice(1)
        self.assertEqual(row["invoice_date"], "")
        self.assertEqual(row["invoice_date_iso"], "")
        self.assertEqual(len(db.unresolved_date_invoices()), 1)

        flashed = self.client.get("/fixer").get_data(as_text=True)
        self.assertIn("Could not read", flashed)
        self.assertIn("banana", flashed)

    def test_missing_invoice_is_rejected_and_writes_nothing(self):
        # id 999 was never inserted - the row simply does not exist.
        resp = self.client.post("/fixer/999/date", data={"invoice_date": "2024-01-15"})
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.headers.get("Location", "").endswith("/fixer"))

        self.assertIsNone(db.get_invoice(999))
        self.assertEqual(db.unresolved_date_invoices(), [])   # nothing spuriously created

        flashed = self.client.get("/fixer").get_data(as_text=True)
        self.assertIn("no longer exists", flashed)

    def test_valid_input_writes_both_columns_and_resolves_the_row(self):
        self._insert_undated(1)

        resp = self.client.post("/fixer/1/date", data={"invoice_date": "2024-01-15"})
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.headers.get("Location", "").endswith("/fixer"))

        row = db.get_invoice(1)
        self.assertEqual(row["invoice_date"], "2024-01-15")
        self.assertEqual(row["invoice_date_iso"], "2024-01-15")
        self.assertEqual(db.unresolved_date_invoices(), [])   # gone from the queue

        flashed = self.client.get("/fixer").get_data(as_text=True)
        self.assertIn("Date set to 2024-01-15.", flashed)


if __name__ == "__main__":
    unittest.main()
