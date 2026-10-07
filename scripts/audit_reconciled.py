# -*- coding: utf-8 -*-
"""Cross-check every `reconciled` stamp in the database against the month that invoice's
check actually cleared, according to the rec reports themselves.

READ-ONLY. It never writes to the database - it prints what disagrees and stops there.

    python scripts/audit_reconciled.py                 # findings only
    python scripts/audit_reconciled.py --all           # findings + the rows it cannot judge
    python scripts/audit_reconciled.py --csv out.csv   # also write the findings as CSV

Why it exists: an invoice's `reconciled` stamp is what drops it out of next month's staging.
A matcher bug used to place invoices on same-amount cleared checks written to other payees;
those placements graded `high`, and core/reconcile.py stamps `high`. So a bill that never
cleared could be stamped and quietly leave the pending pool. The matcher is fixed, but the
stamps it already wrote are still there, and the rec reports are the only record that can
contradict them: each one lists, per property and per month, exactly which check numbers
cleared and which were still outstanding.

Verdicts:
    wrong-month     stamped, but the check cleared in a different month
    missing-stamp   the check cleared and nothing was stamped (the invoice keeps re-staging)
    never-cleared   stamped, but the check is only ever listed as outstanding
    no-check-number the row's check number isn't a number (ACH, blank) - cannot be judged
    no-rec-data     no rec report for this property names that check - cannot be judged
"""
import argparse
import collections
import csv
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
from core import bankrec
from core import db
from core import processor as ip

OK = "ok"
WRONG_MONTH = "wrong-month"
MISSING_STAMP = "missing-stamp"
NEVER_CLEARED = "never-cleared"
NOT_YET_RECONCILED = "not-yet-reconciled"
NO_CHECK_NUMBER = "no-check-number"
NO_REC_DATA = "no-rec-data"

# The verdicts a person has to act on. The two "cannot be judged" ones are counted, not listed,
# so a shrinking pool of unjudgeable rows stays visible without burying the real findings.
ACTIONABLE = (WRONG_MONTH, MISSING_STAMP, NEVER_CLEARED)

CheckFacts = collections.namedtuple("CheckFacts", "cleared_month outstanding_months")
Finding = collections.namedtuple("Finding", "stored_file property check_number verdict detail")


def prop_key(name):
    """Properties are keyed the way core/reconcile.py keys them, so the two agree on identity."""
    return ip._normalize_key(str(name or ""))


def _check_int(tran):
    """A check number as an int, or None when it isn't one ('ACH', 'Autopay', blank)."""
    digits = "".join(ch for ch in str(tran or "") if ch.isdigit())
    return int(digits) if digits else None


def _month_sort_key(label):
    """Order month labels chronologically; unparseable labels sort last but stay stable."""
    end = bankrec.period_last_day(label)
    return (0, end) if end else (1, None)


def build_check_index(rec_reads):
    """rec_reads: (month_label, property_name, parsed) triples, `parsed` as parse_rec returns.
    Returns {(prop_key, check_no): CheckFacts}. A check listed cleared in one month and
    outstanding in an earlier one is indexed to the month it CLEARED - that is the whole point
    of the audit, since an invoice stamped with the earlier month is stamped wrong."""
    cleared, outstanding = {}, collections.defaultdict(set)
    for month, prop, parsed in rec_reads:
        key_prop = prop_key(prop)
        for t in parsed.get("checks", ()):           # deposits carry their own tran numbers
            n = _check_int(t.get("tran"))            # which are NOT check numbers - skip them
            if n is not None:
                cleared.setdefault((key_prop, n), []).append(month)
        for n in parsed.get("outstanding_checks", ()):
            outstanding[(key_prop, n)].add(month)
    index = {}
    for key in set(cleared) | set(outstanding):
        months = sorted(cleared.get(key, ()), key=_month_sort_key)
        index[key] = CheckFacts(cleared_month=months[0] if months else None,
                                outstanding_months=tuple(sorted(outstanding.get(key, ()))))
    return index


def closed_months(rows):
    """The months that have actually been reconciled - the ones some row is stamped with.
    Derived from the data rather than from a calendar: the month currently being assembled
    has no stamps yet, and every check it cleared is legitimately unstamped."""
    return {str(r.get("reconciled") or "").strip()
            for r in rows if str(r.get("reconciled") or "").strip()}


def decide(row, index, closed=None):
    """One database row against the index -> (verdict, detail). Pure.
    `closed` limits missing-stamp to months already reconciled; None judges every month."""
    stamp = str(row.get("reconciled") or "").strip()
    n = _check_int(row.get("check_number"))
    if n is None:
        return NO_CHECK_NUMBER, "check number %r is not a number" % (row.get("check_number"),)
    facts = index.get((prop_key(row.get("property")), n))
    if facts is None:
        return NO_REC_DATA, "no rec report for this property lists check #%d" % n
    if facts.cleared_month:
        if not stamp:
            if closed is not None and facts.cleared_month not in closed:
                return NOT_YET_RECONCILED, ("check #%d cleared in %s, which has not been "
                                            "reconciled yet" % (n, facts.cleared_month))
            return MISSING_STAMP, "check #%d cleared in %s; not stamped" % (n, facts.cleared_month)
        if stamp != facts.cleared_month:
            return WRONG_MONTH, ("stamped %s; check #%d cleared in %s"
                                 % (stamp, n, facts.cleared_month))
        return OK, ""
    if stamp:
        return NEVER_CLEARED, ("stamped %s; check #%d is outstanding in %s"
                               % (stamp, n, ", ".join(facts.outstanding_months) or "every rec"))
    return OK, ""


def findings(rows, index, closed=None):
    """Only the rows a person has to act on, worst first, then by file name."""
    out = []
    for row in rows:
        verdict, detail = decide(row, index, closed)
        if verdict in ACTIONABLE:
            out.append(Finding(str(row.get("stored_file") or ""), str(row.get("property") or ""),
                               str(row.get("check_number") or ""), verdict, detail))
    order = {v: i for i, v in enumerate(ACTIONABLE)}
    return sorted(out, key=lambda f: (order[f.verdict], f.stored_file.lower()))


def tally(rows, index, closed=None):
    """How many rows landed on each verdict, including the ones that cannot be judged."""
    counts = collections.Counter()
    for row in rows:
        counts[decide(row, index, closed)[0]] += 1
    return counts


# ---------------------------------------------------------------- filesystem side
def discover_recs(root):
    """Walk data/Bank Rec/<Month> Bank Rec/<Property>/ and read each property's rec report.
    Yields (month_label, property_name, parsed). Folders with no rec report are skipped."""
    root = Path(root)
    if not root.is_dir():
        return
    for month_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        month = month_dir.name
        if month.lower().endswith(" bank rec"):        # "August 2026 Bank Rec" -> "August 2026"
            month = month[: -len(" bank rec")].strip()
        for prop_dir in sorted(p for p in month_dir.iterdir() if p.is_dir()):
            if prop_dir.name.startswith("_"):          # _output holds the assembled PDFs
                continue
            recs = [p for p in bankrec.list_source_files(str(prop_dir))
                    if "ASSEMBLED" not in p and bankrec.classify(p) == "rec"]
            if not recs:
                continue
            parsed = bankrec.parse_rec(recs[0])
            if parsed.get("error"):
                print("  ! %s / %s: %s" % (month, prop_dir.name, parsed["error"]))
                continue
            yield month, prop_dir.name, parsed


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=str(config.BANK_REC_ROOT),
                    help="Bank Rec root folder (default: this app's data/Bank Rec)")
    ap.add_argument("--all", action="store_true",
                    help="also list the rows that cannot be judged")
    ap.add_argument("--every-month", action="store_true",
                    help="judge months that have not been reconciled yet too (noisy: every "
                         "check cleared in the open month reads as a missing stamp)")
    ap.add_argument("--csv", metavar="PATH", help="write the findings to a CSV as well")
    args = ap.parse_args()

    print("Reading rec reports under %s" % args.root)
    reads = list(discover_recs(args.root))
    index = build_check_index(reads)
    months = sorted({m for m, _p, _x in reads}, key=_month_sort_key)
    print("  %d rec report(s) across %d month(s): %s"
          % (len(reads), len(months), ", ".join(months) or "-"))
    print("  %d check number(s) indexed" % len(index))
    if not index:
        print("\nNo rec reports found - nothing to audit against.")
        return 1

    db.init()
    rows = [r for r in db.list_invoices() if str(r["status"] or "") != "DUPLICATE"]
    # Judge a month only once it has been reconciled. The month being assembled right now has
    # no stamps yet, so every check it cleared is legitimately unstamped - without this, the
    # open month contributes one "missing stamp" per cleared check and buries the real findings.
    closed = None if args.every_month else closed_months(rows)
    if closed is not None:
        print("  reconciled month(s): %s"
              % (", ".join(sorted(closed, key=_month_sort_key)) or "none yet"))
    found = findings(rows, index, closed)
    counts = tally(rows, index, closed)

    print("\n%d invoice row(s) checked - %d ok, %d in a month not yet reconciled, %d to look at"
          % (len(rows), counts[OK], counts[NOT_YET_RECONCILED], len(found)))
    print("  not judged: %d have no check number (ACH, autopay, blank), %d name a check no rec "
          "report on disk lists" % (counts[NO_CHECK_NUMBER], counts[NO_REC_DATA]))
    if found:
        print("\n--- findings ---")
        width = max(len(f.stored_file) for f in found)
        for f in found:
            print("  %-15s %-*s  %-28s %s"
                  % (f.verdict, width, f.stored_file, f.property[:28], f.detail))
    else:
        print("\nEvery stamp agrees with the rec reports.")

    if args.all:
        print("\n--- cannot be judged (%d no check number, %d not in any rec) ---"
              % (counts[NO_CHECK_NUMBER], counts[NO_REC_DATA]))
        for row in rows:
            verdict, detail = decide(row, index, closed)
            if verdict in (NO_CHECK_NUMBER, NO_REC_DATA):
                print("  %-15s %-40s %s" % (verdict, str(row["stored_file"])[:40], detail))

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(Finding._fields)
            w.writerows(found)
        print("\nWrote %d finding(s) to %s" % (len(found), args.csv))

    print("\nRead-only: nothing in the database was changed.")
    return 2 if found else 0


if __name__ == "__main__":
    sys.exit(main())
