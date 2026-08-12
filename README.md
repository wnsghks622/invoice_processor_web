# Invoice Processor (web)

A local web app for property-management invoice handling. Drop invoice PDFs in, and Claude reads
each one (vendor, invoice number, amount, dates, service location), files the PDF under the right
property, and records it in a SQLite database. At month end it stages each property's outstanding
invoices, assembles a bank-reconciliation packet as a single PDF, and marks off what cleared.

Everything runs on your own machine. The only thing that leaves it is the invoice image/PDF sent
to the Anthropic API for extraction.

---

## Requirements

- **Python 3.10 or newer** (developed and tested on 3.14). Get it from [python.org](https://python.org) —
  during install, tick **"Add Python to PATH"**.
- **An Anthropic API key** — [console.anthropic.com](https://console.anthropic.com). Extraction calls cost money
  per invoice; the default model is Haiku, the cheapest.
- **Windows** for the one-click `start.bat` launcher and the "show file in Explorer" buttons. The app
  itself also runs on macOS/Linux — start it with `python app.py` instead.
- **Tesseract OCR** — *optional*. Only used for scanned deposit slips and invoices that have no text
  layer. Without it those files fall back to their filename amount and get flagged for review.
  Windows installer: [UB-Mannheim/tesseract](https://github.com/UB-Mannheim/tesseract/wiki).

---

## Setup on a new machine

### 1. Clone the repo

```bash
git clone https://github.com/wnsghks622/invoice_processor_web.git
cd invoice_processor_web
```

### 2. Create a virtual environment (recommended)

Keeps this project's packages separate from the rest of your system.

```bash
python -m venv .venv
```

Activate it — **PowerShell**:

```bash
.venv\Scripts\Activate.ps1
```

**Command Prompt**:

```bash
.venv\Scripts\activate.bat
```

If PowerShell blocks the activation script, allow local scripts once with
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`, then try again.

### 3. Install dependencies

```bash
python -m pip install -r requirements.txt
```

### 4. Add your API key

Create a file named `.env` in the project root:

```
ANTHROPIC_API_KEY=sk-ant-your-key-here
```

`.env` is gitignored and never leaves your machine. Optionally add `CLAUDE_MODEL=claude-sonnet-5`
on a second line to use a stronger model for messy or handwritten invoices — the default is
`claude-haiku-4-5`.

### 5. Start it

```bash
python app.py
```

Then open <http://127.0.0.1:5057>. On Windows you can instead double-click **`start.bat`**, which
checks dependencies and opens the browser for you.

On first run the app creates its `data/` folder and an empty database, so every page works
immediately — you just won't have any invoices or properties yet.

---

## First-time configuration

The app starts with an empty database. Before processing invoices, set up your lists:

1. **Properties** page — add each property you manage. The **canonical name** is what gets used
   for folder names and reports. Put every address and spelling a vendor might print on an invoice
   into **aliases** (semicolon-separated, e.g. `711 Hope; 3785 Wilshire Blvd`). This is what lets
   Claude match an invoice's service location to the right property automatically.
2. **Vendors** page — optional. Short names only shorten the filed PDF names
   (e.g. "Southern California Edison" → `SCE_05_2026.pdf`).
3. **Settings** page — set the current period (e.g. `July 2026`).

The more aliases you add, the fewer invoices land in **Needs Review**.

### Migrating from the old spreadsheet tool

Only relevant if you're moving from the original CSV/Excel version. Put the old
`invoice_processor/` folder (containing `invoices.xlsx`, `properties.csv`, `vendors.csv`) **next to**
this project folder, then run:

```bash
python migrate_to_db.py
```

It reads those files read-only and imports everything into the database. It refuses to run if the
database already has invoices — use `--force` to wipe and re-import.

> **Note:** on a fresh machine with no legacy data, `start.bat` still attempts this migration on the
> very first launch and prints `[ERROR] Original spreadsheet not found`. That error is harmless —
> the app starts normally right after it with an empty database.

---

## Backfilling dates and vendors

`invoice_date_iso` and `vendor_id` are only populated when an invoice is *written* — an existing
database doesn't get either one retroactively just because you pulled this update. If you're
bringing an existing `data/invoices.db` onto this version, catch it up with three scripts, run in
this order:

```bash
python scripts/backfill_dates.py
python scripts/bootstrap_vendors.py
python scripts/backfill_vendors.py
```

1. **`backfill_dates.py`** parses `invoice_date` into `invoice_date_iso` for every row that
   doesn't already have one. Anything it can't parse is queued for review instead of being
   silently left out of monthly totals.
2. **`bootstrap_vendors.py`** builds the vendor list from the raw vendor names already sitting in
   the invoice table, grouping spellings that are probably the same vendor. **Read what it prints
   for every multi-member cluster before applying.** It's deliberately conservative, but a wrong
   merge tangles two real vendors' invoice history together — cheap to catch by reading the
   cluster now, expensive to unpick later once more invoices have piled up bound to the wrong
   vendor.
3. **`backfill_vendors.py`** matches every invoice's raw vendor name against the vendor list
   `bootstrap_vendors.py` just created, and binds `vendor_id` on confident matches. Anything less
   confident is queued for review instead of guessed at. Run it after `bootstrap_vendors.py` —
   without a vendor list there's nothing to match against, and every invoice ends up queued.

All three are **dry run by default**: they print what they would do and change nothing until you
add `--apply`. All three are also **idempotent** — safe to re-run any time, because each one only
acts on rows (or clusters) it hasn't already resolved. That's also what makes them worth re-running
later, e.g. run `backfill_vendors.py --apply` again after adding a vendor by hand, to pick up any
invoices that can now match it.

---

## Daily use

**Process invoices** — drag PDFs or images onto the drop zone, click **Run processor**. Each file is
read by Claude, filed into `data/processed/<property>/`, and logged. Output streams live.

**Invoices** — every invoice, searchable and filterable by property, month, amount range, and status.
Tick **Yardi** as you key each one in (saves instantly). Click a vendor name to open its PDF. The
edit panel fixes any field; changing the property moves the filed PDF too.

**Month** — everything expected in one month, grouped by property: invoices that normally
arrive and haven't yet, plus reminders you've written for yourself. Expectations are learned
from your own billing history, so a vendor that bills on the 5th is flagged around the 7th
while one that wanders across the month stays quiet until the last week. Vendors that bill
only when work is done can be marked **on-demand** and are never flagged.

**Needs Review** — one page, three separate queues, each flagging a different problem:
- **Property didn't match** — the service location didn't match any property. Click the vendor to
  read the PDF, then assign the right property. The lasting fix is adding that address as an alias.
- **Date couldn't be read** — the printed date didn't parse. Pick the correct date right there;
  until you do, the invoice is held out of monthly totals rather than silently dropped, because a
  dropped invoice looks identical to a vendor who skipped a month.
- **Vendor needs confirming** — the vendor name didn't confidently match a known vendor. Confirm
  the suggestion (or pick the right one) and that spelling is added to the vendor's aliases, so
  it's never asked about again.

**Month-end close** — four steps:
1. **Stage** — copies each property's outstanding invoices into `data/Bank Rec/<Month> Bank Rec/<property>/`
2. **Drop bank documents** — you add each property's bank statement, rec report, deposit slips and
   financial reports to those folders in File Explorer
3. **Assemble** — builds one reconciliation PDF per property, plus a manifest and a `matched.csv`
4. **Reconcile** — marks invoices that cleared; anything unmatched carries forward to next month

Only one job runs at a time — starting a second while one is running is refused rather than
processing your invoices twice.

---

## Where your data lives

Everything is under `data/`, which is gitignored — none of it is ever pushed to GitHub.

| Path | Contents |
|---|---|
| `data/invoices.db` | SQLite database — the source of truth |
| `data/backups/` | Automatic daily copies, newest 14 kept |
| `data/invoices_to_process/` | Drop folder for unprocessed files |
| `data/processed/<property>/` | Filed invoice PDFs |
| `data/Bank Rec/<Month> Bank Rec/` | Month-end staging and assembled reports |
| `data/settings.json` | Current period |

**Backing up:** copy the whole `data/` folder. The daily backups protect against corruption, not
against losing the drive.

**Moving to another machine:** clone the repo there, follow the setup above, then copy your `data/`
folder across before first launch.

---

## Running the tests

```bash
python -m unittest discover -s tests -t .
```

125 tests covering amount parsing, duplicate detection, invoice merging, property matching, the
bank-statement and rec-report parsers, and the subset-sum matcher, plus date parsing, vendor
identity matching and clustering, the schema migration, and the Needs Review page's routes.
No database or network needed.

---

## Troubleshooting

**"Python is not installed or not on PATH"** — reinstall Python with the "Add Python to PATH" box
ticked, then open a *new* terminal.

**Port 5057 already in use / your changes don't show up** — the app is already running in another
window. `start.bat` detects this and just opens the browser. To actually restart, close the old
console window (or end `python.exe` in Task Manager) and start again.

**"No Anthropic API key found"** — `.env` is missing, in the wrong folder, or the line is misspelled.
It must sit next to `app.py` and read `ANTHROPIC_API_KEY=sk-ant-...`. Restart the app after editing.

**A file is "open in a viewer"** — Windows locks open PDFs and spreadsheets. Close the file in
Acrobat/Excel and retry; the app tells you which file is blocking it and never half-applies a change.

**Invoices land in Needs Review a lot** — add the address exactly as the vendor prints it to that
property's aliases.

**Scanned slips aren't read** — install Tesseract (see Requirements). The assembler's log says
whether OCR is available at the start of each run.

**"Blocked: this request came from another website"** — the app only accepts requests from its own
pages, to stop other websites from triggering it in your browser. Navigate from within the app.

**An invoice's date shows as blank** — the app could not read the date the vendor printed.
Open **Needs Review**; unreadable dates are listed at the top with a date picker. They are
held out of monthly totals until resolved rather than being silently dropped, because a
dropped invoice looks identical to a vendor who skipped a month.

**The app asks about a vendor spelling** — a vendor's name was printed differently enough
that the match was not certain. Confirm it once on **Needs Review** and that spelling is
added to the vendor's aliases, so it is never asked about again. Confirming a vendor never
renames an already-filed PDF.

---

## How it works

| File | Role |
|---|---|
| `app.py` | Flask routes and page rendering |
| `state.py` | Reads current state for the UI; file resolution on disk |
| `runner.py` | Runs the long jobs as subprocesses, streams output to the browser |
| `core/processor.py` | Claude extraction, duplicate detection, invoice filing |
| `core/db.py` | SQLite layer, backups, amount sidecars |
| `core/stage_month.py` | Copies pending invoices into the month folder tree |
| `core/bankrec.py` | Classifies documents and assembles the reconciliation PDF |
| `core/reconcile.py` | Applies the assembler's results back to the database |
| `core/cleanup.py` | Trims reconciled invoices from the archive (dry run by default) |

The bank-rec assembler matches supporting documents to bank-statement lines using, strongest first:
a check number you typed in, a verified amount from the processor, batched settlement membership,
subset sums, then vendor name. Only strong matches are auto-reconciled — weak guesses are left out
of the PDF and flagged, so a wrong match can't silently clear an invoice.
