# Data Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the invoice database two things the workflow dashboard cannot be built without — a parsed date column with an explicit failure queue, and real vendor identities with a human verification loop.

**Architecture:** Two independent-but-sequenced layers over the existing SQLite schema. First a column-migration helper (none exists today), then a hardened date parser writing `invoices.invoice_date_iso`, then a vendor identity layer (`vendors.canonical_name`/`active`, `invoices.vendor_id`) with a five-outcome match pipeline whose ambiguous results queue onto the existing Fixer page as new tabs. Nothing in the existing extraction, staging, assembly, or reconciliation paths changes behaviour.

**Tech Stack:** Python 3.10+, Flask, SQLite (stdlib `sqlite3`), `unittest`, Jinja2. No new dependencies.

**Source spec:** `docs/superpowers/specs/2026-08-06-workflow-dashboard-design.md` — this plan implements Phases 0 and 1 of §13.

## Global Constraints

- Python 3.10 or newer. Developed on 3.14.
- No new third-party dependencies. `requirements.txt` is not modified by this plan.
- Tests live in `tests/`, use `unittest`, and must run with **no database, no network, and no filesystem writes**. Run from the project root with `python -m unittest discover -s tests -t .`
- Existing test suite is 26 cases and must stay green after every task.
- `invoices.vendor_name` (the raw string Claude extracted) is **never overwritten**. It is provenance and it is what makes a bad merge reversible.
- `invoices.stored_file` is **never rewritten by any vendor operation** in this plan. It is the sidecar/assembler join key (`core/db.py:45`), is indexed, and copies already exist under `data/Bank Rec/<month>/`. Only property reassignment may rename a filed PDF, and that path already exists.
- All new DB columns are added by the migration helper from Task 1, never by editing `_SCHEMA` alone — `_SCHEMA` uses `CREATE TABLE IF NOT EXISTS`, which does not alter existing tables.
- Every timing decision anywhere in the system reads `invoice_date_iso`. Nothing re-parses `invoice_date` free text at read time, and nothing keys a due window off `date_processed`.
- Ambiguous `NN-NN-YYYY` input resolves as **MM-DD-YYYY** (US convention). This is a decision, and Task 2 asserts it.
- Commit after every task. Conventional-commit prefixes (`feat:`, `fix:`, `test:`, `refactor:`).

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `core/db.py` | SQLite layer. Gains `_ensure_columns()` migration helper and the new column definitions; gains vendor and date-queue queries. | Modify |
| `core/dates.py` | **New.** All invoice-date parsing and normalization. One responsibility, pure functions, no imports from `db` or `state`. | Create |
| `core/vendor_match.py` | **New.** Vendor identity: normalization, the five-outcome match pipeline, and the bootstrap clusterer. Pure functions; takes a vendor list, returns decisions. | Create |
| `core/processor.py` | Extraction and filing. Gains `invoice_date_iso` and `vendor_id` population at insert time. | Modify |
| `app.py` | Flask routes. Gains two Fixer tabs and their POST handlers; populates `invoice_date_iso` on edit. | Modify |
| `templates/fixer.html` | Needs Review page. Gains tab navigation and two new panels. | Modify |
| `scripts/backfill_dates.py` | **New.** One-shot backfill of `invoice_date_iso` over existing rows. | Create |
| `scripts/bootstrap_vendors.py` | **New.** One-shot vendor clustering + interactive confirmation. | Create |
| `tests/test_dates.py` | **New.** Date parsing and normalization. | Create |
| `tests/test_vendor_match.py` | **New.** Match pipeline and clustering. | Create |

`core/dates.py` and `core/vendor_match.py` are new files rather than additions to `core/processor.py` because that file is already 1136 lines with several responsibilities. Both new modules are pure and independently testable, which is what makes their tests runnable without a database.

---

## Task 1: Column migration helper

**Files:**
- Modify: `core/db.py` (add `_ADDED_COLUMNS` and `_ensure_columns()` near `_SCHEMA` at line 53; call from `init()` at line 114)
- Test: `tests/test_migration.py` (create)

**Interfaces:**
- Consumes: nothing.
- Produces: `db._ensure_columns(conn) -> None` — idempotently `ALTER TABLE ... ADD COLUMN` for every entry in `db._ADDED_COLUMNS`, which is a list of `(table: str, column: str, ddl: str)` tuples. Called from `db.init()`. Every later task adds its columns by appending to `_ADDED_COLUMNS`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_migration.py`:

```python
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


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_migration -v`
Expected: FAIL with `AttributeError: module 'core.db' has no attribute '_ensure_columns'`

- [ ] **Step 3: Write minimal implementation**

In `core/db.py`, immediately after the `_SCHEMA` string (which ends at line 92), add:

```python
# Columns added after the original schema shipped. `_SCHEMA` uses CREATE TABLE IF NOT
# EXISTS, which does nothing to a table that already exists - so every column added
# later must go through _ensure_columns() instead. Append here; never edit _SCHEMA
# alone for a column that existing databases won't have.
_ADDED_COLUMNS = [
    # (table, column, column DDL)
]


def _ensure_columns(conn, spec=None) -> None:
    """Add any column in `spec` that the table doesn't already have. Idempotent, and
    safe to call on every startup. A table that doesn't exist yet is skipped - the
    CREATE in _SCHEMA will include the column, so there is nothing to add."""
    for table, column, ddl in (_ADDED_COLUMNS if spec is None else spec):
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if not existing:                       # table absent: nothing to migrate
            continue
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
```

Then change `init()` (line 114) to call it:

```python
def init() -> None:
    """Create tables/indexes if absent, then add any columns introduced later. Idempotent."""
    with _connect() as conn:
        conn.executescript(_SCHEMA)
        _ensure_columns(conn)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m unittest tests.test_migration -v`
Expected: PASS, 3 tests

Run: `python -m unittest discover -s tests -t .`
Expected: PASS, 29 tests (26 existing + 3 new)

- [ ] **Step 5: Commit**

```bash
git add core/db.py tests/test_migration.py
git commit -m "feat: add idempotent column-migration helper to db layer"
```

---

## Task 2: Date parser hardening

**Files:**
- Create: `core/dates.py`
- Test: `tests/test_dates.py` (create)

**Interfaces:**
- Consumes: `processor._DATE_FORMATS` is *not* consumed — `core/dates.py` owns its own format list so it has no import cycle with `processor`.
- Produces:
  - `dates.parse_invoice_date(raw: str | None) -> datetime.date | None`
  - `dates.to_iso(raw: str | None) -> str` — returns `"YYYY-MM-DD"`, or `""` when unparseable. This is the only function callers use to populate `invoice_date_iso`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_dates.py`. Every shape below is present in the live database; the two
marked `# currently fails` are the two rows the existing parser cannot read.

```python
# -*- coding: utf-8 -*-
"""Invoice-date parsing. This is the highest-risk pure function in the system: every
timing decision reads its output, and a silent failure is indistinguishable from a
vendor who skipped a month.

Run from the project root:

    python -m unittest discover -s tests -t . -v
"""
import datetime
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import dates


class ParseInvoiceDate(unittest.TestCase):
    def test_shapes_present_in_live_data(self):
        cases = {
            "06/01/2026":          datetime.date(2026, 6, 1),
            "5/31/2026":           datetime.date(2026, 5, 31),
            "6/11/26":             datetime.date(2026, 6, 11),
            "06-15-2026":          datetime.date(2026, 6, 15),
            "06-24-26":            datetime.date(2026, 6, 24),
            "2026-05-22 00:00:00": datetime.date(2026, 5, 22),
            "2026-06-03":          datetime.date(2026, 6, 3),
            "Jun 2, 2026":         datetime.date(2026, 6, 2),
            "Jun/01/26":           datetime.date(2026, 6, 1),
            "13-Jul-26":           datetime.date(2026, 7, 13),   # currently fails
            "06262026":            datetime.date(2026, 6, 26),   # currently fails
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(dates.parse_invoice_date(raw), expected)

    def test_ambiguous_two_number_dates_resolve_us_style(self):
        # 07-01-2026 is MM-DD-YYYY or DD-MM-YYYY depending on convention. These are US
        # invoices, so it is July 1st. This is a DECISION, asserted so it cannot drift.
        self.assertEqual(dates.parse_invoice_date("07-01-2026"), datetime.date(2026, 7, 1))
        self.assertEqual(dates.parse_invoice_date("08-01-2026"), datetime.date(2026, 8, 1))
        self.assertEqual(dates.parse_invoice_date("01/02/2026"), datetime.date(2026, 1, 2))

    def test_whitespace_is_tolerated(self):
        self.assertEqual(dates.parse_invoice_date("  06/01/2026 "), datetime.date(2026, 6, 1))

    def test_unparseable_returns_none_not_a_guess(self):
        for raw in ("", "   ", None, "not a date", "13/45/2026", "0", "N/A"):
            with self.subTest(raw=raw):
                self.assertIsNone(dates.parse_invoice_date(raw))


class ToIso(unittest.TestCase):
    def test_formats_as_iso(self):
        self.assertEqual(dates.to_iso("06/01/2026"), "2026-06-01")
        self.assertEqual(dates.to_iso("13-Jul-26"), "2026-07-13")

    def test_failure_is_empty_string_never_none(self):
        # Empty string, not None: it goes straight into a TEXT NOT NULL-ish column and
        # the review queue selects on COALESCE(invoice_date_iso,'') = ''.
        self.assertEqual(dates.to_iso("not a date"), "")
        self.assertEqual(dates.to_iso(None), "")
        self.assertEqual(dates.to_iso(""), "")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_dates -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'core.dates'`

- [ ] **Step 3: Write minimal implementation**

Create `core/dates.py`:

```python
# -*- coding: utf-8 -*-
"""Invoice-date parsing and normalization.

Every timing decision in the app reads `invoices.invoice_date_iso`, which is produced
here. Nothing re-parses the raw `invoice_date` text at read time.

Two rules this module exists to make explicit:

1. Failure is visible. `parse_invoice_date` returns None and `to_iso` returns "", and
   the caller routes the row to the date review queue. A row that cannot be parsed must
   never be silently dropped - a dropped row looks exactly like a vendor who skipped a
   month, which is the signal the whole system exists to detect.

2. Ambiguous NN-NN-YYYY input is MM-DD-YYYY. These are US invoices. Note there is
   deliberately no %d/%m/... format in the list below, so the preference is structural
   rather than a matter of ordering luck.
"""
import datetime
import re
from typing import Optional

# Ordered most-specific first. Formats with separators are tried before the bare-digit
# form so "06262026" is the only thing that can reach %m%d%Y.
_FORMATS = (
    "%m/%d/%Y", "%m-%d-%Y", "%m.%d.%Y",
    "%Y-%m-%d", "%Y/%m/%d",
    "%m/%d/%y", "%m-%d-%y",
    "%B %d, %Y", "%b %d, %Y",
    "%B %d %Y", "%b %d %Y",
    "%d %B %Y", "%d %b %Y",
    "%d-%b-%Y", "%d-%B-%Y",
    "%d-%b-%y", "%d-%B-%y",          # 13-Jul-26
    "%b/%d/%Y", "%B/%d/%Y",
    "%b/%d/%y", "%B/%d/%y",          # Jun/01/26
    "%m%d%Y",                        # 06262026 - bare digits, tried last
)

# Values that mean "no date" even when they are non-empty text.
_MISSING = frozenset({
    "", "null", "none", "nil", "n/a", "na", "n.a.", "-", "--",
    "not available", "not found", "not provided", "unknown", "tbd",
})


def parse_invoice_date(raw: Optional[str]) -> Optional[datetime.date]:
    """Parse an invoice date to a `date`, or None if it cannot be read.

    Returning None is a real outcome, not an error to swallow: the caller sends the row
    to the date review queue so a human can resolve it.
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if text.lower() in _MISSING:
        return None
    # Excel datetime cells stringify with a time part; drop it before matching.
    text = re.sub(r"\s+\d{1,2}:\d{2}(:\d{2})?(\.\d+)?$", "", text).strip()
    if not text:
        return None
    for fmt in _FORMATS:
        try:
            return datetime.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def to_iso(raw: Optional[str]) -> str:
    """Normalize an invoice date to 'YYYY-MM-DD', or '' when it cannot be parsed.

    Empty string rather than None so it drops straight into a TEXT column and the
    review-queue query can select on COALESCE(invoice_date_iso, '') = ''.
    """
    parsed = parse_invoice_date(raw)
    return parsed.isoformat() if parsed else ""
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m unittest tests.test_dates -v`
Expected: PASS, 6 tests

Run: `python -m unittest discover -s tests -t .`
Expected: PASS, 35 tests

- [ ] **Step 5: Verify against the live database**

This is a read-only spot check that the parser handles real data, not just fixtures.

Run:

```bash
python -c "
import sqlite3
from core import dates
rows = [r[0] for r in sqlite3.connect('data/invoices.db').execute('select invoice_date from invoices')]
bad = [r for r in rows if not dates.to_iso(r)]
print(f'{len(rows)} rows, {len(bad)} unparseable')
for b in sorted(set(str(x) for x in bad)): print('   ', repr(b))
"
```

Expected: `265 rows, 2 unparseable` and the two values printed are both empty/blank. The
two previously-failing shapes (`06262026`, `13-Jul-26`) must **not** appear. If any
non-empty value appears, add its format to `_FORMATS` and add it to the Task 2 test before
continuing.

- [ ] **Step 6: Commit**

```bash
git add core/dates.py tests/test_dates.py
git commit -m "feat: add invoice date normalization with explicit parse failures"
```

---

## Task 3: `invoice_date_iso` column and backfill

**Files:**
- Modify: `core/db.py` (`_ADDED_COLUMNS`; add `invoice_date_iso` to `_COLUMNS` at line ~44; add `unresolved_date_invoices()` and `set_invoice_date()`)
- Create: `scripts/backfill_dates.py`
- Test: `tests/test_migration.py` (extend)

**Interfaces:**
- Consumes: `db._ensure_columns` (Task 1), `dates.to_iso` (Task 2).
- Produces:
  - `invoices.invoice_date_iso TEXT DEFAULT ''`
  - `db.unresolved_date_invoices() -> list[dict]` — rows where `COALESCE(invoice_date_iso,'') = ''`, newest first.
  - `db.set_invoice_date(invoice_id: int, raw: str, iso: str) -> None` — writes both columns together so they can never disagree.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_migration.py`, inside the file but after the existing `EnsureColumns`
class:

```python
class AddedColumnsRegistry(unittest.TestCase):
    def test_invoice_date_iso_is_registered(self):
        self.assertIn(
            ("invoices", "invoice_date_iso", "TEXT DEFAULT ''"),
            db._ADDED_COLUMNS,
        )

    def test_invoice_date_iso_is_in_the_column_list(self):
        # _COLUMNS drives insert/update field ordering; a column missing from it is
        # silently never written.
        self.assertIn("invoice_date_iso", db._COLUMNS)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_migration -v`
Expected: FAIL — `("invoices", "invoice_date_iso", ...) not found in []`

- [ ] **Step 3: Write minimal implementation**

In `core/db.py`, add the column to `_ADDED_COLUMNS`:

```python
_ADDED_COLUMNS = [
    ("invoices", "invoice_date_iso", "TEXT DEFAULT ''"),
]
```

Add `"invoice_date_iso"` to the `_COLUMNS` list (the list beginning around line 30 that ends
with `"origin"`), immediately after `"invoice_date"`:

```python
    "invoice_date",
    "invoice_date_iso",  # 'YYYY-MM-DD' parsed from invoice_date; '' when unparseable
```

Add the column to `_SCHEMA`'s `invoices` CREATE (so fresh databases get it directly),
immediately after the `invoice_date` line:

```sql
    invoice_date     TEXT DEFAULT '',
    invoice_date_iso TEXT DEFAULT '',
```

Then add the two query functions near `review_invoices()` (line 384):

```python
def unresolved_date_invoices() -> list[dict]:
    """Invoices whose date could not be parsed. These are the date review queue - they
    are NOT dropped, because a dropped row is indistinguishable from a skipped month."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM invoices WHERE COALESCE(invoice_date_iso,'') = '' "
            "ORDER BY id DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def set_invoice_date(invoice_id: int, raw: str, iso: str) -> None:
    """Write the raw date and its parsed form together, so the two can never disagree."""
    with _connect() as conn:
        conn.execute(
            "UPDATE invoices SET invoice_date = ?, invoice_date_iso = ? WHERE id = ?",
            (raw, iso, invoice_id),
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m unittest discover -s tests -t .`
Expected: PASS, 37 tests

- [ ] **Step 5: Write the backfill script**

Create `scripts/backfill_dates.py`:

```python
# -*- coding: utf-8 -*-
"""One-shot backfill of invoices.invoice_date_iso from the raw invoice_date text.

Dry run by default - prints what it would change and writes nothing:

    python scripts/backfill_dates.py

Apply:

    python scripts/backfill_dates.py --apply

Safe to re-run. Rows that already have an invoice_date_iso are left alone.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import dates, db


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    args = ap.parse_args()

    db.init()
    rows = db.list_invoices()
    filled = skipped = failed = 0
    failures = []

    for row in rows:
        if (row.get("invoice_date_iso") or "").strip():
            skipped += 1
            continue
        iso = dates.to_iso(row.get("invoice_date"))
        if not iso:
            failed += 1
            failures.append((row["id"], row.get("invoice_date"), row.get("vendor_name")))
            continue
        if args.apply:
            db.set_invoice_date(row["id"], row.get("invoice_date") or "", iso)
        filled += 1

    verb = "filled" if args.apply else "would fill"
    print(f"{len(rows)} invoices: {verb} {filled}, already set {skipped}, unparseable {failed}")
    if failures:
        print("\nUnparseable - these go to the date review queue:")
        for inv_id, raw, vendor in failures:
            print(f"   id={inv_id:<5} {str(raw)!r:<24} {vendor}")
    if not args.apply:
        print("\nDry run. Re-run with --apply to write.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 6: Run the backfill, dry first**

Run: `python scripts/backfill_dates.py`
Expected: `265 invoices: would fill 263, already set 0, unparseable 2`, and the two
unparseable rows are the blank ones.

Then run: `python scripts/backfill_dates.py --apply`
Expected: `265 invoices: filled 263, already set 0, unparseable 2`

Verify it is idempotent — run: `python scripts/backfill_dates.py --apply`
Expected: `265 invoices: filled 0, already set 263, unparseable 2`

- [ ] **Step 7: Commit**

```bash
git add core/db.py scripts/backfill_dates.py tests/test_migration.py
git commit -m "feat: add invoice_date_iso column with idempotent backfill"
```

---

## Task 4: Populate `invoice_date_iso` on every write path

**Files:**
- Modify: `core/processor.py` (extract a pure record builder out of `write_invoice`, line 570–618)
- Modify: `app.py` (`edit_invoice`, line 322)
- Test: `tests/test_dates.py` (extend)

**Interfaces:**
- Consumes: `dates.to_iso` (Task 2).
- Produces: `processor.build_invoice_record(data: dict, source_file: str, status: str,
  date_processed: str, needs_review: bool = False, stored_file: str = "") -> dict` — assembles
  the row dict that `db.insert_invoice` consumes. Pure: touches no database and no filesystem.
  Task 9 adds the vendor fields to this same function.

**Why the extraction:** `write_invoice` (line 570) currently computes the row and calls
`db.insert_invoice` in one breath, so there is nothing to assert against without a live
database — and this plan's Global Constraints require tests that touch no DB. Splitting the
pure assembly out is the smallest change that makes the write path testable, and it is where
Task 9 will add `vendor_id` too. `write_invoice` keeps the dedup bookkeeping and its
`(status, vendor, invoice_no)` return, so every existing caller is unaffected.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_dates.py`:

```python
from core import processor as ip


class RecordCarriesIsoDate(unittest.TestCase):
    """The processor's record builder must emit invoice_date_iso alongside invoice_date,
    so no row can ever be inserted with one set and the other missing."""

    def _record(self, invoice_date):
        data = {
            "vendor_name":   "Athens Services",
            "invoice_number": "A1",
            "invoice_date":  invoice_date,
            "total_amount":  "100.00",
            "property":      "Kenmore Plaza",
        }
        return ip.build_invoice_record(
            data, source_file="x.pdf", status="OK",
            date_processed="06/03/2026", stored_file="Athens_06_2026.pdf",
        )

    def test_parseable_date_produces_iso(self):
        self.assertEqual(self._record("06/01/2026")["invoice_date_iso"], "2026-06-01")

    def test_previously_failing_shapes_now_produce_iso(self):
        self.assertEqual(self._record("13-Jul-26")["invoice_date_iso"], "2026-07-13")
        self.assertEqual(self._record("06262026")["invoice_date_iso"], "2026-06-26")

    def test_unparseable_date_produces_empty_iso_not_a_missing_key(self):
        rec = self._record("garbage")
        self.assertIn("invoice_date_iso", rec)
        self.assertEqual(rec["invoice_date_iso"], "")

    def test_raw_invoice_date_is_preserved_verbatim(self):
        # The raw string is provenance and must survive untouched next to the parsed form.
        self.assertEqual(self._record("13-Jul-26")["invoice_date"], "13-Jul-26")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_dates -v`
Expected: FAIL with `AttributeError: module 'core.processor' has no attribute 'build_invoice_record'`

- [ ] **Step 3: Extract the pure builder and add the ISO field**

At the top of `core/processor.py`, add to the existing imports:

```python
from core import dates
```

Insert this function immediately **above** `write_invoice` (line 570):

```python
def build_invoice_record(data: Dict, source_file: str, status: str, date_processed: str,
                         needs_review: bool = False, stored_file: str = "") -> Dict:
    """Assemble the invoice row that db.insert_invoice consumes.

    Pure - no database, no filesystem - so the write path can be unit-tested. write_invoice
    owns the duplicate bookkeeping and calls this for the row itself.
    """
    vendor        = (data.get("vendor_name") or "").strip() or "Unknown Vendor"
    invoice_no    = resolve_invoice_number(data)
    unit          = (data.get("unit") or "").strip()
    parsed_amount = _parse_amount(data.get("total_amount"))
    amount_text   = "" if parsed_amount is not None else (
        str(data.get("total_amount")).strip() if data.get("total_amount") else "")
    items_note    = _summarize_line_items(data.get("line_items"))
    invoice_date  = data.get("invoice_date") or ""

    return {
        "status":         status,
        "vendor_name":    vendor,
        "invoice_number": invoice_no,
        "unit":           unit,
        "invoice_date":     invoice_date,
        # Parsed form. Every timing decision in the app reads this, never the raw text.
        # Empty string when unparseable, which routes the row to the date review queue.
        "invoice_date_iso": dates.to_iso(invoice_date),
        "due_date":       data.get("due_date") or "",
        "amount":         parsed_amount,
        "amount_text":    amount_text,
        "description":    data.get("description") or "",
        "line_items":     items_note,
        "property":       data.get("property") or "",
        "source_file":    source_file,
        "date_processed": date_processed,
        "entered_in_yardi": 0,
        "stored_file":    stored_file,
        "needs_review":   1 if needs_review else 0,
        "origin":         "processor",
    }
```

Now replace the body of `write_invoice` from the `db.insert_invoice({` call to its closing
`})` with a call to the new function. The lines above it (`vendor`, `invoice_no`, `unit`,
`when`, `parsed_amount`, `amount_text`, `items_note`, `date_based`, `key`, `is_dup`,
`status`, `seen.add`) stay exactly as they are — the duplicate key still needs them:

```python
    db.insert_invoice(build_invoice_record(
        data, source_file=source_file, status=status, date_processed=when,
        needs_review=needs_review, stored_file=stored_file,
    ))
    return status, vendor, invoice_no
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m unittest tests.test_dates -v`
Expected: PASS, 10 tests

Run: `python -m unittest discover -s tests -t .`
Expected: PASS, 41 tests

- [ ] **Step 5: Keep the edit form in step**

In `app.py`, inside `edit_invoice` (line 322), after the submitted fields are collected and
before `db.update_invoice(...)` is called, add:

```python
    # invoice_date_iso must never disagree with invoice_date - recompute it on every edit.
    if "invoice_date" in fields:
        from core import dates
        fields["invoice_date_iso"] = dates.to_iso(fields["invoice_date"])
```

- [ ] **Step 6: Verify the edit path in the browser**

Run `python app.py`, open any invoice's edit panel from <http://127.0.0.1:5057/invoices>,
change the date to `13-Jul-26`, and save. Then:

```bash
python -c "
from core import db
r = db.get_invoice(<that invoice id>)
print(repr(r['invoice_date']), '->', repr(r['invoice_date_iso']))
"
```

Expected: `'13-Jul-26' -> '2026-07-13'`

- [ ] **Step 7: Commit**

```bash
git add core/processor.py app.py tests/test_dates.py
git commit -m "feat: extract pure record builder and populate invoice_date_iso on write"
```

---

## Task 5: Date review queue on the Fixer page

**Files:**
- Modify: `app.py` (`fixer` route at line 432; add `fixer_set_date` handler)
- Modify: `templates/fixer.html`
- Test: manual — this is a template and route change with no pure logic to unit test.

**Interfaces:**
- Consumes: `db.unresolved_date_invoices()`, `db.set_invoice_date()` (Task 3), `dates.to_iso` (Task 2).
- Produces: route `POST /fixer/<int:invoice_id>/date`, and `dates` in the `fixer.html` template context.

- [ ] **Step 1: Add the queue to the Fixer route**

In `app.py`, replace the `fixer()` function (line 432) with:

```python
@app.route("/fixer")
def fixer():
    reviews = db.review_invoices()
    for r in reviews:                    # let each row link to its PDF (lives in Needs Review/)
        r["has_file"] = state.resolve_invoice_file(r) is not None
    undated = db.unresolved_date_invoices()
    return render_template("fixer.html",
                           reviews=reviews,
                           undated=undated,
                           properties=db.all_properties())
```

- [ ] **Step 2: Add the date-fix handler**

In `app.py`, immediately after `fixer_assign` (which ends around line 462), add:

```python
@app.route("/fixer/<int:invoice_id>/date", methods=["POST"])
def fixer_set_date(invoice_id):
    """Resolve an invoice whose date could not be parsed. The user supplies the date from
    a picker, so we store an already-valid ISO string and echo it into the raw column."""
    from core import dates
    chosen = request.form.get("invoice_date", "").strip()
    if not chosen:
        flash("Pick a date.")
        return redirect(url_for("fixer"))
    iso = dates.to_iso(chosen)
    if not iso:
        flash(f"Could not read '{chosen}' as a date.")
        return redirect(url_for("fixer"))
    if not db.get_invoice(invoice_id):
        flash("That invoice no longer exists.")
        return redirect(url_for("fixer"))
    db.set_invoice_date(invoice_id, iso, iso)
    flash(f"Date set to {iso}.")
    return redirect(url_for("fixer"))
```

- [ ] **Step 3: Add the panel to the template**

In `templates/fixer.html`, inside the `{% block content %}`, add this panel **above** the
existing needs-review table so the smaller queue is dealt with first:

```html
{% if undated %}
<section class="panel">
  <div class="panel-head">
    <div class="panel-title">Dates that could not be read</div>
    <div class="panel-note">{{ undated|length }} invoice{{ '' if undated|length == 1 else 's' }}</div>
  </div>
  <p class="muted">
    These invoices have a date the app could not parse. They are not counted in any
    monthly total until resolved &mdash; an unreadable date looks the same as a vendor
    who skipped a month.
  </p>
  <table class="props">
    <thead>
      <tr><th>Vendor</th><th>Property</th><th>As printed</th><th>Correct date</th></tr>
    </thead>
    <tbody>
    {% for inv in undated %}
      <tr>
        <td>{{ inv.vendor_name or '(no vendor)' }}</td>
        <td>{{ inv.property or '(none)' }}</td>
        <td><code>{{ inv.invoice_date or '(blank)' }}</code></td>
        <td>
          <form method="post" action="{{ url_for('fixer_set_date', invoice_id=inv.id) }}">
            <input type="date" name="invoice_date" required>
            <button class="btn" type="submit">Save</button>
          </form>
        </td>
      </tr>
    {% endfor %}
    </tbody>
  </table>
</section>
{% endif %}
```

> The `<form>` sits inside a `<td>`, which is valid HTML — unlike a `<form>` wrapping a
> `<tr>`, which is what the July review fixed on the properties and vendors pages. Do not
> restructure this to wrap the row.

- [ ] **Step 4: Verify in the browser**

Start the app and open the Fixer page:

```bash
python app.py
```

Navigate to <http://127.0.0.1:5057/fixer>. Expected: the new panel lists the 2 rows with
blank dates. Setting a date on one makes it disappear from the list on reload, and
`SELECT invoice_date_iso FROM invoices WHERE id = <that id>` returns the chosen ISO date.

> If the page does not reflect your edit, check for a stale server on port 5057 before
> assuming a code problem — repeated launches stack `python.exe` instances and the browser
> reconnects to whichever one still holds the port.

- [ ] **Step 5: Commit**

```bash
git add app.py templates/fixer.html
git commit -m "feat: add date review queue to the Fixer page"
```

---

## Task 6: Vendor identity schema

**Files:**
- Modify: `core/db.py` (`_ADDED_COLUMNS`, `_SCHEMA` vendors table, `_COLUMNS`)
- Test: `tests/test_migration.py` (extend)

**Interfaces:**
- Consumes: `db._ensure_columns` (Task 1).
- Produces:
  - `vendors.canonical_name TEXT DEFAULT ''`, `vendors.active INTEGER DEFAULT 1`
  - `invoices.vendor_id INTEGER`, `invoices.vendor_needs_review INTEGER DEFAULT 0`
  - `db.all_vendors()` rows now carry `canonical_name` and `active`.

`vendors` already has `short_name` and `aliases`; only `canonical_name` and `active` are new.

- [ ] **Step 1: Write the failing test**

Append to the `AddedColumnsRegistry` class in `tests/test_migration.py`:

```python
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
        self.assertIn("vendor_id", db._COLUMNS)
        self.assertIn("vendor_needs_review", db._COLUMNS)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_migration -v`
Expected: FAIL on the first `subTest` — the tuple is not in `_ADDED_COLUMNS`.

- [ ] **Step 3: Write minimal implementation**

In `core/db.py`, extend `_ADDED_COLUMNS`:

```python
_ADDED_COLUMNS = [
    ("invoices", "invoice_date_iso",     "TEXT DEFAULT ''"),
    ("vendors",  "canonical_name",       "TEXT DEFAULT ''"),
    ("vendors",  "active",               "INTEGER DEFAULT 1"),
    ("invoices", "vendor_id",            "INTEGER"),
    ("invoices", "vendor_needs_review",  "INTEGER DEFAULT 0"),
]
```

Add both invoice columns to `_COLUMNS`, after `"vendor_name"`:

```python
    "vendor_name",
    "vendor_id",             # FK to vendors.id; NULL until matched
    "vendor_needs_review",   # 0 | 1 - queued on the Fixer page's vendor tab
```

Update the `_SCHEMA` CREATE statements so fresh databases get them directly. In the
`vendors` table:

```sql
CREATE TABLE IF NOT EXISTS vendors (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    short_name     TEXT NOT NULL UNIQUE,
    canonical_name TEXT DEFAULT '',
    aliases        TEXT DEFAULT '',
    active         INTEGER DEFAULT 1
);
```

In the `invoices` table, after the `vendor_name` line:

```sql
    vendor_name         TEXT DEFAULT '',
    vendor_id           INTEGER,
    vendor_needs_review INTEGER DEFAULT 0,
```

And add an index alongside the existing three at the bottom of `_SCHEMA`:

```sql
CREATE INDEX IF NOT EXISTS idx_invoices_vendor_id ON invoices(vendor_id);
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m unittest discover -s tests -t .`
Expected: PASS, 43 tests

- [ ] **Step 5: Verify the migration ran against the live database**

Run:

```bash
python -c "
from core import db; db.init()
import sqlite3
c = sqlite3.connect('data/invoices.db')
for t in ('invoices','vendors'):
    print(t, sorted(r[1] for r in c.execute(f'PRAGMA table_info({t})')))
"
```

Expected: `invoices` includes `invoice_date_iso`, `vendor_id`, `vendor_needs_review`;
`vendors` includes `canonical_name`, `active`.

- [ ] **Step 6: Commit**

```bash
git add core/db.py tests/test_migration.py
git commit -m "feat: add vendor identity columns to vendors and invoices"
```

---

## Task 7: Vendor match pipeline

**Files:**
- Create: `core/vendor_match.py`
- Test: `tests/test_vendor_match.py` (create)

**Interfaces:**
- Consumes: nothing from earlier tasks — pure functions over plain data.
- Produces:
  - `vendor_match.normalize(name: str) -> str`
  - `vendor_match.match(raw: str, vendors: list[dict]) -> MatchResult`, where `MatchResult` is a
    `NamedTuple` with fields `outcome: str` (`"bind"` | `"suggest"` | `"new"`),
    `vendor_id: int | None`, `score: float`, `reason: str`.
  - Each `vendors` entry is a dict with at least `id`, `canonical_name`, `short_name`, `aliases`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_vendor_match.py`:

```python
# -*- coding: utf-8 -*-
"""Vendor identity matching. Expectations key on vendor_id, not on a string, so a
mis-tiered match here turns one confident monthly expectation into two sporadic ones.

Run from the project root:

    python -m unittest discover -s tests -t . -v
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import vendor_match as vm


VENDORS = [
    {"id": 1, "canonical_name": "Athens Services", "short_name": "Athens",
     "aliases": "ATHENS SERVICES; Athens Svcs"},
    {"id": 2, "canonical_name": "Los Angeles Department of Water and Power",
     "short_name": "LADWP", "aliases": "LADWP; L.A. Dept of Water & Power"},
    {"id": 3, "canonical_name": "Mitsubishi Electric US, Inc.",
     "short_name": "Mitsubishi", "aliases": ""},
]


class Normalize(unittest.TestCase):
    def test_case_punctuation_and_suffixes_collapse(self):
        self.assertEqual(vm.normalize("MITSUBISHI ELECTRIC US, INC."),
                         vm.normalize("Mitsubishi Electric US, Inc."))
        self.assertEqual(vm.normalize("Ganahl Lumber Company"),
                         vm.normalize("GANAHL LUMBER COMPANY"))

    def test_distinct_vendors_do_not_collapse(self):
        self.assertNotEqual(vm.normalize("Athens Services"), vm.normalize("Athena Services"))


class MatchTiers(unittest.TestCase):
    def test_exact_canonical_binds(self):
        r = vm.match("Athens Services", VENDORS)
        self.assertEqual((r.outcome, r.vendor_id), ("bind", 1))
        self.assertEqual(r.reason, "exact")

    def test_case_variant_binds_via_normalization(self):
        r = vm.match("ATHENS SERVICES", VENDORS)
        self.assertEqual((r.outcome, r.vendor_id), ("bind", 1))

    def test_alias_inside_extracted_name_binds(self):
        r = vm.match("LADWP - Water and Power Billing Dept", VENDORS)
        self.assertEqual((r.outcome, r.vendor_id), ("bind", 2))
        self.assertEqual(r.reason, "alias-inside")

    def test_close_spelling_above_threshold_binds(self):
        r = vm.match("Mitsubishi Electric US Inc", VENDORS)
        self.assertEqual((r.outcome, r.vendor_id), ("bind", 3))
        self.assertEqual(r.reason, "close-spelling")

    def test_unknown_vendor_is_new(self):
        r = vm.match("Rolling Greens Nursery", VENDORS)
        self.assertEqual((r.outcome, r.vendor_id), ("new", None))

    def test_empty_input_is_new_not_a_crash(self):
        for raw in ("", "   ", None):
            with self.subTest(raw=raw):
                self.assertEqual(vm.match(raw, VENDORS).outcome, "new")

    def test_no_vendors_configured_is_new(self):
        self.assertEqual(vm.match("Athens Services", []).outcome, "new")


class MatchThresholds(unittest.TestCase):
    """The band boundaries are load-bearing: below BIND a human must confirm."""

    def test_band_constants(self):
        self.assertEqual(vm.BIND_THRESHOLD, 0.92)
        self.assertEqual(vm.SUGGEST_THRESHOLD, 0.80)

    def test_score_just_below_bind_suggests(self):
        r = vm._classify(0.91, vendor_id=7, reason="close-spelling")
        self.assertEqual((r.outcome, r.vendor_id), ("suggest", 7))

    def test_score_at_bind_binds(self):
        r = vm._classify(0.92, vendor_id=7, reason="close-spelling")
        self.assertEqual(r.outcome, "bind")

    def test_score_below_suggest_is_new(self):
        r = vm._classify(0.79, vendor_id=7, reason="close-spelling")
        self.assertEqual((r.outcome, r.vendor_id), ("new", None))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_vendor_match -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'core.vendor_match'`

- [ ] **Step 3: Write minimal implementation**

Create `core/vendor_match.py`:

```python
# -*- coding: utf-8 -*-
"""Vendor identity: turning the raw vendor string Claude extracted into a vendor_id.

Why this matters beyond tidiness: recurring-invoice expectations key on vendor_id, not
on a string. Without this layer 'Athens Services' and 'ATHENS SERVICES' are two
half-confident expectations that each look sporadic; with it they are one confident
monthly expectation.

The pipeline mirrors match_property() in processor.py - exact, alias-inside, then close
spelling - with an added confidence band so anything uncertain reaches a human instead
of being guessed at. Confirming a suggestion writes the raw string into that vendor's
aliases, so the same spelling is never asked about twice.
"""
import difflib
import re
from typing import NamedTuple, Optional

# Score at or above which a match is applied without asking. Below SUGGEST_THRESHOLD the
# candidate is discarded entirely and the vendor is treated as new.
BIND_THRESHOLD = 0.92
SUGGEST_THRESHOLD = 0.80

# Corporate suffixes and filler that carry no identifying information.
_NOISE = re.compile(
    r"\b(inc|llc|ltd|lp|corp|corporation|company|co|the|of|and|dba|"
    r"services|service|us|usa)\b"
)


class MatchResult(NamedTuple):
    outcome: str              # "bind" | "suggest" | "new"
    vendor_id: Optional[int]
    score: float
    reason: str               # "exact" | "alias-inside" | "close-spelling" | "no-match"


def normalize(name: Optional[str]) -> str:
    """Lowercase, strip punctuation and corporate suffixes, collapse whitespace."""
    text = (name or "").lower()
    text = re.sub(r"[^a-z0-9 ]", " ", text)
    text = _NOISE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def _aliases_of(vendor: dict) -> list[str]:
    """Every string that identifies this vendor: canonical name, short name, aliases."""
    raw = [vendor.get("canonical_name"), vendor.get("short_name")]
    raw += (vendor.get("aliases") or "").split(";")
    return [a.strip() for a in raw if a and a.strip()]


def _classify(score: float, vendor_id: Optional[int], reason: str) -> MatchResult:
    """Apply the confidence bands. Split out so the boundaries are directly testable."""
    if vendor_id is not None and score >= BIND_THRESHOLD:
        return MatchResult("bind", vendor_id, score, reason)
    if vendor_id is not None and score >= SUGGEST_THRESHOLD:
        return MatchResult("suggest", vendor_id, score, reason)
    return MatchResult("new", None, score, "no-match")


def match(raw: Optional[str], vendors: list[dict]) -> MatchResult:
    """Match an extracted vendor string against the known vendor list."""
    key = normalize(raw)
    if not key or not vendors:
        return MatchResult("new", None, 0.0, "no-match")

    # 1) Exact match on any identifying string, ignoring case/punctuation/suffixes.
    for v in vendors:
        if any(normalize(a) == key for a in _aliases_of(v)):
            return MatchResult("bind", v["id"], 1.0, "exact")

    # 2) A known alias appears inside the extracted name - common when the invoice
    #    prints a department or billing suffix. Longest alias wins as most specific.
    best_id, best_len = None, 0
    for v in vendors:
        for alias in _aliases_of(v):
            akey = normalize(alias)
            if akey and akey in key and len(akey) > best_len:
                best_id, best_len = v["id"], len(akey)
    if best_id is not None:
        return MatchResult("bind", best_id, 1.0, "alias-inside")

    # 3) Close spelling. Take the single best candidate, then let the bands decide.
    best_id, best_score = None, 0.0
    for v in vendors:
        for alias in _aliases_of(v):
            score = difflib.SequenceMatcher(None, key, normalize(alias)).ratio()
            if score > best_score:
                best_id, best_score = v["id"], score
    return _classify(best_score, best_id, "close-spelling")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m unittest tests.test_vendor_match -v`
Expected: PASS, 13 tests

Run: `python -m unittest discover -s tests -t .`
Expected: PASS, 56 tests

- [ ] **Step 5: Commit**

```bash
git add core/vendor_match.py tests/test_vendor_match.py
git commit -m "feat: add vendor match pipeline with confidence bands"
```

---

## Task 8: Vendor clustering bootstrap

**Files:**
- Modify: `core/vendor_match.py` (add `cluster`)
- Create: `scripts/bootstrap_vendors.py`
- Test: `tests/test_vendor_match.py` (extend)

**Interfaces:**
- Consumes: `vendor_match.normalize` (Task 7), `db.all_vendors`, `db.add_vendor`, `db.list_invoices`.
- Produces: `vendor_match.cluster(names: list[str], threshold: float = 0.86) -> list[list[str]]` — groups
  raw vendor strings that are probably the same vendor. Order within a group is by
  descending frequency in the input.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_vendor_match.py`:

```python
class Cluster(unittest.TestCase):
    def test_case_variants_group_together(self):
        groups = vm.cluster([
            "Mitsubishi Electric US, Inc.", "MITSUBISHI ELECTRIC US, INC.",
            "Ganahl Lumber Company", "GANAHL LUMBER COMPANY",
        ])
        sizes = sorted(len(g) for g in groups)
        self.assertEqual(sizes, [2, 2])

    def test_genuinely_different_vendors_stay_separate(self):
        # These four are the real ambiguous cases from the live data. Each pair MUST
        # stay split - merging them silently would erase a real distinction that only
        # a human can adjudicate.
        pairs = [
            ("Michelle Suh", "Michelle Suh (Rooter Plumbing)"),
            ("James Chin", "James Chin (Stamp Reimbursement)"),
            ("City of Los Angeles",
             "City of Los Angeles, Department of Public Works, Bureau of Sanitation"),
        ]
        for left, right in pairs:
            with self.subTest(pair=(left, right)):
                groups = vm.cluster([left, right])
                self.assertEqual(len(groups), 2, f"{left!r} and {right!r} must not merge")

    def test_singleton_input_gives_one_group(self):
        self.assertEqual(vm.cluster(["Rolling Greens"]), [["Rolling Greens"]])

    def test_empty_input_gives_no_groups(self):
        self.assertEqual(vm.cluster([]), [])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_vendor_match -v`
Expected: FAIL with `AttributeError: module 'core.vendor_match' has no attribute 'cluster'`

- [ ] **Step 3: Write minimal implementation**

Append to `core/vendor_match.py`:

```python
def cluster(names: list[str], threshold: float = 0.86) -> list[list[str]]:
    """Group raw vendor strings that are probably the same vendor.

    Used once, at bootstrap, to turn the raw strings already in the invoice table into a
    starting vendor list. Deliberately conservative: it is far cheaper for a human to
    merge two groups than to discover months later that two real vendors were silently
    combined and their expectations tangled.

    Substring containment alone is NOT treated as a match - 'Michelle Suh' is inside
    'Michelle Suh (Rooter Plumbing)' but they may be a person and a plumbing company.
    """
    import collections

    counts = collections.Counter(n for n in names if (n or "").strip())
    groups: list[list[str]] = []
    for name in sorted(counts, key=lambda n: (-counts[n], n)):
        key = normalize(name)
        placed = False
        for group in groups:
            if any(difflib.SequenceMatcher(None, key, normalize(m)).ratio() >= threshold
                   for m in group):
                group.append(name)
                placed = True
                break
        if not placed:
            groups.append([name])
    return groups
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m unittest tests.test_vendor_match -v`
Expected: PASS, 17 tests (and 60 overall)

- [ ] **Step 5: Write the bootstrap script**

Create `scripts/bootstrap_vendors.py`:

```python
# -*- coding: utf-8 -*-
"""One-shot bootstrap of the vendor list from the raw strings already in the invoice table.

Dry run by default - prints the clusters and writes nothing:

    python scripts/bootstrap_vendors.py

Apply (creates one vendor per cluster, with every raw spelling as an alias):

    python scripts/bootstrap_vendors.py --apply

Safe to re-run: a cluster whose canonical name already exists is skipped.
Review the multi-member clusters before applying - the script never merges two vendors
that a human has not looked at.
"""
import argparse
import collections
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import db, vendor_match as vm


def _short_name(canonical: str) -> str:
    """A filename-safe short name: the first meaningful word, or an acronym for long names."""
    import re
    words = [w for w in re.split(r"\s+", canonical) if w]
    if len(words) >= 4:
        acronym = "".join(w[0] for w in words if w[0].isalnum()).upper()[:8]
        if len(acronym) >= 3:
            return acronym
    return re.sub(r"[^0-9A-Za-z&-]", "", words[0]) if words else "Vendor"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write vendors (default: dry run)")
    args = ap.parse_args()

    db.init()
    names = [(r.get("vendor_name") or "").strip() for r in db.list_invoices()]
    names = [n for n in names if n]
    counts = collections.Counter(names)
    groups = vm.cluster(names)
    existing = {(v.get("canonical_name") or "").strip().lower() for v in db.all_vendors()}

    multi = [g for g in groups if len(g) > 1]
    print(f"{len(counts)} distinct raw strings -> {len(groups)} clusters "
          f"({len(multi)} need a look, {len(groups) - len(multi)} singletons)\n")

    print("Clusters needing a human decision:")
    for g in sorted(multi, key=lambda g: -sum(counts[x] for x in g)):
        print("  *", " | ".join(f"{x} ({counts[x]})" for x in g))

    created = skipped = 0
    for group in groups:
        canonical = group[0]                    # most frequent spelling wins
        if canonical.strip().lower() in existing:
            skipped += 1
            continue
        if args.apply:
            vendor_id = db.add_vendor(_short_name(canonical), "; ".join(group))
            db.update_vendor_identity(vendor_id, canonical_name=canonical)
        created += 1

    verb = "created" if args.apply else "would create"
    print(f"\n{verb} {created} vendors, skipped {skipped} already present")
    if not args.apply:
        print("Dry run. Re-run with --apply to write.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 6: Add the db helper the script needs**

In `core/db.py`, beside `update_vendor` (line 217), add:

```python
def update_vendor_identity(vendor_id: int, canonical_name: str = None,
                           active: int = None) -> None:
    """Set the identity fields added after the vendors table shipped. Only the arguments
    actually supplied are written, so callers can update one field without clobbering
    the other."""
    sets, params = [], []
    if canonical_name is not None:
        sets.append("canonical_name = ?")
        params.append(canonical_name)
    if active is not None:
        sets.append("active = ?")
        params.append(int(active))
    if not sets:
        return
    params.append(vendor_id)
    with _connect() as conn:
        conn.execute(f"UPDATE vendors SET {', '.join(sets)} WHERE id = ?", params)
```

- [ ] **Step 7: Run the bootstrap, dry first**

Run: `python scripts/bootstrap_vendors.py`

Expected: roughly `95 distinct raw strings -> 82 clusters (13 need a look, 69 singletons)`,
then the 13 clusters printed. Read them. Confirm the four genuinely-ambiguous pairs from
the plan's Task 8 test appear as **separate** clusters, not merged.

Then run: `python scripts/bootstrap_vendors.py --apply`
Expected: `created 82 vendors, skipped 0`

Verify idempotence — run: `python scripts/bootstrap_vendors.py --apply`
Expected: `created 0 vendors, skipped 82`

- [ ] **Step 8: Commit**

```bash
git add core/vendor_match.py core/db.py scripts/bootstrap_vendors.py tests/test_vendor_match.py
git commit -m "feat: add vendor clustering bootstrap from existing invoice history"
```

---

## Task 9: Bind `vendor_id` on the processor write path

**Files:**
- Modify: `core/processor.py` (record builder, same function edited in Task 4)
- Modify: `core/db.py` (add `vendor_review_invoices()`, `set_invoice_vendor()`)
- Test: `tests/test_vendor_match.py` (extend)

**Interfaces:**
- Consumes: `vendor_match.match` (Task 7), the columns from Task 6.
- Produces:
  - Records emitted by the processor carry `vendor_id` and `vendor_needs_review`.
  - `db.vendor_review_invoices() -> list[dict]` — rows where `vendor_needs_review = 1`.
  - `db.set_invoice_vendor(invoice_id: int, vendor_id: int) -> None` — binds the vendor and
    clears the review flag. Never touches `vendor_name` or `stored_file`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_vendor_match.py`:

```python
class ResultToRecordFields(unittest.TestCase):
    """A MatchResult has to become two column values. Both outcomes below must set
    vendor_needs_review, because an unreviewed 'suggest' silently binding would defeat
    the point of the queue."""

    def test_bind_sets_vendor_and_clears_flag(self):
        fields = vm.record_fields(vm.MatchResult("bind", 3, 1.0, "exact"))
        self.assertEqual(fields, {"vendor_id": 3, "vendor_needs_review": 0})

    def test_suggest_records_candidate_but_flags_for_review(self):
        fields = vm.record_fields(vm.MatchResult("suggest", 5, 0.88, "close-spelling"))
        self.assertEqual(fields, {"vendor_id": 5, "vendor_needs_review": 1})

    def test_new_leaves_vendor_null_and_flags_for_review(self):
        fields = vm.record_fields(vm.MatchResult("new", None, 0.0, "no-match"))
        self.assertEqual(fields, {"vendor_id": None, "vendor_needs_review": 1})


class RecordCarriesVendorId(unittest.TestCase):
    """End-to-end through the record builder, still with no database: the vendor list is
    passed in, so purity is preserved."""

    def _record(self, vendor_name, vendors):
        from core import processor as ip
        return ip.build_invoice_record(
            {"vendor_name": vendor_name, "invoice_number": "A1",
             "invoice_date": "06/01/2026", "total_amount": "100.00",
             "property": "Kenmore Plaza"},
            source_file="x.pdf", status="OK", date_processed="06/03/2026",
            stored_file="Athens_06_2026.pdf", vendors=vendors,
        )

    def test_known_vendor_binds(self):
        rec = self._record("ATHENS SERVICES", VENDORS)
        self.assertEqual((rec["vendor_id"], rec["vendor_needs_review"]), (1, 0))

    def test_unknown_vendor_queues_and_keeps_raw_name(self):
        rec = self._record("Rolling Greens Nursery", VENDORS)
        self.assertEqual((rec["vendor_id"], rec["vendor_needs_review"]), (None, 1))
        self.assertEqual(rec["vendor_name"], "Rolling Greens Nursery")

    def test_no_vendor_list_touches_no_database(self):
        rec = self._record("Athens Services", None)
        self.assertEqual((rec["vendor_id"], rec["vendor_needs_review"]), (None, 1))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_vendor_match -v`
Expected: FAIL with `AttributeError: module 'core.vendor_match' has no attribute 'record_fields'`

- [ ] **Step 3: Write minimal implementation**

Append to `core/vendor_match.py`:

```python
def record_fields(result: MatchResult) -> dict:
    """Turn a MatchResult into the two invoice column values it implies.

    A 'suggest' keeps the candidate id so the review screen can pre-select it, but still
    flags the row - a suggestion that bound itself would make the queue pointless.
    """
    return {
        "vendor_id": result.vendor_id if result.outcome != "new" else None,
        "vendor_needs_review": 0 if result.outcome == "bind" else 1,
    }
```

In `core/processor.py`, add the import at the top:

```python
from core import vendor_match
```

`build_invoice_record` must stay pure — a `db.all_vendors()` call inside it would break the
no-database constraint the Task 4 tests rely on. So the vendor list is **passed in**, and
`write_invoice` does the lookup. Change the signature (line added in Task 4):

```python
def build_invoice_record(data: Dict, source_file: str, status: str, date_processed: str,
                         needs_review: bool = False, stored_file: str = "",
                         vendors: Optional[list] = None) -> Dict:
```

Inside it, directly beneath the `"vendor_name": vendor,` entry in the returned dict, add:

```python
        "vendor_name":    vendor,
        # vendor_id is bound only on a confident match; anything less queues for review.
        # The raw vendor_name above is never overwritten - it is provenance, and it is
        # what makes a bad merge reversible.
        **vendor_match.record_fields(vendor_match.match(vendor, vendors or [])),
```

Then in `write_invoice`, pass the list through to it:

```python
    db.insert_invoice(build_invoice_record(
        data, source_file=source_file, status=status, date_processed=when,
        needs_review=needs_review, stored_file=stored_file,
        vendors=db.all_vendors(),
    ))
```

With `vendors=None` (the default, used by the Task 4 tests) `match` returns `"new"`, so the
record gets `vendor_id=None, vendor_needs_review=1` — the correct outcome for an unknown
vendor, and no database is touched.

In `core/db.py`, add the two query functions beside `review_invoices()`:

```python
def vendor_review_invoices() -> list[dict]:
    """Invoices whose vendor could not be matched with confidence. Queued on the Fixer
    page's vendor tab; confirming one writes the raw string into that vendor's aliases,
    so the same spelling is never asked about twice."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM invoices WHERE COALESCE(vendor_needs_review,0) = 1 "
            "ORDER BY id DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def set_invoice_vendor(invoice_id: int, vendor_id: int) -> None:
    """Bind an invoice to a vendor and clear its review flag.

    Deliberately does NOT touch vendor_name (provenance - it is what makes a bad merge
    reversible) or stored_file (the sidecar/assembler join key, whose copies already
    exist under data/Bank Rec/). Vendor changes never rename a filed PDF.
    """
    with _connect() as conn:
        conn.execute(
            "UPDATE invoices SET vendor_id = ?, vendor_needs_review = 0 WHERE id = ?",
            (vendor_id, invoice_id),
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m unittest discover -s tests -t .`
Expected: PASS, 66 tests (23 in test_vendor_match.py)

- [ ] **Step 5: Backfill vendor_id over existing invoices**

Run:

```bash
python -c "
from core import db, vendor_match as vm
db.init()
vendors = db.all_vendors()
bound = flagged = 0
for inv in db.list_invoices():
    if inv.get('vendor_id'):
        continue
    r = vm.match(inv.get('vendor_name'), vendors)
    f = vm.record_fields(r)
    if f['vendor_id'] and not f['vendor_needs_review']:
        db.set_invoice_vendor(inv['id'], f['vendor_id']); bound += 1
    else:
        db.update_invoice(inv['id'], vendor_needs_review=1); flagged += 1
print(f'bound {bound}, flagged for review {flagged}')
"
```

Expected: the large majority bound (every raw spelling became an alias in Task 8, so most
hit the exact tier), with a small number flagged.

- [ ] **Step 6: Commit**

```bash
git add core/processor.py core/db.py core/vendor_match.py tests/test_vendor_match.py
git commit -m "feat: bind vendor_id on the processor write path"
```

---

## Task 10: Vendor review queue on the Fixer page

**Files:**
- Modify: `app.py` (`fixer` route; add `fixer_set_vendor` handler)
- Modify: `templates/fixer.html`
- Test: manual — route and template change.

**Interfaces:**
- Consumes: `db.vendor_review_invoices()`, `db.set_invoice_vendor()` (Task 9),
  `db.all_vendors()`, `db.update_vendor()`, `vendor_match.match` (Task 7).
- Produces: route `POST /fixer/<int:invoice_id>/vendor`.

- [ ] **Step 1: Extend the Fixer route**

In `app.py`, replace the `fixer()` function body with:

```python
@app.route("/fixer")
def fixer():
    from core import vendor_match
    reviews = db.review_invoices()
    for r in reviews:                    # let each row link to its PDF (lives in Needs Review/)
        r["has_file"] = state.resolve_invoice_file(r) is not None
    vendors = db.all_vendors()
    unvendored = db.vendor_review_invoices()
    for r in unvendored:                 # pre-select the best candidate on each row
        r["suggestion"] = vendor_match.match(r.get("vendor_name"), vendors)
    return render_template("fixer.html",
                           reviews=reviews,
                           undated=db.unresolved_date_invoices(),
                           unvendored=unvendored,
                           vendors=vendors,
                           properties=db.all_properties())
```

- [ ] **Step 2: Add the vendor-fix handler**

In `app.py`, after `fixer_set_date`, add:

```python
@app.route("/fixer/<int:invoice_id>/vendor", methods=["POST"])
def fixer_set_vendor(invoice_id):
    """Confirm which vendor an invoice belongs to.

    Confirming also writes the raw extracted string into that vendor's aliases, which is
    what makes the queue shrink: the same spelling is never asked about twice.
    """
    chosen = request.form.get("vendor_id", "").strip()
    if not chosen.isdigit():
        flash("Pick a vendor.")
        return redirect(url_for("fixer"))
    vendor_id = int(chosen)
    inv = db.get_invoice(invoice_id)
    if not inv:
        flash("That invoice no longer exists.")
        return redirect(url_for("fixer"))
    vendor = next((v for v in db.all_vendors() if v["id"] == vendor_id), None)
    if not vendor:
        flash("That vendor no longer exists.")
        return redirect(url_for("fixer"))

    raw = (inv.get("vendor_name") or "").strip()
    aliases = [a.strip() for a in (vendor.get("aliases") or "").split(";") if a.strip()]
    if raw and raw.lower() not in {a.lower() for a in aliases}:
        db.update_vendor(vendor_id, vendor["short_name"], "; ".join(aliases + [raw]))

    db.set_invoice_vendor(invoice_id, vendor_id)
    flash(f"Matched to {vendor.get('canonical_name') or vendor['short_name']}.")
    return redirect(url_for("fixer"))
```

- [ ] **Step 3: Add the panel to the template**

In `templates/fixer.html`, add below the date panel from Task 5:

```html
{% if unvendored %}
<section class="panel">
  <div class="panel-head">
    <div class="panel-title">Vendors needing confirmation</div>
    <div class="panel-note">{{ unvendored|length }} invoice{{ '' if unvendored|length == 1 else 's' }}</div>
  </div>
  <p class="muted">
    Confirming also teaches the app this spelling &mdash; you will not be asked about it again.
  </p>
  <table class="props">
    <thead>
      <tr><th>As printed on the invoice</th><th>Property</th><th>Match to</th></tr>
    </thead>
    <tbody>
    {% for inv in unvendored %}
      <tr>
        <td><code>{{ inv.vendor_name or '(no vendor)' }}</code></td>
        <td>{{ inv.property or '(none)' }}</td>
        <td>
          <form method="post" action="{{ url_for('fixer_set_vendor', invoice_id=inv.id) }}">
            <select name="vendor_id" required>
              <option value="">Choose a vendor&hellip;</option>
              {% for v in vendors %}
                <option value="{{ v.id }}"
                  {% if inv.suggestion.vendor_id == v.id %}selected{% endif %}>
                  {{ v.canonical_name or v.short_name }}
                  {%- if inv.suggestion.vendor_id == v.id %} (suggested){% endif %}
                </option>
              {% endfor %}
            </select>
            <button class="btn" type="submit">Confirm</button>
          </form>
        </td>
      </tr>
    {% endfor %}
    </tbody>
  </table>
</section>
{% endif %}
```

- [ ] **Step 4: Verify in the browser**

Run: `python app.py`, then open <http://127.0.0.1:5057/fixer>.

Expected: the vendor panel lists the flagged invoices, each with its suggestion
pre-selected in the dropdown. Confirming one removes it from the list on reload, and:

```bash
python -c "
from core import db
v = [x for x in db.all_vendors() if x['id'] == <the vendor id you chose>][0]
print(v['aliases'])
"
```

shows the raw string appended to that vendor's aliases.

- [ ] **Step 5: Verify no filed PDF was renamed**

This is the constraint most likely to be violated by a well-meaning edit.

Run:

```bash
python -c "
from core import db
rows = db.list_invoices()
print('invoices with a stored_file:', sum(1 for r in rows if (r.get('stored_file') or '').strip()))
"
```

Expected: the same count as before Task 9. Vendor operations must never change
`stored_file`; if this number moved, a vendor path is renaming filed PDFs and must be
reverted.

- [ ] **Step 6: Run the full suite and commit**

Run: `python -m unittest discover -s tests -t .`
Expected: PASS, 66 tests

```bash
git add app.py templates/fixer.html
git commit -m "feat: add vendor review queue to the Fixer page"
```

---

## Task 11: Update README and record the re-measurement

**Files:**
- Modify: `README.md`
- Modify: `docs/superpowers/specs/2026-08-06-workflow-dashboard-design.md` (§5.2 caveat)

**Interfaces:**
- Consumes: everything above.
- Produces: documentation only.

- [ ] **Step 1: Re-measure the recurring-pair statistics post-merge**

Spec §5.2 flags its figures as pre-merge and requires re-measurement once vendor identities
exist. Run:

```bash
python -c "
import collections, statistics
from core import db, dates
rows = db.list_invoices()
pairs = collections.defaultdict(list)
for r in rows:
    iso = (r.get('invoice_date_iso') or '').strip()
    if not iso or not r.get('vendor_id'):
        continue
    pairs[(r.get('property'), r['vendor_id'])].append(iso)
rec = {k: v for k, v in pairs.items() if len({d[:7] for d in v}) >= 2}
spread = [max(int(d[8:10]) for d in v) - min(int(d[8:10]) for d in v) for v in rec.values()]
print(f'recurring pairs (post-merge): {len(rec)}')
print(f'median day-of-month spread  : {statistics.median(spread):.1f}')
print(f'within 3 days               : {sum(1 for s in spread if s <= 3)}/{len(spread)}')
"
```

Record the three numbers — they replace the pre-merge figures and are the input to the
learning engine in a later plan.

- [ ] **Step 2: Update the spec's §5.2 caveat**

In `docs/superpowers/specs/2026-08-06-workflow-dashboard-design.md`, replace the first
bullet of the "Two caveats" block in §5.2 with the post-merge figures from Step 1, and note
the date they were measured. Leave the second caveat (two complete months is thin)
unchanged — it is still true.

- [ ] **Step 3: Add a Troubleshooting entry to the README**

In `README.md`, under **Troubleshooting**, add:

```markdown
**An invoice's date shows as blank** — the app could not read the date the vendor printed.
Open **Needs Review**; unreadable dates are listed at the top with a date picker. They are
held out of monthly totals until resolved rather than being silently dropped, because a
dropped invoice looks identical to a vendor who skipped a month.

**The app asks about a vendor spelling** — a vendor's name was printed differently enough
that the match was not certain. Confirm it once on **Needs Review** and that spelling is
added to the vendor's aliases, so it is never asked about again. Confirming a vendor never
renames an already-filed PDF.
```

- [ ] **Step 4: Commit**

```bash
git add README.md docs/superpowers/specs/2026-08-06-workflow-dashboard-design.md
git commit -m "docs: record post-merge pair statistics and date/vendor review guidance"
```

---

## Done criteria

- `python -m unittest discover -s tests -t .` passes, 66 tests (26 pre-existing + 40 new).
- `data/invoices.db` has `invoice_date_iso`, `vendor_id`, `vendor_needs_review` on
  `invoices`, and `canonical_name`, `active` on `vendors`.
- Every invoice with a readable date has `invoice_date_iso` populated; the rest appear in
  the date review queue on the Fixer page.
- The vendor list is populated from history, and every invoice is either bound to a
  `vendor_id` or queued for confirmation.
- `stored_file` values are unchanged from before this plan.
- Existing behaviour — processing, staging, assembly, reconciliation — is unchanged.

## What this plan does not do

Deliberately out of scope; each belongs to a later plan from the spec's §13:

- The obligation ledger, window rules, rollover (Phase 3).
- Statement persistence and the statement-line matcher (Phases 4 and 6).
- Recurrence profiles, cadence detection, warning eligibility (Phase 5) — this plan
  produces the *inputs* those need, not the engine.
- The workbook import (Phase 2).
- Today / Month / Expected / Statements / Requests pages (Phase 7).
- The four Anthropic API additions (Phase 8).
