# -*- coding: utf-8 -*-
"""Vendor identity matching. Expectations key on vendor_id, not on a string, so a
mis-tiered match here turns one confident monthly expectation into two sporadic ones.

Run from the project root:

    python -m unittest discover -s tests -t . -v
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import vendor_match as vm


VENDORS = [
    {"id": 1, "canonical_name": "Athens Services", "short_name": "Athens",
     "aliases": "ATHENS SERVICES; Athens Svcs"},
    {"id": 2, "canonical_name": "Los Angeles Department of Water and Power",
     "short_name": "LADWP", "aliases": "LADWP; L.A. Dept of Water & Power"},
    {"id": 3, "canonical_name": "Mitsubishi Electric US, Inc.",
     "short_name": "Mitsubishi", "aliases": ""},
]


class Normalize(unittest.TestCase):
    def test_case_punctuation_and_suffixes_collapse(self):
        self.assertEqual(vm.normalize("MITSUBISHI ELECTRIC US, INC."),
                         vm.normalize("Mitsubishi Electric US, Inc."))
        self.assertEqual(vm.normalize("Ganahl Lumber Company"),
                         vm.normalize("GANAHL LUMBER COMPANY"))

    def test_distinct_vendors_do_not_collapse(self):
        self.assertNotEqual(vm.normalize("Athens Services"), vm.normalize("Athena Services"))


class MatchTiers(unittest.TestCase):
    def test_exact_canonical_binds(self):
        r = vm.match("Athens Services", VENDORS)
        self.assertEqual((r.outcome, r.vendor_id), ("bind", 1))
        self.assertEqual(r.reason, "exact")

    def test_case_variant_binds_via_normalization(self):
        r = vm.match("ATHENS SERVICES", VENDORS)
        self.assertEqual((r.outcome, r.vendor_id), ("bind", 1))

    def test_alias_inside_extracted_name_binds(self):
        r = vm.match("LADWP - Water and Power Billing Dept", VENDORS)
        self.assertEqual((r.outcome, r.vendor_id), ("bind", 2))
        self.assertEqual(r.reason, "alias-inside")

    def test_close_spelling_above_threshold_binds(self):
        r = vm.match("Mitsubishi Electric US Inc", VENDORS)
        self.assertEqual((r.outcome, r.vendor_id), ("bind", 3))
        self.assertEqual(r.reason, "close-spelling")

    def test_unknown_vendor_is_new(self):
        r = vm.match("Rolling Greens Nursery", VENDORS)
        self.assertEqual((r.outcome, r.vendor_id), ("new", None))

    def test_empty_input_is_new_not_a_crash(self):
        for raw in ("", "   ", None):
            with self.subTest(raw=raw):
                self.assertEqual(vm.match(raw, VENDORS).outcome, "new")

    def test_no_vendors_configured_is_new(self):
        self.assertEqual(vm.match("Athens Services", []).outcome, "new")


class MatchThresholds(unittest.TestCase):
    """The band boundaries are load-bearing: below BIND a human must confirm."""

    def test_band_constants(self):
        self.assertEqual(vm.BIND_THRESHOLD, 0.92)
        self.assertEqual(vm.SUGGEST_THRESHOLD, 0.80)

    def test_score_just_below_bind_suggests(self):
        r = vm._classify(0.91, vendor_id=7, reason="close-spelling")
        self.assertEqual((r.outcome, r.vendor_id), ("suggest", 7))

    def test_score_at_bind_binds(self):
        r = vm._classify(0.92, vendor_id=7, reason="close-spelling")
        self.assertEqual(r.outcome, "bind")

    def test_score_below_suggest_is_new(self):
        r = vm._classify(0.79, vendor_id=7, reason="close-spelling")
        self.assertEqual((r.outcome, r.vendor_id), ("new", None))


class AppendAlias(unittest.TestCase):
    """Confirming a vendor on the review screen writes that raw spelling into its alias
    list. This is the mechanism that makes the queue shrink toward zero instead of asking
    the same question every month, so it is tested rather than left in a route handler."""

    def test_new_spelling_is_appended(self):
        self.assertEqual(vm.append_alias("Athens Svcs", "ATHENS SERVICES"),
                         "Athens Svcs; ATHENS SERVICES")

    def test_existing_spelling_is_not_duplicated_case_insensitively(self):
        self.assertEqual(vm.append_alias("Athens Svcs; ATHENS SERVICES", "athens services"),
                         "Athens Svcs; ATHENS SERVICES")

    def test_empty_existing_list_yields_just_the_new_alias(self):
        self.assertEqual(vm.append_alias("", "Athens Services"), "Athens Services")

    def test_blank_raw_is_a_no_op(self):
        self.assertEqual(vm.append_alias("Athens Svcs", "   "), "Athens Svcs")
        self.assertEqual(vm.append_alias("Athens Svcs", None), "Athens Svcs")

    def test_whitespace_and_empty_segments_are_cleaned(self):
        self.assertEqual(vm.append_alias(" A ;; B ", "C"), "A; B; C")


class Cluster(unittest.TestCase):
    def test_case_variants_group_together(self):
        groups = vm.cluster([
            "Mitsubishi Electric US, Inc.", "MITSUBISHI ELECTRIC US, INC.",
            "Ganahl Lumber Company", "GANAHL LUMBER COMPANY",
        ])
        sizes = sorted(len(g) for g in groups)
        self.assertEqual(sizes, [2, 2])

    def test_genuinely_different_vendors_stay_separate(self):
        # These four are the real ambiguous cases from the live data. Each pair MUST
        # stay split - merging them silently would erase a real distinction that only
        # a human can adjudicate.
        pairs = [
            ("Michelle Suh", "Michelle Suh (Rooter Plumbing)"),
            ("James Chin", "James Chin (Stamp Reimbursement)"),
            ("City of Los Angeles",
             "City of Los Angeles, Department of Public Works, Bureau of Sanitation"),
            ("South Coast Mechanical, LLC", "South Coast Mechanical, Inc."),
        ]
        for left, right in pairs:
            with self.subTest(pair=(left, right)):
                groups = vm.cluster([left, right])
                self.assertEqual(len(groups), 2, f"{left!r} and {right!r} must not merge")

    def test_singleton_input_gives_one_group(self):
        self.assertEqual(vm.cluster(["Rolling Greens"]), [["Rolling Greens"]])

    def test_empty_input_gives_no_groups(self):
        self.assertEqual(vm.cluster([]), [])


if __name__ == "__main__":
    unittest.main()
