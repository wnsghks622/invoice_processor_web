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


class AliasInsideMinimumLength(unittest.TestCase):
    """Tier 2 binds at confidence 1.0 with no human review, so the fragment it searches
    for has to be long enough to actually be evidence of identity.

    Four vendors bootstrapped from the live invoice history normalize to under five
    characters - 'gas' (SoCalGas's alias 'The Gas Company'), 'dwp', 'home', 'at t'. The
    whole-branch review reproduced all of the binds below against the real 86-vendor
    list, every one of them at score 1.0 with vendor_needs_review left at 0, i.e. wrong
    and invisible. processor.match_property already requires len(key) >= 5 on its own
    substring tier; this is the same floor.
    """

    SHORT_ALIAS_VENDORS = [
        # Exactly the four short-normalizing shapes bootstrap_vendors.py produced.
        {"id": 5,  "canonical_name": "SoCalGas", "short_name": "SoCalGas",
         "aliases": "The Gas Company"},                    # normalizes to 'gas'
        {"id": 11, "canonical_name": "AT&T", "short_name": "AT&T",
         "aliases": "AT&T"},                               # normalizes to 'at t'
        {"id": 68, "canonical_name": "HOME SERVICE", "short_name": "HOME",
         "aliases": ""},                                   # normalizes to 'home'
    ]

    def test_short_alias_does_not_swallow_an_unrelated_name(self):
        # 'Home Depot' contains 'home'; without the floor this bound to HOME SERVICE.
        r = vm.match("Home Depot", self.SHORT_ALIAS_VENDORS)
        self.assertNotEqual(r.reason, "alias-inside")
        self.assertNotEqual(r.vendor_id, 68)
        self.assertEqual(r.outcome, "new")

    def test_short_alias_does_not_match_across_a_word_boundary(self):
        # 'gre[at t]ile' contains 'at t' only because normalize() strips punctuation.
        # This is the most alarming of the reproductions: nothing about the two names
        # is related at all.
        r = vm.match("Great Tile Co", self.SHORT_ALIAS_VENDORS)
        self.assertNotEqual(r.reason, "alias-inside")
        self.assertNotEqual(r.vendor_id, 11)
        self.assertEqual(r.outcome, "new")

    def test_short_alias_does_not_match_a_substring_of_a_longer_word(self):
        # '[gas]parian' - 'gas' inside an unrelated surname.
        r = vm.match("Gasparian Plumbing", self.SHORT_ALIAS_VENDORS)
        self.assertNotEqual(r.reason, "alias-inside")
        self.assertNotEqual(r.vendor_id, 5)
        self.assertEqual(r.outcome, "new")

    def test_the_floor_does_not_disable_the_tier(self):
        # The guard must be a floor, not an off switch. 'LADWP' normalizes to exactly
        # five characters, so it sits on the boundary and must still bind - this is the
        # same case MatchTiers.test_alias_inside_extracted_name_binds covers, asserted
        # here at the boundary itself.
        self.assertEqual(len(vm.normalize("LADWP")), vm.MIN_ALIAS_INSIDE_LEN)
        r = vm.match("LADWP - Water and Power Billing Dept", VENDORS)
        self.assertEqual((r.outcome, r.vendor_id, r.reason), ("bind", 2, "alias-inside"))

    def test_a_short_alias_still_identifies_its_own_vendor_exactly(self):
        # The floor removes an alias from the *substring* tier only. Asked about that
        # exact spelling, tier 1 must still bind it - otherwise the guard would have
        # made these four vendors unmatchable rather than merely un-greedy.
        r = vm.match("The Gas Company", self.SHORT_ALIAS_VENDORS)
        self.assertEqual((r.outcome, r.vendor_id, r.reason), ("bind", 5, "exact"))


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


class ResultToRecordFields(unittest.TestCase):
    """A MatchResult has to become two column values. Both outcomes below must set
    vendor_needs_review, because an unreviewed 'suggest' silently binding would defeat
    the point of the queue."""

    def test_bind_sets_vendor_and_clears_flag(self):
        fields = vm.record_fields(vm.MatchResult("bind", 3, 1.0, "exact"))
        self.assertEqual(fields, {"vendor_id": 3, "vendor_needs_review": 0})

    def test_suggest_records_candidate_but_flags_for_review(self):
        fields = vm.record_fields(vm.MatchResult("suggest", 5, 0.88, "close-spelling"))
        self.assertEqual(fields, {"vendor_id": 5, "vendor_needs_review": 1})

    def test_new_leaves_vendor_null_and_flags_for_review(self):
        fields = vm.record_fields(vm.MatchResult("new", None, 0.0, "no-match"))
        self.assertEqual(fields, {"vendor_id": None, "vendor_needs_review": 1})


class RecordCarriesVendorId(unittest.TestCase):
    """End-to-end through the record builder, still with no database: the vendor list is
    passed in, so purity is preserved."""

    def _record(self, vendor_name, vendors):
        from core import processor as ip
        return ip.build_invoice_record(
            {"vendor_name": vendor_name, "invoice_number": "A1",
             "invoice_date": "06/01/2026", "total_amount": "100.00",
             "property": "Kenmore Plaza"},
            source_file="x.pdf", status="OK", date_processed="06/03/2026",
            stored_file="Athens_06_2026.pdf", vendors=vendors,
        )

    def test_known_vendor_binds(self):
        rec = self._record("ATHENS SERVICES", VENDORS)
        self.assertEqual((rec["vendor_id"], rec["vendor_needs_review"]), (1, 0))

    def test_unknown_vendor_queues_and_keeps_raw_name(self):
        rec = self._record("Rolling Greens Nursery", VENDORS)
        self.assertEqual((rec["vendor_id"], rec["vendor_needs_review"]), (None, 1))
        self.assertEqual(rec["vendor_name"], "Rolling Greens Nursery")

    def test_no_vendor_list_touches_no_database(self):
        rec = self._record("Athens Services", None)
        self.assertEqual((rec["vendor_id"], rec["vendor_needs_review"]), (None, 1))

    def test_suggest_band_match_sets_vendor_id_and_still_flags_for_review(self):
        """Carried from Task 9's review: only 'bind' and 'new' were pinned end-to-end through
        build_invoice_record, but 'suggest' is what drives the Fixer page's pre-selected
        dropdown (Task 10) - a suggestion that bound itself silently would defeat the queue.
        'Athen Services' scores 0.9091 against VENDORS[0] ('Athens Services'), inside the
        suggest band (SUGGEST_THRESHOLD <= score < BIND_THRESHOLD)."""
        rec = self._record("Athen Services", VENDORS)
        self.assertEqual((rec["vendor_id"], rec["vendor_needs_review"]), (1, 1))

    def test_record_has_exactly_the_expected_fields(self):
        """Pin the builder's complete key set, not just a handful of values checked above.
        db.insert_invoice filters `rec` down to db.INVOICE_COLUMNS rather than erroring on
        an unknown one - so a field silently dropped (or renamed) from build_invoice_record's
        return dict would never be written to the database, and no test that only checks
        individual values (like the ones above) would ever notice. A caught-earlier real
        example: a mutation that deleted the "description" key left the full suite green."""
        rec = self._record("Athens Services", VENDORS)
        self.assertEqual(set(rec.keys()), {
            "status", "vendor_name", "vendor_id", "vendor_needs_review", "invoice_number",
            "unit", "invoice_date", "invoice_date_iso", "due_date", "amount", "amount_text",
            "description", "line_items", "property", "source_file", "date_processed",
            "entered_in_yardi", "stored_file", "needs_review", "origin",
        })


class ShortNameDerivation(unittest.TestCase):
    """short_name()/unique_short_name(), moved here from scripts/bootstrap_vendors.py so the
    Fixer page's 'create new vendor' box and the one-off bootstrap derive names the same way.
    bootstrap keeps importing them under its old private names, so tests/test_bootstrap.py
    still pins the same behaviour from the other side."""

    def test_a_short_name_is_the_first_meaningful_word(self):
        self.assertEqual(vm.short_name("Athens Services"), "Athens")

    def test_a_long_name_becomes_an_acronym(self):
        self.assertEqual(vm.short_name("Los Angeles Department of Water and Power"), "LADOWAP")

    def test_punctuation_is_stripped_so_the_result_is_filename_safe(self):
        self.assertEqual(vm.short_name("A.B.C. Plumbing"), "ABC")

    def test_a_blank_name_falls_back_rather_than_raising(self):
        """short_name() feeds a NOT NULL column. An invoice whose vendor string is blank must
        not take the route down with an IndexError on words[0]."""
        self.assertEqual(vm.short_name("   "), "Vendor")

    def test_a_first_word_of_pure_punctuation_still_yields_a_name(self):
        """Stripping to filename-safe characters can empty the first word ('***' -> '').
        short_name() feeds a NOT NULL column and prefills a required form box, so it falls
        back the same way a blank name does rather than handing back ''."""
        self.assertEqual(vm.short_name("*** Plumbing"), "Vendor")

    def test_a_taken_short_name_is_suffixed_rather_than_colliding(self):
        used = {"black"}
        self.assertEqual(vm.unique_short_name("Black Jack Market", used), "Black2")
        self.assertEqual(vm.unique_short_name("Black Water Operations", used), "Black3")

    def test_the_used_set_is_matched_case_insensitively(self):
        """vendors.short_name is UNIQUE but SQLite's UNIQUE is case-SENSITIVE, so 'Black' and
        'BLACK' would both insert. Suffixing on a case-insensitive compare is what keeps the
        vendor list readable, not what keeps the insert legal."""
        used = {"black"}
        self.assertEqual(vm.unique_short_name("BLACK MARKET", used), "BLACK2")


class IsExact(unittest.TestCase):
    """is_exact(): tier 1's rule, exposed. The Fixer page's create-vendor sweep binds every
    queued invoice printing the same string, and it must apply exactly the bar match() applies
    - a second hand-rolled comparison in app.py would drift from this one."""

    def test_case_and_whitespace_only_differences_are_exact(self):
        self.assertTrue(vm.is_exact("Athens Services", "  athens   SERVICES "))

    def test_a_punctuation_difference_is_not_exact(self):
        """Deliberately narrower than normalize(): 'Mitsubishi Electric US Inc' is a close
        spelling of 'Mitsubishi Electric US, Inc.', not the same string. The sweep must leave
        that second invoice queued for a human."""
        self.assertFalse(vm.is_exact("Mitsubishi Electric US Inc",
                                     "Mitsubishi Electric US, Inc."))

    def test_two_blanks_are_not_exact(self):
        """Guards the sweep: without this, creating a vendor from one blank-vendor invoice
        would bind every other blank-vendor invoice in the queue to it."""
        self.assertFalse(vm.is_exact("", ""))
        self.assertFalse(vm.is_exact("   ", None))


if __name__ == "__main__":
    unittest.main()
