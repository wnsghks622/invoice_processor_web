@echo off
setlocal
cd /d "%~dp0"

rem Already running? Don't stack a second server on the same port - that's how you end up
rem with a stale copy still serving old code. Just open the browser instead.
netstat -ano | findstr /r /c:":5057 .*LISTENING" >nul
if not errorlevel 1 (
  echo Invoice Processor is already running - opening it in your browser.
  echo If you just changed the code, close the other server window first, then run this again.
  start "" http://127.0.0.1:5057/
  pause
  exit /b 0
)

where python >nul 2>&1
if errorlevel 1 (
  echo Python is not installed or not on PATH.
  echo Install from https://python.org and tick "Add Python to PATH".
  pause
  exit /b 1
)

rem Check every required package, not just Flask - a half-installed environment
rem otherwise slips through and crashes later.
python -c "import flask, anthropic, openpyxl, pypdf, dotenv" 2>nul
if errorlevel 1 (
  echo Installing required packages...
  python -m pip install -r requirements.txt
)

if not exist "data\invoices.db" (
  echo First run: importing your data into the database...
  python migrate_to_db.py
)

start "" http://127.0.0.1:5057/
python app.py
