# -*- coding: utf-8 -*-
"""
stage_month.py - build the month folder tree and stage each property's pending invoices.

    python stage_month.py --month "May 2026" --dest "<_AutoAssembler root>"

For every property it creates "<dest>/<Month> Bank Rec/<property>/" and copies in that
property's PENDING invoices - the files listed in processed/<property>/_amounts.csv - plus
the sidecar itself. The bank statement, rec report, deposit slips and financial reports are
dropped into each folder by hand afterward, then the assembler is run over the tree.

Copy, not move: the processed/ archive stays intact. An invoice that doesn't clear this month
stays un-reconciled, so it stays in the sidecar and is re-staged automatically next month.
"""

import csv
import shutil
import argparse
from pathlib import Path

from . import processor as ip
from . import db

SIDECAR_NAME = "_amounts.csv"


def pending_files(sidecar_path):
    """Unique stored_file names listed in a property's _amounts.csv (its pending pool)."""
    files, seen = [], set()
    with open(sidecar_path, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            f = (row.get("stored_file") or "").strip()
            if f and f.lower() not in seen:
                seen.add(f.lower())
                files.append(f)
    return files


def main():
    ap = argparse.ArgumentParser(description="Stage each property's pending invoices into a month folder")
    ap.add_argument("--month", required=True,
                    help='e.g. "May 2026" -> "<dest>/May 2026 Bank Rec/<property>/"')
    ap.add_argument("--dest", required=True, help="the _AutoAssembler root the month tree is built under")
    ap.add_argument("--processed", default=str(ip.PROCESSED_FOLDER))
    ap.add_argument("--no-refresh", action="store_true",
                    help="stage from the existing sidecars without re-reading the database")
    ap.add_argument("--property", help="stage ONLY this property (full name, alias, or partial like \"Kenmore\"); default = all")
    args = ap.parse_args()

    db.init()
    processed  = Path(args.processed)
    month_root = Path(args.dest) / f"{args.month} Bank Rec"

    # Refresh each property's sidecar from the database first, so anything you entered after
    # processing - check numbers, newly-reconciled rows - is current before staging.
    if not args.no_refresh:
        n = db.export_amount_sidecars(processed)
        print(f"[*] Refreshed {n} sidecar(s) from the database")
    proplist = ip.load_property_list()
    names = [c for c, _keys, _aliases in proplist]
    if not names:                                  # no list -> stage whatever has a sidecar
        names = [p.name for p in processed.iterdir() if p.is_dir()]
    if args.property:                              # one-property mode (name, code, alias, or partial)
        matches = ip.resolve_property(args.property, proplist)
        if len(matches) == 1:
            names = [matches[0]]
            print(f"[*] One-property mode: staging only '{matches[0]}'.")
        elif len(matches) > 1:
            print(f"[!] '{args.property}' matches several properties: {', '.join(matches)} - be more specific.")
            return
        else:
            print(f"[!] '{args.property}' didn't match any property. Nothing staged.")
            return

    props = staged = total = 0
    for canonical in names:
        safe = ip._safe_folder_name(canonical)     # same folder spelling the processor filed under
        src, dest = processed / safe, month_root / safe
        dest.mkdir(parents=True, exist_ok=True)    # always create - the user drops the statement here
        props += 1
        sidecar = src / SIDECAR_NAME
        if not sidecar.exists():
            print(f"  {canonical}: no pending sidecar - empty folder created for manual drops")
            continue
        copied = 0
        for f in pending_files(sidecar):
            s = src / f
            if s.exists():
                shutil.copy2(s, dest / f)
                copied += 1
            else:
                print(f"  [!] {canonical}: sidecar lists '{f}' but it's missing in {src}")
        shutil.copy2(sidecar, dest / SIDECAR_NAME)
        total += copied
        staged += 1 if copied else 0
        print(f"  {canonical}: staged {copied} pending invoice(s) + sidecar")

    print(f"\n[*] Month tree: {month_root}")
    print(f"    {props} property folder(s); {staged} with pending invoices; {total} invoice file(s) copied.")
    print("    Now drop each property's bank statement, rec report, deposit slips & financials")
    print("    into its folder, then run the assembler.")


if __name__ == "__main__":
    main()
