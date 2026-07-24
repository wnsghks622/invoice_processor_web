# -*- coding: utf-8 -*-
"""
cleanup_processed.py - safely trim the processed/ archive after a property's bank rec is done.

    python cleanup_processed.py [--property NAME]           (DRY RUN - shows what it would delete)
    python cleanup_processed.py [--property NAME] --delete  (actually delete)

It deletes ONLY invoices that are RECONCILED (cleared - their record already lives in the bank
rec folder and the assembled PDF). It KEEPS every invoice still in the pending pool (its
processed/<property>/_amounts.csv), so outstanding bills roll forward into next month's rec, and
it keeps anything it can't positively confirm is cleared. Your Bank Rec/<month>/ folders (the
month records) are never touched.
"""
import csv
import argparse
from pathlib import Path

from . import processor as ip
from . import db


def sidecar_pending(folder):
    """Set of stored-file names (lowercased) the property still has pending (must keep)."""
    sc = folder / "_amounts.csv"
    if not sc.exists():
        return set()
    with sc.open(newline="", encoding="utf-8-sig") as fh:
        return {(row.get("stored_file") or "").strip().lower() for row in csv.DictReader(fh)}


def clean_property(prop, reconciled, dup_only, processed, do_delete):
    folder = processed / ip._safe_folder_name(prop)
    if not folder.exists():
        return 0, 0, 0
    pn = ip._normalize_key(prop)
    pending = sidecar_pending(folder)
    pdfs = sorted(folder.glob("*.pdf"))
    if not pdfs:
        return 0, 0, 0
    print(f"=== {prop} ===")
    deleted = kept_pending = kept_other = 0
    for f in pdfs:
        fl = f.name.lower()
        k = (pn, fl)
        if fl in pending:                       # in the pending pool -> must keep
            kept_pending += 1
            print(f"    KEEP          {f.name:30} (pending - rolls forward)")
            continue
        reason = "reconciled" if k in reconciled else ("duplicate" if k in dup_only else None)
        if reason:
            if do_delete:
                try:
                    f.unlink()
                except OSError as e:
                    print(f"    [!] couldn't delete {f.name}: {e}")
                    kept_other += 1
                    continue
            deleted += 1
            print(f"    {'DELETED ' if do_delete else 'would delete'}  {f.name:30} ({reason})")
        else:
            kept_other += 1
            print(f"    KEEP          {f.name:30} (not confirmed cleared - left alone)")
    return deleted, kept_pending, kept_other


def main():
    ap = argparse.ArgumentParser(description="Trim reconciled invoices from the processed/ archive")
    ap.add_argument("--property", help="just this property (name/alias/partial); omit = all properties")
    ap.add_argument("--delete", action="store_true", help="actually delete (default: dry run)")
    ap.add_argument("--processed", default=str(ip.PROCESSED_FOLDER))
    args = ap.parse_args()

    db.init()
    proplist = ip.load_property_list()
    names = [c for c, _k, _a in proplist]
    if args.property:
        m = ip.resolve_property(args.property, proplist)
        if len(m) == 1:
            names = [m[0]]
        elif len(m) > 1:
            print(f"[!] '{args.property}' matches several: {', '.join(m)} - be more specific."); return
        else:
            print(f"[!] '{args.property}' didn't match any property."); return

    reconciled, dup_only = db.file_status()
    processed = Path(args.processed)
    td = tp = to = 0
    for prop in names:
        d, p, o = clean_property(prop, reconciled, dup_only, processed, args.delete)
        td += d; tp += p; to += o

    verb = "Deleted" if args.delete else "Would delete"
    tail = "" if args.delete else "  Re-run with --delete to actually remove them."
    print(f"\n[*] {verb} {td} file(s) (reconciled or duplicate); kept {tp} pending + {to} other.{tail}")


if __name__ == "__main__":
    main()
