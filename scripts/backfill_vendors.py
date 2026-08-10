# -*- coding: utf-8 -*-
"""One-shot backfill of invoices.vendor_id from the raw vendor_name text.

Third and last of the post-merge scripts. `bootstrap_vendors.py` creates the vendor rows;
nothing in it binds them to invoices, so without this step every invoice is left with
`vendor_id IS NULL` *and* `vendor_needs_review = 0` - unbound and not queued, which is the
one combination that looks healthy from every screen while nothing is actually bound.

Dry run by default - prints what it would change and writes nothing:

    python scripts/backfill_vendors.py

Apply:

    python scripts/backfill_vendors.py --apply

Run it after bootstrap_vendors.py, so there is a vendor list to match against.

Safe to re-run. Rows that already have a vendor_id are left alone, so a binding a human
confirmed on the Fixer page is never re-litigated; a row that is still unbound is re-matched
against the current vendor list, which is what makes re-running after adding vendors useful.
"""
import argparse
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import db, vendor_match as vm

# What to do with one invoice. Returned by decide() so the decision is testable without a
# database - the same split bootstrap_vendors.py uses for its own pure helpers.
SKIP, BIND, FLAG = "skip", "bind", "flag"


def decide(invoice: dict, vendors: list) -> tuple[str, Optional[vm.MatchResult]]:
    """Choose this row's outcome: SKIP it, BIND it, or FLAG it for human review.

    Only a confident match binds. Anything less - a 'suggest' inside the confidence band,
    or no candidate at all - is flagged rather than bound *or* silently left alone: an
    unbound row that is not queued never reaches the Fixer page's vendor panel, so the
    work of resolving it would simply never be offered to anyone.

    A 'suggest' outcome deliberately does not write its candidate vendor_id. vendor_id
    means "this invoice is bound to this vendor"; a guess stored there would make the row
    look bound to every reader that checks the column rather than the flag. The Fixer page
    recomputes the suggestion with vendor_match.match() when it renders, so nothing the
    reviewer needs is lost by leaving it out.
    """
    if invoice.get("vendor_id") is not None:
        return SKIP, None
    result = vm.match(invoice.get("vendor_name"), vendors)
    fields = vm.record_fields(result)
    if fields["vendor_id"] is not None and not fields["vendor_needs_review"]:
        return BIND, result
    return FLAG, result


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    args = ap.parse_args()

    db.init()
    vendors = db.all_vendors()          # read once; the loop below must not re-query per row
    rows = db.list_invoices()
    bound = flagged = skipped = 0
    queued = []

    for row in rows:
        action, result = decide(row, vendors)
        if action == SKIP:
            skipped += 1
            continue
        if action == BIND:
            if args.apply:
                db.set_invoice_vendor(row["id"], result.vendor_id)
            bound += 1
            continue
        if args.apply:
            db.update_invoice(row["id"], vendor_needs_review=1)
        flagged += 1
        queued.append((row["id"], row.get("vendor_name"), result))

    verb = "bound" if args.apply else "would bind"
    verb2 = "queued" if args.apply else "would queue"
    print(f"{len(rows)} invoices: {verb} {bound}, {verb2} for review {flagged}, "
          f"already bound {skipped}")
    if not vendors:
        print("\nNo vendors exist yet - run scripts/bootstrap_vendors.py first, or every "
              "invoice lands in the review queue.")
    if queued:
        print("\nQueued - these reach the Fixer page's vendor panel:")
        for inv_id, name, result in queued:
            print(f"   id={inv_id:<5} {str(name)!r:<40} {result.reason} {result.score:.2f}")
    if not args.apply:
        print("\nDry run. Re-run with --apply to write.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
