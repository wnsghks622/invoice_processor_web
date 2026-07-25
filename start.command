#!/bin/bash
# macOS launcher - the twin of start.bat. Double-click it in Finder.
# Keep this file LF-only and executable (see .gitattributes); a CRLF shebang fails on macOS.

# Double-clicking a .command starts in the user's home folder, not the project.
cd "$(dirname "$0")" || exit 1

PORT=5057
URL="http://127.0.0.1:$PORT/"

# Any error should leave the window readable instead of flashing shut.
fail() {
  echo ""
  echo "  $1"
  echo ""
  echo "Press any key to close this window."
  read -r -n 1 -s
  exit 1
}

# Already running? Don't stack a second server on the same port - that's how you end up
# with a stale copy still serving old code. Just open the browser instead.
if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "Invoice Processor is already running - opening it in your browser."
  echo "If you just changed the code, close the other Terminal window first, then run this again."
  open "$URL"
  echo ""
  echo "Press any key to close this window."
  read -r -n 1 -s
  exit 0
fi

# macOS has no "python" command, only "python3" - and that can be an Xcode stub that just
# prompts to install developer tools. So run it and read the version rather than trusting
# that the command exists.
if ! command -v python3 >/dev/null 2>&1; then
  fail "Python 3 is not installed.
  Download it from https://www.python.org/downloads/macos/ (get the latest 3.x),
  run the installer, then double-click this file again."
fi

echo "Checking Python..."
echo "(macOS may now ask to install developer tools - if it does, click Install and wait; this can take a few minutes.)"

if ! PYVER="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null)"; then
  fail "Python 3 is not fully installed yet.
  If macOS just offered to install the Command Line Developer Tools, that is not enough.
  Download Python from https://www.python.org/downloads/macos/ and run the installer,
  then double-click this file again."
fi

if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
  fail "This app needs Python 3.10 or newer, but this Mac has Python $PYVER.
  Download a newer version from https://www.python.org/downloads/macos/,
  then double-click this file again."
fi

# Create the virtual environment on first run.
if [ ! -d ".venv" ]; then
  echo "First run: setting up. This takes a couple of minutes..."
  python3 -m venv .venv || fail "Could not create the virtual environment (.venv).
  Make sure this Mac has a few hundred MB of free disk space, then double-click this file again."
fi

VPY=".venv/bin/python"
[ -x "$VPY" ] || fail "The .venv folder looks damaged.
  In Finder, press Cmd+Shift+. (period) to show hidden files, open this project folder,
  delete the .venv folder, then double-click this file again."

# Install dependencies if anything is missing.
if ! "$VPY" -c "import flask, anthropic, openpyxl, pypdf, dotenv" >/dev/null 2>&1; then
  echo "Installing required packages..."
  "$VPY" -m pip install --quiet --upgrade pip
  "$VPY" -m pip install -r requirements.txt || fail "Could not install the required packages.
  Check your internet connection and try again."
fi

# Only import the legacy spreadsheet data if that old tool is actually sitting next door.
if [ ! -f "data/invoices.db" ] && [ -f "../invoice_processor/invoices.xlsx" ]; then
  echo "First run: importing your existing data..."
  "$VPY" migrate_to_db.py
fi

echo "Starting Invoice Processor at $URL"
open "$URL"
"$VPY" app.py
