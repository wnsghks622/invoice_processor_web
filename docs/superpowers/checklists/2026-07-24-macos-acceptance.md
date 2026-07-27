# macOS acceptance checklist

Run these on a Mac and record pass/fail. The implementer works on Windows and cannot run any
of them, so nothing here is verified until a human fills it in.

**Tester:** ____________  **Date:** ____________
**Mac model / chip:** ____________  (Apple Silicon or Intel)
**macOS version:** ____________

| # | Check | Expected | Pass/Fail | Notes |
|---|---|---|---|---|
| 1 | Clone the repo, double-click `start.command` | Terminal window opens, dependencies install, browser opens the dashboard | | |
| 2 | Double-click `start.command` again while it is running | Says "already running", opens the browser, does NOT start a second server | | |
| 3 | Settings page → paste API key → save | Status shows "Saved"; the key is never displayed back | | |
| 4 | Process page → drop one invoice PDF → Run processor | Extraction succeeds; the invoice appears on the Invoices page | | |
| 5 | Invoices page → click the reveal (folder) button | Finder opens with that file selected | | |
| 6 | Month-end page → "Open this folder" | Finder opens the month/property folder | | |
| 7 | Month-end page → change the property dropdown | The displayed path updates and uses forward slashes (`/`), no backslashes | | |
| 8 | Any page with a reveal button → hover it | Tooltip reads "Finder", never "Windows File Explorer" | | |
| 9 | Month-end → Stage, then Assemble on a real property folder | An ASSEMBLED PDF, a manifest.txt, and a matched.csv are all written; the PDF page count is plausible | | |
| 10 | Assemble BEFORE installing Tesseract | The log line reads `OCR: NOT available (...)` and the run still completes | | |
| 11 | `brew install tesseract`, then Assemble again | The log line reads `OCR: available` | | |
| 12 | Run the test suite: `python3 -m unittest discover -s tests -t .` | OK, all tests pass | | |

Repeat items 1, 9, 10, and 11 on **both** an Apple Silicon Mac and an Intel Mac.

## Known risks to watch for

- **Property names with accented characters.** macOS stores filenames decomposed (NFD),
  Windows composed (NFC), so an invoice for a property named with non-ASCII characters could
  show as "no file" in the UI even though the PDF is on disk. If you hit this, report the exact
  property name — the fix is a `unicodedata.normalize` at the comparison points in
  `state.resolve_invoice_file()` and `core/db.export_amount_sidecars()`.
- **PyMuPDF on Apple Silicon.** If `pip install` fails on PyMuPDF, OCR is unavailable but
  everything else works. Report it rather than working around it.
