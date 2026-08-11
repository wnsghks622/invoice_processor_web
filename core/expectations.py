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
