# -*- coding: utf-8 -*-
"""Route-level coverage for the Fixer page's review queues: `GET /fixer`'s undated-invoice and
unvendored-invoice panels, the `POST /fixer/<id>/date` handler (`fixer_set_date`), and the
`POST /fixer/<id>/vendor` handler (`fixer_set_vendor`, added in Task 10).

Why this file exists: `fixer_set_date` has four branches (empty-reject, unparseable-reject,
missing-invoice-reject, validate-then-write), and the ordering between validation and the
single write is precisely the guarantee that route exists to provide. No Flask route anywhere
in this codebase had automated coverage before this file, so a future edit that reordered those
checks would regress silently - `python -m unittest discover` would stay green while the
route quietly started writing bad data, or writing before validating.

`fixer_set_vendor` (Task 10) has strictly more of that same shape: reject-missing-vendor_id,
reject-nonexistent-invoice, reject-nonexistent-vendor, and only then two writes (an alias-list
update that is itself conditional/deduped, then the unconditional vendor bind) - so it gets the
same treatment here rather than the "no unit test" call the original Task 5 draft made.

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


class FixerVendorQueuePanel(unittest.TestCase):
    """GET /fixer: the vendor-confirmation panel (Task 10), including the pre-selected
    suggestion that `vendor_match.match()` computes for each queued row."""

    def setUp(self):
        _conn.execute("DELETE FROM invoices")
        _conn.execute("DELETE FROM vendors")
        _conn.commit()
        self.client = app.app.test_client()

    def _insert_unvendored(self, invoice_id, vendor_name="Athens Svcs", property_="Test Property"):
        # invoice_date_iso is deliberately non-blank: a blank one would also queue this row
        # in the *date* panel, and this class only wants to exercise the vendor panel.
        _conn.execute(
            "INSERT INTO invoices (id, vendor_name, property, vendor_id, vendor_needs_review, "
            "invoice_date, invoice_date_iso) VALUES (?, ?, ?, NULL, 1, '06/01/2026', '2026-06-01')",
            (invoice_id, vendor_name, property_),
        )
        _conn.commit()

    def _insert_vendor(self, vendor_id, short_name, canonical_name="", aliases=""):
        _conn.execute(
            "INSERT INTO vendors (id, short_name, canonical_name, aliases) VALUES (?, ?, ?, ?)",
            (vendor_id, short_name, canonical_name, aliases),
        )
        _conn.commit()

    def test_panel_renders_and_lists_unvendored_rows(self):
        self._insert_vendor(1, "Athens", canonical_name="Athens Services")
        self._insert_unvendored(1, vendor_name="Rolling Greens Nursery", property_="Maple Court")
        self._insert_unvendored(2, vendor_name="Beta Electric Co", property_="Oak Ridge")

        resp = self.client.get("/fixer")

        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("Vendors needing confirmation", html)
        self.assertIn("2 invoices", html)
        self.assertIn("Rolling Greens Nursery", html)
        self.assertIn("Beta Electric Co", html)
        self.assertIn('action="/fixer/1/vendor"', html)
        self.assertIn('action="/fixer/2/vendor"', html)

    def test_panel_is_absent_when_the_queue_is_empty(self):
        """Converse of the above: the {% if unvendored %} guard actually guards - same
        rationale as FixerDateQueuePanel's own converse test."""
        resp = self.client.get("/fixer")
        self.assertNotIn("Vendors needing confirmation", resp.get_data(as_text=True))

    def test_panel_preselects_the_matched_suggestion(self):
        self._insert_vendor(1, "Athens", canonical_name="Athens Services",
                            aliases="ATHENS SERVICES; Athens Svcs")
        # "Athen Services" scores 0.9091 against vendor 1 (verified directly against
        # vendor_match.match) - inside the suggest band (SUGGEST_THRESHOLD <= score <
        # BIND_THRESHOLD), so the route should suggest vendor_id 1 without binding it.
        self._insert_unvendored(1, vendor_name="Athen Services")

        html = self.client.get("/fixer").get_data(as_text=True)

        self.assertIn("(suggested)", html)
        self.assertRegex(html, r'<option value="1"\s+selected')


class FixerSetVendorRoute(unittest.TestCase):
    """POST /fixer/<id>/vendor (fixer_set_vendor): reject-before-write ordering, the
    alias-append/dedup side effect, and the stored_file-must-never-change constraint."""

    def setUp(self):
        _conn.execute("DELETE FROM invoices")
        _conn.execute("DELETE FROM vendors")
        _conn.commit()
        self.client = app.app.test_client()

    def _insert_unvendored(self, invoice_id, vendor_name="Athens Svcs", property_="Test Property",
                           stored_file=""):
        _conn.execute(
            "INSERT INTO invoices (id, vendor_name, property, vendor_id, vendor_needs_review, "
            "stored_file) VALUES (?, ?, ?, NULL, 1, ?)",
            (invoice_id, vendor_name, property_, stored_file),
        )
        _conn.commit()

    def _insert_vendor(self, vendor_id, short_name, canonical_name="", aliases=""):
        _conn.execute(
            "INSERT INTO vendors (id, short_name, canonical_name, aliases) VALUES (?, ?, ?, ?)",
            (vendor_id, short_name, canonical_name, aliases),
        )
        _conn.commit()

    def test_non_numeric_or_missing_vendor_id_is_rejected_and_writes_nothing(self):
        self._insert_vendor(1, "Athens", canonical_name="Athens Services")
        self._insert_unvendored(1, vendor_name="Athens Svcs")

        for payload in ({}, {"vendor_id": "abc"}, {"vendor_id": "-1"}):
            with self.subTest(payload=payload):
                resp = self.client.post("/fixer/1/vendor", data=payload)
                self.assertEqual(resp.status_code, 302)
                self.assertTrue(resp.headers.get("Location", "").endswith("/fixer"))

                row = db.get_invoice(1)
                self.assertIsNone(row["vendor_id"])
                self.assertEqual(row["vendor_needs_review"], 1)
                self.assertEqual(db.all_vendors()[0]["aliases"], "")

        flashed = self.client.get("/fixer").get_data(as_text=True)
        self.assertIn("Pick a vendor.", flashed)

    def test_nonexistent_vendor_id_is_rejected_and_writes_nothing(self):
        self._insert_unvendored(1, vendor_name="Rolling Greens Nursery")
        # vendor id 999 was never inserted - no vendors table row exists for it at all.

        resp = self.client.post("/fixer/1/vendor", data={"vendor_id": "999"})
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.headers.get("Location", "").endswith("/fixer"))

        row = db.get_invoice(1)
        self.assertIsNone(row["vendor_id"])
        self.assertEqual(row["vendor_needs_review"], 1)

        flashed = self.client.get("/fixer").get_data(as_text=True)
        self.assertIn("That vendor no longer exists.", flashed)

    def test_missing_invoice_is_rejected_and_writes_nothing(self):
        """Not on the brief's required list, but the same reject-before-write shape as the
        other two branches, and directly parallel to FixerSetDateRoute's own missing-invoice
        case - cheap to lock in alongside them."""
        self._insert_vendor(1, "Athens", canonical_name="Athens Services")
        # id 999 was never inserted as an invoice.

        resp = self.client.post("/fixer/999/vendor", data={"vendor_id": "1"})
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.headers.get("Location", "").endswith("/fixer"))

        self.assertIsNone(db.get_invoice(999))
        self.assertEqual(db.all_vendors()[0]["aliases"], "")   # untouched

        flashed = self.client.get("/fixer").get_data(as_text=True)
        self.assertIn("That invoice no longer exists.", flashed)

    def test_valid_confirmation_binds_vendor_clears_flag_and_appends_alias(self):
        self._insert_vendor(1, "Athens", canonical_name="Athens Services",
                            aliases="ATHENS SERVICES")
        self._insert_unvendored(1, vendor_name="Athens Svcs")

        resp = self.client.post("/fixer/1/vendor", data={"vendor_id": "1"})
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.headers.get("Location", "").endswith("/fixer"))

        row = db.get_invoice(1)
        self.assertEqual(row["vendor_id"], 1)
        self.assertEqual(row["vendor_needs_review"], 0)
        self.assertEqual(row["vendor_name"], "Athens Svcs")   # provenance - never overwritten

        vendor = db.all_vendors()[0]
        self.assertEqual(vendor["aliases"], "ATHENS SERVICES; Athens Svcs")

        flashed = self.client.get("/fixer").get_data(as_text=True)
        self.assertIn("Matched to Athens Services.", flashed)

    def test_confirming_second_invoice_with_known_spelling_does_not_duplicate_alias(self):
        self._insert_vendor(1, "Athens", canonical_name="Athens Services",
                            aliases="ATHENS SERVICES")
        self._insert_unvendored(1, vendor_name="Athens Svcs")
        self._insert_unvendored(2, vendor_name="athens svcs")   # same spelling, different case

        self.client.post("/fixer/1/vendor", data={"vendor_id": "1"})
        after_first = db.all_vendors()[0]["aliases"]
        self.assertEqual(after_first, "ATHENS SERVICES; Athens Svcs")

        resp = self.client.post("/fixer/2/vendor", data={"vendor_id": "1"})
        self.assertEqual(resp.status_code, 302)

        after_second = db.all_vendors()[0]["aliases"]
        self.assertEqual(after_second, after_first)             # no duplicate appended

        row2 = db.get_invoice(2)
        self.assertEqual(row2["vendor_id"], 1)
        self.assertEqual(row2["vendor_needs_review"], 0)        # still resolved either way

    def test_stored_file_is_unchanged_after_confirming_vendor(self):
        self._insert_vendor(1, "Athens", canonical_name="Athens Services")
        self._insert_unvendored(1, vendor_name="Athens Svcs",
                                stored_file="Athens_06_2026.pdf")

        resp = self.client.post("/fixer/1/vendor", data={"vendor_id": "1"})
        self.assertEqual(resp.status_code, 302)

        row = db.get_invoice(1)
        self.assertEqual(row["vendor_id"], 1)                   # sanity: the confirm happened
        self.assertEqual(row["stored_file"], "Athens_06_2026.pdf")


if __name__ == "__main__":
    unittest.main()
