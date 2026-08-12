# -*- coding: utf-8 -*-
"""Route-level coverage for the Fixer page's review queues: `GET /fixer`'s undated-invoice and
unvendored-invoice panels, the `POST /fixer/<id>/date` handler (`fixer_set_date`), and the
`POST /fixer/<id>/vendor` handler (`fixer_set_vendor`, added in Task 10). Also covers
`POST /invoices/<id>/edit` (`edit_invoice`), the Invoices page's own editor, which recomputes
the same two derived fields (`invoice_date_iso`, `vendor_id`) through a different route.

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

`edit_invoice` (`EditInvoiceRoute`, added in the post-review fix wave) earns its place here for
a sharper reason than "no coverage yet": a mutation that deleted its `invoice_date_iso`
recompute previously left the full suite green, because nothing exercised the route at all.
It pins two derive-on-write invariants (date and vendor) plus one deliberate non-invariant -
vendor_id is re-derived only when vendor_name actually changes, so resubmitting the edit form
unchanged can never silently re-open a binding a human already confirmed.

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


class EditInvoiceRoute(unittest.TestCase):
    """POST /invoices/<id>/edit (edit_invoice): the two derived-field invariants the route
    is responsible for keeping in sync with their editable source field - invoice_date_iso
    must follow invoice_date, and vendor_id must follow vendor_name - plus the deliberate
    exception that vendor_id is re-derived only when vendor_name actually *changes*, so a
    human-confirmed binding is never silently re-opened by re-saving the same form.

    export_amount_sidecars() is patched to a no-op for every test in this class. edit_invoice
    calls it unconditionally after every save, and it globs config.PROCESSED (a real directory
    on disk, per core/db.py) even when no row ends up written - a checkout where that directory
    holds real sidecar files would have them rewritten as a side effect of running this suite.
    None of the invariants pinned here depend on it running at all.
    """

    def setUp(self):
        _conn.execute("DELETE FROM invoices")
        _conn.execute("DELETE FROM vendors")
        _conn.commit()
        self.client = app.app.test_client()
        sidecar_patcher = mock.patch.object(db, "export_amount_sidecars")
        sidecar_patcher.start()
        self.addCleanup(sidecar_patcher.stop)

    def _insert_invoice(self, invoice_id, vendor_name="Test Vendor", vendor_id=None,
                        vendor_needs_review=0, invoice_date="06/01/2026",
                        invoice_date_iso="2026-06-01", property_="Test Property"):
        _conn.execute(
            "INSERT INTO invoices (id, vendor_name, vendor_id, vendor_needs_review, "
            "property, invoice_date, invoice_date_iso) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (invoice_id, vendor_name, vendor_id, vendor_needs_review, property_,
             invoice_date, invoice_date_iso),
        )
        _conn.commit()

    def _insert_vendor(self, vendor_id, short_name, canonical_name="", aliases=""):
        _conn.execute(
            "INSERT INTO vendors (id, short_name, canonical_name, aliases) VALUES (?, ?, ?, ?)",
            (vendor_id, short_name, canonical_name, aliases),
        )
        _conn.commit()

    def test_editing_to_a_parseable_date_updates_invoice_date_iso(self):
        self._insert_invoice(1, invoice_date="05/01/2026", invoice_date_iso="2026-05-01")

        resp = self.client.post("/invoices/1/edit", data={"invoice_date": "06/15/2026"})
        self.assertEqual(resp.status_code, 302)

        row = db.get_invoice(1)
        self.assertEqual(row["invoice_date"], "06/15/2026")
        self.assertEqual(row["invoice_date_iso"], "2026-06-15")

    def test_editing_to_unparseable_text_clears_iso_and_requeues_the_row(self):
        self._insert_invoice(1, invoice_date="06/01/2026", invoice_date_iso="2026-06-01")

        resp = self.client.post("/invoices/1/edit", data={"invoice_date": "banana"})
        self.assertEqual(resp.status_code, 302)

        row = db.get_invoice(1)
        self.assertEqual(row["invoice_date"], "banana")
        self.assertEqual(row["invoice_date_iso"], "")
        self.assertIn(1, [r["id"] for r in db.unresolved_date_invoices()])

    def test_editing_vendor_name_to_a_different_known_vendor_rederives_vendor_id(self):
        self._insert_vendor(1, "Athens", canonical_name="Athens Services")
        self._insert_vendor(2, "Beta", canonical_name="Beta Electric Co")
        self._insert_invoice(1, vendor_name="Athens Services", vendor_id=1,
                             vendor_needs_review=0)

        # Exact canonical-name match against vendor 2 - binds at score 1.0, no ambiguity.
        resp = self.client.post("/invoices/1/edit", data={"vendor_name": "Beta Electric Co"})
        self.assertEqual(resp.status_code, 302)

        row = db.get_invoice(1)
        self.assertEqual(row["vendor_name"], "Beta Electric Co")
        self.assertEqual(row["vendor_id"], 2)
        self.assertEqual(row["vendor_needs_review"], 0)

    def test_editing_vendor_name_to_something_unmatchable_flags_for_review(self):
        self._insert_vendor(1, "Athens", canonical_name="Athens Services")
        self._insert_invoice(1, vendor_name="Athens Services", vendor_id=1,
                             vendor_needs_review=0)

        # Verified against vendor_match.match directly: scores 0.30 against this vendor
        # list, well under SUGGEST_THRESHOLD - an unambiguous "new".
        resp = self.client.post(
            "/invoices/1/edit", data={"vendor_name": "Zzxqvorp Unrelated Holdings"})
        self.assertEqual(resp.status_code, 302)

        row = db.get_invoice(1)
        self.assertEqual(row["vendor_needs_review"], 1)
        self.assertIn(1, [r["id"] for r in db.vendor_review_invoices()])

    def test_unchanged_vendor_name_does_not_disturb_an_existing_binding(self):
        self._insert_vendor(1, "Athens", canonical_name="Athens Services",
                            aliases="Athens Svcs")
        self._insert_vendor(2, "Beta", canonical_name="Beta Electric Co")
        # vendor_id deliberately points at vendor 2, NOT the vendor "Athens Svcs" would
        # itself match (vendor 1, via an exact alias hit - confirmed directly against
        # vendor_match.match). If the route ever re-derived on a no-op resubmit, this row
        # would flip to vendor_id 1; the whole point of the change-guard is that it must not.
        self._insert_invoice(1, vendor_name="Athens Svcs", vendor_id=2,
                             vendor_needs_review=0)

        resp = self.client.post(
            "/invoices/1/edit",
            data={"vendor_name": "Athens Svcs", "invoice_number": "A-999"})
        self.assertEqual(resp.status_code, 302)

        row = db.get_invoice(1)
        self.assertEqual(row["invoice_number"], "A-999")   # sanity: the edit did apply
        self.assertEqual(row["vendor_id"], 2)
        self.assertEqual(row["vendor_needs_review"], 0)


class MonthPage(unittest.TestCase):
    """The Month page groups instances by property and marks the late ones."""

    def setUp(self):
        _conn.execute("DELETE FROM obligation_instance")
        _conn.execute("DELETE FROM obligation")
        _conn.execute("DELETE FROM properties")
        # Invoices and vendors too: /month/open runs expectations.sync(), so any invoice
        # history left behind by another test in this class becomes an extra learned
        # obligation and an extra instance. Two tests here seed invoices deliberately, and
        # without this the ones that count instances depend on alphabetical test order.
        _conn.execute("DELETE FROM invoices")
        _conn.execute("DELETE FROM vendors")
        _conn.execute("INSERT INTO properties (id, canonical_name) VALUES (1, 'Kenmore Plaza')")
        self.client = app.app.test_client()

    def test_empty_period_says_so_rather_than_rendering_blank(self):
        # A blank page reads as "nothing is missing", which is the opposite of the truth
        # when the ledger has simply never been populated.
        html = self.client.get("/month?period=August+2026").get_data(as_text=True)
        self.assertIn("Nothing scheduled", html)

    def test_lists_an_instance_under_its_property(self):
        from core import ledger
        ledger.add_obligation(kind="ACTION", title="Call Michelle", property_id=1,
                              window_rule="day:12", cadence="monthly")
        ledger.open_period("August 2026")
        html = self.client.get("/month?period=August+2026").get_data(as_text=True)
        self.assertIn("Call Michelle", html)
        self.assertIn("Kenmore Plaza", html)

    def test_open_period_creates_instances_and_redirects(self):
        from core import ledger
        ledger.add_obligation(kind="ACTION", title="Rent posting", window_rule="last-week",
                              cadence="monthly")
        resp = self.client.post("/month/open", data={"period": "August 2026"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(len(ledger.instances_for_period("August 2026")), 1)

    def test_open_period_twice_does_not_duplicate(self):
        from core import ledger
        ledger.add_obligation(kind="ACTION", title="Rent posting", window_rule="last-week",
                              cadence="monthly")
        self.client.post("/month/open", data={"period": "August 2026"})
        self.client.post("/month/open", data={"period": "August 2026"})
        self.assertEqual(len(ledger.instances_for_period("August 2026")), 1)

    def test_a_bad_period_is_rejected_rather_than_crashing(self):
        resp = self.client.get("/month?period=not-a-month", follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Could not read", resp.get_data(as_text=True))

    def test_opening_a_period_also_learns_expectations(self):
        # Without this, sync() is built and tested but never runs in the app, and no
        # expectation is ever learned no matter how much history accumulates.
        from core import ledger
        _conn.execute("DELETE FROM invoices")
        _conn.execute("DELETE FROM vendors")
        _conn.execute("INSERT INTO vendors (id, short_name) VALUES (7, 'Athens')")
        for iso in ("2026-06-05", "2026-07-05"):
            _conn.execute(
                "INSERT INTO invoices (property, vendor_id, invoice_date, invoice_date_iso) "
                "VALUES ('Kenmore Plaza', 7, ?, ?)", (iso, iso))

        self.client.post("/month/open", data={"period": "August 2026"})
        kinds = [o["kind"] for o in ledger.active_obligations()]
        self.assertIn("EXPECT", kinds)
        self.assertEqual(len(ledger.instances_for_period("August 2026")), 1)

    def test_an_overdue_row_is_marked_possibly_missing(self):
        # is_missing is covered thoroughly as a function in test_ledger, but nothing
        # checked that this page CALLS it or renders what it returns - the seam between
        # the two, rather than either side. Suppressing the flag entirely leaves every
        # other test on this page green.
        #
        # day:1 is due 2026-08-01 and this is an ACTION, so it is overdue for any today
        # after that date and the case cannot go stale.
        from core import ledger
        ledger.add_obligation(kind="ACTION", title="Rent posting", property_id=1,
                              window_rule="day:1", cadence="monthly")
        ledger.open_period("August 2026")
        html = self.client.get("/month?period=August+2026").get_data(as_text=True)
        # Two separate renderings of the same flag: the row's own status cell and the
        # header tally. Asserting the bare phrase would not distinguish them, because the
        # header reads "1 possibly missing" and would satisfy it on its own.
        self.assertIn("<strong>possibly missing</strong>", html)
        self.assertIn("1 possibly missing", html)
        self.assertNotIn("nothing overdue", html)

    def test_a_get_satisfies_an_expectation_whose_invoice_has_arrived(self):
        # The route writes on GET deliberately, so an invoice processed since you last
        # looked shows as arrived without pressing anything. That is the justification in
        # month_page's docstring, and nothing exercised it through the route - removing the
        # satisfy_period call left the whole suite green.
        from core import ledger
        _conn.execute("DELETE FROM invoices")
        _conn.execute("DELETE FROM vendors")
        _conn.execute("INSERT INTO vendors (id, short_name) VALUES (7, 'Athens')")
        for iso in ("2026-06-05", "2026-07-05", "2026-08-05"):
            _conn.execute(
                "INSERT INTO invoices (property, vendor_id, invoice_date, invoice_date_iso) "
                "VALUES ('Kenmore Plaza', 7, ?, ?)", (iso, iso))

        self.client.post("/month/open", data={"period": "August 2026"})
        self.assertEqual(
            ledger.instances_for_period("August 2026")[0]["state"], "open")

        self.client.get("/month?period=August+2026")
        after = ledger.instances_for_period("August 2026")[0]
        self.assertEqual(after["state"], "done")
        self.assertTrue(after["satisfied_by"].startswith("invoice:"))


class InstanceActions(unittest.TestCase):
    def setUp(self):
        _conn.execute("DELETE FROM obligation_instance")
        _conn.execute("DELETE FROM obligation")
        self.client = app.app.test_client()
        from core import ledger
        ledger.add_obligation(kind="ACTION", title="Call Michelle",
                              window_rule="day:12", cadence="monthly")
        ledger.open_period("August 2026")
        self.inst = ledger.instances_for_period("August 2026")[0]

    def test_a_closed_row_stops_counting_towards_the_missing_tally(self):
        # The other direction of the missing flag. Asserting only that an overdue row says
        # "possibly missing" leaves a page that marks EVERYTHING missing looking correct,
        # so this pins that a closed row is not counted. It reads the header tally rather
        # than the row, because the row's status cell shows "done" ahead of the missing
        # branch and would hide the difference. Date-independent: is_missing returns False
        # on state alone, whatever today is.
        self.client.post(f"/month/instance/{self.inst['id']}/done")
        html = self.client.get("/month?period=August+2026").get_data(as_text=True)
        self.assertIn("nothing overdue", html)
        self.assertNotIn("possibly missing", html)

    def test_done_marks_the_instance_and_stamps_the_date(self):
        from core import ledger
        self.client.post(f"/month/instance/{self.inst['id']}/done")
        after = ledger.instances_for_period("August 2026")[0]
        self.assertEqual(after["state"], "done")
        self.assertTrue(after["done_at"])

    def test_skip_records_the_reason(self):
        from core import ledger
        self.client.post(f"/month/instance/{self.inst['id']}/skip",
                         data={"note": "LADWP skips odd months"})
        after = ledger.instances_for_period("August 2026")[0]
        self.assertEqual(after["state"], "skipped")
        self.assertEqual(after["note"], "LADWP skips odd months")

    def test_skip_without_a_reason_is_rejected_and_writes_nothing(self):
        # A dismissal with no reason is indistinguishable from a mis-click six months later.
        from core import ledger
        self.client.post(f"/month/instance/{self.inst['id']}/skip", data={"note": "  "})
        after = ledger.instances_for_period("August 2026")[0]
        self.assertEqual(after["state"], "open")

    def test_acting_on_a_missing_instance_is_rejected_and_writes_nothing(self):
        # "Writes nothing" is the weaker half of this: UPDATE ... WHERE id=9999 matches no
        # rows whatever the lookup returned, so SQLite gives that for free even if the
        # lookup fabricated a row. The flash is the only thing that distinguishes
        # "rejected" from "silently did nothing", so it is asserted too.
        from core import ledger
        resp = self.client.post("/month/instance/9999/done")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(len(ledger.instances_for_period("August 2026")), 1)
        self.assertIn("no longer exists",
                      self.client.get("/month?period=August+2026").get_data(as_text=True))


class AddReminder(unittest.TestCase):
    def setUp(self):
        _conn.execute("DELETE FROM obligation_instance")
        _conn.execute("DELETE FROM obligation")
        _conn.execute("DELETE FROM properties")
        _conn.execute("INSERT INTO properties (id, canonical_name) VALUES (1, 'Kenmore Plaza')")
        self.client = app.app.test_client()

    def test_a_one_off_reminder_lands_in_one_month_only(self):
        from core import ledger
        self.client.post("/month/reminder", data={
            "title": "Check the Namwoo distribution", "kind": "once",
            "on_date": "2026-08-18", "period": "August 2026"})
        self.assertEqual(len(ledger.instances_for_period("August 2026")), 1)
        ledger.open_period("September 2026")
        self.assertEqual(len(ledger.instances_for_period("September 2026")), 0)
        # The instance counts above are governed entirely by the date: window rule -
        # resolve_window returns None outside August - so they hold whether cadence was
        # stored as "once" or "monthly". applies_to_period returns True for both. Pin the
        # stored value directly, because Task 12 lets a human edit cadence and needs a
        # correct starting point to edit from.
        ob = ledger.active_obligations()[0]
        self.assertEqual(ob["cadence"], "once")
        self.assertEqual(ob["window_rule"], "date:2026-08-18")

    def test_a_recurring_reminder_appears_in_later_months_too(self):
        from core import ledger
        self.client.post("/month/reminder", data={
            "title": "Post next month's rent", "kind": "monthly",
            "window_rule": "last-week", "period": "August 2026"})
        ledger.open_period("September 2026")
        self.assertEqual(len(ledger.instances_for_period("September 2026")), 1)

    def test_a_reminder_can_be_attached_to_a_property(self):
        from core import ledger
        self.client.post("/month/reminder", data={
            "title": "Ask James for invoices", "kind": "monthly",
            "window_rule": "day:1", "property_id": "1", "period": "August 2026"})
        ob = ledger.active_obligations()[0]
        self.assertEqual(ob["property_id"], 1)

    def test_an_empty_title_is_rejected_and_writes_nothing(self):
        from core import ledger
        self.client.post("/month/reminder", data={
            "title": "   ", "kind": "monthly", "window_rule": "day:1",
            "period": "August 2026"})
        self.assertEqual(ledger.active_obligations(), [])

    def test_a_one_off_without_a_date_is_rejected_and_writes_nothing(self):
        from core import ledger
        self.client.post("/month/reminder", data={
            "title": "x", "kind": "once", "on_date": "", "period": "August 2026"})
        self.assertEqual(ledger.active_obligations(), [])

    def test_an_unparseable_window_rule_is_rejected_and_writes_nothing(self):
        from core import ledger
        self.client.post("/month/reminder", data={
            "title": "x", "kind": "monthly", "window_rule": "phase-of-moon",
            "period": "August 2026"})
        self.assertEqual(ledger.active_obligations(), [])

    def test_a_new_reminder_is_visible_in_the_current_period_immediately(self):
        # Adding something and not seeing it would read as the save having failed.
        #
        # Assert the rendered ROW, not the bare title. The route also flashes
        # "Added: Call Michelle", and base.html echoes flashes on the next GET - so a bare
        # assertIn("Call Michelle") passes even when open_period is skipped and the
        # reminder is genuinely absent from the table. The flash proves the POST was
        # accepted; only the <td> proves the reminder is on the page.
        self.client.post("/month/reminder", data={
            "title": "Call Michelle", "kind": "monthly", "window_rule": "day:12",
            "period": "August 2026"})
        html = self.client.get("/month?period=August+2026").get_data(as_text=True)
        self.assertIn("<td>Call Michelle</td>", html)

    def test_an_inverted_range_is_rejected_and_writes_nothing(self):
        # day:30-1 parses at both ends, so it passes every check that exists and yields
        # due_from 2026-08-30 with due_to 2026-08-01 - a window no BETWEEN can match, so
        # the reminder is stored, scheduled, and permanently invisible. Carried from
        # Task 2's review; the guard belongs on resolve_window, which is what this route
        # validates through.
        from core import ledger
        self.client.post("/month/reminder", data={
            "title": "x", "kind": "monthly", "window_rule": "day:30-1",
            "period": "August 2026"})
        self.assertEqual(ledger.active_obligations(), [])

    def test_the_property_dropdown_shows_real_names(self):
        # all_properties() returns "name", not "canonical_name", and Jinja renders an
        # unknown attribute as the empty string instead of raising - so the wrong spelling
        # is a dropdown of blank options with no error anywhere and every other test on
        # this page still green.
        html = self.client.get("/month?period=August+2026").get_data(as_text=True)
        self.assertIn('<option value="1">Kenmore Plaza</option>', html)


if __name__ == "__main__":
    unittest.main()
