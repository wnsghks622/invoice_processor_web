# -*- coding: utf-8 -*-
"""
reassign_review.py - move "Needs Review" invoices into their correct property (DB version).

    python -m core.reassign_review

An invoice is flagged needs_review when its service location didn't match the property list.
This re-resolves each such invoice end to end:

  1. Re-checks the location against the CURRENT property list - so if you've since added the
     address as an alias, it's matched automatically (no prompting).
  2. Anything still unmatched gets an interactive property picker (skipped in an unattended run).
  3. For each fix it sets the invoice's property + clears needs_review in the DB, moves the filed
     PDF from processed/Needs Review/ to processed/<property>/, and refreshes the amount sidecars
     so the invoice enters that property's pending pool.

The lasting fix is to add the address to the property's aliases (Properties page / DB) so future
invoices match on their own - then this auto-resolves the backlog in one pass.
"""

import sys
import argparse
import shutil
from pathlib import Path

from . import processor as ip
from . import db

REVIEW_FOLDER = ip.PROPERTY_REVIEW_TAB   # "Needs Review" - the processed/ subfolder name


def pick_property(raw, properties, interactive):
    """Chosen canonical name, or None to skip."""
    print(f"\n  Needs Review: read location = {raw!r}")
    if not interactive:
        print("    (no interactive console - skipping; re-run from a terminal to assign)")
        return None
    print("    Known properties:")
    for i, (canon, _k, _a) in enumerate(properties, 1):
        print(f"      {i:>2}. {canon}")
    while True:
        v = input("    Correct property (number, name, or Enter to skip): ").strip()
        if not v:
            return None
        if v.isdigit() and 1 <= int(v) <= len(properties):
            return properties[int(v) - 1][0]
        canon = ip.match_property(v, properties)
        if canon:
            return canon
        print("    Not recognized - type a number from the list, or a name/alias it matches.")


def move_file(processed, stored, property_name):
    """Move processed/Needs Review/<stored> -> processed/<property>/. Returns the final name
    (which may differ on a collision), None if there's nothing to move, or False if locked."""
    if not stored:
        return None
    src = processed / ip._safe_folder_name(REVIEW_FOLDER) / stored
    if not src.exists():
        return None
    dest_dir = processed / ip._safe_folder_name(property_name)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / stored
    stem, suffix, i = dest.stem, dest.suffix, 1
    while dest.exists():
        dest = dest_dir / f"{stem}_{i}{suffix}"; i += 1
    try:
        shutil.move(str(src), str(dest))
    except (PermissionError, OSError):
        return False           # file is open in a viewer / locked - caller leaves this one alone
    return dest.name


def resolve_and_apply(inv, properties, processed, interactive):
    """Resolve one needs-review invoice to a property and apply it. Returns
    'fixed' | 'skipped' | 'moved+fixed' style tallies via a small dict."""
    raw = inv["property"]
    chosen = ip.match_property(str(raw or ""), properties)     # auto if an alias now matches
    if chosen:
        print(f"\n  Auto-matched: {raw!r} -> {chosen}")
    else:
        chosen = pick_property(raw, properties, interactive)
    if not chosen:
        return {"skipped": 1}

    stored = str(inv["stored_file"] or "").strip()
    newname = move_file(processed, stored, chosen)             # move the PDF first
    if newname is False:                                       # open in a viewer / locked
        print(f"    !! {stored} is open in a viewer - close it and re-run to assign this one")
        return {"skipped": 1}

    # collision-renamed -> keep the stored_file join key in sync
    new_stored = newname if (newname and newname != stored) else None
    db.reassign_property(inv["id"], chosen, new_stored)
    where = (f"file -> processed/{ip._safe_folder_name(chosen)}/{newname}"
             if newname else "no filed PDF found - record updated only")
    print(f"    -> {chosen}  ({where})")
    return {"fixed": 1, "moved": 1 if newname else 0}


def main():
    ap = argparse.ArgumentParser(description="Reassign Needs Review invoices to their correct property")
    ap.add_argument("--processed", default=str(ip.PROCESSED_FOLDER))
    args = ap.parse_args()

    db.init()
    properties = ip.load_property_list()
    if not properties:
        print("[ERROR] No properties in the database.")
        sys.exit(1)
    try:
        interactive = bool(sys.stdin and sys.stdin.isatty())
    except Exception:
        interactive = False

    rows = db.review_invoices()
    if not rows:
        print("[*] No invoices in Needs Review. Nothing to reassign.")
        return
    print(f"[*] {len(rows)} invoice(s) need a property assigned.")

    processed = Path(args.processed)
    fixed = skipped = moved = 0
    for inv in rows:
        res = resolve_and_apply(inv, properties, processed, interactive)
        fixed += res.get("fixed", 0)
        skipped += res.get("skipped", 0)
        moved += res.get("moved", 0)

    sidecars = db.export_amount_sidecars(processed)
    print(f"\n[*] Reassigned {fixed}; skipped {skipped}; moved {moved} file(s); "
          f"refreshed {sidecars} sidecar(s).")
    if skipped:
        print("    Skipped invoices stay on Needs Review - add their address to the property's "
              "aliases, or re-run from a terminal.")


if __name__ == "__main__":
    main()
