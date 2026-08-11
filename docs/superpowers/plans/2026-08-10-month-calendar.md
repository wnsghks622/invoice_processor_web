# Month Calendar and Reminders Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A per-month page showing which normally-arriving invoices haven't arrived yet, plus reminders you write for yourself.

**Architecture:** Three new pure-or-injectable modules over two new tables. `core/periods.py` holds all date and cadence arithmetic as pure functions. `core/ledger.py` owns the `obligation` / `obligation_instance` tables and the rollover that materializes a month. `core/expectations.py` reads invoice history and syncs learned EXPECT obligations. A new Month page renders instances grouped by property; the existing Fixer and Invoices pages gain small changes.

**Tech Stack:** Python 3.10+, Flask, SQLite (stdlib `sqlite3`), `unittest`, Jinja2. No new dependencies.

**Source spec:** `docs/superpowers/specs/2026-08-10-month-calendar-design.md`
**Parent spec:** `docs/superpowers/specs/2026-08-06-workflow-dashboard-design.md`

## Global Constraints

- Python 3.10 or newer. No new third-party dependencies.
- Tests live in `tests/`, use `unittest`, and must run with **no database file, no network, and no filesystem writes**. Run from the project root: `python -m unittest discover -s tests -t .`
- The suite is **125** tests at the start of this plan and must stay green after every task.
- New **tables** go in `db._SCHEMA_TABLES`. New **columns on existing tables** go through `db._ADDED_COLUMNS` + `_ensure_columns()` — `_SCHEMA` uses `CREATE TABLE IF NOT EXISTS` and does nothing to a table that already exists.
- Every new DB query function takes an optional trailing `conn=None` routed through `db._conn_or(conn)`, so it is testable against an in-memory database. **Never duplicate the SQL into two branches** — that is what the helper exists to prevent.
- Period strings are `"%B %Y"` — e.g. `"August 2026"` — matching `state.load_settings()["month"]` exactly.
- Every timing decision reads `invoices.invoice_date_iso`. **Nothing reads `date_processed`** — it records when the user got to an invoice, not when the vendor billed, and its day-of-month spread is 15 days against `invoice_date`'s 2.
- `invoices.vendor_name` is never overwritten. `invoices.stored_file` is never rewritten by any operation in this plan.
- Flask routes get real tests in `tests/test_app.py`, per the standing ruling. Extend the existing fixture — it patches `db._connect` in `setUpModule` **before** `import app`, because `app.py` calls `db.init()` at import time. Do not build a second fixture.
- Every test must be able to fail. Mutate the code it covers and confirm it does. Two mutations survived the whole suite during the previous plan; both were tests asserting something that could not break.
- Commit after every task with a conventional-commit prefix.

---

## Prerequisite: the post-merge runbook

`core/expectations.py` reads `invoices.vendor_id` and `invoices.invoice_date_iso`. On the live database **neither is populated yet** — the three scripts from the previous plan have not been run. Tasks 6 onward can still be developed and unit-tested without them (all fixtures are synthetic), but any verification against real data needs:

```bash
python scripts/backfill_dates.py --apply
python scripts/bootstrap_vendors.py          # read the clusters, then --apply
python scripts/backfill_vendors.py --apply
```

Run these against the **worktree's** database copy, not the user's. The README documents the sequence for the real one; that run is the user's to trigger.

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `core/periods.py` | **New.** All period and cadence arithmetic: parse/format a period, resolve a window rule to dates, classify cadence from observation gaps, decide whether a cadence applies to a period. Pure functions, stdlib only, imports nothing from `db`/`state`/`app`. | Create |
| `core/ledger.py` | **New.** The `obligation` / `obligation_instance` tables: CRUD, rollover, satisfaction, missing-eligibility. DB-touching, every function `conn`-injectable. | Create |
| `core/expectations.py` | **New.** Reads invoice history, builds recurrence profiles, syncs learned EXPECT obligations. Depends on `periods` and `ledger`. | Create |
| `core/db.py` | Gains the two new tables in `_SCHEMA_TABLES`. | Modify |
| `app.py` | Month route, row-action routes, reminder-add route, obligation-edit route. Invoices list date display. | Modify |
| `templates/month.html` | **New.** The Month page. | Create |
| `templates/base.html` | Nav item for Month. | Modify |
| `templates/invoices.html` | Date column renders parsed date. | Modify |
| `tests/test_periods.py` | **New.** Pure arithmetic — the largest test file in this plan. | Create |
| `tests/test_ledger.py` | **New.** In-memory schema fixture. | Create |
| `tests/test_expectations.py` | **New.** In-memory schema fixture. | Create |
| `tests/test_app.py` | Route tests. | Modify |

`periods.py` is separated from `ledger.py` because cadence and window arithmetic is the part most likely to be wrong and is entirely testable without a database. Keeping it pure means its tests are fast, exhaustive, and free of fixture noise.

---

## Task 1: Ledger schema

**Files:**
- Modify: `core/db.py` (`_SCHEMA_TABLES`, ends at line 97)
- Test: `tests/test_ledger.py` (create)

**Interfaces:**
- Consumes: `db._SCHEMA_TABLES`, `db._ensure_columns`, `db._conn_or` (all exist).
- Produces: tables `obligation` and `obligation_instance` as defined below. Every later task depends on this shape.

- [ ] **Step 1: Write the failing test**

Create `tests/test_ledger.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_ledger -v`
Expected: FAIL with `sqlite3.OperationalError: no such table: obligation`

- [ ] **Step 3: Write minimal implementation**

In `core/db.py`, append these two tables to `_SCHEMA_TABLES`, immediately before its closing `"""`:

```sql
CREATE TABLE IF NOT EXISTS obligation (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT NOT NULL,           -- EXPECT | ACTION
    title        TEXT DEFAULT '',
    property_id  INTEGER,                 -- nullable: portfolio-wide when NULL
    vendor_id    INTEGER,                 -- nullable: set for EXPECT
    window_rule  TEXT DEFAULT 'learned',  -- see core/periods.resolve_window
    cadence      TEXT DEFAULT 'monthly',  -- monthly|even-months|odd-months|quarterly|
                                          -- irregular|on-demand|once
    anchor       INTEGER,                 -- nullable; parity or month%3, see periods.py
    source       TEXT DEFAULT 'manual',   -- learned | manual
    confidence   TEXT DEFAULT 'high',     -- high | medium | low
    active       INTEGER DEFAULT 1,
    notes        TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS obligation_instance (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    obligation_id INTEGER NOT NULL,
    period        TEXT NOT NULL,          -- "August 2026", matching settings.month
    due_from      TEXT DEFAULT '',        -- 'YYYY-MM-DD'
    due_to        TEXT DEFAULT '',
    state         TEXT DEFAULT 'open',    -- open | done | skipped
    satisfied_by  TEXT DEFAULT '',        -- '' | 'tick' | 'invoice:<id>'
    done_at       TEXT DEFAULT '',
    note          TEXT DEFAULT '',
    UNIQUE (obligation_id, period)
);
```

Then add two indexes to `_SCHEMA_INDEXES`:

```sql
CREATE INDEX IF NOT EXISTS idx_instance_period ON obligation_instance(period);
CREATE INDEX IF NOT EXISTS idx_obligation_pair ON obligation(property_id, vendor_id);
```

> `_SCHEMA_INDEXES` runs after `_ensure_columns` in `init()` specifically so an index can reference a column the migration just added. Do not move these into `_SCHEMA_TABLES`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m unittest tests.test_ledger -v`
Expected: PASS, 6 tests

Run: `python -m unittest discover -s tests -t .`
Expected: PASS, 131 tests

- [ ] **Step 5: Commit**

```bash
git add core/db.py tests/test_ledger.py
git commit -m "feat: add obligation ledger tables"
```

---

## Task 2: Period arithmetic and window rules

**Files:**
- Create: `core/periods.py`
- Test: `tests/test_periods.py` (create)

**Interfaces:**
- Consumes: nothing. Pure module, stdlib only.
- Produces:
  - `periods.parse_period(s: str) -> tuple[int, int]` — `"August 2026"` → `(2026, 8)`; raises `ValueError` on junk.
  - `periods.format_period(year: int, month: int) -> str` — `(2026, 8)` → `"August 2026"`.
  - `periods.period_of(iso_date: str) -> str` — `"2026-08-12"` → `"August 2026"`.
  - `periods.period_bounds(period: str) -> tuple[datetime.date, datetime.date]` — first and last day, inclusive.
  - `periods.resolve_window(rule: str, period: str, due_day: int | None = None, due_spread: int | None = None) -> tuple[str, str] | None` — returns `(due_from, due_to)` as ISO strings, clamped to the period, or `None` when the rule does not apply to this period.

- [ ] **Step 1: Write the failing test**

Create `tests/test_periods.py`:

```python
# -*- coding: utf-8 -*-
"""Period and window arithmetic.

This is the part of the calendar most likely to be wrong and the cheapest to test, so it
is a pure module with no database. Every timing decision in the app resolves through here.

Run from the project root:

    python -m unittest discover -s tests -t . -v
"""
import datetime
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import periods


class ParseAndFormat(unittest.TestCase):
    def test_round_trip(self):
        self.assertEqual(periods.parse_period("August 2026"), (2026, 8))
        self.assertEqual(periods.format_period(2026, 8), "August 2026")

    def test_every_month_round_trips(self):
        for m in range(1, 13):
            with self.subTest(month=m):
                self.assertEqual(periods.parse_period(periods.format_period(2026, m)), (2026, m))

    def test_period_of_an_iso_date(self):
        self.assertEqual(periods.period_of("2026-08-12"), "August 2026")
        self.assertEqual(periods.period_of("2026-01-01"), "January 2026")

    def test_junk_raises_rather_than_guessing(self):
        for bad in ("", "Augus 2026", "2026-08", "August", None):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    periods.parse_period(bad)


class PeriodBounds(unittest.TestCase):
    def test_ordinary_month(self):
        first, last = periods.period_bounds("August 2026")
        self.assertEqual((first, last), (datetime.date(2026, 8, 1), datetime.date(2026, 8, 31)))

    def test_thirty_day_month(self):
        _, last = periods.period_bounds("September 2026")
        self.assertEqual(last, datetime.date(2026, 9, 30))

    def test_february_in_a_common_year(self):
        _, last = periods.period_bounds("February 2026")
        self.assertEqual(last, datetime.date(2026, 2, 28))

    def test_february_in_a_leap_year(self):
        _, last = periods.period_bounds("February 2028")
        self.assertEqual(last, datetime.date(2028, 2, 29))


class ResolveWindow(unittest.TestCase):
    def test_single_day(self):
        self.assertEqual(periods.resolve_window("day:1", "August 2026"),
                         ("2026-08-01", "2026-08-01"))

    def test_day_range(self):
        self.assertEqual(periods.resolve_window("day:28-30", "August 2026"),
                         ("2026-08-28", "2026-08-30"))

    def test_day_range_clamps_to_a_short_month(self):
        # 28-30 in February must not produce a 30th.
        self.assertEqual(periods.resolve_window("day:28-30", "February 2026"),
                         ("2026-02-28", "2026-02-28"))

    def test_week_is_seven_day_blocks_from_the_first(self):
        # Week 3 is the 15th-21st, which is NOT the same as "the third Monday".
        self.assertEqual(periods.resolve_window("week:3", "August 2026"),
                         ("2026-08-15", "2026-08-21"))

    def test_week_range(self):
        self.assertEqual(periods.resolve_window("week:1-2", "August 2026"),
                         ("2026-08-01", "2026-08-14"))

    def test_last_week_is_the_final_seven_days(self):
        self.assertEqual(periods.resolve_window("last-week", "August 2026"),
                         ("2026-08-25", "2026-08-31"))

    def test_last_week_in_a_short_month(self):
        self.assertEqual(periods.resolve_window("last-week", "February 2026"),
                         ("2026-02-22", "2026-02-28"))

    def test_month_end_is_the_final_day(self):
        self.assertEqual(periods.resolve_window("month-end", "August 2026"),
                         ("2026-08-31", "2026-08-31"))

    def test_learned_uses_the_profile_day_and_spread(self):
        self.assertEqual(
            periods.resolve_window("learned", "August 2026", due_day=12, due_spread=2),
            ("2026-08-10", "2026-08-14"))

    def test_learned_clamps_to_the_period(self):
        self.assertEqual(
            periods.resolve_window("learned", "August 2026", due_day=30, due_spread=5),
            ("2026-08-25", "2026-08-31"))

    def test_learned_without_a_profile_spans_the_whole_month(self):
        # No profile yet is not an error - it means "sometime this month".
        self.assertEqual(periods.resolve_window("learned", "August 2026"),
                         ("2026-08-01", "2026-08-31"))

    def test_absolute_date_inside_the_period(self):
        self.assertEqual(periods.resolve_window("date:2026-08-12", "August 2026"),
                         ("2026-08-12", "2026-08-12"))

    def test_absolute_date_outside_the_period_does_not_apply(self):
        # This is what makes a one-off reminder appear in exactly one month.
        self.assertIsNone(periods.resolve_window("date:2026-08-12", "September 2026"))

    def test_unknown_rule_raises(self):
        with self.assertRaises(ValueError):
            periods.resolve_window("phase-of-moon", "August 2026")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_periods -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'core.periods'`

- [ ] **Step 3: Write minimal implementation**

Create `core/periods.py`:

```python
# -*- coding: utf-8 -*-
"""Period and window arithmetic for the month calendar.

A "period" is a calendar month written the way settings.json already writes it:
"%B %Y", e.g. "August 2026". Everything here is a pure function - no database, no
filesystem - because this is the arithmetic every timing decision in the app depends on,
and it needs to be exhaustively testable without fixtures.

Two conventions worth stating, because both have a defensible alternative:

- A "week" is a seven-day block counted from the 1st, so week 3 is the 15th-21st. It is
  NOT "the third Monday". The SOP's calendar is written as "2nd week", "3rd week", and
  the invoices it describes do not care which weekday it is.
- Windows are always clamped to the period. "day:28-30" in February ends on the 28th
  rather than overflowing into March.
"""
import calendar
import datetime
import re
from typing import Optional, Tuple

_MONTHS = {calendar.month_name[i].lower(): i for i in range(1, 13)}


def parse_period(s: Optional[str]) -> Tuple[int, int]:
    """'August 2026' -> (2026, 8). Raises ValueError on anything else."""
    text = (s or "").strip()
    parts = text.split()
    if len(parts) != 2 or parts[0].lower() not in _MONTHS or not parts[1].isdigit():
        raise ValueError(f"not a period: {s!r}")
    return int(parts[1]), _MONTHS[parts[0].lower()]


def format_period(year: int, month: int) -> str:
    """(2026, 8) -> 'August 2026'."""
    return f"{calendar.month_name[month]} {year}"


def period_of(iso_date: str) -> str:
    """'2026-08-12' -> 'August 2026'."""
    d = datetime.date.fromisoformat(iso_date)
    return format_period(d.year, d.month)


def period_bounds(period: str) -> Tuple[datetime.date, datetime.date]:
    """First and last day of the period, inclusive."""
    year, month = parse_period(period)
    last = calendar.monthrange(year, month)[1]
    return datetime.date(year, month, 1), datetime.date(year, month, last)


def _clamp(day: int, year: int, month: int) -> int:
    last = calendar.monthrange(year, month)[1]
    return max(1, min(day, last))


def resolve_window(rule: str, period: str,
                   due_day: Optional[int] = None,
                   due_spread: Optional[int] = None) -> Optional[Tuple[str, str]]:
    """Resolve a window rule to (due_from, due_to) ISO dates inside `period`.

    Returns None when the rule does not apply to this period at all - currently only
    `date:YYYY-MM-DD` outside the period. That is what confines a one-off reminder to a
    single month without any active-flag bookkeeping.
    """
    year, month = parse_period(period)
    first, last = period_bounds(period)

    def iso(day: int) -> str:
        return datetime.date(year, month, _clamp(day, year, month)).isoformat()

    text = (rule or "").strip()

    if text.startswith("date:"):
        when = datetime.date.fromisoformat(text[5:])
        if (when.year, when.month) != (year, month):
            return None
        return when.isoformat(), when.isoformat()

    if text == "month-end":
        return last.isoformat(), last.isoformat()

    if text == "last-week":
        return iso(last.day - 6), last.isoformat()

    if text == "learned":
        if due_day is None:
            return first.isoformat(), last.isoformat()
        spread = due_spread or 0
        return iso(due_day - spread), iso(due_day + spread)

    m = re.fullmatch(r"day:(\d+)(?:-(\d+))?", text)
    if m:
        lo = int(m.group(1))
        hi = int(m.group(2)) if m.group(2) else lo
        return iso(lo), iso(hi)

    m = re.fullmatch(r"week:(\d+)(?:-(\d+))?", text)
    if m:
        lo = int(m.group(1))
        hi = int(m.group(2)) if m.group(2) else lo
        return iso((lo - 1) * 7 + 1), iso(hi * 7)

    raise ValueError(f"unknown window rule: {rule!r}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m unittest tests.test_periods -v`
Expected: PASS, 22 tests

Run: `python -m unittest discover -s tests -t .`
Expected: PASS, 153 tests

- [ ] **Step 5: Commit**

```bash
git add core/periods.py tests/test_periods.py
git commit -m "feat: add period and window-rule arithmetic"
```

---

## Task 3: Cadence classification and anchoring

**Files:**
- Modify: `core/periods.py`
- Test: `tests/test_periods.py` (extend)

**Interfaces:**
- Consumes: `periods.parse_period` (Task 2).
- Produces:
  - `periods.classify_cadence(months: list[str], recent: int = 2) -> tuple[str, int | None]` — `months` are `"YYYY-MM"` strings, any order. Returns `(cadence, anchor)`.
  - `periods.applies_to_period(cadence: str, anchor: int | None, period: str) -> bool`
  - `periods.CADENCE_GATE_OBSERVATIONS = 4`, `periods.CADENCE_GATE_SPAN_MONTHS = 4`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_periods.py`, before the `if __name__` guard:

```python
class ClassifyCadence(unittest.TestCase):
    """Cadence comes from the gaps between observations, and only recent ones decide it.

    Four of the eight pairs in the live data that meet the gate changed frequency
    mid-history, nearly all toward monthly - so whole-history classification would call
    them irregular and surface them only in the last week of the month, which is late to
    discover a missing utility bill.
    """

    def test_consecutive_months_are_monthly(self):
        self.assertEqual(periods.classify_cadence(["2026-06", "2026-07", "2026-08"]),
                         ("monthly", None))

    def test_two_observations_with_a_gap_are_irregular_not_bimonthly(self):
        # Below the gate, a 2-month gap is not enough evidence for even/odd.
        self.assertEqual(periods.classify_cadence(["2026-05", "2026-07"]),
                         ("irregular", None))

    def test_even_months_need_the_gate_and_carry_parity(self):
        self.assertEqual(
            periods.classify_cadence(["2026-02", "2026-04", "2026-06", "2026-08"]),
            ("even-months", 0))

    def test_odd_months_carry_the_other_parity(self):
        self.assertEqual(
            periods.classify_cadence(["2026-01", "2026-03", "2026-05", "2026-07"]),
            ("odd-months", 1))

    def test_quarterly_anchors_on_the_observed_month(self):
        # The real amtech elevator sequence: months 10, 1, 4, 7 - all == 1 (mod 3).
        self.assertEqual(
            periods.classify_cadence(["2025-10", "2026-01", "2026-04", "2026-07"]),
            ("quarterly", 1))

    def test_a_differently_anchored_quarterly_is_not_forced_onto_calendar_quarters(self):
        self.assertEqual(
            periods.classify_cadence(["2026-02", "2026-05", "2026-08", "2026-11"]),
            ("quarterly", 2))

    def test_recent_gaps_win_over_old_ones(self):
        # The real rolling greens sequence. Whole-history gaps are 3,2,1,1 -> irregular.
        # Recent gaps are 1,1 -> monthly, which is what it has actually been since May.
        self.assertEqual(
            periods.classify_cadence(
                ["2025-12", "2026-03", "2026-05", "2026-06", "2026-07"]),
            ("monthly", None))

    def test_a_vendor_that_went_bimonthly_to_monthly_reads_monthly(self):
        # The real mitsubishi electric sequence: gaps 2,2,1,1.
        self.assertEqual(
            periods.classify_cadence(
                ["2026-02", "2026-04", "2026-06", "2026-07", "2026-08"]),
            ("monthly", None))

    def test_genuinely_mixed_recent_gaps_are_irregular(self):
        self.assertEqual(
            periods.classify_cadence(
                ["2025-01", "2025-04", "2025-06", "2025-11", "2026-03"]),
            ("irregular", None))

    def test_below_the_gate_only_monthly_or_irregular_are_possible(self):
        # Three observations at 2-month gaps: consistent, but not yet enough.
        cadence, _ = periods.classify_cadence(["2026-02", "2026-04", "2026-06"])
        self.assertIn(cadence, ("monthly", "irregular"))
        self.assertNotEqual(cadence, "even-months")

    def test_unsorted_input_is_handled(self):
        self.assertEqual(periods.classify_cadence(["2026-08", "2026-06", "2026-07"]),
                         ("monthly", None))

    def test_duplicate_months_collapse(self):
        # Two invoices in one month is one observation for cadence purposes.
        self.assertEqual(
            periods.classify_cadence(["2026-06", "2026-06", "2026-07", "2026-08"]),
            ("monthly", None))

    def test_fewer_than_two_observations_is_irregular(self):
        self.assertEqual(periods.classify_cadence(["2026-08"]), ("irregular", None))
        self.assertEqual(periods.classify_cadence([]), ("irregular", None))

    def test_a_single_recent_gap_cannot_reclassify_a_cadence(self):
        # Gaps 3,3,3,1 - one monthly-looking interval at the end of a clean quarterly run.
        # TWO equal gaps are required, so this stays irregular instead of flipping to
        # monthly on the strength of a single interval. Without this case the window could
        # be narrowed to one gap and every other test would still pass, which would quietly
        # undo the "a repeat, not a coincidence" rule the window exists to enforce.
        self.assertEqual(
            periods.classify_cadence(
                ["2025-10", "2026-01", "2026-04", "2026-07", "2026-08"]),
            ("irregular", None))

    def test_three_month_gaps_below_the_gate_are_not_quarterly(self):
        # Two consistent 3-month gaps, but only three observations. Quarterly suppresses
        # instances in eight months of twelve, so it is the classification with the most to
        # lose from being wrong and it must not be reachable below the gate.
        self.assertEqual(
            periods.classify_cadence(["2026-01", "2026-04", "2026-07"]),
            ("irregular", None))

    def test_duplicates_do_not_inflate_the_observation_count(self):
        # Four rows, three distinct months: below the gate, so even-months is unreachable.
        # Deduplication is what the gate counts, so a pair billed twice in one month must
        # not buy its way past the evidence bar with a repeat.
        self.assertEqual(
            periods.classify_cadence(["2026-02", "2026-02", "2026-04", "2026-06"]),
            ("irregular", None))


class AppliesToPeriod(unittest.TestCase):
    def test_monthly_applies_everywhere(self):
        for p in ("July 2026", "August 2026"):
            self.assertTrue(periods.applies_to_period("monthly", None, p))

    def test_irregular_applies_everywhere(self):
        self.assertTrue(periods.applies_to_period("irregular", None, "August 2026"))

    def test_on_demand_never_applies(self):
        # This is what makes an on-demand vendor incapable of being "missing".
        for p in ("July 2026", "August 2026", "September 2026"):
            self.assertFalse(periods.applies_to_period("on-demand", None, p))

    def test_even_months(self):
        self.assertTrue(periods.applies_to_period("even-months", 0, "August 2026"))
        self.assertFalse(periods.applies_to_period("even-months", 0, "July 2026"))

    def test_odd_months(self):
        self.assertTrue(periods.applies_to_period("odd-months", 1, "July 2026"))
        self.assertFalse(periods.applies_to_period("odd-months", 1, "August 2026"))

    def test_quarterly_only_on_its_anchor(self):
        # anchor 1 -> January, April, July, October
        self.assertTrue(periods.applies_to_period("quarterly", 1, "July 2026"))
        self.assertTrue(periods.applies_to_period("quarterly", 1, "October 2026"))
        self.assertFalse(periods.applies_to_period("quarterly", 1, "August 2026"))
        self.assertFalse(periods.applies_to_period("quarterly", 1, "September 2026"))

    def test_quarterly_on_a_different_anchor(self):
        # anchor 2 -> February, May, August, November
        self.assertTrue(periods.applies_to_period("quarterly", 2, "August 2026"))
        self.assertFalse(periods.applies_to_period("quarterly", 2, "July 2026"))

    def test_once_applies_everywhere_and_lets_the_window_rule_decide(self):
        # A one-off is confined by its date: rule (Task 2), not by cadence.
        self.assertTrue(periods.applies_to_period("once", None, "August 2026"))

    def test_on_demand_is_recognised_whatever_its_casing_or_padding(self):
        # cadence is a free TEXT column and Task 12 lets a human set it. A variant spelling
        # must not fall through to the monthly default: that turns a work-order vendor into
        # a standing monthly expectation, which is the permanent false expectation 6.1
        # calls the one unacceptable outcome. An unset cadence still means monthly, because
        # that is what the column's own DEFAULT says.
        for variant in ("on-demand", "On-Demand", " ON-DEMAND ", "On-demand"):
            self.assertFalse(
                periods.applies_to_period(variant, None, "August 2026"), variant)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_periods -v`
Expected: FAIL with `AttributeError: module 'core.periods' has no attribute 'classify_cadence'`

- [ ] **Step 3: Write minimal implementation**

Append to `core/periods.py`:

```python
# A pair needs this much evidence before any non-monthly cadence is inferred. Below it,
# a pair seen in June and August is equally consistent with even-months, quarterly, and
# two unrelated jobs - so learning stays with monthly or irregular.
CADENCE_GATE_OBSERVATIONS = 4
CADENCE_GATE_SPAN_MONTHS = 4


def _month_index(ym: str) -> int:
    """'2026-08' -> an integer month index, so gaps are plain subtraction."""
    return int(ym[:4]) * 12 + int(ym[5:7])


def classify_cadence(months, recent: int = 2) -> Tuple[str, Optional[int]]:
    """Classify billing cadence from the months a pair was observed in.

    `months` are 'YYYY-MM' strings, any order, duplicates allowed. Returns
    (cadence, anchor) where anchor is the parity for even/odd-months, month % 3 for
    quarterly, and None otherwise.

    Only the most recent `recent` gaps decide the cadence. Older observations still count
    toward the gate and toward confidence, but a vendor that billed quarterly last year and
    monthly since is monthly now - classifying it over the whole record would call it
    irregular and delay its warning to the last week of the month.

    `recent` is 2 because two equal gaps in a row are the smallest thing that is a repeat
    rather than a coincidence, AND because a wider window would not fit the data: no live
    pair has more than five observations, so a 4-gap window spans every pair's entire
    history and quietly degrades into the whole-history rule this exists to replace. See
    spec 6.1 "Why the window is two and not four".
    """
    uniq = sorted({m for m in months if m})
    if len(uniq) < 2:
        return "irregular", None

    idx = [_month_index(m) for m in uniq]
    gaps = [b - a for a, b in zip(idx, idx[1:])]
    span = idx[-1] - idx[0] + 1
    gated = len(uniq) >= CADENCE_GATE_OBSERVATIONS and span >= CADENCE_GATE_SPAN_MONTHS

    recent_gaps = gaps[-recent:]
    distinct = set(recent_gaps)

    if distinct == {1}:
        return "monthly", None
    if gated and distinct == {2}:
        last_month = idx[-1] % 12 or 12
        return ("even-months", 0) if last_month % 2 == 0 else ("odd-months", 1)
    if gated and distinct == {3}:
        last_month = idx[-1] % 12 or 12
        return "quarterly", last_month % 3
    return "irregular", None


def applies_to_period(cadence: str, anchor: Optional[int], period: str) -> bool:
    """Does an obligation with this cadence generate an instance in this period?

    `on-demand` never does, which is what makes a work-order vendor incapable of showing
    up as missing. Off-anchor periods for even/odd/quarterly also generate nothing, rather
    than generating an instance that would immediately read as missing.

    The cadence is normalised first because the column is free TEXT that a human can edit
    (Task 12). Every unrecognised value falls through to True, so a variant spelling of
    `on-demand` would otherwise become a silent standing monthly expectation. Falling
    through to True is right for a genuinely absent cadence - the column's DEFAULT is
    'monthly' - but it must not be reachable by a typo.
    """
    cadence = (cadence or "").strip().lower()
    if cadence == "on-demand":
        return False
    _, month = parse_period(period)
    if cadence == "even-months":
        return month % 2 == 0
    if cadence == "odd-months":
        return month % 2 == 1
    if cadence == "quarterly":
        return anchor is not None and month % 3 == anchor
    return True          # monthly, irregular, once
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m unittest tests.test_periods -v`
Expected: PASS, 47 tests

Run: `python -m unittest discover -s tests -t .`
Expected: PASS, 178 tests

- [ ] **Step 5: Verify against the live billing history**

This is a read-only check that the classifier agrees with the real data the spec was written from.

Run:

```bash
python -c "
import sqlite3, collections
from core import dates, periods, vendor_match as vm
c = sqlite3.connect('file:data/invoices.db?mode=ro', uri=True)
pairs = collections.defaultdict(set)
for prop, vn, d in c.execute('select property, vendor_name, invoice_date from invoices'):
    iso = dates.to_iso(d)
    if iso and vn:
        pairs[(prop, vm.normalize(vn))].add(iso[:7])
for (p, v), ms in sorted(pairs.items()):
    if len(ms) < 4:
        continue
    print(f'{p[:22]:24} {v[:24]:26} {periods.classify_cadence(sorted(ms))}')
"
```

Expected, and all five must hold — these are the pairs spec §6.1 was written from:

| pair | gaps | required |
|---|---|---|
| `amtech elevator` | 3,3,3 | `('quarterly', 1)` |
| `rolling greens` | 3,2,1,1 | `('monthly', None)` |
| `mitsubishi electric` | 2,2,1,1 | `('monthly', None)` |
| `iktelecom` | 5,1,1 | `('monthly', None)` |
| `cost sign` | 2,1,1 | `('monthly', None)` |

If any of the four monthly pairs reads `irregular`, the recent-window logic is wrong — it has
collapsed back into whole-history classification. Do not adjust the tests to match; report it.

- [ ] **Step 6: Commit**

```bash
git add core/periods.py tests/test_periods.py
git commit -m "feat: classify cadence from recent observation gaps"
```

---

## Task 4: Obligation and instance queries

**Files:**
- Create: `core/ledger.py`
- Test: `tests/test_ledger.py` (extend)

**Interfaces:**
- Consumes: `db._conn_or` (exists), the tables from Task 1.
- Produces:
  - `ledger.add_obligation(conn=None, **fields) -> int` — inserts, returns the new id. Unknown keys are ignored.
  - `ledger.update_obligation(obligation_id, conn=None, **fields) -> None`
  - `ledger.get_obligation(obligation_id, conn=None) -> dict | None`
  - `ledger.active_obligations(conn=None) -> list[dict]`
  - `ledger.instances_for_period(period, conn=None) -> list[dict]` — each row joined with its obligation's fields under the same keys, plus `obligation_id`.
  - `ledger.set_instance_state(instance_id, state, note='', satisfied_by='tick', conn=None) -> None`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_ledger.py`, before the `if __name__` guard:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_ledger -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'core.ledger'`

- [ ] **Step 3: Write minimal implementation**

Create `core/ledger.py`:

```python
# -*- coding: utf-8 -*-
"""The obligation ledger: templates and their per-period instances.

An obligation is a thing that is due in a window and either happens or does not. Expected
invoices (kind EXPECT, learned from history) and reminders you write yourself (kind ACTION,
source manual) are the same shape, so they share one table, one rollover, and one rendering.

Every function takes an optional `conn` routed through db._conn_or, so the whole module is
testable against an in-memory database with no file on disk.
"""
import datetime
from typing import Optional

from . import db

OBLIGATION_COLUMNS = [
    "kind", "title", "property_id", "vendor_id", "window_rule",
    "cadence", "anchor", "source", "confidence", "active", "notes",
]

INSTANCE_COLUMNS = [
    "obligation_id", "period", "due_from", "due_to",
    "state", "satisfied_by", "done_at", "note",
]


def add_obligation(conn=None, **fields) -> int:
    """Insert an obligation. Unknown keys are ignored, the same way db.insert_invoice
    filters against INVOICE_COLUMNS."""
    cols = [c for c in OBLIGATION_COLUMNS if c in fields]
    placeholders = ",".join("?" for _ in cols)
    with db._conn_or(conn) as c:
        cur = c.execute(
            f"INSERT INTO obligation ({','.join(cols)}) VALUES ({placeholders})",
            [fields[c_] for c_ in cols])
        return cur.lastrowid


def update_obligation(obligation_id: int, conn=None, **fields) -> None:
    """Update only the columns passed. Anything else is left alone."""
    cols = [c for c in OBLIGATION_COLUMNS if c in fields]
    if not cols:
        return
    assignments = ",".join(f"{c}=?" for c in cols)
    with db._conn_or(conn) as c:
        c.execute(f"UPDATE obligation SET {assignments} WHERE id=?",
                  [fields[c_] for c_ in cols] + [obligation_id])


def get_obligation(obligation_id: int, conn=None) -> Optional[dict]:
    with db._conn_or(conn) as c:
        row = c.execute("SELECT * FROM obligation WHERE id=?", (obligation_id,)).fetchone()
    return dict(row) if row else None


def active_obligations(conn=None) -> list[dict]:
    with db._conn_or(conn) as c:
        rows = c.execute("SELECT * FROM obligation WHERE COALESCE(active,1)=1 "
                         "ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def instances_for_period(period: str, conn=None) -> list[dict]:
    """Every instance in a period, each carrying its obligation's fields.

    The join is done here rather than in the template so the page has one flat row shape
    to render and the ordering lives in SQL.
    """
    with db._conn_or(conn) as c:
        rows = c.execute(
            "SELECT i.id AS id, i.obligation_id AS obligation_id, i.period AS period, "
            "       i.due_from AS due_from, i.due_to AS due_to, i.state AS state, "
            "       i.satisfied_by AS satisfied_by, i.done_at AS done_at, i.note AS note, "
            "       o.kind AS kind, o.title AS title, o.property_id AS property_id, "
            "       o.vendor_id AS vendor_id, o.window_rule AS window_rule, "
            "       o.cadence AS cadence, o.anchor AS anchor, o.source AS source, "
            "       o.confidence AS confidence, o.notes AS notes "
            "FROM obligation_instance i JOIN obligation o ON o.id = i.obligation_id "
            "WHERE i.period = ? ORDER BY o.property_id, o.title COLLATE NOCASE",
            (period,)).fetchall()
    return [dict(r) for r in rows]


def set_instance_state(instance_id: int, state: str, note: str = "",
                       satisfied_by: str = "tick", conn=None) -> None:
    """Record an outcome on one instance. `done_at` is stamped for any terminal state so
    the page can show when you dealt with it."""
    stamp = datetime.date.today().isoformat() if state in ("done", "skipped") else ""
    with db._conn_or(conn) as c:
        c.execute(
            "UPDATE obligation_instance SET state=?, note=?, satisfied_by=?, done_at=? "
            "WHERE id=?", (state, note, satisfied_by, stamp, instance_id))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m unittest tests.test_ledger -v`
Expected: PASS, 16 tests

Run: `python -m unittest discover -s tests -t .`
Expected: PASS, 188 tests

- [ ] **Step 5: Commit**

```bash
git add core/ledger.py tests/test_ledger.py
git commit -m "feat: add obligation and instance queries"
```

---

## Task 5: Rollover

**Files:**
- Modify: `core/ledger.py`
- Test: `tests/test_ledger.py` (extend)

**Interfaces:**
- Consumes: `periods.applies_to_period`, `periods.resolve_window` (Tasks 2–3); `ledger.active_obligations` (Task 4).
- Produces: `ledger.open_period(period: str, conn=None) -> dict` — returns `{"created": int, "existing": int, "skipped": int}`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_ledger.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_ledger -v`
Expected: FAIL with `AttributeError: module 'core.ledger' has no attribute 'open_period'`

- [ ] **Step 3: Write minimal implementation**

Add to the imports at the top of `core/ledger.py`:

```python
from . import periods
```

Then append:

```python
def open_period(period: str, conn=None) -> dict:
    """Materialize a period: one instance per applicable active obligation.

    Idempotent by construction - UNIQUE (obligation_id, period) means a re-run inserts only
    what is missing and never resets state on an instance that already exists. Opening a
    month twice is a no-op, which matters because the button is easy to press twice.

    An obligation is skipped entirely when its cadence does not apply to this period
    (on-demand always; even/odd/quarterly off their anchor) or when its window rule does
    not resolve here (a one-off dated in another month). Skipping means no row at all,
    rather than a row that would immediately read as missing.
    """
    created = existing = skipped = 0
    with db._conn_or(conn) as c:
        rows = c.execute("SELECT * FROM obligation WHERE COALESCE(active,1)=1").fetchall()
        for o in rows:
            if not periods.applies_to_period(o["cadence"], o["anchor"], period):
                skipped += 1
                continue
            window = periods.resolve_window(o["window_rule"], period)
            if window is None:
                skipped += 1
                continue
            due_from, due_to = window
            cur = c.execute(
                "INSERT OR IGNORE INTO obligation_instance "
                "(obligation_id, period, due_from, due_to) VALUES (?, ?, ?, ?)",
                (o["id"], period, due_from, due_to))
            if cur.rowcount:
                created += 1
            else:
                existing += 1
    return {"created": created, "existing": existing, "skipped": skipped}
```

> `resolve_window` is called without `due_day`/`due_spread` here, so a `learned` rule spans the whole month. Task 7 fills the profile in when it syncs expectations, and Task 6's profiles are what supply those numbers.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m unittest tests.test_ledger -v`
Expected: PASS, 24 tests

Run: `python -m unittest discover -s tests -t .`
Expected: PASS, 196 tests

- [ ] **Step 5: Commit**

```bash
git add core/ledger.py tests/test_ledger.py
git commit -m "feat: add idempotent period rollover"
```

---

## Task 6: Recurrence profiles from invoice history

**Files:**
- Create: `core/expectations.py`
- Modify: `core/periods.py` (add `cadence_recently_changed`)
- Test: `tests/test_expectations.py` (create), `tests/test_periods.py` (extend)

**Interfaces:**
- Consumes: `periods.classify_cadence` (Task 3), `db._conn_or`.
- Produces: `periods.cadence_recently_changed(months, recent: int = 2) -> bool` — True when
  the gaps *before* the classification window disagree with the window's own gap, i.e. the
  pair has just shifted rhythm and the new one has not yet repeated beyond the minimum.
  Lives in `periods.py` because it is pure month arithmetic over the same gap sequence
  `classify_cadence` reads; it is added here rather than in Task 3 because Task 6 is its
  only consumer and confidence is Task 6's concern.
- Produces: `expectations.build_profiles(conn=None) -> dict[tuple[int, int], dict]` — keyed by `(property_id, vendor_id)`, each value `{"months": [...], "due_day": int, "due_spread": int, "confidence": str, "cadence": str, "anchor": int|None, "n": int}`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_expectations.py`:

```python
# -*- coding: utf-8 -*-
"""Recurrence profiles built from invoice history.

Profiles are what turn "this vendor bills this property" into "and it should have arrived
by the 7th". They read invoice_date_iso and group on vendor_id - never on the vendor
string, which is the whole reason the vendor identity layer exists, and never on
date_processed, which records when the user got to the invoice rather than when the vendor
billed.

Run from the project root:

    python -m unittest discover -s tests -t . -v
"""
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import db, expectations, periods


def make_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db._SCHEMA)
    db._ensure_columns(conn)
    conn.execute("INSERT INTO properties (id, canonical_name) VALUES (1, 'Kenmore Plaza')")
    conn.execute("INSERT INTO vendors (id, short_name) VALUES (7, 'Athens')")
    return conn


def add_invoice(conn, iso, property_id=1, vendor_id=7, day_note="",
                vendor_name="", date_processed=""):
    # Look the name up rather than hard-coding it. invoices stores the property NAME and
    # build_profiles maps that name back through the properties table, so a literal here
    # silently drops every row whose name is not registered - which is exactly what a
    # hard-coded "Other" did: property 2 was inserted as 'Solair', the invoice was written
    # as 'Other', the lookup missed, and the pair vanished from the profiles.
    name = conn.execute("SELECT canonical_name FROM properties WHERE id=?",
                        (property_id,)).fetchone()["canonical_name"]
    conn.execute(
        "INSERT INTO invoices (property, vendor_id, invoice_date, invoice_date_iso, "
        "vendor_name, date_processed) VALUES (?, ?, ?, ?, ?, ?)",
        (name, vendor_id, day_note or iso, iso, vendor_name, date_processed))


class BuildProfiles(unittest.TestCase):
    def test_a_pair_seen_in_two_months_gets_a_profile(self):
        conn = make_db()
        add_invoice(conn, "2026-06-05")
        add_invoice(conn, "2026-07-06")
        profiles = expectations.build_profiles(conn=conn)
        self.assertIn((1, 7), profiles)

    def test_a_pair_seen_once_is_not_a_profile(self):
        conn = make_db()
        add_invoice(conn, "2026-07-06")
        self.assertEqual(expectations.build_profiles(conn=conn), {})

    def test_due_day_is_the_median_and_spread_is_the_half_width(self):
        # due_spread is a HALF-WIDTH, not the range: periods.resolve_window applies it as
        # due_day - spread .. due_day + spread. Days 4, 6, 8 have a range of 4, so the
        # half-width is 2 and the window is exactly 4..8.
        conn = make_db()
        for iso in ("2026-05-04", "2026-06-06", "2026-07-08"):
            add_invoice(conn, iso)
        p = expectations.build_profiles(conn=conn)[(1, 7)]
        self.assertEqual(p["due_day"], 6)
        self.assertEqual(p["due_spread"], 2)
        self.assertEqual(
            periods.resolve_window("learned", "August 2026",
                                   due_day=p["due_day"], due_spread=p["due_spread"]),
            ("2026-08-04", "2026-08-08"))

    def test_one_vendor_id_with_two_name_spellings_is_one_profile(self):
        # The first rule this module exists for. Both rows are the same vendor, so this is
        # one pair with two observations. Grouping on the string would make it two pairs of
        # one month each, and a single observation is not a profile at all - so the wrong
        # grouping produces NOTHING here rather than something subtly off.
        conn = make_db()
        add_invoice(conn, "2026-06-05", vendor_name="Athens Services")
        add_invoice(conn, "2026-07-05", vendor_name="ATHENS SERVICES, INC.")
        profiles = expectations.build_profiles(conn=conn)
        self.assertEqual(set(profiles), {(1, 7)})
        self.assertEqual(profiles[(1, 7)]["n"], 2)

    def test_the_day_comes_from_the_invoice_date_not_the_processing_date(self):
        # The second rule. date_processed is when the user got to the invoice - 15 days of
        # spread across the live history against invoice_date's 2 - so reading it would
        # learn the user's batching habit and report it as the vendor's schedule. Both
        # columns are populated here, because a fixture that leaves date_processed empty
        # cannot tell "reads the right column" from "reads a blank one".
        conn = make_db()
        add_invoice(conn, "2026-06-05", date_processed="2026-06-25")
        add_invoice(conn, "2026-07-05", date_processed="2026-07-27")
        p = expectations.build_profiles(conn=conn)[(1, 7)]
        self.assertEqual(p["due_day"], 5)
        self.assertEqual(p["due_spread"], 0)

    def test_confidence_high_needs_three_observations_and_a_tight_spread(self):
        conn = make_db()
        for iso in ("2026-05-05", "2026-06-06", "2026-07-05"):
            add_invoice(conn, iso)
        self.assertEqual(expectations.build_profiles(conn=conn)[(1, 7)]["confidence"], "high")

    def test_confidence_medium_when_the_spread_is_wider(self):
        conn = make_db()
        for iso in ("2026-05-02", "2026-06-09", "2026-07-05"):
            add_invoice(conn, iso)
        self.assertEqual(expectations.build_profiles(conn=conn)[(1, 7)]["confidence"], "medium")

    def test_confidence_low_with_only_two_observations(self):
        conn = make_db()
        add_invoice(conn, "2026-06-05")
        add_invoice(conn, "2026-07-06")
        self.assertEqual(expectations.build_profiles(conn=conn)[(1, 7)]["confidence"], "low")

    def test_cadence_comes_from_the_classifier(self):
        conn = make_db()
        for iso in ("2025-10-20", "2026-01-20", "2026-04-20", "2026-07-20"):
            add_invoice(conn, iso)
        p = expectations.build_profiles(conn=conn)[(1, 7)]
        self.assertEqual((p["cadence"], p["anchor"]), ("quarterly", 1))

    def test_rows_without_a_parsed_date_are_ignored(self):
        # An unparseable date is in the Fixer queue, not evidence of a billing schedule.
        conn = make_db()
        add_invoice(conn, "2026-06-05")
        conn.execute("INSERT INTO invoices (property, vendor_id, invoice_date, "
                     "invoice_date_iso) VALUES ('Kenmore Plaza', 7, '06262026', '')")
        p = expectations.build_profiles(conn=conn)
        self.assertEqual(p, {})     # only one usable observation left

    def test_rows_without_a_vendor_id_are_ignored(self):
        conn = make_db()
        conn.execute("INSERT INTO invoices (property, invoice_date_iso) "
                     "VALUES ('Kenmore Plaza', '2026-06-05')")
        conn.execute("INSERT INTO invoices (property, invoice_date_iso) "
                     "VALUES ('Kenmore Plaza', '2026-07-05')")
        self.assertEqual(expectations.build_profiles(conn=conn), {})

    def test_two_invoices_in_one_month_are_one_observation_for_cadence(self):
        conn = make_db()
        add_invoice(conn, "2026-06-05")
        add_invoice(conn, "2026-06-20")
        add_invoice(conn, "2026-07-05")
        p = expectations.build_profiles(conn=conn)[(1, 7)]
        self.assertEqual(p["cadence"], "monthly")

    def test_different_properties_are_different_profiles(self):
        conn = make_db()
        conn.execute("INSERT INTO properties (id, canonical_name) VALUES (2, 'Solair')")
        add_invoice(conn, "2026-06-05", property_id=1)
        add_invoice(conn, "2026-07-05", property_id=1)
        add_invoice(conn, "2026-06-20", property_id=2)
        add_invoice(conn, "2026-07-20", property_id=2)
        profiles = expectations.build_profiles(conn=conn)
        self.assertEqual(set(profiles), {(1, 7), (2, 7)})

    def test_a_freshly_changed_cadence_cannot_be_high_confidence(self):
        # The amtech failure: a clean quarterly contract billed on the 5th every time,
        # plus two consecutive repair invoices also on the 5th. Day spread is 0, n is 6,
        # so the spread rule alone says "high" - and the cadence now reads monthly off
        # two intervals. High + monthly means a 2-day slack under 6.3 and a missing-bill
        # warning in the eight months a year this pair was never going to bill.
        conn = make_db()
        for iso in ("2025-10-05", "2026-01-05", "2026-04-05",
                    "2026-07-05", "2026-08-05", "2026-09-05"):
            add_invoice(conn, iso)
        p = expectations.build_profiles(conn=conn)[(1, 7)]
        self.assertEqual(p["cadence"], "monthly")
        self.assertEqual(p["confidence"], "medium")


if __name__ == "__main__":
    unittest.main()
```

Append to `tests/test_periods.py`, before the `if __name__` guard:

```python
class CadenceRecentlyChanged(unittest.TestCase):
    """Confidence must not survive a change of rhythm.

    classify_cadence reads two gaps, so a pair that has just shifted is classified on two
    intervals of evidence. Day-of-month spread cannot see this - a vendor can bill on the
    3rd every single time while changing how often it bills - so confidence has to be told
    separately.
    """

    def test_a_steady_monthly_rhythm_has_not_changed(self):
        self.assertFalse(periods.cadence_recently_changed(
            ["2026-01", "2026-02", "2026-03", "2026-04"]))

    def test_a_clean_quarterly_run_has_not_changed(self):
        self.assertFalse(periods.cadence_recently_changed(
            ["2025-10", "2026-01", "2026-04", "2026-07"]))

    def test_a_shift_to_monthly_is_a_change(self):
        # rolling greens, gaps 3,2,1,1: the monthly reading rests on the last two gaps.
        self.assertTrue(periods.cadence_recently_changed(
            ["2025-12", "2026-03", "2026-05", "2026-06", "2026-07"]))

    def test_two_strays_beside_a_quarterly_contract_are_a_change(self):
        # amtech (3,3,3) plus two consecutive repair invoices reads monthly. This is the
        # case the cap exists for.
        self.assertTrue(periods.cadence_recently_changed(
            ["2025-10", "2026-01", "2026-04", "2026-07", "2026-08", "2026-09"]))

    def test_too_little_history_to_have_changed(self):
        # Nothing before the window to disagree with it.
        self.assertFalse(periods.cadence_recently_changed(["2026-07", "2026-08"]))
        self.assertFalse(periods.cadence_recently_changed(
            ["2026-06", "2026-07", "2026-08"]))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_expectations -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'core.expectations'`

- [ ] **Step 3: Write minimal implementation**

First append to `core/periods.py`:

```python
def cadence_recently_changed(months, recent: int = 2) -> bool:
    """Has this pair's rhythm shifted inside the window that decides its cadence?

    True when the gaps BEFORE the classification window disagree with the window's own
    gap - that is, the current cadence rests on the minimum evidence and the pair used to
    behave differently. A pair with one steady rhythm all the way back returns False and
    keeps whatever confidence its date spread earned.

    `recent` must match the value classify_cadence used, or this describes a window that
    was not the one classified.
    """
    uniq = sorted({m for m in months if m})
    if len(uniq) < 2:
        return False
    idx = [_month_index(m) for m in uniq]
    gaps = [b - a for a, b in zip(idx, idx[1:])]
    window, earlier = gaps[-recent:], gaps[:-recent]
    if not earlier or len(set(window)) != 1:
        # Nothing older to disagree with, or the window itself is mixed - in which case
        # classify_cadence already returned irregular and there is no confidence to cap.
        return False
    return set(earlier) != set(window)
```

Then create `core/expectations.py`:

```python
# -*- coding: utf-8 -*-
"""Learning what should arrive, from what has arrived before.

A profile is built per (property_id, vendor_id) pair from that pair's invoice history:
when in the month it usually bills, how tightly, how often, and how much to trust that.

Two rules this module exists to enforce:

- Group on vendor_id, never on the vendor string. Without it 'Athens Services' and
  'ATHENS SERVICES' are two half-confident profiles that each look sporadic.
- Read invoice_date_iso, never date_processed. The former is when the vendor billed
  (2-day median spread across the live history); the latter is when the user got to it
  (15 days), and is the user's batching habit rather than the vendor's schedule.
"""
import collections
import statistics
from typing import Optional

from . import db, periods

# A pair must be seen in at least this many distinct months before it is a profile at all.
MIN_MONTHS = 2


def _property_ids(conn) -> dict:
    """canonical_name -> id. Invoices store the property name, not its id."""
    return {r["canonical_name"]: r["id"]
            for r in conn.execute("SELECT id, canonical_name FROM properties")}


def build_profiles(conn=None) -> dict:
    """Recurrence profiles keyed by (property_id, vendor_id)."""
    with db._conn_or(conn) as c:
        by_name = _property_ids(c)
        rows = c.execute(
            "SELECT property, vendor_id, invoice_date_iso FROM invoices "
            "WHERE vendor_id IS NOT NULL AND COALESCE(invoice_date_iso,'') <> ''"
        ).fetchall()

    seen = collections.defaultdict(list)
    for r in rows:
        pid = by_name.get(r["property"])
        if pid is None:
            continue                      # a property that is not in the canonical list
        seen[(pid, r["vendor_id"])].append(r["invoice_date_iso"])

    profiles = {}
    for key, isos in seen.items():
        months = sorted({iso[:7] for iso in isos})
        if len(months) < MIN_MONTHS:
            continue
        days = [int(iso[8:10]) for iso in isos]
        # due_spread is stored as a HALF-WIDTH because periods.resolve_window applies it as
        # due_day - spread .. due_day + spread. Confidence, though, reads the FULL range:
        # days 2/9/5 span 7 and must read medium, but their half-width of 3 would read high.
        day_range = max(days) - min(days)
        spread = day_range // 2
        n = len(months)
        if n >= 3 and day_range <= 3:
            confidence = "high"
        elif n >= 3 and day_range <= 10:
            confidence = "medium"
        else:
            confidence = "low"
        cadence, anchor = periods.classify_cadence(months)
        if periods.cadence_recently_changed(months):
            # The cadence is decided by the last two gaps, so a pair that has just shifted
            # is classified on two intervals of evidence. Day-of-month spread does not see
            # that - a vendor can bill on the 3rd every time while changing how OFTEN it
            # bills - so without this a fresh shift can read "high" and buy a 2-day slack
            # under 6.3. That is the cry-wolf direction: it flags missing in months the
            # pair was never going to bill. Cap at medium until the new rhythm repeats.
            confidence = "low" if confidence == "low" else "medium"
        profiles[key] = {
            "months": months,
            "n": n,
            "due_day": int(statistics.median(days)),
            "due_spread": spread,
            "confidence": confidence,
            "cadence": cadence,
            "anchor": anchor,
        }
    return profiles
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m unittest tests.test_expectations -v`
Expected: PASS, 14 tests

Run: `python -m unittest tests.test_periods -v`
Expected: PASS, 52 tests

Run: `python -m unittest discover -s tests -t .`
Expected: PASS, 215 tests

- [ ] **Step 5: Commit**

```bash
git add core/expectations.py core/periods.py tests/test_expectations.py tests/test_periods.py
git commit -m "feat: build recurrence profiles from invoice history"
```

---

## Task 7: Sync learned expectations

**Files:**
- Modify: `core/expectations.py`
- Test: `tests/test_expectations.py` (extend)

**Interfaces:**
- Consumes: `expectations.build_profiles` (Task 6), `ledger.add_obligation` / `update_obligation` / `active_obligations` (Task 4).
- Produces: `expectations.sync(conn=None) -> dict` — returns `{"created": int, "updated": int, "pinned": int}`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_expectations.py`:

```python
from core import ledger


class Sync(unittest.TestCase):
    def _pair(self, conn, isos):
        for iso in isos:
            add_invoice(conn, iso)

    def test_creates_one_expect_obligation_per_profile(self):
        conn = make_db()
        self._pair(conn, ["2026-06-05", "2026-07-06"])
        result = expectations.sync(conn=conn)
        self.assertEqual(result["created"], 1)
        obs = ledger.active_obligations(conn=conn)
        self.assertEqual(len(obs), 1)
        self.assertEqual((obs[0]["kind"], obs[0]["source"]), ("EXPECT", "learned"))
        self.assertEqual((obs[0]["property_id"], obs[0]["vendor_id"]), (1, 7))

    def test_is_idempotent(self):
        conn = make_db()
        self._pair(conn, ["2026-06-05", "2026-07-06"])
        expectations.sync(conn=conn)
        again = expectations.sync(conn=conn)
        self.assertEqual(again["created"], 0)
        self.assertEqual(len(ledger.active_obligations(conn=conn)), 1)

    def test_updates_a_learned_obligation_when_the_profile_moves(self):
        conn = make_db()
        self._pair(conn, ["2026-05-05", "2026-06-05"])
        expectations.sync(conn=conn)
        add_invoice(conn, "2026-07-05")            # now 3 observations, tight
        expectations.sync(conn=conn)
        ob = ledger.active_obligations(conn=conn)[0]
        self.assertEqual(ob["confidence"], "high")

    def test_never_overwrites_an_obligation_you_edited(self):
        # A manual edit pins the obligation. Recompute must not revert a decision.
        conn = make_db()
        self._pair(conn, ["2026-06-05", "2026-07-06"])
        expectations.sync(conn=conn)
        ob = ledger.active_obligations(conn=conn)[0]
        ledger.update_obligation(ob["id"], conn=conn, cadence="on-demand", source="manual")

        result = expectations.sync(conn=conn)
        self.assertEqual(result["pinned"], 1)
        after = ledger.get_obligation(ob["id"], conn=conn)
        self.assertEqual(after["cadence"], "on-demand")
        self.assertEqual(after["source"], "manual")

    def test_a_new_pair_starts_unconfirmed(self):
        # Promotion must not silently create a confident monthly expectation - a repair
        # vendor that happens to bill twice would then nag every month afterwards.
        conn = make_db()
        self._pair(conn, ["2026-06-05", "2026-07-06"])
        expectations.sync(conn=conn)
        ob = ledger.active_obligations(conn=conn)[0]
        self.assertEqual(ob["notes"], expectations.UNCONFIRMED)

    def test_confirming_clears_the_unconfirmed_marker(self):
        conn = make_db()
        self._pair(conn, ["2026-06-05", "2026-07-06"])
        expectations.sync(conn=conn)
        ob = ledger.active_obligations(conn=conn)[0]
        ledger.update_obligation(ob["id"], conn=conn, notes="", source="manual")
        after = ledger.get_obligation(ob["id"], conn=conn)
        self.assertEqual(after["notes"], "")

    def test_sync_does_not_re_mark_an_expectation_you_confirmed(self):
        # Clearing the marker is how you say "yes, this really does recur". Sync still has
        # to refresh the learned numbers afterwards, so the risk is that it puts the marker
        # back on its way past and sends the obligation round the confirmation loop again -
        # which would make confirming pointless and is the cry-wolf direction 6.6 exists to
        # prevent. Note source stays 'learned' here: this is the UPDATE path, not the
        # pinned path that test_never_overwrites_an_obligation_you_edited covers.
        conn = make_db()
        self._pair(conn, ["2026-06-05", "2026-07-06"])
        expectations.sync(conn=conn)
        ob = ledger.active_obligations(conn=conn)[0]
        ledger.update_obligation(ob["id"], conn=conn, notes="")

        add_invoice(conn, "2026-08-05")
        expectations.sync(conn=conn)
        after = ledger.get_obligation(ob["id"], conn=conn)
        self.assertEqual(after["notes"], "")
        self.assertEqual(after["confidence"], "high")     # refreshed, not frozen

    def test_one_vendor_at_two_properties_stays_two_obligations(self):
        # A vendor billing several properties is the normal case in this data, not an edge
        # one, so `existing` has to be keyed on the PAIR. Keyed on vendor_id alone the two
        # rows collapse, only the last is reachable, and the other property's obligation is
        # created once and then never refreshed - it silently freezes at whatever it was on
        # the day it was promoted. The SECOND sync is what exposes this: on the first,
        # `existing` is empty and both pairs take the create path whatever the key is.
        conn = make_db()
        conn.execute("INSERT INTO properties (id, canonical_name) VALUES (2, 'Solair')")
        for iso in ("2026-05-05", "2026-06-05"):
            add_invoice(conn, iso, property_id=1)
        for iso in ("2025-10-20", "2026-01-20", "2026-04-20", "2026-07-20"):
            add_invoice(conn, iso, property_id=2)
        expectations.sync(conn=conn)

        add_invoice(conn, "2026-07-05", property_id=1)   # property 1 now n=3 and tight
        expectations.sync(conn=conn)

        by_property = {o["property_id"]: o for o in ledger.active_obligations(conn=conn)}
        self.assertEqual(set(by_property), {1, 2})
        self.assertEqual(by_property[1]["cadence"], "monthly")
        self.assertEqual(by_property[1]["confidence"], "high")    # refreshed, not frozen
        self.assertEqual(by_property[2]["cadence"], "quarterly")

    def test_the_obligation_carries_the_learned_window(self):
        conn = make_db()
        self._pair(conn, ["2026-05-05", "2026-06-06", "2026-07-05"])
        expectations.sync(conn=conn)
        ob = ledger.active_obligations(conn=conn)[0]
        self.assertEqual(ob["window_rule"], "learned")
        self.assertEqual(ob["cadence"], "monthly")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_expectations -v`
Expected: FAIL with `AttributeError: module 'core.expectations' has no attribute 'sync'`

- [ ] **Step 3: Write minimal implementation**

Append to `core/expectations.py`, and add `from . import ledger` to its imports:

```python
# Marker on a freshly-promoted obligation, until you say whether it is really recurring.
# Task 9 excludes anything carrying it from being flagged missing.
UNCONFIRMED = "unconfirmed"


def sync(conn=None) -> dict:
    """Create or refresh learned EXPECT obligations from the current profiles.

    An obligation whose source is 'manual' is pinned: you edited it, so recompute leaves it
    entirely alone. Learned values only ever overwrite values nobody has touched.

    A newly created obligation is marked UNCONFIRMED. Promotion is cheap and wrong guesses
    are common - roughly half the vendor/property pairs in the live history bill only when
    work is done - so a new expectation does not get to raise a warning until a human has
    said it is real.
    """
    profiles = build_profiles(conn=conn)
    created = updated = pinned = 0

    with db._conn_or(conn) as c:
        existing = {}
        for r in c.execute(
                "SELECT * FROM obligation WHERE kind='EXPECT' "
                "AND property_id IS NOT NULL AND vendor_id IS NOT NULL"):
            existing[(r["property_id"], r["vendor_id"])] = dict(r)

    for key, p in profiles.items():
        fields = {
            "cadence": p["cadence"],
            "anchor": p["anchor"],
            "confidence": p["confidence"],
        }
        current = existing.get(key)
        if current is None:
            add_obligation_id = ledger.add_obligation(
                conn=conn, kind="EXPECT", property_id=key[0], vendor_id=key[1],
                window_rule="learned", source="learned", notes=UNCONFIRMED,
                title="", **fields)
            created += 1
            continue
        if current.get("source") == "manual":
            pinned += 1
            continue
        ledger.update_obligation(current["id"], conn=conn, **fields)
        updated += 1

    return {"created": created, "updated": updated, "pinned": pinned}
```

> `title` is left empty on a learned expectation. The Month page renders the vendor's name from `vendor_id`, so storing it here would be a second copy that drifts when a vendor is renamed.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m unittest tests.test_expectations -v`
Expected: PASS, 23 tests

Run: `python -m unittest discover -s tests -t .`
Expected: PASS, 224 tests

- [ ] **Step 5: Commit**

```bash
git add core/expectations.py tests/test_expectations.py
git commit -m "feat: sync learned expectations, pinning manual edits"
```

---

## Task 8: Evidence satisfaction

**Files:**
- Modify: `core/expectations.py`
- Test: `tests/test_expectations.py` (extend)

**Interfaces:**
- Consumes: `ledger.instances_for_period` (Task 4), `periods.period_of` (Task 2).
- Produces: `expectations.satisfy_period(period: str, conn=None) -> int` — number of instances newly satisfied.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_expectations.py`:

```python
class SatisfyPeriod(unittest.TestCase):
    def _expect(self, conn):
        add_invoice(conn, "2026-06-05")
        add_invoice(conn, "2026-07-05")
        expectations.sync(conn=conn)
        ledger.open_period("August 2026", conn=conn)

    def test_an_arriving_invoice_satisfies_its_instance(self):
        conn = make_db()
        self._expect(conn)
        add_invoice(conn, "2026-08-05")
        self.assertEqual(expectations.satisfy_period("August 2026", conn=conn), 1)
        inst = ledger.instances_for_period("August 2026", conn=conn)[0]
        self.assertEqual(inst["state"], "done")
        self.assertTrue(inst["satisfied_by"].startswith("invoice:"))

    def test_an_invoice_in_another_month_does_not_satisfy(self):
        conn = make_db()
        self._expect(conn)
        add_invoice(conn, "2026-09-05")
        self.assertEqual(expectations.satisfy_period("August 2026", conn=conn), 0)
        self.assertEqual(
            ledger.instances_for_period("August 2026", conn=conn)[0]["state"], "open")

    def test_an_invoice_for_another_vendor_does_not_satisfy(self):
        conn = make_db()
        self._expect(conn)
        conn.execute("INSERT INTO vendors (id, short_name) VALUES (8, 'Other')")
        add_invoice(conn, "2026-08-05", vendor_id=8)
        self.assertEqual(expectations.satisfy_period("August 2026", conn=conn), 0)

    def test_the_same_vendor_at_another_property_does_not_satisfy(self):
        # Athens bills a dozen properties in the live data, so matching on vendor alone is
        # not a hypothetical mistake. It would let one property's invoice close another
        # property's expectation, and the month page would then show a bill as ARRIVED that
        # never came - a false negative on the one thing this feature exists to catch. The
        # default fixture has a single property, so without this case a matcher keyed on
        # vendor_id alone passes the entire suite.
        conn = make_db()
        self._expect(conn)
        conn.execute("INSERT INTO properties (id, canonical_name) VALUES (2, 'Solair')")
        add_invoice(conn, "2026-08-05", property_id=2)
        self.assertEqual(expectations.satisfy_period("August 2026", conn=conn), 0)
        self.assertEqual(
            ledger.instances_for_period("August 2026", conn=conn)[0]["state"], "open")

    def test_it_is_idempotent(self):
        conn = make_db()
        self._expect(conn)
        add_invoice(conn, "2026-08-05")
        expectations.satisfy_period("August 2026", conn=conn)
        self.assertEqual(expectations.satisfy_period("August 2026", conn=conn), 0)

    def test_a_second_invoice_does_not_double_satisfy(self):
        conn = make_db()
        self._expect(conn)
        add_invoice(conn, "2026-08-05")
        add_invoice(conn, "2026-08-19")
        self.assertEqual(expectations.satisfy_period("August 2026", conn=conn), 1)

    def test_evidence_overrides_a_skip(self):
        # You said it was not coming; it came. The invoice wins, and the note is kept.
        conn = make_db()
        self._expect(conn)
        inst = ledger.instances_for_period("August 2026", conn=conn)[0]
        ledger.set_instance_state(inst["id"], "skipped", note="vendor said none this month",
                                  satisfied_by="", conn=conn)
        add_invoice(conn, "2026-08-05")
        self.assertEqual(expectations.satisfy_period("August 2026", conn=conn), 1)
        after = ledger.instances_for_period("August 2026", conn=conn)[0]
        self.assertEqual(after["state"], "done")
        self.assertEqual(after["note"], "vendor said none this month")

    def test_a_reminder_is_never_auto_satisfied(self):
        # ACTION instances are yours to tick; no invoice can close them.
        #
        # The reminder is ATTACHED to the same property and vendor as the invoice, which is
        # what makes this test about the kind filter at all. Left unattached it carries a
        # (None, None) key that matches no invoice under any implementation, so it would
        # pass whether or not satisfy_period filters on kind - and 5 supports attaching a
        # reminder to a property and vendor, so the attached case is the one that can go
        # wrong. Without the filter an arriving Athens invoice silently ticks "call
        # Michelle" for you.
        conn = make_db()
        ledger.add_obligation(conn=conn, kind="ACTION", title="call Michelle",
                              property_id=1, vendor_id=7,
                              window_rule="day:12", cadence="monthly")
        ledger.open_period("August 2026", conn=conn)
        add_invoice(conn, "2026-08-12")
        self.assertEqual(expectations.satisfy_period("August 2026", conn=conn), 0)
        inst = ledger.instances_for_period("August 2026", conn=conn)[0]
        self.assertEqual(inst["state"], "open")
        self.assertEqual(inst["satisfied_by"], "")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_expectations -v`
Expected: FAIL with `AttributeError: module 'core.expectations' has no attribute 'satisfy_period'`

- [ ] **Step 3: Write minimal implementation**

Append to `core/expectations.py`:

```python
def satisfy_period(period: str, conn=None) -> int:
    """Close every EXPECT instance in `period` that has a matching invoice.

    Matching is (property_id, vendor_id) plus an invoice_date_iso inside the period. One
    invoice satisfies one instance; a second invoice from the same vendor in the same month
    is left alone rather than silently double-counted - genuine duplicates are already the
    processor's job.

    Evidence beats a skip. If you marked something as not coming and it then arrives, the
    instance flips to done and your note is preserved, because the note is still the record
    of what you believed at the time.
    """
    satisfied = 0
    with db._conn_or(conn) as c:
        by_name = _property_ids(c)
        invoices = collections.defaultdict(list)
        for r in c.execute(
                "SELECT id, property, vendor_id, invoice_date_iso FROM invoices "
                "WHERE vendor_id IS NOT NULL AND COALESCE(invoice_date_iso,'') <> ''"):
            pid = by_name.get(r["property"])
            if pid is None:
                continue
            if periods.period_of(r["invoice_date_iso"]) == period:
                invoices[(pid, r["vendor_id"])].append(r["id"])

        rows = c.execute(
            "SELECT i.id AS id, o.kind AS kind, o.property_id AS pid, o.vendor_id AS vid, "
            "       i.satisfied_by AS satisfied_by "
            "FROM obligation_instance i JOIN obligation o ON o.id = i.obligation_id "
            "WHERE i.period = ? AND o.kind = 'EXPECT'", (period,)).fetchall()

        for inst in rows:
            if inst["satisfied_by"]:
                continue                      # already carries evidence
            ids = invoices.get((inst["pid"], inst["vid"]))
            if not ids:
                continue
            c.execute(
                "UPDATE obligation_instance SET state='done', satisfied_by=?, done_at=? "
                "WHERE id=?",
                (f"invoice:{ids[0]}", datetime.date.today().isoformat(), inst["id"]))
            satisfied += 1
    return satisfied
```

Add `import datetime` to the imports at the top of `core/expectations.py`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m unittest tests.test_expectations -v`
Expected: PASS, 31 tests

Run: `python -m unittest discover -s tests -t .`
Expected: PASS, 232 tests

- [ ] **Step 5: Commit**

```bash
git add core/expectations.py tests/test_expectations.py
git commit -m "feat: satisfy expectation instances from arriving invoices"
```

---

## Task 9: Missing eligibility

**Files:**
- Modify: `core/ledger.py`
- Test: `tests/test_ledger.py` (extend)

**Interfaces:**
- Consumes: `periods.period_bounds` (Task 2), `expectations.UNCONFIRMED` (Task 7).
- Produces:
  - `ledger.SLACK_DAYS = {"high": 2, "medium": 7, "low": None}` — `None` means "last week of the period only".
  - `ledger.is_missing(instance: dict, today: datetime.date) -> bool` — `instance` is a row from `instances_for_period`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_ledger.py`:

```python
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

    def test_a_reminder_uses_its_window_with_no_slack(self):
        # You set the date yourself, so there is no learned uncertainty to allow for.
        inst = self._inst(kind="ACTION", confidence="high", due_to="2026-08-12")
        self.assertFalse(ledger.is_missing(inst, datetime.date(2026, 8, 12)))
        self.assertTrue(ledger.is_missing(inst, datetime.date(2026, 8, 13)))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_ledger -v`
Expected: FAIL with `AttributeError: module 'core.ledger' has no attribute 'is_missing'`

- [ ] **Step 3: Write minimal implementation**

Append to `core/ledger.py`:

```python
# Days past the due window before an EXPECT is treated as missing. None means "do not
# surface until the last week of the period" - the right answer when the schedule itself
# is only loosely known, because flagging on a guessed date is noise.
SLACK_DAYS = {"high": 2, "medium": 7, "low": None}

# Set by expectations.sync on a freshly promoted pair. Mirrored here rather than imported
# to keep ledger free of a dependency on expectations, which depends on ledger.
UNCONFIRMED = "unconfirmed"


def is_missing(instance: dict, today: datetime.date) -> bool:
    """Is this instance late enough to be worth flagging?

    Only open, unsatisfied instances can be missing. A reminder (ACTION) uses its own
    window with no slack, because you chose the date. A learned expectation gets slack
    scaled to how well its schedule is actually known.
    """
    if instance.get("state") != "open" or instance.get("satisfied_by"):
        return False
    if (instance.get("notes") or "") == UNCONFIRMED:
        return False

    due_to = instance.get("due_to") or ""
    if not due_to:
        return False
    due = datetime.date.fromisoformat(due_to)

    if instance.get("kind") == "ACTION":
        return today > due

    if instance.get("cadence") == "irregular":
        slack = None
    else:
        slack = SLACK_DAYS.get(instance.get("confidence") or "low", None)

    if slack is None:
        _, last = periods.period_bounds(instance["period"])
        return today >= last - datetime.timedelta(days=6)
    return today > due + datetime.timedelta(days=slack)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m unittest tests.test_ledger -v`
Expected: PASS, 34 tests

Run: `python -m unittest discover -s tests -t .`
Expected: PASS, 242 tests

- [ ] **Step 5: Commit**

```bash
git add core/ledger.py tests/test_ledger.py
git commit -m "feat: add confidence-gated missing eligibility"
```

---

## Task 10: The Month page and its row actions

**Files:**
- Create: `templates/month.html`
- Modify: `app.py` (add the `month` route near `fixer`, around line 437), `templates/base.html` (nav)
- Test: `tests/test_app.py` (extend)

**Interfaces:**
- Consumes: `ledger.instances_for_period`, `ledger.is_missing`, `ledger.open_period`, `expectations.satisfy_period`, `db.all_properties`, `db.all_vendors`, `state.load_settings`.
- Produces: route `GET /month` (optional `?period=`), route `POST /month/open`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_app.py`, before the `if __name__` guard:

```python
class MonthPage(unittest.TestCase):
    """The Month page groups instances by property and marks the late ones."""

    def setUp(self):
        _conn.execute("DELETE FROM obligation_instance")
        _conn.execute("DELETE FROM obligation")
        _conn.execute("DELETE FROM properties")
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
```

```python
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
        from core import ledger
        resp = self.client.post("/month/instance/9999/done")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(len(ledger.instances_for_period("August 2026")), 1)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_app -v`
Expected: FAIL with a 404 — the `/month` route does not exist.

- [ ] **Step 3: Add the route**

In `app.py`, immediately before the `@app.route("/fixer")` definition, add:

```python
@app.route("/month")
def month_page():
    """Everything due in one period, grouped by property.

    Reads a period from the query string so you can look back at a closed month; falls back
    to the configured period.

    Satisfaction runs on every render, so an invoice processed since you last looked shows
    as arrived without you having to press anything. That does mean a GET writes, which is
    normally worth avoiding - it is a deliberate call here. The app is single-user on
    127.0.0.1, _reject_cross_site already refuses requests that did not come from its own
    pages, and satisfy_period is idempotent, so a repeated or prefetched request changes
    nothing. The alternative - only syncing on an explicit button - leaves the page showing
    an invoice as missing hours after it was processed, which is the kind of stale screen
    that stops being trusted.
    """
    from core import expectations, ledger, periods
    settings = state.load_settings()
    period = (request.args.get("period") or settings["month"]).strip()
    try:
        periods.parse_period(period)
    except ValueError:
        flash(f"Could not read '{period}' as a month.")
        return redirect(url_for("month_page"))

    expectations.satisfy_period(period)

    today = datetime.date.today()
    prop_names = {p["id"]: p["canonical_name"] for p in db.all_properties()}
    vendor_names = {v["id"]: (v.get("canonical_name") or v["short_name"])
                    for v in db.all_vendors()}

    groups = {}
    for inst in ledger.instances_for_period(period):
        inst["missing"] = ledger.is_missing(inst, today)
        inst["label"] = inst["title"] or vendor_names.get(inst["vendor_id"], "(vendor)")
        group = prop_names.get(inst["property_id"], "All properties")
        groups.setdefault(group, []).append(inst)

    return render_template("month.html",
                           period=period,
                           groups=sorted(groups.items()),
                           months=state.month_options(),
                           missing_count=sum(1 for g in groups.values()
                                             for i in g if i["missing"]))


@app.route("/month/open", methods=["POST"])
def month_open():
    """Materialize a period. Idempotent, so pressing it twice is harmless.

    Learning runs first, then rollover. Order matters: expectations.sync creates or
    refreshes the EXPECT obligations from current invoice history, and open_period turns
    obligations into instances — so syncing second would leave a newly learned expectation
    with no instance until the next time you opened a month.

    This is the only place recompute is triggered in this scope. Spec section 8 also lists
    a vendor merge and an explicit Recompute action; both belong with the Expected page,
    which is not part of this plan.
    """
    from core import expectations, ledger, periods
    period = (request.form.get("period") or "").strip()
    try:
        periods.parse_period(period)
    except ValueError:
        flash("Pick a month to open.")
        return redirect(url_for("month_page"))
    learned = expectations.sync()
    result = ledger.open_period(period)
    flash(f"Opened {period}: {result['created']} new, {result['existing']} already there. "
          f"Learned {learned['created']} new expectation"
          f"{'' if learned['created'] == 1 else 's'}.")
    return redirect(url_for("month_page", period=period))
```

Add `import datetime` to `app.py`'s imports if it is not already there.

- [ ] **Step 4: Add the template and the row-action handlers**

Create `templates/month.html`:

```html
{% extends "base.html" %}
{% block title %}{{ period }} — Invoice Processor{% endblock %}
{% block page_title %}{{ period }}{% endblock %}
{% block page_subtitle %}What's expected this month, and what hasn't turned up{% endblock %}
{% block content %}
<div class="page">

<section class="panel">
  <div class="panel-head">
    <div class="panel-title">Month</div>
    <div class="panel-note">
      {% if missing_count %}{{ missing_count }} possibly missing{% else %}nothing overdue{% endif %}
    </div>
  </div>
  <form method="get" action="{{ url_for('month_page') }}" class="inline-form">
    <select name="period">
      {% for m in months %}
        <option value="{{ m }}" {% if m == period %}selected{% endif %}>{{ m }}</option>
      {% endfor %}
    </select>
    <button class="btn" type="submit">View</button>
  </form>
  <form method="post" action="{{ url_for('month_open') }}" class="inline-form">
    <input type="hidden" name="period" value="{{ period }}">
    <button class="btn" type="submit">Open {{ period }}</button>
  </form>
</section>

{% if not groups %}
<section class="panel">
  <p class="muted">
    Nothing scheduled for {{ period }}. Either the month hasn't been opened yet — use
    <strong>Open {{ period }}</strong> above — or no expectations have been learned. Learned
    expectations need <code>vendor_id</code> and <code>invoice_date_iso</code> populated; see
    the backfill steps in the README.
  </p>
</section>
{% endif %}

{% for group, rows in groups %}
<section class="panel">
  <div class="panel-head">
    <div class="panel-title">{{ group }}</div>
    <div class="panel-note">{{ rows|length }} item{{ '' if rows|length == 1 else 's' }}</div>
  </div>
  <table class="props">
    <thead>
      <tr><th>What</th><th>Window</th><th>Source</th><th>Status</th><th></th></tr>
    </thead>
    <tbody>
    {% for r in rows %}
      <tr class="{% if r.missing %}attn{% elif r.state != 'open' %}dim{% endif %}">
        <td>{{ r.label }}</td>
        <td class="date">{{ r.due_from }}{% if r.due_to != r.due_from %} – {{ r.due_to }}{% endif %}</td>
        <td>
          {%- if r.source == 'manual' %}you added this
          {%- elif r.notes == 'unconfirmed' %}new — confirm it
          {%- else %}learned, {{ r.confidence }} confidence{% endif -%}
        </td>
        <td>
          {%- if r.satisfied_by.startswith('invoice:') %}arrived
          {%- elif r.state == 'done' %}done {{ r.done_at }}
          {%- elif r.state == 'skipped' %}skipped — {{ r.note }}
          {%- elif r.missing %}<strong>possibly missing</strong>
          {%- else %}not yet due{% endif -%}
        </td>
        <td>
          {% if r.state == 'open' %}
          <form method="post" action="{{ url_for('instance_done', instance_id=r.id) }}">
            <button class="btn" type="submit">Done</button>
          </form>
          <form method="post" action="{{ url_for('instance_skip', instance_id=r.id) }}">
            <input type="text" name="note" placeholder="why?" required>
            <button class="btn" type="submit">Skip</button>
          </form>
          {% endif %}
        </td>
      </tr>
    {% endfor %}
    </tbody>
  </table>
</section>
{% endfor %}

</div>
{% endblock %}
```

Then both row-action handlers the template links to, so the task commits nothing
provisional:

```python
def _instance_or_redirect(instance_id):
    """Look an instance up, or flash and hand back None. Both row actions need this, and
    acting on a deleted row must not 500."""
    from core import db as _db
    with _db._connect() as conn:
        row = conn.execute("SELECT * FROM obligation_instance WHERE id=?",
                           (instance_id,)).fetchone()
    if row is None:
        flash("That item no longer exists.")
        return None
    return dict(row)


@app.route("/month/instance/<int:instance_id>/done", methods=["POST"])
def instance_done(instance_id):
    """Tick a row off by hand. Expected invoices normally close themselves when the invoice
    lands; this is for reminders and for the cases evidence cannot see."""
    from core import ledger
    if _instance_or_redirect(instance_id) is None:
        return redirect(url_for("month_page"))
    ledger.set_instance_state(instance_id, "done")
    return redirect(request.referrer or url_for("month_page"))


@app.route("/month/instance/<int:instance_id>/skip", methods=["POST"])
def instance_skip(instance_id):
    """Dismiss a row for this period, with a reason.

    The reason is required. A silent dismissal is indistinguishable from a mis-click when
    you come back to the month later, and the whole value of a dismissal is that it tells
    the next reader why the gap was fine.
    """
    from core import ledger
    note = (request.form.get("note") or "").strip()
    if not note:
        flash("Say why you're skipping it — that note is the whole point.")
        return redirect(request.referrer or url_for("month_page"))
    if _instance_or_redirect(instance_id) is None:
        return redirect(url_for("month_page"))
    ledger.set_instance_state(instance_id, "skipped", note=note, satisfied_by="")
    return redirect(request.referrer or url_for("month_page"))
```

- [ ] **Step 5: Add the nav item**

In `templates/base.html`, after the Invoices `<a class="nav-item" ...>` block and before Month-end, add:

```html
      <a class="nav-item{{ ' active' if request.endpoint in ('month_page',) }}" href="{{ url_for('month_page') }}">
        <svg width="18" height="18" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4.5" width="14" height="12" rx="1.6"/><line x1="3" y1="8" x2="17" y2="8"/><line x1="7" y1="2.5" x2="7" y2="5.5"/><line x1="13" y1="2.5" x2="13" y2="5.5"/><circle cx="7" cy="12" r="1" fill="currentColor" stroke="none"/></svg>
        <span class="nav-label">Month</span>
      </a>
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `python -m unittest tests.test_app -v`
Expected: PASS

Run: `python -m unittest discover -s tests -t .`
Expected: PASS, 252 tests

- [ ] **Step 7: Commit**

```bash
git add app.py templates/month.html templates/base.html tests/test_app.py
git commit -m "feat: add the Month page with row actions"
```

---

## Task 11: Adding reminders

**Files:**
- Modify: `app.py`, `templates/month.html`
- Test: `tests/test_app.py` (extend)

**Interfaces:**
- Consumes: `ledger.add_obligation` (Task 4), `ledger.open_period` (Task 5), `periods.parse_period` (Task 2).
- Produces: route `POST /month/reminder`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_app.py`:

```python
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
        self.client.post("/month/reminder", data={
            "title": "Call Michelle", "kind": "monthly", "window_rule": "day:12",
            "period": "August 2026"})
        html = self.client.get("/month?period=August+2026").get_data(as_text=True)
        self.assertIn("Call Michelle", html)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_app -v`
Expected: FAIL with a 404 — `/month/reminder` does not exist.

- [ ] **Step 3: Add the route**

In `app.py`, after `instance_skip`, add:

```python
@app.route("/month/reminder", methods=["POST"])
def add_reminder():
    """Create a reminder — a manual obligation.

    Validated fully before anything is written: a rejected reminder must leave the ledger
    untouched. A one-off carries an absolute date rule, which is what confines it to a
    single period without any active-flag bookkeeping.
    """
    from core import ledger, periods
    title = (request.form.get("title") or "").strip()
    kind = (request.form.get("kind") or "monthly").strip()
    period = (request.form.get("period") or "").strip()

    if not title:
        flash("Give the reminder a title.")
        return redirect(request.referrer or url_for("month_page"))

    if kind == "once":
        on_date = (request.form.get("on_date") or "").strip()
        try:
            datetime.date.fromisoformat(on_date)
        except ValueError:
            flash("Pick a date for a one-off reminder.")
            return redirect(request.referrer or url_for("month_page"))
        window_rule, cadence = f"date:{on_date}", "once"
    else:
        window_rule, cadence = (request.form.get("window_rule") or "").strip(), "monthly"
        try:
            periods.resolve_window(window_rule, period or "August 2026")
        except ValueError:
            flash(f"Could not read '{window_rule}' as a schedule.")
            return redirect(request.referrer or url_for("month_page"))

    prop = (request.form.get("property_id") or "").strip()
    vend = (request.form.get("vendor_id") or "").strip()

    ledger.add_obligation(
        kind="ACTION", title=title, window_rule=window_rule, cadence=cadence,
        source="manual", confidence="high",
        property_id=int(prop) if prop.isdecimal() else None,
        vendor_id=int(vend) if vend.isdecimal() else None)

    if period:
        try:
            periods.parse_period(period)
            ledger.open_period(period)     # so it shows up straight away
        except ValueError:
            pass
    flash(f"Added: {title}")
    return redirect(request.referrer or url_for("month_page"))
```

- [ ] **Step 4: Add the form to the template**

In `templates/month.html`, immediately before the `{% if not groups %}` block, add:

```html
<section class="panel">
  <div class="panel-head"><div class="panel-title">Add a reminder</div></div>
  <form method="post" action="{{ url_for('add_reminder') }}">
    <input type="hidden" name="period" value="{{ period }}">
    <input type="text" name="title" placeholder="What do you need to remember?" required>
    <select name="kind">
      <option value="monthly">Every month</option>
      <option value="once">Just once</option>
    </select>
    <select name="window_rule">
      <option value="day:1">1st</option>
      <option value="week:2">2nd week</option>
      <option value="week:3">3rd week</option>
      <option value="last-week">Last week</option>
      <option value="month-end">Month end</option>
    </select>
    <input type="date" name="on_date" title="for a one-off">
    <select name="property_id">
      <option value="">All properties</option>
      {% for p in properties %}<option value="{{ p.id }}">{{ p.canonical_name }}</option>{% endfor %}
    </select>
    <button class="btn primary" type="submit">Add</button>
  </form>
  <p class="muted">Pick a date for a one-off; the schedule dropdown is used for monthly ones.</p>
</section>
```

Add `properties=db.all_properties()` to `month_page`'s `render_template(...)` call.

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m unittest discover -s tests -t .`
Expected: PASS, 259 tests

- [ ] **Step 6: Commit**

```bash
git add app.py templates/month.html tests/test_app.py
git commit -m "feat: add reminders, one-off and recurring"
```

---

## Task 12: Cadence editing, on-demand, and the promotion choice

**Files:**
- Modify: `app.py`, `templates/month.html`
- Test: `tests/test_app.py` (extend)

**Interfaces:**
- Consumes: `ledger.update_obligation`, `ledger.get_obligation` (Task 4).
- Produces: route `POST /month/obligation/<id>/edit`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_app.py`:

```python
class EditObligation(unittest.TestCase):
    def setUp(self):
        _conn.execute("DELETE FROM obligation_instance")
        _conn.execute("DELETE FROM obligation")
        _conn.execute("DELETE FROM vendors")
        _conn.execute("INSERT INTO vendors (id, short_name) VALUES (7, 'LADWP')")
        self.client = app.app.test_client()
        from core import ledger
        self.oid = ledger.add_obligation(kind="EXPECT", vendor_id=7, property_id=1,
                                         window_rule="learned", cadence="monthly",
                                         source="learned", confidence="low",
                                         notes="unconfirmed")

    def test_setting_a_cadence_pins_the_obligation(self):
        from core import ledger
        self.client.post(f"/month/obligation/{self.oid}/edit",
                         data={"cadence": "even-months"})
        ob = ledger.get_obligation(self.oid)
        self.assertEqual(ob["cadence"], "even-months")
        self.assertEqual(ob["source"], "manual")
        self.assertEqual(ob["anchor"], 0)

    def test_odd_months_gets_the_other_anchor(self):
        from core import ledger
        self.client.post(f"/month/obligation/{self.oid}/edit", data={"cadence": "odd-months"})
        self.assertEqual(ledger.get_obligation(self.oid)["anchor"], 1)

    def test_editing_clears_the_unconfirmed_marker(self):
        from core import ledger
        self.client.post(f"/month/obligation/{self.oid}/edit", data={"cadence": "monthly"})
        self.assertEqual(ledger.get_obligation(self.oid)["notes"], "")

    def test_marking_on_demand_stops_it_generating_instances(self):
        from core import ledger
        self.client.post(f"/month/obligation/{self.oid}/edit", data={"cadence": "on-demand"})
        self.assertEqual(ledger.open_period("September 2026")["created"], 0)

    def test_quarterly_requires_an_anchor_and_takes_it_from_the_period(self):
        from core import ledger
        self.client.post(f"/month/obligation/{self.oid}/edit",
                         data={"cadence": "quarterly", "period": "August 2026"})
        ob = ledger.get_obligation(self.oid)
        self.assertEqual((ob["cadence"], ob["anchor"]), ("quarterly", 2))

    def test_apply_to_every_property_updates_the_vendors_other_pairs(self):
        from core import ledger
        other = ledger.add_obligation(kind="EXPECT", vendor_id=7, property_id=2,
                                      window_rule="learned", cadence="monthly",
                                      source="learned")
        self.client.post(f"/month/obligation/{self.oid}/edit",
                         data={"cadence": "on-demand", "everywhere": "1"})
        self.assertEqual(ledger.get_obligation(other)["cadence"], "on-demand")

    def test_an_unknown_cadence_is_rejected_and_writes_nothing(self):
        from core import ledger
        self.client.post(f"/month/obligation/{self.oid}/edit", data={"cadence": "whenever"})
        self.assertEqual(ledger.get_obligation(self.oid)["cadence"], "monthly")

    def test_a_missing_obligation_is_rejected_rather_than_crashing(self):
        resp = self.client.post("/month/obligation/9999/edit", data={"cadence": "monthly"})
        self.assertEqual(resp.status_code, 302)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_app -v`
Expected: FAIL with a 404.

- [ ] **Step 3: Add the route**

In `app.py`, after `add_reminder`, add:

```python
VALID_CADENCES = ("monthly", "even-months", "odd-months", "quarterly",
                  "irregular", "on-demand")


@app.route("/month/obligation/<int:obligation_id>/edit", methods=["POST"])
def edit_obligation(obligation_id):
    """Change how often something is expected.

    Setting a cadence by hand pins the obligation: source becomes 'manual' and the learned
    recompute leaves it alone from then on. That is the intended path for a vendor whose
    schedule you already know from the SOP — LADWP bills some properties on even months and
    one on odd, and you should not have to wait for the system to infer it.

    Anchors are derived, never asked for: parity for even/odd, and the viewed period's month
    modulo 3 for quarterly, so a vendor billing February/May/August is not forced onto
    calendar quarters.
    """
    from core import ledger, periods
    cadence = (request.form.get("cadence") or "").strip()
    if cadence not in VALID_CADENCES:
        flash("Pick a schedule.")
        return redirect(request.referrer or url_for("month_page"))
    if ledger.get_obligation(obligation_id) is None:
        flash("That item no longer exists.")
        return redirect(url_for("month_page"))

    anchor = None
    if cadence == "even-months":
        anchor = 0
    elif cadence == "odd-months":
        anchor = 1
    elif cadence == "quarterly":
        period = (request.form.get("period") or "").strip()
        try:
            _, month = periods.parse_period(period)
        except ValueError:
            month = datetime.date.today().month
        anchor = month % 3

    fields = {"cadence": cadence, "anchor": anchor, "source": "manual", "notes": ""}
    ledger.update_obligation(obligation_id, **fields)

    if request.form.get("everywhere"):
        target = ledger.get_obligation(obligation_id)
        if target and target["vendor_id"]:
            from core import db as _db
            with _db._connect() as conn:
                ids = [r["id"] for r in conn.execute(
                    "SELECT id FROM obligation WHERE vendor_id=? AND id<>?",
                    (target["vendor_id"], obligation_id))]
            for other in ids:
                ledger.update_obligation(other, **fields)
            flash(f"Applied to {len(ids)} other propert{'y' if len(ids) == 1 else 'ies'}.")

    return redirect(request.referrer or url_for("month_page"))
```

- [ ] **Step 4: Add the control to the template**

In `templates/month.html`, inside the row loop's `Source` cell, append this form beneath the existing text:

```html
          <form method="post" action="{{ url_for('edit_obligation', obligation_id=r.obligation_id) }}">
            <input type="hidden" name="period" value="{{ period }}">
            <select name="cadence">
              {% for c in ('monthly', 'even-months', 'odd-months', 'quarterly', 'irregular', 'on-demand') %}
                <option value="{{ c }}" {% if c == r.cadence %}selected{% endif %}>{{ c }}</option>
              {% endfor %}
            </select>
            {% if r.vendor_id %}
            <label class="muted"><input type="checkbox" name="everywhere" value="1"> everywhere</label>
            {% endif %}
            <button class="btn" type="submit">Set</button>
          </form>
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m unittest discover -s tests -t .`
Expected: PASS, 267 tests

- [ ] **Step 6: Commit**

```bash
git add app.py templates/month.html tests/test_app.py
git commit -m "feat: edit cadence, mark vendors on-demand, confirm promotions"
```

---

## Task 13: Show the parsed date on the invoices list

**Files:**
- Modify: `templates/invoices.html:116`
- Test: `tests/test_app.py` (extend)

**Interfaces:**
- Consumes: `invoices.invoice_date_iso` (already populated by the processor).
- Produces: nothing new. Display change only.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_app.py`:

```python
class InvoiceDateDisplay(unittest.TestCase):
    """The parse already happens; the list just never showed it. A page rendering
    '2026-06-03 00:00:00' beside 'Jun 2, 2026' beside '6/11/26' is unreadable."""

    def setUp(self):
        _conn.execute("DELETE FROM invoices")
        self.client = app.app.test_client()

    def test_the_parsed_date_is_shown_and_the_raw_one_is_kept(self):
        _conn.execute(
            "INSERT INTO invoices (vendor_name, property, invoice_date, invoice_date_iso) "
            "VALUES ('Athens', 'Kenmore Plaza', '13-Jul-26', '2026-07-13')")
        html = self.client.get("/invoices").get_data(as_text=True)
        self.assertIn("2026-07-13", html)
        self.assertIn('title="13-Jul-26"', html)

    def test_an_unparsed_date_still_renders_and_is_marked(self):
        _conn.execute(
            "INSERT INTO invoices (vendor_name, property, invoice_date, invoice_date_iso) "
            "VALUES ('Athens', 'Kenmore Plaza', '06262026', '')")
        html = self.client.get("/invoices").get_data(as_text=True)
        self.assertIn("06262026", html)
        self.assertIn("needs review", html)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_app -v`
Expected: FAIL — `title="13-Jul-26"` is not present; the cell renders the raw string only.

- [ ] **Step 3: Change the cell**

In `templates/invoices.html`, replace line 116:

```html
      <td class="date">{{ inv.invoice_date }}</td>
```

with:

```html
      {# The parsed date is what every calculation uses, so it is what the list shows.
         The raw string the vendor printed stays one hover away, so provenance is never
         lost - and an unparsed date is called out rather than shown as an empty cell. #}
      <td class="date" title="{{ inv.invoice_date }}">
        {%- if inv.invoice_date_iso -%}
          {{ inv.invoice_date_iso }}
        {%- else -%}
          {{ inv.invoice_date }} <span class="muted">needs review</span>
        {%- endif -%}
      </td>
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m unittest discover -s tests -t .`
Expected: PASS, 269 tests

- [ ] **Step 5: Update the README**

In `README.md`, under **Daily use**, add after the existing Invoices paragraph:

```markdown
**Month** — everything expected in one month, grouped by property: invoices that normally
arrive and haven't yet, plus reminders you've written for yourself. Expectations are learned
from your own billing history, so a vendor that bills on the 5th is flagged around the 7th
while one that wanders across the month stays quiet until the last week. Vendors that bill
only when work is done can be marked **on-demand** and are never flagged.
```

- [ ] **Step 6: Commit**

```bash
git add templates/invoices.html README.md tests/test_app.py
git commit -m "feat: show the parsed invoice date in the list"
```

---

## Done criteria

- `python -m unittest discover -s tests -t .` passes, 269 tests.
- The Month page lists expected invoices and reminders grouped by property, marks late ones, and states plainly when a period is empty rather than rendering blank.
- A reminder can be added as one-off or recurring, optionally attached to a property.
- A vendor can be marked on-demand and then never appears as missing.
- Cadence can be set by hand, pins against recompute, and can be applied to a vendor's other properties in one action.
- Opening a period twice creates nothing the second time.
- The invoices list shows parsed dates with the raw string on hover.
- No test reads or writes `data/invoices.db`.

## What this plan does not do

Deferred to the parent spec, all additive on the same ledger:

- The workbook import, the six day-1 information requests, check runs, rent posting.
- Statement persistence and autopay verification (parent §4.3, §4.5).
- Carry-forward between periods (parent §4.6).
- Amount checking — this answers "did it arrive", not "was it right".
- The Today page. The Month page is a checklist you browse; the warnings surface is separate.
