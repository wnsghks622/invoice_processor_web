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
- **Windows or macOS.** Windows users double-click `start.bat`; macOS users double-click
  `start.command`. The app also runs on Linux via `python app.py`, though that is untested.
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

### macOS notes

Steps 1–4 above are the same on a Mac. Two differences:

- Use `python3` everywhere the steps say `python`. macOS has no `python` command.
  Activate the virtual environment with `source .venv/bin/activate`.
- Instead of `start.bat`, double-click **`start.command`** in Finder. On first run it creates
  the virtual environment and installs everything for you.

If `start.command` opens in a text editor instead of running it, its executable bit was lost —
restore it with:

```bash
chmod +x start.command
```

**API key:** rather than editing the hidden `.env` file (Finder hides dotfiles), open the
**Settings** page in the app and paste the key into the API key field.

**OCR (optional):** scanned deposit slips need Tesseract. Install
[Homebrew](https://brew.sh) once, then:

```bash
brew install tesseract
```

Without it the app still runs — scanned files fall back to their filename amount and are flagged
for review.

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

## Daily use

**Process invoices** — drag PDFs or images onto the drop zone, click **Run processor**. Each file is
read by Claude, filed into `data/processed/<property>/`, and logged. Output streams live.

**Invoices** — every invoice, searchable and filterable by property, month, amount range, and status.
Tick **Yardi** as you key each one in (saves instantly). Click a vendor name to open its PDF. The
edit panel fixes any field; changing the property moves the filed PDF too.

**Needs Review** — invoices whose service location didn't match any property. Click the vendor to
read the PDF, then assign the right property. The lasting fix is adding that address as an alias.

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

57 tests covering amount parsing, duplicate detection, invoice merging, property matching, the
bank-statement and rec-report parsers, the subset-sum matcher, the platform helpers (file-manager
naming, the Bank Rec example path, Tesseract discovery), and the `.env` API-key writer. No
database or network needed.

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
