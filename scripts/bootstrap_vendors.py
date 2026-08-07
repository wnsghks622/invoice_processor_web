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
