# -*- coding: utf-8 -*-
"""Recurrence profiles built from invoice history.

Profiles are what turn "this vendor bills this property" into "and it should have arrived
by the 7th". They read invoice_date_iso and group on vendor_id - never on the vendor
string, which is the whole reason the vendor identity layer exists, and never on
date_processed, which records when the user got to the invoice rather than when the vendor
billed.

Run from the project root:

    python -m unittest discover -s tests -t . -v
"""
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import db, expectations, periods


def make_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db._SCHEMA)
    db._ensure_columns(conn)
    conn.execute("INSERT INTO properties (id, canonical_name) VALUES (1, 'Kenmore Plaza')")
    conn.execute("INSERT INTO vendors (id, short_name) VALUES (7, 'Athens')")
    return conn


def add_invoice(conn, iso, property_id=1, vendor_id=7, day_note="",
                vendor_name="", date_processed=""):
    # Look the name up rather than hard-coding it. invoices stores the property NAME and
    # build_profiles maps that name back through the properties table, so a literal here
    # silently drops every row whose name is not registered - which is exactly what a
    # hard-coded "Other" did: property 2 was inserted as 'Solair', the invoice was written
    # as 'Other', the lookup missed, and the pair vanished from the profiles.
    name = conn.execute("SELECT canonical_name FROM properties WHERE id=?",
                        (property_id,)).fetchone()["canonical_name"]
    conn.execute(
        "INSERT INTO invoices (property, vendor_id, invoice_date, invoice_date_iso, "
        "vendor_name, date_processed) VALUES (?, ?, ?, ?, ?, ?)",
        (name, vendor_id, day_note or iso, iso, vendor_name, date_processed))


class BuildProfiles(unittest.TestCase):
    def test_a_pair_seen_in_two_months_gets_a_profile(self):
        conn = make_db()
        add_invoice(conn, "2026-06-05")
        add_invoice(conn, "2026-07-06")
        profiles = expectations.build_profiles(conn=conn)
        self.assertIn((1, 7), profiles)

    def test_a_pair_seen_once_is_not_a_profile(self):
        conn = make_db()
        add_invoice(conn, "2026-07-06")
        self.assertEqual(expectations.build_profiles(conn=conn), {})

    def test_due_day_is_the_median_and_spread_is_the_half_width(self):
        # due_spread is a HALF-WIDTH, not the range: periods.resolve_window applies it as
        # due_day - spread .. due_day + spread. Days 4, 6, 8 have a range of 4, so the
        # half-width is 2 and the window is exactly 4..8.
        conn = make_db()
        for iso in ("2026-05-04", "2026-06-06", "2026-07-08"):
            add_invoice(conn, iso)
        p = expectations.build_profiles(conn=conn)[(1, 7)]
        self.assertEqual(p["due_day"], 6)
        self.assertEqual(p["due_spread"], 2)
        self.assertEqual(
            periods.resolve_window("learned", "August 2026",
                                   due_day=p["due_day"], due_spread=p["due_spread"]),
            ("2026-08-04", "2026-08-08"))

    def test_one_vendor_id_with_two_name_spellings_is_one_profile(self):
        # The first rule this module exists for. Both rows are the same vendor, so this is
        # one pair with two observations. Grouping on the string would make it two pairs of
        # one month each, and a single observation is not a profile at all - so the wrong
        # grouping produces NOTHING here rather than something subtly off.
        conn = make_db()
        add_invoice(conn, "2026-06-05", vendor_name="Athens Services")
        add_invoice(conn, "2026-07-05", vendor_name="ATHENS SERVICES, INC.")
        profiles = expectations.build_profiles(conn=conn)
        self.assertEqual(set(profiles), {(1, 7)})
        self.assertEqual(profiles[(1, 7)]["n"], 2)

    def test_the_day_comes_from_the_invoice_date_not_the_processing_date(self):
        # The second rule. date_processed is when the user got to the invoice - 15 days of
        # spread across the live history against invoice_date's 2 - so reading it would
        # learn the user's batching habit and report it as the vendor's schedule. Both
        # columns are populated here, because a fixture that leaves date_processed empty
        # cannot tell "reads the right column" from "reads a blank one".
        conn = make_db()
        add_invoice(conn, "2026-06-05", date_processed="2026-06-25")
        add_invoice(conn, "2026-07-05", date_processed="2026-07-27")
        p = expectations.build_profiles(conn=conn)[(1, 7)]
        self.assertEqual(p["due_day"], 5)
        self.assertEqual(p["due_spread"], 0)

    def test_confidence_high_needs_three_observations_and_a_tight_spread(self):
        conn = make_db()
        for iso in ("2026-05-05", "2026-06-06", "2026-07-05"):
            add_invoice(conn, iso)
        self.assertEqual(expectations.build_profiles(conn=conn)[(1, 7)]["confidence"], "high")

    def test_confidence_medium_when_the_spread_is_wider(self):
        conn = make_db()
        for iso in ("2026-05-02", "2026-06-09", "2026-07-05"):
            add_invoice(conn, iso)
        self.assertEqual(expectations.build_profiles(conn=conn)[(1, 7)]["confidence"], "medium")

    def test_confidence_low_with_only_two_observations(self):
        conn = make_db()
        add_invoice(conn, "2026-06-05")
        add_invoice(conn, "2026-07-06")
        self.assertEqual(expectations.build_profiles(conn=conn)[(1, 7)]["confidence"], "low")

    def test_cadence_comes_from_the_classifier(self):
        conn = make_db()
        for iso in ("2025-10-20", "2026-01-20", "2026-04-20", "2026-07-20"):
            add_invoice(conn, iso)
        p = expectations.build_profiles(conn=conn)[(1, 7)]
        self.assertEqual((p["cadence"], p["anchor"]), ("quarterly", 1))

    def test_rows_without_a_parsed_date_are_ignored(self):
        # An unparseable date is in the Fixer queue, not evidence of a billing schedule.
        conn = make_db()
        add_invoice(conn, "2026-06-05")
        conn.execute("INSERT INTO invoices (property, vendor_id, invoice_date, "
                     "invoice_date_iso) VALUES ('Kenmore Plaza', 7, '06262026', '')")
        p = expectations.build_profiles(conn=conn)
        self.assertEqual(p, {})     # only one usable observation left

    def test_rows_without_a_vendor_id_are_ignored(self):
        conn = make_db()
        conn.execute("INSERT INTO invoices (property, invoice_date_iso) "
                     "VALUES ('Kenmore Plaza', '2026-06-05')")
        conn.execute("INSERT INTO invoices (property, invoice_date_iso) "
                     "VALUES ('Kenmore Plaza', '2026-07-05')")
        self.assertEqual(expectations.build_profiles(conn=conn), {})

    def test_two_invoices_in_one_month_are_one_observation_for_cadence(self):
        conn = make_db()
        add_invoice(conn, "2026-06-05")
        add_invoice(conn, "2026-06-20")
        add_invoice(conn, "2026-07-05")
        p = expectations.build_profiles(conn=conn)[(1, 7)]
        self.assertEqual(p["cadence"], "monthly")

    def test_different_properties_are_different_profiles(self):
        conn = make_db()
        conn.execute("INSERT INTO properties (id, canonical_name) VALUES (2, 'Solair')")
        add_invoice(conn, "2026-06-05", property_id=1)
        add_invoice(conn, "2026-07-05", property_id=1)
        add_invoice(conn, "2026-06-20", property_id=2)
        add_invoice(conn, "2026-07-20", property_id=2)
        profiles = expectations.build_profiles(conn=conn)
        self.assertEqual(set(profiles), {(1, 7), (2, 7)})

    def test_a_freshly_changed_cadence_cannot_be_high_confidence(self):
        # The amtech failure: a clean quarterly contract billed on the 5th every time,
        # plus two consecutive repair invoices also on the 5th. Day spread is 0, n is 6,
        # so the spread rule alone says "high" - and the cadence now reads monthly off
        # two intervals. High + monthly means a 2-day slack under 6.3 and a missing-bill
        # warning in the eight months a year this pair was never going to bill.
        conn = make_db()
        for iso in ("2025-10-05", "2026-01-05", "2026-04-05",
                    "2026-07-05", "2026-08-05", "2026-09-05"):
            add_invoice(conn, iso)
        p = expectations.build_profiles(conn=conn)[(1, 7)]
        self.assertEqual(p["cadence"], "monthly")
        self.assertEqual(p["confidence"], "medium")


from core import ledger


class Sync(unittest.TestCase):
    def _pair(self, conn, isos):
        for iso in isos:
            add_invoice(conn, iso)

    def test_creates_one_expect_obligation_per_profile(self):
        conn = make_db()
        self._pair(conn, ["2026-06-05", "2026-07-06"])
        result = expectations.sync(conn=conn)
        self.assertEqual(result["created"], 1)
        obs = ledger.active_obligations(conn=conn)
        self.assertEqual(len(obs), 1)
        self.assertEqual((obs[0]["kind"], obs[0]["source"]), ("EXPECT", "learned"))
        self.assertEqual((obs[0]["property_id"], obs[0]["vendor_id"]), (1, 7))

    def test_is_idempotent(self):
        conn = make_db()
        self._pair(conn, ["2026-06-05", "2026-07-06"])
        expectations.sync(conn=conn)
        again = expectations.sync(conn=conn)
        self.assertEqual(again["created"], 0)
        self.assertEqual(len(ledger.active_obligations(conn=conn)), 1)

    def test_updates_a_learned_obligation_when_the_profile_moves(self):
        conn = make_db()
        self._pair(conn, ["2026-05-05", "2026-06-05"])
        expectations.sync(conn=conn)
        add_invoice(conn, "2026-07-05")            # now 3 observations, tight
        expectations.sync(conn=conn)
        ob = ledger.active_obligations(conn=conn)[0]
        self.assertEqual(ob["confidence"], "high")

    def test_never_overwrites_an_obligation_you_edited(self):
        # A manual edit pins the obligation. Recompute must not revert a decision.
        conn = make_db()
        self._pair(conn, ["2026-06-05", "2026-07-06"])
        expectations.sync(conn=conn)
        ob = ledger.active_obligations(conn=conn)[0]
        ledger.update_obligation(ob["id"], conn=conn, cadence="on-demand", source="manual")

        result = expectations.sync(conn=conn)
        self.assertEqual(result["pinned"], 1)
        after = ledger.get_obligation(ob["id"], conn=conn)
        self.assertEqual(after["cadence"], "on-demand")
        self.assertEqual(after["source"], "manual")

    def test_a_new_pair_starts_unconfirmed(self):
        # Promotion must not silently create a confident monthly expectation - a repair
        # vendor that happens to bill twice would then nag every month afterwards.
        conn = make_db()
        self._pair(conn, ["2026-06-05", "2026-07-06"])
        expectations.sync(conn=conn)
        ob = ledger.active_obligations(conn=conn)[0]
        self.assertEqual(ob["notes"], expectations.UNCONFIRMED)

    def test_confirming_clears_the_unconfirmed_marker(self):
        conn = make_db()
        self._pair(conn, ["2026-06-05", "2026-07-06"])
        expectations.sync(conn=conn)
        ob = ledger.active_obligations(conn=conn)[0]
        ledger.update_obligation(ob["id"], conn=conn, notes="", source="manual")
        after = ledger.get_obligation(ob["id"], conn=conn)
        self.assertEqual(after["notes"], "")

    def test_sync_does_not_re_mark_an_expectation_you_confirmed(self):
        # Clearing the marker is how you say "yes, this really does recur". Sync still has
        # to refresh the learned numbers afterwards, so the risk is that it puts the marker
        # back on its way past and sends the obligation round the confirmation loop again -
        # which would make confirming pointless and is the cry-wolf direction 6.6 exists to
        # prevent. Note source stays 'learned' here: this is the UPDATE path, not the
        # pinned path that test_never_overwrites_an_obligation_you_edited covers.
        conn = make_db()
        self._pair(conn, ["2026-06-05", "2026-07-06"])
        expectations.sync(conn=conn)
        ob = ledger.active_obligations(conn=conn)[0]
        ledger.update_obligation(ob["id"], conn=conn, notes="")

        add_invoice(conn, "2026-08-05")
        expectations.sync(conn=conn)
        after = ledger.get_obligation(ob["id"], conn=conn)
        self.assertEqual(after["notes"], "")
        self.assertEqual(after["confidence"], "high")     # refreshed, not frozen

    def test_one_vendor_at_two_properties_stays_two_obligations(self):
        # A vendor billing several properties is the normal case in this data, not an edge
        # one, so `existing` has to be keyed on the PAIR. Keyed on vendor_id alone the two
        # rows collapse, only the last is reachable, and the other property's obligation is
        # created once and then never refreshed - it silently freezes at whatever it was on
        # the day it was promoted. The SECOND sync is what exposes this: on the first,
        # `existing` is empty and both pairs take the create path whatever the key is.
        conn = make_db()
        conn.execute("INSERT INTO properties (id, canonical_name) VALUES (2, 'Solair')")
        for iso in ("2026-05-05", "2026-06-05"):
            add_invoice(conn, iso, property_id=1)
        for iso in ("2025-10-20", "2026-01-20", "2026-04-20", "2026-07-20"):
            add_invoice(conn, iso, property_id=2)
        expectations.sync(conn=conn)

        add_invoice(conn, "2026-07-05", property_id=1)   # property 1 now n=3 and tight
        expectations.sync(conn=conn)

        by_property = {o["property_id"]: o for o in ledger.active_obligations(conn=conn)}
        self.assertEqual(set(by_property), {1, 2})
        self.assertEqual(by_property[1]["cadence"], "monthly")
        self.assertEqual(by_property[1]["confidence"], "high")    # refreshed, not frozen
        self.assertEqual(by_property[2]["cadence"], "quarterly")

    def test_the_obligation_carries_the_learned_window(self):
        conn = make_db()
        self._pair(conn, ["2026-05-05", "2026-06-06", "2026-07-05"])
        expectations.sync(conn=conn)
        ob = ledger.active_obligations(conn=conn)[0]
        self.assertEqual(ob["window_rule"], "learned")
        self.assertEqual(ob["cadence"], "monthly")


class SatisfyPeriod(unittest.TestCase):
    def _expect(self, conn):
        add_invoice(conn, "2026-06-05")
        add_invoice(conn, "2026-07-05")
        expectations.sync(conn=conn)
        ledger.open_period("August 2026", conn=conn)

    def test_an_arriving_invoice_satisfies_its_instance(self):
        conn = make_db()
        self._expect(conn)
        add_invoice(conn, "2026-08-05")
        self.assertEqual(expectations.satisfy_period("August 2026", conn=conn), 1)
        inst = ledger.instances_for_period("August 2026", conn=conn)[0]
        self.assertEqual(inst["state"], "done")
        self.assertTrue(inst["satisfied_by"].startswith("invoice:"))

    def test_an_invoice_in_another_month_does_not_satisfy(self):
        conn = make_db()
        self._expect(conn)
        add_invoice(conn, "2026-09-05")
        self.assertEqual(expectations.satisfy_period("August 2026", conn=conn), 0)
        self.assertEqual(
            ledger.instances_for_period("August 2026", conn=conn)[0]["state"], "open")

    def test_an_invoice_for_another_vendor_does_not_satisfy(self):
        conn = make_db()
        self._expect(conn)
        conn.execute("INSERT INTO vendors (id, short_name) VALUES (8, 'Other')")
        add_invoice(conn, "2026-08-05", vendor_id=8)
        self.assertEqual(expectations.satisfy_period("August 2026", conn=conn), 0)

    def test_the_same_vendor_at_another_property_does_not_satisfy(self):
        # Athens bills a dozen properties in the live data, so matching on vendor alone is
        # not a hypothetical mistake. It would let one property's invoice close another
        # property's expectation, and the month page would then show a bill as ARRIVED that
        # never came - a false negative on the one thing this feature exists to catch. The
        # default fixture has a single property, so without this case a matcher keyed on
        # vendor_id alone passes the entire suite.
        conn = make_db()
        self._expect(conn)
        conn.execute("INSERT INTO properties (id, canonical_name) VALUES (2, 'Solair')")
        add_invoice(conn, "2026-08-05", property_id=2)
        self.assertEqual(expectations.satisfy_period("August 2026", conn=conn), 0)
        self.assertEqual(
            ledger.instances_for_period("August 2026", conn=conn)[0]["state"], "open")

    def test_it_is_idempotent(self):
        conn = make_db()
        self._expect(conn)
        add_invoice(conn, "2026-08-05")
        expectations.satisfy_period("August 2026", conn=conn)
        self.assertEqual(expectations.satisfy_period("August 2026", conn=conn), 0)

    def test_a_second_invoice_does_not_double_satisfy(self):
        conn = make_db()
        self._expect(conn)
        add_invoice(conn, "2026-08-05")
        add_invoice(conn, "2026-08-19")
        self.assertEqual(expectations.satisfy_period("August 2026", conn=conn), 1)

    def test_evidence_overrides_a_skip(self):
        # You said it was not coming; it came. The invoice wins, and the note is kept.
        conn = make_db()
        self._expect(conn)
        inst = ledger.instances_for_period("August 2026", conn=conn)[0]
        ledger.set_instance_state(inst["id"], "skipped", note="vendor said none this month",
                                  satisfied_by="", conn=conn)
        add_invoice(conn, "2026-08-05")
        self.assertEqual(expectations.satisfy_period("August 2026", conn=conn), 1)
        after = ledger.instances_for_period("August 2026", conn=conn)[0]
        self.assertEqual(after["state"], "done")
        self.assertEqual(after["note"], "vendor said none this month")

    def test_a_reminder_is_never_auto_satisfied(self):
        # ACTION instances are yours to tick; no invoice can close them.
        conn = make_db()
        ledger.add_obligation(conn=conn, kind="ACTION", title="call Michelle",
                              window_rule="day:12", cadence="monthly")
        ledger.open_period("August 2026", conn=conn)
        add_invoice(conn, "2026-08-12")
        self.assertEqual(expectations.satisfy_period("August 2026", conn=conn), 0)


if __name__ == "__main__":
    unittest.main()
