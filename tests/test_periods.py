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


if __name__ == "__main__":
    unittest.main()
