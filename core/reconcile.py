# -*- coding: utf-8 -*-
"""
reconcile.py - apply the Bank Rec assembler's `_matched.csv` files back to the database.

    python -m core.reconcile "<assembler output dir>" --month "May 2026"

Stamps `reconciled` for invoices the assembler placed with HIGH or GROUPED confidence; med /
low / unplaced invoices are held back for human review (never auto-reconciled, so a wrong match
can't silently leave the pending pool). Then refreshes the per-property amount sidecars so
reconciled invoices drop out of next month's staging - even if the processor isn't re-run first.
"""

import csv
import glob
import argparse
from pathlib import Path

from . import processor as ip          # pure helpers (_normalize_key)
from . import db

AUTO_CONF = {"high", "grouped"}         # confidence levels safe to auto-reconcile


def read_matched(matched_dir):
    """
    Scan every '* - matched.csv' in matched_dir. Keyed on (normalized_property, stored_file_lower)
    because a filename like SoCalGas_06_2026.pdf is only unique WITHIN a property - every property
    has one, so matching on the filename alone cross-contaminates properties. Returns:
      reconciled  = {(prop, file): period}  for invoice rows placed high/grouped,
      outstanding = {(prop, file): period}  for invoice rows that did NOT clear this month, and
      review      = [(stored_file, confidence, property), ...]  for the console review list.
    """
    reconciled, outstanding, review = {}, {}, []
    for path in sorted(glob.glob(str(Path(matched_dir) / "* - matched.csv"))):
        with open(path, newline="", encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                if (row.get("kind") or "").strip() != "invoice":
                    continue                       # slips aren't part of the pending pool
                sf     = (row.get("stored_file") or "").strip()
                prop   = (row.get("property") or "").strip()
                conf   = (row.get("confidence") or "").strip()
                period = (row.get("period") or "").strip()
                if not sf:
                    continue
                k = (ip._normalize_key(prop), sf.lower())
                if conf in AUTO_CONF and (row.get("matched") or "").strip() == "yes":
                    reconciled[k] = period
                else:
                    outstanding[k] = period        # staged this month but didn't clear
                    review.append((sf, conf or "?", prop))
    return reconciled, outstanding, review


def apply_reconciliation(reconciled, outstanding, default_month):
    """
    Stamp `reconciled` on DB rows that cleared (high/grouped), and `carried_forward` on rows
    that were in this month's rec but did NOT clear - so they're visibly held for next month
    (they also stay in the pending pool and re-stage on their own). Never overwrites an existing
    reconciled mark. Matching mirrors the original: (normalized property, stored_file lower).
    Returns (marked, already_marked, orphans, carried).
    """
    marked = already = carried = 0
    seen = set()
    for inv in db.list_invoices():
        if inv["status"] == "DUPLICATE":
            continue
        sf = str(inv["stored_file"] or "").strip().lower()
        if not sf:
            continue
        k = (ip._normalize_key(str(inv["property"] or "")), sf)   # (property, file)
        already_reconciled = bool(str(inv["reconciled"] or "").strip())
        if k in reconciled:
            seen.add(k)
            if already_reconciled:                 # never overwrite an existing mark
                already += 1
                continue
            period = reconciled[k] or default_month
            if not period:
                continue                           # no month known -> leave for a --month re-run
            db.update_invoice(inv["id"], reconciled=period, carried_forward="")
            marked += 1
        elif k in outstanding and not already_reconciled:
            db.update_invoice(inv["id"], carried_forward=(outstanding[k] or default_month))
            carried += 1
    return marked, already, set(reconciled) - seen, carried


def main():
    ap = argparse.ArgumentParser(description="Apply assembler _matched.csv back to the database")
    ap.add_argument("matched_dir", help="folder holding the assembler's '* - matched.csv' files")
    ap.add_argument("--month", default="", help='period stamped when a CSV omits one, e.g. "May 2026"')
    args = ap.parse_args()

    db.init()
    reconciled, outstanding, review = read_matched(args.matched_dir)
    if not reconciled and not review:
        print("[*] No invoice rows in any '* - matched.csv'. Nothing to do.")
        return

    marked, already, orphans, carried = apply_reconciliation(reconciled, outstanding, args.month)

    # Refresh the pending-pool sidecars, so reconciled invoices drop out of next month's staging.
    sidecars = db.export_amount_sidecars(ip.PROCESSED_FOLDER)

    print(f"[*] Reconciled {marked} invoice(s); {already} already marked; "
          f"{carried} carried forward to next month; {sidecars} sidecar(s) refreshed.")
    if orphans:
        shown = ", ".join(sf for _p, sf in sorted(orphans)[:8]) + ("..." if len(orphans) > 8 else "")
        print(f"  [!] {len(orphans)} matched file(s) had no row in the log (manual add / rename?): {shown}")
    if review:
        print(f"  [CARRIED FORWARD] {len(review)} invoice(s) didn't clear this month - flagged in the "
              f"'Carried Forward' column and re-staged into next month's rec:")
        for key, conf, prop in review[:20]:
            print(f"      {conf:8} {prop:22} {key}")


if __name__ == "__main__":
    main()
