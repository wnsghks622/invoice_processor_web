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
