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
import datetime
import statistics
from typing import Optional

from . import db, ledger, periods

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



def payment_lag_months(conn=None) -> dict:
    """(property_id, vendor_id) -> whole months between when a pair bills and the month its
    invoices actually clear in. Only pairs that really run late are returned; a pair that
    clears in its own month is absent, which reads the same as a lag of zero.

    Some bills are drafted a month after they are issued - Spectrum mails at the end of July
    and autopay takes it in mid-August - and where such a vendor charges the SAME amount every
    month, amount and vendor evidence cannot say which of two invoices a bank line belongs to.
    The gap between invoice_date_iso and `reconciled` already records the answer, one row per
    bill that has been through a rec, so it is read rather than configured.

    Same two rules as build_profiles: group on vendor_id, and read invoice_date_iso rather
    than date_processed. MIN_MONTHS distinct billing months are required before a gap counts
    as a schedule instead of a coincidence, and a negative gap - reconciled before the invoice
    was even dated - is a data error rather than a rhythm, so it is dropped.
    """
    with db._conn_or(conn) as c:
        by_name = _property_ids(c)
        rows = c.execute(
            "SELECT property, vendor_id, invoice_date_iso, reconciled FROM invoices "
            "WHERE vendor_id IS NOT NULL AND COALESCE(invoice_date_iso,'') <> '' "
            "AND COALESCE(reconciled,'') <> ''"
        ).fetchall()

    seen = collections.defaultdict(list)
    for r in rows:
        pid = by_name.get(r["property"])
        if pid is None:
            continue                      # a property that is not in the canonical list
        try:
            year, month = periods.parse_period(r["reconciled"])
        except ValueError:
            continue                      # not a period string - nothing to measure against
        iso = r["invoice_date_iso"]
        billed = int(iso[:4]) * 12 + int(iso[5:7])
        seen[(pid, r["vendor_id"])].append((iso[:7], year * 12 + month - billed))

    lags = {}
    for key, pairs in seen.items():
        if len({ym for ym, _gap in pairs}) < MIN_MONTHS:
            continue
        lag = int(statistics.median([gap for _ym, gap in pairs]))
        if lag >= 1:
            lags[key] = lag
    return lags

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
        # Two groups, because a manual edit pins one of them and not the other. `shape` is
        # how OFTEN a pair bills - that is what a human overrides when they know the
        # schedule better than the history does. `learned` is how WELL and WHEN it is
        # known, which no override should freeze.
        shape = {"cadence": p["cadence"], "anchor": p["anchor"]}
        learned = {"confidence": p["confidence"],
                   "due_day": p["due_day"], "due_spread": p["due_spread"]}
        current = existing.get(key)
        if current is None:
            ledger.add_obligation(
                conn=conn, kind="EXPECT", property_id=key[0], vendor_id=key[1],
                window_rule="learned", source="learned", notes=UNCONFIRMED,
                title="", **shape, **learned)
            created += 1
            continue
        if current.get("source") == "manual":
            # Pinned means "I have told you the CADENCE". It does not mean "stop learning
            # when this vendor bills": freezing that kept every confirmed pair at the
            # confidence it had on promotion day, and a new pair has two observations, so
            # it is `low` - which surfaces only in the last week, if at all.
            ledger.update_obligation(current["id"], conn=conn, **learned)
            pinned += 1
            continue
        ledger.update_obligation(current["id"], conn=conn, **shape, **learned)
        updated += 1

    return {"created": created, "updated": updated, "pinned": pinned}


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
