# -*- coding: utf-8 -*-
"""Unit tests for the pure helpers that guard the money math - duplicate keys, amount
parsing, over-split merging, property matching, subset-sum placement, and the rec/statement
text parsers. No database or filesystem writes.

Run from the project root:

    python -m unittest discover -s tests -t . -v
"""
import datetime
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import state
from core import bankrec
from core import processor as ip


class ParseAmount(unittest.TestCase):
    def test_plain_and_currency(self):
        self.assertEqual(ip._parse_amount("$1,228.50"), 1228.50)
        self.assertEqual(ip._parse_amount("1234"), 1234.0)
        self.assertEqual(ip._parse_amount(80), 80.0)
        self.assertEqual(ip._parse_amount(80.5), 80.5)

    def test_accounting_negatives(self):
        self.assertEqual(ip._parse_amount("(123.45)"), -123.45)
        self.assertEqual(ip._parse_amount("$ (1,000.00)"), -1000.0)
        self.assertEqual(ip._parse_amount("-50.25"), -50.25)
        self.assertEqual(ip._parse_amount("123.45-"), -123.45)

    def test_unparseable(self):
        for bad in (None, "", "abc", "12.34.56", True, "-", "."):
            self.assertIsNone(ip._parse_amount(bad), repr(bad))


class DuplicateKey(unittest.TestCase):
    def test_real_number_key_ignores_amount(self):
        a = ip._invoice_key("LADWP", "INV-1001", "", property_name="Solair", amount=100.0)
        b = ip._invoice_key("LADWP", "INV-1001", "", property_name="Solair", amount=999.0)
        self.assertEqual(a, b)

    def test_property_separates_same_number(self):
        a = ip._invoice_key("LADWP", "INV-1001", "", property_name="Solair")
        b = ip._invoice_key("LADWP", "INV-1001", "", property_name="Kenmore Plaza")
        self.assertNotEqual(a, b)

    def test_date_based_folds_amount_in(self):
        a = ip._invoice_key("SoCalGas", "07212026", "", property_name="Solair",
                            amount=141.05, date_based=True)
        b = ip._invoice_key("SoCalGas", "07212026", "", property_name="Solair",
                            amount=99.00, date_based=True)
        self.assertNotEqual(a, b)

    def test_no_number_means_no_key(self):
        self.assertEqual(ip._invoice_key("Vendor", "", ""), "")

    def test_is_date_based_number(self):
        self.assertTrue(ip._is_date_based_number("07212026", "07/21/2026", None))
        self.assertFalse(ip._is_date_based_number("INV-1001", "07/21/2026", None))
        self.assertFalse(ip._is_date_based_number("07212026", "01/01/2020", None))

    def test_resolve_invoice_number_falls_back_to_date(self):
        self.assertEqual(ip.resolve_invoice_number({"invoice_number": " A-77 "}), "A-77")
        self.assertEqual(
            ip.resolve_invoice_number({"invoice_number": None, "invoice_date": "07/21/2026"}),
            "07212026")
        self.assertEqual(ip.resolve_invoice_number({"invoice_number": "n/a"}), "")


class MergeOversplit(unittest.TestCase):
    def test_same_number_and_total_merge_units(self):
        merged = ip.merge_oversplit_invoices([
            {"invoice_number": "INV-9", "total_amount": "$500.00", "unit": "APT 1"},
            {"invoice_number": "INV-9", "total_amount": "500.00", "unit": "APT 2"},
        ])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["unit"], "APT 1; APT 2")

    def test_different_totals_stay_separate(self):
        merged = ip.merge_oversplit_invoices([
            {"invoice_number": "INV-9", "total_amount": "500.00", "unit": "APT 1"},
            {"invoice_number": "INV-9", "total_amount": "250.00", "unit": "APT 2"},
        ])
        self.assertEqual(len(merged), 2)


class SummarizeLineItems(unittest.TestCase):
    def test_single_item_is_not_itemized(self):
        self.assertEqual(ip._summarize_line_items([{"description": "x", "amount": "1"}]), "")

    def test_two_items(self):
        note = ip._summarize_line_items([
            {"description": "Labor", "amount": "100"},
            {"description": "Parts", "amount": "$50.25"},
        ])
        self.assertTrue(note.startswith("2 items: "))
        self.assertIn("Labor - $100.00", note)
        self.assertIn("Parts - $50.25", note)


class MatchProperty(unittest.TestCase):
    PROPS = [
        ("Solair", {"solair", "3785wilshire"}, ["3785 Wilshire"]),
        ("Kenmore Plaza", {"kenmoreplaza", "4055kenmore"}, ["4055 Kenmore"]),
    ]

    def test_exact_and_alias(self):
        self.assertEqual(ip.match_property("SOLAIR", self.PROPS), "Solair")
        self.assertEqual(ip.match_property("3785 Wilshire", self.PROPS), "Solair")

    def test_alias_inside_longer_address(self):
        self.assertEqual(
            ip.match_property("3785 Wilshire Blvd, Los Angeles CA 90010", self.PROPS), "Solair")

    def test_no_confident_match(self):
        self.assertIsNone(ip.match_property("Totally Unrelated Place", self.PROPS))
        self.assertIsNone(ip.match_property("", self.PROPS))


class SubsetSum(unittest.TestCase):
    def test_exact_pair(self):
        idx = bankrec._subset_sum([8879.96, 39757.99, 12.34], 48637.95)
        self.assertEqual(sorted(idx), [0, 1])

    def test_minimal_size_preferred(self):
        # 30 = 10+20 but also 30 alone; the single item must win.
        idx = bankrec._subset_sum([10.0, 20.0, 30.0], 30.0)
        self.assertEqual(idx, [2])

    def test_each_index_used_once(self):
        idx = bankrec._subset_sum([5.0, 5.0], 10.0)
        self.assertEqual(sorted(idx), [0, 1])
        self.assertIsNone(bankrec._subset_sum([5.0], 10.0))

    def test_maxn_respected(self):
        vals = [1.0] * 10
        self.assertIsNone(bankrec._subset_sum(vals, 9.0, maxn=8))
        self.assertEqual(len(bankrec._subset_sum(vals, 8.0, maxn=8)), 8)

    def test_no_match(self):
        self.assertIsNone(bankrec._subset_sum([1.11, 2.22], 9.99))

    def test_big_pool_terminates_fast(self):
        # The old combinations version would try C(60,8) ~ 2.5 billion subsets here.
        vals = [round(13.07 * (i + 1), 2) for i in range(60)]
        t0 = time.monotonic()
        bankrec._subset_sum(vals, 999999.37)
        self.assertLess(time.monotonic() - t0, 3.0)


STATEMENT_FIXTURE = """Deposits and Additions
07/03/2026 Remote Deposit $8,879.96
07/05/2026 Remote Deposit $39,757.99
Other Debits
07/10/2026 ACH Settlement KORUS $48,637.95
Checks Cleared
1234* 07/12/2026 $500.00
Daily Balance Summary
07/03/2026 $100,000.00
"""

REC_FIXTURE = """Bank Reconciliation Report
Cleared Checks
07/12/2026 1234 Vendor payment 500.00
Total Cleared Checks 500.00
Cleared Deposits
07/03/2026 9999 Remote deposit 8,879.96
Total Cleared Deposits 8,879.96
Difference 0.00
"""


class StatementAndRecParsers(unittest.TestCase):
    def test_parse_statement_sections_signs_and_flags(self):
        with mock.patch.object(bankrec, "text_layer", lambda p: STATEMENT_FIXTURE):
            lines = bankrec.parse_statement(["fake.pdf"])
        self.assertEqual(len(lines), 4)          # the daily-balance block must NOT parse
        self.assertEqual([l.sign for l in lines], ["credit", "credit", "debit", "debit"])
        self.assertEqual([l.amount for l in lines], [8879.96, 39757.99, 48637.95, 500.00])
        self.assertTrue(lines[2].is_settlement)
        self.assertEqual(lines[3].check_no, "1234")
        self.assertEqual([l.seq for l in lines], [1, 2, 3, 4])

    def test_parse_rec_checks_deposits_difference(self):
        with mock.patch.object(bankrec, "text_layer", lambda p: REC_FIXTURE):
            parsed = bankrec.parse_rec("fake.pdf")
        self.assertIsNone(parsed["error"])
        self.assertEqual(parsed["difference"], "0.00")
        self.assertEqual(len(parsed["checks"]), 1)
        self.assertEqual(len(parsed["deposits"]), 1)
        self.assertEqual(parsed["checks"][0]["amount"], 500.00)
        self.assertEqual(parsed["deposits"][0]["tran"], "9999")


class MonthDirSort(unittest.TestCase):
    def test_chronological_not_alphabetical(self):
        dirs = [Path("May 2026 Bank Rec"), Path("June 2026 Bank Rec"),
                Path("December 2025 Bank Rec")]
        newest_first = sorted(dirs, key=state._month_dir_sort_key, reverse=True)
        self.assertEqual([d.name for d in newest_first],
                         ["June 2026 Bank Rec", "May 2026 Bank Rec",
                          "December 2025 Bank Rec"])

    def test_unparseable_sorts_last(self):
        self.assertEqual(state._month_dir_sort_key(Path("weird folder")),
                         datetime.datetime.min)


if __name__ == "__main__":
    unittest.main()
