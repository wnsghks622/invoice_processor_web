# macOS support — design

**Date:** 2026-07-24
**Status:** approved, ready for implementation planning

## Goal

Run the existing invoice processor on macOS with the same feature set it has on Windows, set up
and operated by a non-technical coworker who manages their own separate set of properties.

## Context and decisions

| Question | Decision |
|---|---|
| Windows vs Mac | Both machines stay in use, each with its own independent data |
| Mac scope | Full workflow — invoice processing *and* month-end close, including OCR |
| Mac operator | A non-technical coworker |
| Property/vendor lists | They manage different properties, built in the UI. **No export/import needed** |
| Code delivery | Added as a collaborator on the private GitHub repo; they clone |
| API key entry | New field on the Settings page (writes `.env`) |

Cloning rather than downloading a ZIP means macOS quarantine does not apply, so the launcher only
needs its executable bit and LF line endings — not a Gatekeeper workaround.

## Non-goals

- No data synchronisation between the two machines.
- No property/vendor export/import.
- No `.app` bundle, custom icon, or in-app updater.
- No Homebrew bootstrapping. Tesseract is a documented one-time `brew install tesseract` performed
  during setup; the app already degrades gracefully without it.
- No Linux support. The existing `xdg-open` fallbacks stay, but are not tested or claimed.

## Architecture

One codebase. Platform differences live behind a seam instead of being scattered as inline
`sys.platform` checks.

`state.py` is the single home for platform knowledge — it already owns `reveal_in_explorer()` and
`open_folder()`, both of which have working `darwin` branches today. It gains:

- `IS_MAC` / `IS_WINDOWS` flags
- `FILE_MANAGER` — `"Finder"` or `"File Explorer"`, for UI copy
- `bank_rec_example_path(month, property_name)` — returns the fully-joined absolute path string
  (using the OS separator) that the wizard displays as its "drop documents here" hint, for a given
  month and optional property. Returns the month-root path when no property is selected.

`app.py` injects `file_manager` via the existing `inject_shell` context processor. Templates render
`{{ file_manager }}` and never test the platform themselves.

Two deliberate exceptions:

1. **`core/bankrec.py` stays self-contained.** It imports only stdlib and pypdf and runs standalone.
   Its `_find_tesseract()` candidate list gains the Homebrew locations
   (`/opt/homebrew/bin/tesseract` for Apple Silicon, `/usr/local/bin/tesseract` for Intel) rather
   than taking a dependency on `state.py` for one lookup. `shutil.which()` already covers the
   common case where Tesseract is on `PATH`.
2. **`templates/wizard.html` stops building paths in JavaScript.** It currently concatenates
   backslashes by hand. The server supplies a correctly-joined example path instead.

### Files changed

| File | Change |
|---|---|
| `state.py` | Platform flags, `FILE_MANAGER`, wizard example-path helper |
| `app.py` | Inject `file_manager`; new API-key save route |
| `core/bankrec.py` | Mac Tesseract paths |
| `core/processor.py` | One hardcoded `\\<property>` in a completion message |
| `templates/wizard.html` | Server-supplied path; "File Explorer" → `{{ file_manager }}` |
| `templates/invoices.html` | "Show this file in Windows File Explorer" → `{{ file_manager }}` |
| `templates/settings.html` | API key field |
| `start.command` *(new)* | macOS launcher, mode `100755`, LF endings |
| `.gitattributes` *(new)* | Pin `*.command` and `*.sh` to LF |
| `README.md` | macOS setup section |
| `tests/` | Unit tests for the `.env` writer and platform helpers |

## The launcher: `start.command`

macOS equivalent of `start.bat`, handling three Mac-specific hazards:

1. **Working directory.** Double-clicking a `.command` starts in the user's home folder, so the
   script must `cd "$(dirname "$0")"` before anything else.
2. **Python detection.** macOS has no `python` command — only `python3`, which may exist merely as
   an Xcode Command Line Tools stub that triggers an installer prompt. Checking that the command
   exists is insufficient; the script must actually run it and confirm version ≥ 3.10.
3. **The window must not vanish.** On any error the script prints a plain-language message and
   pauses, so a non-technical user can read what went wrong.

Sequence: `cd` → verify Python 3.10+ → create `.venv` if missing → install dependencies if imports
fail → check port 5057 with `lsof` and, if already serving, just open the browser and exit → `open`
the browser → run the app.

Line endings and the executable bit are load-bearing. A CRLF shebang fails on macOS with
`bad interpreter: /bin/bash^M`, and without mode `100755` a double-click does nothing. `.gitattributes`
pins the former; the file is committed with `git update-index --chmod=+x`.

## API key entry on the Settings page

A password-type field on the Settings page posts to a new route (`POST /settings/api-key`) that
writes `ANTHROPIC_API_KEY` into `.env`, removing the need to find a hidden dotfile in Finder
(macOS hides dotfiles by default). If `.env` does not exist it is created.

Requirements:

- **Never display the value.** Not in the field, not in a flash message, not in logs. The page
  shows only whether a key is saved.
- **Reject newlines and control characters.** The field writes into a config file, so the value is
  untrusted input; a line break would otherwise inject arbitrary `.env` lines. Reject empty values.
  Do **not** hard-reject on a `sk-ant-` prefix check — key formats change, and a format guess that
  rejects a valid key is worse than accepting a bad one, which the app already reports clearly at
  run time ("Claude rejected your API key"). A non-blocking warning on the page is acceptable.
- **Preserve other `.env` lines** (e.g. `CLAUDE_MODEL`).
- **Write atomically** — temp file then replace — so an interrupted save cannot truncate `.env`.
- **`chmod 0600` on POSIX**, so other accounts on the machine cannot read the key.
- **No app restart required.** Each processor run is a fresh subprocess that loads `.env` at import.
- **Report the environment-variable override.** If `ANTHROPIC_API_KEY` is also set as a system
  environment variable it takes precedence over the file, so the page must say so rather than
  appear to save and have no effect.

The existing cross-site request guard already covers this route.

## Windows non-regression

Every change is additive or platform-gated. Before this is considered done, on Windows: the full
test suite passes and all eight pages return 200. The Mac work must not cost a working Windows
install.

## Verification

### Verified on Windows (by the implementer)

1. All existing tests pass, plus new unit tests for the `.env` writer (round-trip, other keys
   preserved, newline/control-character rejection, atomic replace) and the platform helpers
   (`sys.platform` monkeypatched so both branches are exercised).
2. `bash -n start.command` — syntax check via Git Bash.
3. `git ls-files --stage start.command` shows mode `100755`; the committed blob contains no CR bytes.
4. Settings key save: `.env` written correctly, surrounding lines intact, key absent from rendered HTML.
5. All eight pages return 200.

### Verified on macOS (by a human with a Mac — not the implementer)

The implementer has no macOS access and will not claim any of these as verified. Each check states
its expected result so the tester reports only pass/fail.

| # | Check | Expected |
|---|---|---|
| 1 | Clone, double-click `start.command` | Terminal opens, dependencies install, browser opens the dashboard |
| 2 | Double-click again while running | Says already running, opens browser, does not start a second server |
| 3 | Settings → paste key → process one invoice | Key saves; extraction succeeds |
| 4 | Invoices → reveal button | Finder opens with the file selected |
| 5 | Month-end → "Open this folder" | Finder opens the month/property folder |
| 6 | Month-end path hint | Shows a forward-slash macOS path |
| 7 | UI copy | Reads "Finder", never "Windows File Explorer" |
| 8 | Stage → Assemble on a real property folder | ASSEMBLED PDF, manifest, and matched.csv all written; page count plausible |
| 9 | OCR absent, then `brew install tesseract` | Manifest reports "NOT available" and still completes; reports "available" after install |
| 10 | Repeat 1, 8, 9 on both Apple Silicon and Intel | Same results on both |

## Known risks

**Unicode filename normalization.** macOS stores filenames decomposed (NFD), Windows composed
(NFC). A property name containing accented characters could make a database `stored_file` fail to
match the file on disk, so the invoice would show as "no file". The current 19 properties are all
ASCII and the coworker builds their own list, so this cannot bite today. Mitigation, if their names
need it, is `unicodedata.normalize` at the comparison points in `state.resolve_invoice_file()` and
`core/db.export_amount_sidecars()`. Deliberately not implemented pre-emptively.

**PyMuPDF on Apple Silicon.** If the wheel does not install, OCR is unavailable. The assembler
already handles this — it prints "NOT available" and falls back to filename amounts — so the
failure degrades rather than blocks.

**Case sensitivity.** macOS APFS defaults to case-insensitive, matching Windows behaviour, and the
code already lowercases when matching `stored_file`. No action needed unless the Mac uses a
case-sensitive volume, which is non-default and out of scope.
