# -*- coding: utf-8 -*-
"""Unit tests for the pure helpers that guard the money math - duplicate keys, amount
parsing, over-split merging, property matching, subset-sum placement, and the rec/statement
text parsers. No database or filesystem writes.

Run from the project root:

    python -m unittest discover -s tests -t . -v
"""
import datetime
import os
import sys
import tempfile
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


# A section footer ("1 item(s) totaling $X") repeats the section total. When the section holds
# exactly ONE item that total equals the item's own amount, so parsing the footer as a
# transaction hands the assembler a second, date-less line of the same amount - and a stale
# same-amount invoice lands on it instead of being reported as unplaced.
FOOTER_FIXTURE = """Other Debits
08/28/2026 PAYMENT TO COMM PROPERTY LOAN LOAN XXXXXXXXXXX4283 $58,366.16
1 item(s) totaling $58,366.16
Daily Balance Summary
08/03/2026 $100,000.00
"""

SUBTOTAL_FIXTURE = """Other Debits
08/28/2026 PAYMENT TO COMM PROPERTY LOAN $58,366.16
Subtotal $58,366.16
"""


class StatementSectionFooters(unittest.TestCase):
    def test_item_count_footer_is_not_a_transaction(self):
        with mock.patch.object(bankrec, "text_layer", lambda p: FOOTER_FIXTURE):
            lines = bankrec.parse_statement(["fake.pdf"])
        self.assertEqual([(l.amount, l.date) for l in lines],
                         [(58366.16, "08/28/2026")])

    def test_subtotal_footer_is_not_a_transaction(self):
        with mock.patch.object(bankrec, "text_layer", lambda p: SUBTOTAL_FIXTURE):
            lines = bankrec.parse_statement(["fake.pdf"])
        self.assertEqual([(l.amount, l.date) for l in lines],
                         [(58366.16, "08/28/2026")])


class VerifiedScanProfiling(unittest.TestCase):
    """An invoice with a verified sidecar amount is still read for its NAME signals. A scanned
    (image-only) invoice has no text layer, so skipping OCR left it with no vendor keys - and an
    unverified, OCR'd copy of last month's bill out-scored it on the vendor bonus."""

    SIDECAR = {"us_08_2026.pdf": {"amounts": {58366.16}, "checks": set(),
                                  "date": datetime.datetime(2026, 8, 12), "total": 58366.16}}

    def test_image_only_invoice_gets_vendor_keys_from_ocr(self):
        with mock.patch.object(bankrec, "text_layer", lambda p: ""),              mock.patch.object(bankrec, "ocr_text",
                               lambda p: "U S BANK COMMERCIAL PROPERTY LOAN STATEMENT"):
            docs = bankrec.profile_support(["/x/US_08_2026.pdf"], "auto", self.SIDECAR)
        self.assertIn("loan", docs[0].vendor_keys)
        self.assertIn("property", docs[0].vendor_keys)

    def test_sidecar_amount_stays_authoritative_and_doc_stays_verified(self):
        with mock.patch.object(bankrec, "text_layer", lambda p: ""),              mock.patch.object(bankrec, "ocr_text", lambda p: "U S BANK $999.99 LOAN"):
            docs = bankrec.profile_support(["/x/US_08_2026.pdf"], "auto", self.SIDECAR)
        d = docs[0]
        self.assertEqual(d.content_amounts, {58366.16})   # OCR figures never join the amounts
        self.assertTrue(d.verified)
        self.assertFalse(d.ocr_used)                      # tag reports AMOUNT provenance

    def test_text_layer_invoice_does_not_call_ocr(self):
        def boom(p):
            raise AssertionError("OCR ran on a doc that has a text layer")
        with mock.patch.object(bankrec, "text_layer", lambda p: "U S BANK LOAN STATEMENT PAGE 1"),              mock.patch.object(bankrec, "ocr_text", boom):
            docs = bankrec.profile_support(["/x/US_08_2026.pdf"], "auto", self.SIDECAR)
        self.assertIn("loan", docs[0].vendor_keys)


def _mk_invoice(path, amount, doc_date, vendor_text="", verified=True):
    """A minimal non-slip Doc, as profile_support would build it."""
    d = bankrec.Doc()
    d.path, d.is_slip = path, False
    d.fname_ints, d.fname_moneys = set(), set()
    d.content_amounts = {amount}
    d.slip_total = d.slip_date = d.deposit_no = None
    d.doc_date = doc_date
    d.vendor_keys = bankrec.vendor_keys(vendor_text)
    d.ocr_used = False
    d.verified = verified
    d.sidecar_total = amount if verified else None
    d.check_numbers = set()
    d.lag_months = 0
    return d


class PlacementTieBreak(unittest.TestCase):
    """Two invoices of the same amount tie on evidence. The one whose own date sits nearest the
    statement line's date is this period's bill; the other is last period's copy left in the
    folder, and must be reported unplaced rather than winning on folder order."""

    LINE = bankrec.StmtLine(1, 58366.16, "debit", "other_debit", "08/28/2026",
                            "08/28/2026 PAYMENT TO COMM PROPERTY LOAN $58,366.16",
                            None, False, pos=100)
    PERIOD_END = datetime.datetime(2026, 8, 31)

    def _place(self, docs):
        doc_line, reason, conf, _covered = bankrec.assign_docs(
            docs, [self.LINE], period_end=self.PERIOD_END)
        return {p: sl.seq for p, sl in doc_line.items()}

    def test_nearer_dated_invoice_wins_the_line(self):
        stale = _mk_invoice("/x/US_07_2026.pdf", 58366.16, datetime.datetime(2026, 7, 1))
        current = _mk_invoice("/x/US_08_2026.pdf", 58366.16, datetime.datetime(2026, 8, 12))
        placed = self._place([stale, current])          # stale listed FIRST on purpose
        self.assertEqual(placed, {"/x/US_08_2026.pdf": 1})

    def test_order_in_the_folder_does_not_decide(self):
        stale = _mk_invoice("/x/US_07_2026.pdf", 58366.16, datetime.datetime(2026, 7, 1))
        current = _mk_invoice("/x/US_08_2026.pdf", 58366.16, datetime.datetime(2026, 8, 12))
        self.assertEqual(self._place([current, stale]), self._place([stale, current]))

    def test_verified_invoice_wins_when_dates_are_equal(self):
        same = datetime.datetime(2026, 8, 12)
        raw = _mk_invoice("/x/US_a.pdf", 58366.16, same, verified=False)
        ver = _mk_invoice("/x/US_b.pdf", 58366.16, same, verified=True)
        self.assertEqual(self._place([raw, ver]), {"/x/US_b.pdf": 1})

    def test_undated_invoice_still_places_when_it_is_alone(self):
        lone = _mk_invoice("/x/US_x.pdf", 58366.16, None)
        self.assertEqual(self._place([lone]), {"/x/US_x.pdf": 1})


class FilenameNumberTokens(unittest.TestCase):
    """Stored invoices are named <Vendor>_MM_YYYY[_N]. Those digits describe the FILE, not the
    bill - reading them as check numbers or amounts let "KORUS_07_2026_1.pdf" answer to cleared
    check #1 with no amount evidence at all, and would let it answer to a $2,026.00 line."""

    def test_stored_name_stamp_yields_no_numbers(self):
        self.assertEqual(bankrec.file_number_tokens("KORUS_07_2026_1.pdf"),
                         (set(), set()))
        self.assertEqual(bankrec.file_number_tokens("LADWP_07_2026.pdf"), (set(), set()))

    def test_vendor_digits_survive_the_stamp(self):
        ints, _moneys = bankrec.file_number_tokens("7-Eleven_07_2026.pdf")
        self.assertEqual(ints, {7})

    def test_hand_named_number_files_still_count(self):
        self.assertEqual(bankrec.file_number_tokens("4.pdf"), ({4}, set()))
        self.assertEqual(bankrec.file_number_tokens("1234.pdf"), ({1234}, set()))
        self.assertEqual(bankrec.file_number_tokens("20343.52.pdf"), (set(), {20343.52}))


def _txn(kind, tran, notes, amount, date):
    return {"type": kind, "tran": tran, "notes": notes, "amount": amount, "date": date}


class SettlementMemberBinding(unittest.TestCase):
    """One bank line of $11,800.00 covers cleared checks #1 Top Interior $10,500.00 and
    #2 Tate $1,300.00. A same-amount bill from an unrelated vendor must not take a member's
    place, and the members' own files must not ALSO be pulled in a second time by the rec
    fallback - both put invoices in the assembled PDF that never cleared on that line."""

    SETTLE = bankrec.StmtLine(1, 11800.00, "debit", "other_debit", "08/26/2026",
                              "08/26/2026 BPX_KORUSREALEST Settlement 000027923421062 $11,800.00",
                              None, True, pos=100)
    TXNS = [_txn("check", "1", "Top Interior", 10500.00, "08/25/2026"),
            _txn("check", "2", "Tate", 1300.00, "08/25/2026")]

    def _place(self, docs):
        doc_line, reason, conf, _covered = bankrec.assign_docs(
            docs, [self.SETTLE], txns=self.TXNS,
            period_end=datetime.datetime(2026, 8, 31))
        return {os.path.basename(p): "+".join(reason[p])
                for p in (d.path for d in docs) if p in doc_line}

    def _docs(self):
        top = _mk_invoice("/x/Top.pdf", 10500.00, None, vendor_text="Top Interior")
        top.check_numbers = {1}
        jongho = _mk_invoice("/x/Jongho_08_2026.pdf", 1300.00, datetime.datetime(2026, 8, 7),
                             vendor_text="Jongho Lee management fee")
        jongho.check_numbers = {1743}
        tate = _mk_invoice("/x/Michelle_08_2026.pdf", 1300.00, datetime.datetime(2026, 8, 24),
                           vendor_text="Rachael Tate security deposit refund")
        tate.check_numbers = {2}
        return top, jongho, tate            # folder order: Jongho sorts before Michelle

    def test_member_slot_goes_to_the_doc_carrying_that_check_number(self):
        top, jongho, tate = self._docs()
        placed = self._place([jongho, tate, top])
        self.assertEqual(placed.get("Michelle_08_2026.pdf"), "settle-member")
        self.assertEqual(placed.get("Top.pdf"), "settle-member")

    def test_same_amount_stranger_is_not_grouped_onto_the_settlement(self):
        top, jongho, tate = self._docs()
        placed = self._place([jongho, tate, top])
        self.assertNotIn("Jongho_08_2026.pdf", placed)

    def test_covered_member_gets_no_second_rec_fallback_line(self):
        top, jongho, tate = self._docs()
        placed = self._place([jongho, tate, top])
        self.assertEqual(sorted(placed), ["Michelle_08_2026.pdf", "Top.pdf"])

    def test_uncovered_cleared_check_still_gets_its_fallback(self):
        """The fallback must keep working for a cleared item no settlement explains."""
        lone = _mk_invoice("/x/Solo_08_2026.pdf", 640.00, datetime.datetime(2026, 8, 9),
                           vendor_text="Solo Plumbing")
        txns = self.TXNS + [_txn("check", "9", "Solo Plumbing", 640.00, "08/09/2026")]
        doc_line, reason, _c, _cov = bankrec.assign_docs(
            [lone], [self.SETTLE], txns=txns, period_end=datetime.datetime(2026, 8, 31))
        self.assertIn("rec-fallback", reason[lone.path])


class SidecarPaymentLag(unittest.TestCase):
    """The lag is learned from the database, but bankrec never opens the database - it reads
    the folder and _amounts.csv. So the lag rides in as a sidecar column, and a sidecar
    written before the column existed simply reads as no lag."""

    HEADER = ("stored_file,amount,vendor,invoice_number,unit,invoice_date,property,"
              "source_file,check_number")

    def _sidecar(self, *rows, column=True):
        head = self.HEADER + (",payment_lag_months" if column else "")
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "_amounts.csv").write_text("\n".join((head,) + rows) + "\n",
                                                    encoding="utf-8")
            return bankrec.load_amount_sidecar(tmp)

    def test_lag_column_is_read(self):
        sc = self._sidecar(
            "Spectrum_07_2026.pdf,140.00,Spectrum,07302026,,07/30/2026,P,s.pdf,,1")
        self.assertEqual(sc["spectrum_07_2026.pdf"]["lag"], 1)

    def test_sidecar_written_before_the_column_existed_reads_as_no_lag(self):
        sc = self._sidecar(
            "Spectrum_07_2026.pdf,140.00,Spectrum,07302026,,07/30/2026,P,s.pdf,",
            column=False)
        self.assertEqual(sc["spectrum_07_2026.pdf"]["lag"], 0)

    def test_blank_or_unreadable_lag_reads_as_no_lag(self):
        sc = self._sidecar(
            "A_07_2026.pdf,10.00,A,1,,07/30/2026,P,s.pdf,,",
            "B_07_2026.pdf,20.00,B,2,,07/30/2026,P,s.pdf,,later")
        self.assertEqual(sc["a_07_2026.pdf"]["lag"], 0)
        self.assertEqual(sc["b_07_2026.pdf"]["lag"], 0)


class LaggedPlacement(unittest.TestCase):
    """Spectrum bills $140.00 at the end of every month and autopay takes it in the middle of
    the NEXT one. Three invoices of the same amount score identically, so the date decides -
    and the date that matters is when the bill is drafted, not when it was issued."""

    LINE = bankrec.StmtLine(1, 140.00, "debit", "other_debit", "08/19/2026",
                            "08/19/2026 SPECTRUM SPECTRUM 0596250 $140.00",
                            None, False, pos=100)

    def _place(self, docs):
        doc_line, _reason, _conf, _covered = bankrec.assign_docs(
            docs, [self.LINE], period_end=datetime.datetime(2026, 8, 31))
        return sorted(os.path.basename(p) for p in doc_line)

    def _spectrums(self, lag):
        out = []
        for name, billed in (("Spectrum_06_2026.pdf", datetime.datetime(2026, 6, 30)),
                             ("Spectrum_07_2026.pdf", datetime.datetime(2026, 7, 30)),
                             ("Spectrum_08_2026.pdf", datetime.datetime(2026, 8, 30))):
            d = _mk_invoice("/x/" + name, 140.00, billed, vendor_text="Spectrum")
            d.lag_months = lag
            out.append(d)
        return out

    def test_a_one_month_lag_picks_the_invoice_billed_the_month_before(self):
        self.assertEqual(self._place(self._spectrums(1)), ["Spectrum_07_2026.pdf"])

    def test_without_a_lag_the_nearest_invoice_still_wins(self):
        self.assertEqual(self._place(self._spectrums(0)), ["Spectrum_08_2026.pdf"])

    def test_the_lag_reorders_and_never_places_a_second_file(self):
        self.assertEqual(len(self._place(self._spectrums(1))), 1)


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


# The rec report lists check #10626 (All Star Windows Cleaning, $20,000.00) as OUTSTANDING, and
# separately clears two unrelated $20,000.00 checks to other payees. Nothing in the bank
# statement shows $20,000.00 - the bank posted the cleared pair inside a lump - so the rec
# fallback mints a pseudo-line for each, and the outstanding invoice's amount fits one exactly.
REC_OUTSTANDING_FIXTURE = """Bank Reconciliation Report
Outstanding Deposits
08/31/2026 707 :ACH/WIPS Deposit 13,412.69
Plus: Outstanding Deposits 13,412.69
Outstanding Checks
Check Date Check Number Payee Amount
07/21/2026 10597 Buena Park Lock & Key 233.62
08/31/2026 10626 All Star Windows Cleaning 20,000.00
Less: Outstanding Checks 20,233.62
Cleared Items:
Cleared Checks
08/14/2026 220 Fortunatos LLC 20,000.00 08/31/2026
Total Cleared Checks 20,000.00
Cleared Deposits
07/03/2026 9999 Remote deposit 8,879.96
Total Cleared Deposits 8,879.96
Difference 0.00
"""


class OutstandingCheckNeverClears(unittest.TestCase):
    """An invoice whose check is still OUTSTANDING must not be attached to a same-amount
    cleared item written to somebody else. It lands 'high', and reconcile.py auto-stamps
    'high' as reconciled - so a bill that never cleared drops out of next month's pool."""

    PERIOD_END = datetime.datetime(2026, 8, 31)
    # A statement that shows no $20,000.00 anywhere, so the rec fallback has to invent the line.
    OTHER_LINE = bankrec.StmtLine(1, 4508.85, "debit", "other_debit", "08/04/2026",
                                  "08/04/2026 PAYMENT TO SOMEBODY ELSE", None, False, pos=100)
    CLEARED_220 = [_txn("check", "220", "Fortunatos LLC", 20000.00, "08/14/2026")]

    def _aswc(self, check_no=10626):
        d = _mk_invoice("/x/ASWC_08_2026.pdf", 20000.00, datetime.datetime(2026, 8, 2),
                        vendor_text="All Star Window Cleaning")
        d.check_numbers = {check_no}
        return d

    def test_outstanding_checks_are_parsed_off_the_rec_report(self):
        with mock.patch.object(bankrec, "text_layer", lambda p: REC_OUTSTANDING_FIXTURE):
            parsed = bankrec.parse_rec("fake.pdf")
        self.assertEqual(len(parsed["checks"]), 1)          # outstanding rows stay OUT of cleared
        self.assertEqual(parsed["checks"][0]["tran"], "220")
        self.assertEqual(sorted(parsed["outstanding_checks"]), [10597, 10626])

    def test_invoice_is_not_placed_on_a_cleared_check_with_a_different_number(self):
        aswc = self._aswc()
        doc_line, reason, _conf, _cov = bankrec.assign_docs(
            [aswc], [self.OTHER_LINE], txns=self.CLEARED_220, period_end=self.PERIOD_END)
        self.assertNotIn(aswc.path, doc_line, "placed via %s" % reason.get(aswc.path))

    def test_invoice_carrying_the_cleared_check_number_still_places(self):
        """The veto must read a contradiction, not merely the presence of a check number."""
        paid = self._aswc(check_no=220)
        doc_line, reason, conf, _cov = bankrec.assign_docs(
            [paid], [self.OTHER_LINE], txns=self.CLEARED_220, period_end=self.PERIOD_END)
        self.assertIn(paid.path, doc_line)
        self.assertIn("check#", reason[paid.path])
        self.assertEqual(conf[paid.path], "high")

    def test_outstanding_invoice_is_held_off_a_line_that_carries_no_check_number(self):
        """Second layer, needed on its own: an 'other debits' statement line has no check
        number, so the contradiction veto has nothing to compare and cannot fire."""
        aswc = self._aswc()
        numberless = bankrec.StmtLine(1, 20000.00, "debit", "other_debit", "08/14/2026",
                                      "08/14/2026 OUTGOING WIRE", None, False, pos=100)
        doc_line, reason, _conf, _cov = bankrec.assign_docs(
            [aswc], [numberless], txns=[], period_end=self.PERIOD_END,
            outstanding_checks={10626})
        self.assertNotIn(aswc.path, doc_line, "placed via %s" % reason.get(aswc.path))
        self.assertEqual(reason[aswc.path], ["outstanding-check"])


# Long payee names wrap in the rec PDF's text layer: the row's amount is pushed onto its own
# line, sometimes with a line of payee name in between. The one-line row regex matched none of
# them, so the whole row vanished - 48 of them across 18 of the real rec reports. A dropped
# CLEARED row is a check with no fallback line, no file, and no place in the "no supporting
# file" list; a dropped OUTSTANDING row is a check the hold-out cannot hold.
WRAPPED_REC_FIXTURE = """Bank Reconciliation Report
Outstanding Checks
Check Date Check Number Payee Amount
08/31/2026 10639 RR Franchising, Inc. DBA
Vanguard Cleaning Systems
17,160.60
Less: Outstanding Checks 17,160.60
Cleared Items:
Cleared Checks
08/03/2026 10622 RR Franchising, Inc. DBA
Vanguard Cleaning Systems
17,160.60 08/31/2026
07/21/2026 10610 SUN STEAM CARPET
CLEANING
932.15 08/31/2026
07/12/2026 1234 Vendor payment 500.00
Total Cleared Checks 18,592.75
Cleared Deposits
07/03/2026 9999 Remote deposit 8,879.96
Total Cleared Deposits 8,879.96
Difference 0.00
"""


class WrappedRecRows(unittest.TestCase):
    def _parse(self, text):
        with mock.patch.object(bankrec, "text_layer", lambda p: text):
            return bankrec.parse_rec("fake.pdf")

    def test_a_cleared_row_whose_payee_wraps_is_still_parsed(self):
        parsed = self._parse(WRAPPED_REC_FIXTURE)
        by_tran = {c["tran"]: c for c in parsed["checks"]}
        self.assertIn("10622", by_tran)
        self.assertEqual(by_tran["10622"]["amount"], 17160.60)
        self.assertEqual(by_tran["10622"]["date"], "08/03/2026")

    def test_the_wrapped_payee_name_is_kept_whole(self):
        """The payee is what vendor matching reads, so half a name is worse than none."""
        parsed = self._parse(WRAPPED_REC_FIXTURE)
        notes = {c["tran"]: c["notes"] for c in parsed["checks"]}
        self.assertIn("RR Franchising", notes["10622"])
        self.assertIn("Vanguard Cleaning Systems", notes["10622"])

    def test_every_cleared_row_in_the_section_is_counted(self):
        parsed = self._parse(WRAPPED_REC_FIXTURE)
        self.assertEqual(sorted(c["tran"] for c in parsed["checks"]),
                         ["10610", "10622", "1234"])

    def test_a_wrapped_outstanding_row_is_picked_up_too(self):
        self.assertEqual(self._parse(WRAPPED_REC_FIXTURE)["outstanding_checks"], [10639])

    def test_a_section_total_is_not_glued_onto_a_dangling_row(self):
        """'Total Cleared Checks 18,592.75' must never become a transaction of its own."""
        parsed = self._parse(WRAPPED_REC_FIXTURE)
        self.assertNotIn(18592.75, [c["amount"] for c in parsed["checks"]])

    def test_a_row_with_no_amount_before_the_next_row_is_dropped(self):
        text = ("Cleared Checks\n"
                "08/03/2026 10622 Payee with no amount at all\n"
                "07/12/2026 1234 Vendor payment 500.00\n"
                "Total Cleared Checks 500.00\n")
        parsed = self._parse(text)
        self.assertEqual([c["tran"] for c in parsed["checks"]], ["1234"])

    def test_an_amount_far_below_its_row_is_not_glued_on(self):
        """Bounded look-ahead: an amount several lines down belongs to something else."""
        text = ("Cleared Checks\n"
                "08/03/2026 10622 Payee\n"
                "filler one\nfiller two\nfiller three\nfiller four\n"
                "17,160.60\n"
                "Total Cleared Checks 0.00\n")
        self.assertEqual(self._parse(text)["checks"], [])

    def test_one_line_rows_are_unaffected(self):
        parsed = self._parse(REC_FIXTURE)
        self.assertEqual(len(parsed["checks"]), 1)
        self.assertEqual(len(parsed["deposits"]), 1)
        self.assertEqual(parsed["checks"][0]["amount"], 500.00)


class InferredCheckNumberOnAnUnnumberedLine(unittest.TestCase):
    """HBS Services bills 3,988.00 every month. Check #219 paid August's bill and cleared; the
    invoice it paid was never uploaded. The SEPTEMBER invoice (check #225) was in the folder,
    matched the August line on the amount alone, and went into the report - because the line
    sits in "other debits", carries no check number, and #225 appears nowhere in an August rec,
    so neither guard had anything to bite on. The rec knows that line is check #219."""

    PERIOD_END = datetime.datetime(2026, 8, 31)
    LINE = bankrec.StmtLine(1, 3988.00, "debit", "other_debit", "08/17/2026",
                            "08/17/2026 ELECTRONIC PAYMENT", None, False, pos=100)
    CLEARED_219 = [_txn("check", "219", "HBS Services LLC", 3988.00, "08/14/2026")]

    def _place(self, doc, txns=None):
        line = bankrec.StmtLine(self.LINE.seq, self.LINE.amount, self.LINE.sign,
                                self.LINE.section, self.LINE.date, self.LINE.desc,
                                None, False, pos=self.LINE.pos)
        doc_line, reason, _c, _cov = bankrec.assign_docs(
            [doc], [line], txns=txns if txns is not None else self.CLEARED_219,
            period_end=self.PERIOD_END)
        return doc_line, reason

    def _hbs(self, check_no):
        d = _mk_invoice("/x/HSLHBLS_08_2026.pdf", 3988.00, datetime.datetime(2026, 8, 31),
                        vendor_text="HBS Services LLC")
        d.check_numbers = {check_no}
        return d

    def test_next_months_invoice_is_kept_off_the_line(self):
        doc = self._hbs(225)
        doc_line, reason = self._place(doc)
        self.assertNotIn(doc.path, doc_line, "placed via %s" % reason.get(doc.path))

    def test_the_invoice_that_check_actually_paid_still_places(self):
        doc = self._hbs(219)
        doc_line, reason = self._place(doc)
        self.assertIn(doc.path, doc_line)
        self.assertIn("check#", reason[doc.path])

    def test_a_doc_with_no_check_number_is_unaffected(self):
        """Inference must not turn 'no evidence' into 'contradicting evidence'."""
        doc = self._hbs(225)
        doc.check_numbers = set()
        doc_line, _r = self._place(doc)
        self.assertIn(doc.path, doc_line)

    def test_two_cleared_checks_of_the_same_amount_infer_nothing(self):
        """Ambiguous: the line could be either check, so no number may be lent to it."""
        txns = self.CLEARED_219 + [_txn("check", "230", "Someone Else", 3988.00, "08/20/2026")]
        doc = self._hbs(225)
        doc_line, _r = self._place(doc, txns=txns)
        self.assertIn(doc.path, doc_line)

    def test_a_line_that_already_has_a_check_number_keeps_it(self):
        line = bankrec.StmtLine(1, 3988.00, "debit", "check", "08/17/2026", "CHECK 219",
                                "219", False, pos=100)
        doc = self._hbs(219)
        doc_line, reason, _c, _cov = bankrec.assign_docs(
            [doc], [line], txns=self.CLEARED_219, period_end=self.PERIOD_END)
        self.assertIn("check#", reason[doc.path])


# A deposit slip printed before the deposit was posted: the line item is there, but the
# Total Deposit / Deposit Number / Deposit Date fields print blank.
UNPOSTED_SLIP = """Payer Property Unit
Payment
Reference
Receipt
Date Notes Amount
Tip Top
Designs
J.H. Lee Property, LLC
(Sherman)
L 1032 09/14/2026 Sep 2026
Rent
3,649.06
Total Items 1
Total Deposit
Bank
Account Number
Deposit Number
Deposit Date
10/5/26, 1:25 PM about:blank
about:blank 1/1"""

EMPTY_SLIP = """Payer Property Unit Payment Reference
Receipt
Date Notes Amount
No Records
Total Items 0
Total Deposit
Bank
Account Number
Deposit Number
Deposit Date
10/5/26, 1:25 PM about:blank
about:blank 1/1"""


class SlipWithBlankTotal(unittest.TestCase):
    def test_printed_total_wins(self):
        self.assertEqual(bankrec.slip_total("Total Items 2\nTotal Deposit $1,234.56"), 1234.56)

    def test_blank_total_falls_back_to_the_single_line_item(self):
        self.assertEqual(bankrec.slip_total(UNPOSTED_SLIP), 3649.06)

    def test_blank_total_sums_line_items_when_count_matches(self):
        text = UNPOSTED_SLIP.replace("3,649.06\nTotal Items 1",
                                     "3,649.06\nB 1033 09/14/2026 Sep 2026\nRent\n3,649.06\n"
                                     "Total Items 2")
        self.assertEqual(bankrec.slip_total(text), 7298.12)

    def test_blank_total_with_item_count_mismatch_is_unknown(self):
        text = UNPOSTED_SLIP.replace("Total Items 1", "Total Items 2")
        self.assertIsNone(bankrec.slip_total(text))

    def test_empty_slip_has_no_total(self):
        self.assertIsNone(bankrec.slip_total(EMPTY_SLIP))

    def test_unposted_slip_is_placed_on_its_deposit_line(self):
        with mock.patch.object(bankrec, "content_text", lambda p, m: (UNPOSTED_SLIP, False)):
            [doc] = bankrec.profile_support(["7.pdf"], "off")
        self.assertTrue(doc.is_slip)
        line = bankrec.StmtLine(1, 3649.06, "credit", "deposit", "09/15/2026", "DEPOSIT",
                                None, False, pos=100)
        doc_line, reason, _c, _cov = bankrec.assign_docs([doc], [line])
        self.assertIn(doc.path, doc_line)
        self.assertIn("slip-total", reason[doc.path])
