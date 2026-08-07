# -*- coding: utf-8 -*-
"""Pure helpers in scripts/bootstrap_vendors.py: deciding whether a cluster is already
known, and picking a collision-free short_name for it. No database - db.py's write path
(add_vendor / update_vendor_identity) is exercised by running the script itself against
the throwaway copy, not here (see task-8-report.md).

This surface has produced two real bugs (fix rounds 1 and 2) with no automated coverage
before now, which is why the second one only turned up in review.

Run from the project root:

    python -m unittest discover -s tests -t . -v
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import bootstrap_vendors as boot


class ClusterIsKnown(unittest.TestCase):
    """Regression coverage for the fix-round-2 bug: the dedup check tested only
    group[0] (the cluster's most-frequent spelling) against the known-identity set, so a
    cluster whose canonical spelling was brand new but which also contained an
    already-known alias at a non-canonical position bypassed the check entirely and
    would create a duplicate vendor."""

    def test_known_alias_at_non_canonical_position_is_recognized(self):
        # Mirrors the real LADWP shape the reviewer reproduced: the cluster's
        # most-frequent spelling is a brand new variant that has never been seen before,
        # but a less-frequent member of the same cluster is already a known alias of the
        # existing LADWP vendor.
        group = ["LADWP Consolidated Billing Statement", "LA DWP"]
        existing = {"ladwp", "la dwp", "dept of water and power"}
        self.assertTrue(boot._cluster_is_known(group, existing))

    def test_cluster_with_no_known_member_is_not_known(self):
        # Contrast case: proves the check isn't vacuously true - a cluster that matches
        # nothing in `existing` must still be treated as new.
        group = ["Rolling Greens Nursery", "Rolling Greens"]
        existing = {"ladwp", "la dwp"}
        self.assertFalse(boot._cluster_is_known(group, existing))


class KnownIdentities(unittest.TestCase):
    def test_alias_is_known_when_canonical_name_is_blank(self):
        # The exact LADWP/SCE shape that caused the fix-round-1 bug: these vendors
        # predate this bootstrap script, created with a short_name and curated aliases,
        # canonical_name still blank. canonical_name alone would miss all of this.
        vendors = [{
            "short_name": "LADWP", "canonical_name": "",
            "aliases": "Los Angeles Department of Water and Power; LA DWP; DWP",
        }]
        known = boot._known_identities(vendors)
        self.assertIn("ladwp", known)                                       # short_name
        self.assertIn("la dwp", known)                                      # an alias
        self.assertIn("los angeles department of water and power", known)   # an alias

    def test_blank_fields_contribute_nothing(self):
        vendors = [{"short_name": "X", "canonical_name": "", "aliases": ""}]
        self.assertEqual(boot._known_identities(vendors), {"x"})


class UniqueShortName(unittest.TestCase):
    def test_distinct_names_across_repeated_collisions(self):
        # vendors.short_name is UNIQUE. Three different real vendors ('Black Jack
        # Market', 'Black Water Operations', 'Black Forest Bakery') all reduce to the
        # same first word - each call must both avoid every prior pick and register its
        # own, so a fourth collision doesn't repeat a name already handed out.
        used = {"black"}
        first = boot._unique_short_name("Black Jack Market", used)
        second = boot._unique_short_name("Black Water Operations", used)
        third = boot._unique_short_name("Black Forest Bakery", used)
        self.assertEqual([first, second, third], ["Black2", "Black3", "Black4"])
        self.assertEqual(used, {"black", "black2", "black3", "black4"})

    def test_case_insensitive_collision_detection(self):
        # vendors.short_name has no COLLATE NOCASE, so the database itself would allow
        # 'Black' and 'BLACK' as two separate rows - _unique_short_name is intentionally
        # stricter than the column constraint and must treat them as the same name.
        used = {"black"}
        self.assertEqual(boot._unique_short_name("BLACK MARKET", used), "BLACK2")


if __name__ == "__main__":
    unittest.main()
