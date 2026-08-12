# -*- coding: utf-8 -*-
"""Period and window arithmetic.

This is the part of the calendar most likely to be wrong and the cheapest to test, so it
is a pure module with no database. Every timing decision in the app resolves through here.

Run from the project root:

    python -m unittest discover -s tests -t . -v
"""
import datetime
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import periods


class ParseAndFormat(unittest.TestCase):
    def test_round_trip(self):
        self.assertEqual(periods.parse_period("August 2026"), (2026, 8))
        self.assertEqual(periods.format_period(2026, 8), "August 2026")

    def test_every_month_round_trips(self):
        for m in range(1, 13):
            with self.subTest(month=m):
                self.assertEqual(periods.parse_period(periods.format_period(2026, m)), (2026, m))

    def test_period_of_an_iso_date(self):
        self.assertEqual(periods.period_of("2026-08-12"), "August 2026")
        self.assertEqual(periods.period_of("2026-01-01"), "January 2026")

    def test_junk_raises_rather_than_guessing(self):
        for bad in ("", "Augus 2026", "2026-08", "August", None):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    periods.parse_period(bad)


class PeriodBounds(unittest.TestCase):
    def test_ordinary_month(self):
        first, last = periods.period_bounds("August 2026")
        self.assertEqual((first, last), (datetime.date(2026, 8, 1), datetime.date(2026, 8, 31)))

    def test_thirty_day_month(self):
        _, last = periods.period_bounds("September 2026")
        self.assertEqual(last, datetime.date(2026, 9, 30))

    def test_february_in_a_common_year(self):
        _, last = periods.period_bounds("February 2026")
        self.assertEqual(last, datetime.date(2026, 2, 28))

    def test_february_in_a_leap_year(self):
        _, last = periods.period_bounds("February 2028")
        self.assertEqual(last, datetime.date(2028, 2, 29))


class ResolveWindow(unittest.TestCase):
    def test_single_day(self):
        self.assertEqual(periods.resolve_window("day:1", "August 2026"),
                         ("2026-08-01", "2026-08-01"))

    def test_day_range(self):
        self.assertEqual(periods.resolve_window("day:28-30", "August 2026"),
                         ("2026-08-28", "2026-08-30"))

    def test_day_range_clamps_to_a_short_month(self):
        # 28-30 in February must not produce a 30th.
        self.assertEqual(periods.resolve_window("day:28-30", "February 2026"),
                         ("2026-02-28", "2026-02-28"))

    def test_week_is_seven_day_blocks_from_the_first(self):
        # Week 3 is the 15th-21st, which is NOT the same as "the third Monday".
        self.assertEqual(periods.resolve_window("week:3", "August 2026"),
                         ("2026-08-15", "2026-08-21"))

    def test_week_range(self):
        self.assertEqual(periods.resolve_window("week:1-2", "August 2026"),
                         ("2026-08-01", "2026-08-14"))

    def test_last_week_is_the_final_seven_days(self):
        self.assertEqual(periods.resolve_window("last-week", "August 2026"),
                         ("2026-08-25", "2026-08-31"))

    def test_last_week_in_a_short_month(self):
        self.assertEqual(periods.resolve_window("last-week", "February 2026"),
                         ("2026-02-22", "2026-02-28"))

    def test_month_end_is_the_final_day(self):
        self.assertEqual(periods.resolve_window("month-end", "August 2026"),
                         ("2026-08-31", "2026-08-31"))

    def test_learned_uses_the_profile_day_and_spread(self):
        self.assertEqual(
            periods.resolve_window("learned", "August 2026", due_day=12, due_spread=2),
            ("2026-08-10", "2026-08-14"))

    def test_learned_clamps_to_the_period(self):
        self.assertEqual(
            periods.resolve_window("learned", "August 2026", due_day=30, due_spread=5),
            ("2026-08-25", "2026-08-31"))

    def test_learned_without_a_profile_spans_the_whole_month(self):
        # No profile yet is not an error - it means "sometime this month".
        self.assertEqual(periods.resolve_window("learned", "August 2026"),
                         ("2026-08-01", "2026-08-31"))

    def test_absolute_date_inside_the_period(self):
        self.assertEqual(periods.resolve_window("date:2026-08-12", "August 2026"),
                         ("2026-08-12", "2026-08-12"))

    def test_absolute_date_outside_the_period_does_not_apply(self):
        # This is what makes a one-off reminder appear in exactly one month.
        self.assertIsNone(periods.resolve_window("date:2026-08-12", "September 2026"))

    def test_unknown_rule_raises(self):
        with self.assertRaises(ValueError):
            periods.resolve_window("phase-of-moon", "August 2026")

    def test_an_inverted_range_is_rejected(self):
        # Both endpoints are valid days, so nothing else in the parser objects. The result
        # would be due_from > due_to: a window no BETWEEN can match, which reads as
        # "scheduled" everywhere while never being due.
        for rule in ("day:30-1", "week:4-2"):
            with self.subTest(rule=rule):
                with self.assertRaises(ValueError):
                    periods.resolve_window(rule, "August 2026")

    def test_an_equal_range_is_still_fine(self):
        # The guard is `lo > hi`, not `lo >= hi` - day:5-5 is a legitimate single day.
        self.assertEqual(periods.resolve_window("day:5-5", "August 2026"),
                         ("2026-08-05", "2026-08-05"))


class ClassifyCadence(unittest.TestCase):
    """Cadence comes from the gaps between observations, and only recent ones decide it.

    Four of the eight pairs in the live data that meet the gate changed frequency
    mid-history, nearly all toward monthly - so whole-history classification would call
    them irregular and surface them only in the last week of the month, which is late to
    discover a missing utility bill.
    """

    def test_consecutive_months_are_monthly(self):
        self.assertEqual(periods.classify_cadence(["2026-06", "2026-07", "2026-08"]),
                         ("monthly", None))

    def test_two_observations_with_a_gap_are_irregular_not_bimonthly(self):
        # Below the gate, a 2-month gap is not enough evidence for even/odd.
        self.assertEqual(periods.classify_cadence(["2026-05", "2026-07"]),
                         ("irregular", None))

    def test_even_months_need_the_gate_and_carry_parity(self):
        self.assertEqual(
            periods.classify_cadence(["2026-02", "2026-04", "2026-06", "2026-08"]),
            ("even-months", 0))

    def test_odd_months_carry_the_other_parity(self):
        self.assertEqual(
            periods.classify_cadence(["2026-01", "2026-03", "2026-05", "2026-07"]),
            ("odd-months", 1))

    def test_quarterly_anchors_on_the_observed_month(self):
        # The real amtech elevator sequence: months 10, 1, 4, 7 - all == 1 (mod 3).
        self.assertEqual(
            periods.classify_cadence(["2025-10", "2026-01", "2026-04", "2026-07"]),
            ("quarterly", 1))

    def test_a_differently_anchored_quarterly_is_not_forced_onto_calendar_quarters(self):
        self.assertEqual(
            periods.classify_cadence(["2026-02", "2026-05", "2026-08", "2026-11"]),
            ("quarterly", 2))

    def test_recent_gaps_win_over_old_ones(self):
        # The real rolling greens sequence. Whole-history gaps are 3,2,1,1 -> irregular.
        # Recent gaps are 1,1 -> monthly, which is what it has actually been since May.
        self.assertEqual(
            periods.classify_cadence(
                ["2025-12", "2026-03", "2026-05", "2026-06", "2026-07"]),
            ("monthly", None))

    def test_a_vendor_that_went_bimonthly_to_monthly_reads_monthly(self):
        # The real mitsubishi electric sequence: gaps 2,2,1,1.
        self.assertEqual(
            periods.classify_cadence(
                ["2026-02", "2026-04", "2026-06", "2026-07", "2026-08"]),
            ("monthly", None))

    def test_genuinely_mixed_recent_gaps_are_irregular(self):
        self.assertEqual(
            periods.classify_cadence(
                ["2025-01", "2025-04", "2025-06", "2025-11", "2026-03"]),
            ("irregular", None))

    def test_below_the_gate_only_monthly_or_irregular_are_possible(self):
        # Three observations at 2-month gaps: consistent, but not yet enough.
        cadence, _ = periods.classify_cadence(["2026-02", "2026-04", "2026-06"])
        self.assertIn(cadence, ("monthly", "irregular"))
        self.assertNotEqual(cadence, "even-months")

    def test_unsorted_input_is_handled(self):
        self.assertEqual(periods.classify_cadence(["2026-08", "2026-06", "2026-07"]),
                         ("monthly", None))

    def test_duplicate_months_collapse(self):
        # Two invoices in one month is one observation for cadence purposes.
        self.assertEqual(
            periods.classify_cadence(["2026-06", "2026-06", "2026-07", "2026-08"]),
            ("monthly", None))

    def test_fewer_than_two_observations_is_irregular(self):
        self.assertEqual(periods.classify_cadence(["2026-08"]), ("irregular", None))
        self.assertEqual(periods.classify_cadence([]), ("irregular", None))

    def test_a_single_recent_gap_cannot_reclassify_a_cadence(self):
        # Gaps 3,3,3,1 - one monthly-looking interval at the end of a clean quarterly run.
        # TWO equal gaps are required, so this stays irregular instead of flipping to
        # monthly on the strength of a single interval. Without this case the window could
        # be narrowed to one gap and every other test would still pass, which would quietly
        # undo the "a repeat, not a coincidence" rule the window exists to enforce.
        self.assertEqual(
            periods.classify_cadence(
                ["2025-10", "2026-01", "2026-04", "2026-07", "2026-08"]),
            ("irregular", None))

    def test_three_month_gaps_below_the_gate_are_not_quarterly(self):
        # Two consistent 3-month gaps, but only three observations. Quarterly suppresses
        # instances in eight months of twelve, so it is the classification with the most to
        # lose from being wrong and it must not be reachable below the gate.
        self.assertEqual(
            periods.classify_cadence(["2026-01", "2026-04", "2026-07"]),
            ("irregular", None))

    def test_duplicates_do_not_inflate_the_observation_count(self):
        # Four rows, three distinct months: below the gate, so even-months is unreachable.
        # Deduplication is what the gate counts, so a pair billed twice in one month must
        # not buy its way past the evidence bar with a repeat.
        self.assertEqual(
            periods.classify_cadence(["2026-02", "2026-02", "2026-04", "2026-06"]),
            ("irregular", None))


class AppliesToPeriod(unittest.TestCase):
    def test_monthly_applies_everywhere(self):
        for p in ("July 2026", "August 2026"):
            self.assertTrue(periods.applies_to_period("monthly", None, p))

    def test_irregular_applies_everywhere(self):
        self.assertTrue(periods.applies_to_period("irregular", None, "August 2026"))

    def test_on_demand_never_applies(self):
        # This is what makes an on-demand vendor incapable of being "missing".
        for p in ("July 2026", "August 2026", "September 2026"):
            self.assertFalse(periods.applies_to_period("on-demand", None, p))

    def test_even_months(self):
        self.assertTrue(periods.applies_to_period("even-months", 0, "August 2026"))
        self.assertFalse(periods.applies_to_period("even-months", 0, "July 2026"))

    def test_odd_months(self):
        self.assertTrue(periods.applies_to_period("odd-months", 1, "July 2026"))
        self.assertFalse(periods.applies_to_period("odd-months", 1, "August 2026"))

    def test_quarterly_only_on_its_anchor(self):
        # anchor 1 -> January, April, July, October
        self.assertTrue(periods.applies_to_period("quarterly", 1, "July 2026"))
        self.assertTrue(periods.applies_to_period("quarterly", 1, "October 2026"))
        self.assertFalse(periods.applies_to_period("quarterly", 1, "August 2026"))
        self.assertFalse(periods.applies_to_period("quarterly", 1, "September 2026"))

    def test_quarterly_on_a_different_anchor(self):
        # anchor 2 -> February, May, August, November
        self.assertTrue(periods.applies_to_period("quarterly", 2, "August 2026"))
        self.assertFalse(periods.applies_to_period("quarterly", 2, "July 2026"))

    def test_once_applies_everywhere_and_lets_the_window_rule_decide(self):
        # A one-off is confined by its date: rule (Task 2), not by cadence.
        self.assertTrue(periods.applies_to_period("once", None, "August 2026"))

    def test_on_demand_is_recognised_whatever_its_casing_or_padding(self):
        # cadence is a free TEXT column and Task 12 lets a human set it. A variant spelling
        # must not fall through to the monthly default: that turns a work-order vendor into
        # a standing monthly expectation, which is the permanent false expectation 6.1
        # calls the one unacceptable outcome. An unset cadence still means monthly, because
        # that is what the column's own DEFAULT says.
        for variant in ("on-demand", "On-Demand", " ON-DEMAND ", "On-demand"):
            self.assertFalse(
                periods.applies_to_period(variant, None, "August 2026"), variant)


class CadenceRecentlyChanged(unittest.TestCase):
    """Confidence must not survive a change of rhythm.

    classify_cadence reads two gaps, so a pair that has just shifted is classified on two
    intervals of evidence. Day-of-month spread cannot see this - a vendor can bill on the
    3rd every single time while changing how often it bills - so confidence has to be told
    separately.
    """

    def test_a_steady_monthly_rhythm_has_not_changed(self):
        self.assertFalse(periods.cadence_recently_changed(
            ["2026-01", "2026-02", "2026-03", "2026-04"]))

    def test_a_clean_quarterly_run_has_not_changed(self):
        self.assertFalse(periods.cadence_recently_changed(
            ["2025-10", "2026-01", "2026-04", "2026-07"]))

    def test_a_shift_to_monthly_is_a_change(self):
        # rolling greens, gaps 3,2,1,1: the monthly reading rests on the last two gaps.
        self.assertTrue(periods.cadence_recently_changed(
            ["2025-12", "2026-03", "2026-05", "2026-06", "2026-07"]))

    def test_two_strays_beside_a_quarterly_contract_are_a_change(self):
        # amtech (3,3,3) plus two consecutive repair invoices reads monthly. This is the
        # case the cap exists for.
        self.assertTrue(periods.cadence_recently_changed(
            ["2025-10", "2026-01", "2026-04", "2026-07", "2026-08", "2026-09"]))

    def test_too_little_history_to_have_changed(self):
        # Nothing before the window to disagree with it.
        self.assertFalse(periods.cadence_recently_changed(["2026-07", "2026-08"]))
        self.assertFalse(periods.cadence_recently_changed(
            ["2026-06", "2026-07", "2026-08"]))


if __name__ == "__main__":
    unittest.main()
