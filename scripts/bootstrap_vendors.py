# -*- coding: utf-8 -*-
"""One-shot bootstrap of the vendor list from the raw strings already in the invoice table.

Dry run by default - prints the clusters and writes nothing:

    python scripts/bootstrap_vendors.py

Apply (creates one vendor per cluster, with every raw spelling as an alias):

    python scripts/bootstrap_vendors.py --apply

Safe to re-run: a cluster is skipped if any spelling in it already identifies a known
vendor, not only its most-frequent (canonical) spelling.
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


def _unique_short_name(canonical: str, used: set) -> str:
    """_short_name(canonical), disambiguated against short names already taken.

    vendors.short_name is NOT NULL UNIQUE, but _short_name() only looks at one cluster
    at a time - it has no way to know that, say, 'Black Shadow III', 'Black Jack
    Market', and 'Black Water Operations' are three different real vendors that all
    reduce to 'Black'. cluster() correctly keeps them as three separate clusters; this
    is what stops that correct decision from crashing the insert. `used` is mutated in
    place so later collisions in the same run see earlier picks.
    """
    base = _short_name(canonical)
    candidate, n = base, 2
    while candidate.strip().lower() in used:
        candidate = f"{base}{n}"
        n += 1
    used.add(candidate.strip().lower())
    return candidate


def _known_identities(vendors: list[dict]) -> set:
    """Every string that already identifies some vendor: canonical name, short name, and
    aliases. A cluster matching any of these is the same vendor under a new spelling, not
    a new one - checking canonical_name alone missed vendors (like the pre-existing
    'SoCalGas' row) that were created with a short_name but no canonical_name yet, which
    would otherwise both duplicate the vendor and crash on the short_name UNIQUE
    constraint."""
    known = set()
    for v in vendors:
        for field in (v.get("canonical_name"), v.get("short_name")):
            if field and field.strip():
                known.add(field.strip().lower())
        for alias in (v.get("aliases") or "").split(";"):
            if alias.strip():
                known.add(alias.strip().lower())
    return known


def _cluster_is_known(group: list[str], existing: set) -> bool:
    """True if ANY spelling in the cluster - not just group[0], the most-frequent one -
    already identifies a known vendor.

    Checking only the canonical spelling missed clusters where that spelling was brand
    new but a less-frequent member was already a known alias - e.g. a fresh 'LADWP
    Consolidated Billing Statement' cluster that also contains the already-known alias
    'LA DWP' bypassed the check entirely and would create a duplicate LADWP vendor,
    splitting future matching across two vendor_ids with no error raised."""
    return any(m.strip().lower() in existing for m in group)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write vendors (default: dry run)")
    args = ap.parse_args()

    db.init()
    names = [(r.get("vendor_name") or "").strip() for r in db.list_invoices()]
    names = [n for n in names if n]
    counts = collections.Counter(names)
    groups = vm.cluster(names)
    existing_vendors = db.all_vendors()
    existing = _known_identities(existing_vendors)
    used_short = {v["short_name"].strip().lower() for v in existing_vendors if v.get("short_name")}

    multi = [g for g in groups if len(g) > 1]
    print(f"{len(counts)} distinct raw strings -> {len(groups)} clusters "
          f"({len(multi)} need a look, {len(groups) - len(multi)} singletons)\n")

    print("Clusters needing a human decision:")
    for g in sorted(multi, key=lambda g: -sum(counts[x] for x in g)):
        print("  *", " | ".join(f"{x} ({counts[x]})" for x in g))

    created = skipped = 0
    for group in groups:
        canonical = group[0]                    # most frequent spelling wins
        if _cluster_is_known(group, existing):
            skipped += 1
            continue
        if args.apply:
            short = _unique_short_name(canonical, used_short)
            vendor_id = db.add_vendor(short, "; ".join(group))
            db.update_vendor_identity(vendor_id, canonical_name=canonical)
        created += 1

    verb = "created" if args.apply else "would create"
    print(f"\n{verb} {created} vendors, skipped {skipped} already present")
    if not args.apply:
        print("Dry run. Re-run with --apply to write.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
