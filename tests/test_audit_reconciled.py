# -*- coding: utf-8 -*-
"""scripts/audit_reconciled.py - cross-check every `reconciled` stamp in the database
against the month that invoice's check actually cleared, per the rec reports themselves.

Why this file exists: a matcher bug placed invoices on same-amount cleared checks written to
other payees, and those placements graded `high`, so core/reconcile.py stamped them. The stamp
is what drops an invoice out of next month's staging, so a wrong one hides a bill that never
cleared. The bug is fixed; the stamps it already wrote are still in the database, and only a
cross-check against the rec reports can find them.

`build_check_index` and `decide` are pure - a month's parsed rec report and a database row are
all they need, so the whole verdict table is exercised without a database or a PDF.

Run from the project root:

    python -m unittest discover -s tests -t . -v
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import audit_reconciled as ar


def _rec(cleared=(), outstanding=()):
    """A parse_rec() result carrying just the check numbers this test cares about."""
    return {"checks": [{"type": "check", "tran": str(n), "notes": "", "amount": 0.0,
                        "date": "07/01/2026"} for n in cleared],
            "deposits": [], "outstanding_checks": list(outstanding),
            "difference": "0.00", "error": None}


BEACH = "6281-6301 Beach Blvd"
SOLAIR = "Solair"

# July clears 10581, still owes 10599 and 10613. August clears both of those.
INDEX = ar.build_check_index([
    ("July 2026", BEACH, _rec(cleared=[10581], outstanding=[10599, 10613, 10626])),
    ("August 2026", BEACH, _rec(cleared=[10599, 10613], outstanding=[10626])),
])


class BuildCheckIndex(unittest.TestCase):
    def test_a_check_outstanding_then_cleared_is_indexed_to_the_month_it_cleared(self):
        facts = INDEX[(ar.prop_key(BEACH), 10599)]
        self.assertEqual(facts.cleared_month, "August 2026")

    def test_a_check_that_only_ever_sat_outstanding_has_no_cleared_month(self):
        facts = INDEX[(ar.prop_key(BEACH), 10626)]
        self.assertIsNone(facts.cleared_month)
        self.assertEqual(sorted(facts.outstanding_months), ["August 2026", "July 2026"])

    def test_check_numbers_are_scoped_to_their_property(self):
        """Every property has its own check register, so #10581 means nothing across them."""
        self.assertIn((ar.prop_key(BEACH), 10581), INDEX)
        self.assertNotIn((ar.prop_key(SOLAIR), 10581), INDEX)

    def test_deposit_transaction_numbers_are_not_read_as_check_numbers(self):
        parsed = _rec(cleared=[])
        parsed["deposits"] = [{"type": "deposit", "tran": "10581", "notes": "",
                               "amount": 0.0, "date": "07/03/2026"}]
        self.assertEqual(ar.build_check_index([("July 2026", BEACH, parsed)]), {})


def _row(check_number, reconciled="", stored_file="X.pdf", prop=BEACH):
    return {"stored_file": stored_file, "property": prop,
            "check_number": check_number, "reconciled": reconciled}


class Decide(unittest.TestCase):
    """One row against the index. The four verdicts that mean somebody has to look."""

    def test_a_stamp_naming_the_month_the_check_cleared_is_ok(self):
        verdict, _d = ar.decide(_row("10581", "July 2026"), INDEX)
        self.assertEqual(verdict, ar.OK)

    def test_a_stamp_naming_the_wrong_month_is_flagged(self):
        """Keun_05_2026.pdf: stamped July, but check #10599 did not clear until August."""
        verdict, detail = ar.decide(_row("10599", "July 2026"), INDEX)
        self.assertEqual(verdict, ar.WRONG_MONTH)
        self.assertIn("August 2026", detail)

    def test_a_cleared_check_with_no_stamp_is_flagged(self):
        """COST_06_2026.jpg: #10581 cleared in July and was never stamped, so it re-stages."""
        verdict, detail = ar.decide(_row("10581", ""), INDEX)
        self.assertEqual(verdict, ar.MISSING_STAMP)
        self.assertIn("July 2026", detail)

    def test_a_stamp_on_a_check_that_never_cleared_is_flagged(self):
        verdict, _d = ar.decide(_row("10626", "August 2026"), INDEX)
        self.assertEqual(verdict, ar.NEVER_CLEARED)

    def test_an_unstamped_outstanding_check_is_ok(self):
        """Still outstanding and not stamped is the correct resting state, not a finding."""
        verdict, _d = ar.decide(_row("10626", ""), INDEX)
        self.assertEqual(verdict, ar.OK)

    def test_a_non_numeric_check_number_cannot_be_judged(self):
        verdict, _d = ar.decide(_row("ACH", "July 2026"), INDEX)
        self.assertEqual(verdict, ar.NO_CHECK_NUMBER)

    def test_a_check_absent_from_every_rec_report_cannot_be_judged(self):
        verdict, _d = ar.decide(_row("99999", "July 2026"), INDEX)
        self.assertEqual(verdict, ar.NO_REC_DATA)

    def test_a_row_is_judged_against_its_own_property(self):
        """The same number in another property's register must not silently clear this row."""
        verdict, _d = ar.decide(_row("10581", "July 2026", prop=SOLAIR), INDEX)
        self.assertEqual(verdict, ar.NO_REC_DATA)


class Findings(unittest.TestCase):
    def test_only_the_verdicts_that_need_a_human_are_returned(self):
        rows = [_row("10581", "July 2026", "rolling_05.pdf"),      # ok
                _row("10599", "July 2026", "Keun_05_2026.pdf"),    # wrong month
                _row("10581", "", "COST_06_2026.jpg"),             # missing stamp
                _row("ACH", "July 2026", "rolling_03.pdf")]        # unjudgeable
        found = ar.findings(rows, INDEX)
        self.assertEqual([f.stored_file for f in found],
                         ["Keun_05_2026.pdf", "COST_06_2026.jpg"])

    def test_unjudgeable_rows_are_counted_so_the_gap_is_visible(self):
        rows = [_row("ACH", "July 2026"), _row("99999", "July 2026")]
        self.assertEqual(ar.findings(rows, INDEX), [])
        self.assertEqual(ar.tally(rows, INDEX)[ar.NO_CHECK_NUMBER], 1)
        self.assertEqual(ar.tally(rows, INDEX)[ar.NO_REC_DATA], 1)


class ClosedMonths(unittest.TestCase):
    """A month is only judgeable once it has actually been reconciled. Without that, every
    check cleared in the month currently being assembled reads as a missing stamp - 50-odd
    of them here, burying the three real findings under the normal pre-reconcile state."""

    def test_months_that_carry_a_stamp_are_the_reconciled_ones(self):
        rows = [_row("1", "July 2026"), _row("2", "July 2026"), _row("3", "")]
        self.assertEqual(ar.closed_months(rows), {"July 2026"})

    def test_a_check_cleared_in_a_month_not_yet_reconciled_is_not_a_missing_stamp(self):
        verdict, _d = ar.decide(_row("10599", ""), INDEX, closed={"July 2026"})
        self.assertEqual(verdict, ar.NOT_YET_RECONCILED)

    def test_a_check_cleared_in_a_reconciled_month_is_still_a_missing_stamp(self):
        verdict, _d = ar.decide(_row("10581", ""), INDEX, closed={"July 2026"})
        self.assertEqual(verdict, ar.MISSING_STAMP)

    def test_a_wrong_month_stamp_is_flagged_even_when_the_real_month_is_still_open(self):
        """The stamp asserts July; check #10599 demonstrably did not clear in July. That the
        August rec is still open changes nothing about the claim already written down."""
        verdict, _d = ar.decide(_row("10599", "July 2026"), INDEX, closed={"July 2026"})
        self.assertEqual(verdict, ar.WRONG_MONTH)

    def test_the_open_month_rows_stay_out_of_the_findings(self):
        rows = [_row("10599", "", "Keun_05_2026.pdf"), _row("10581", "", "COST_06_2026.jpg")]
        found = ar.findings(rows, INDEX, closed={"July 2026"})
        self.assertEqual([f.stored_file for f in found], ["COST_06_2026.jpg"])


if __name__ == "__main__":
    unittest.main()
