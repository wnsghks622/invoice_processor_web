# -*- coding: utf-8 -*-
"""Invoice-date parsing. This is the highest-risk pure function in the system: every
timing decision reads its output, and a silent failure is indistinguishable from a
vendor who skipped a month.

Run from the project root:

    python -m unittest discover -s tests -t . -v
"""
import datetime
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import dates


class ParseInvoiceDate(unittest.TestCase):
    def test_shapes_present_in_live_data(self):
        cases = {
            "06/01/2026":          datetime.date(2026, 6, 1),
            "5/31/2026":           datetime.date(2026, 5, 31),
            "6/11/26":             datetime.date(2026, 6, 11),
            "06-15-2026":          datetime.date(2026, 6, 15),
            "06-24-26":            datetime.date(2026, 6, 24),
            "2026-05-22 00:00:00": datetime.date(2026, 5, 22),
            "2026-06-03":          datetime.date(2026, 6, 3),
            "Jun 2, 2026":         datetime.date(2026, 6, 2),
            "Jun/01/26":           datetime.date(2026, 6, 1),
            "13-Jul-26":           datetime.date(2026, 7, 13),   # currently fails
            "06262026":            datetime.date(2026, 6, 26),   # currently fails
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(dates.parse_invoice_date(raw), expected)

    def test_ambiguous_two_number_dates_resolve_us_style(self):
        # 07-01-2026 is MM-DD-YYYY or DD-MM-YYYY depending on convention. These are US
        # invoices, so it is July 1st. This is a DECISION, asserted so it cannot drift.
        self.assertEqual(dates.parse_invoice_date("07-01-2026"), datetime.date(2026, 7, 1))
        self.assertEqual(dates.parse_invoice_date("08-01-2026"), datetime.date(2026, 8, 1))
        self.assertEqual(dates.parse_invoice_date("01/02/2026"), datetime.date(2026, 1, 2))

    def test_whitespace_is_tolerated(self):
        self.assertEqual(dates.parse_invoice_date("  06/01/2026 "), datetime.date(2026, 6, 1))

    def test_unparseable_returns_none_not_a_guess(self):
        for raw in ("", "   ", None, "not a date", "13/45/2026", "0", "N/A"):
            with self.subTest(raw=raw):
                self.assertIsNone(dates.parse_invoice_date(raw))


class ToIso(unittest.TestCase):
    def test_formats_as_iso(self):
        self.assertEqual(dates.to_iso("06/01/2026"), "2026-06-01")
        self.assertEqual(dates.to_iso("13-Jul-26"), "2026-07-13")

    def test_failure_is_empty_string_never_none(self):
        # Empty string, not None: it goes straight into a TEXT NOT NULL-ish column and
        # the review queue selects on COALESCE(invoice_date_iso,'') = ''.
        self.assertEqual(dates.to_iso("not a date"), "")
        self.assertEqual(dates.to_iso(None), "")
        self.assertEqual(dates.to_iso(""), "")


if __name__ == "__main__":
    unittest.main()
