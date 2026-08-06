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
