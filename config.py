"""Paths + constants for the self-contained web app.

Everything lives under this folder now — its own vendored code (core/) and its own data
(data/). The original invoice_processor/ folder is a frozen fallback and is never read or
written by the running app (only the one-time migrate_to_db.py reads it, read-only)."""
from pathlib import Path

HERE = Path(__file__).resolve().parent

# --- This app's own data (independent copy; seeded once by migrate_to_db.py) ---
DATA_DIR = HERE / "data"
DB_PATH = DATA_DIR / "invoices.db"
INVOICES_TO_PROCESS = DATA_DIR / "invoices_to_process"
PROCESSED = DATA_DIR / "processed"
BANK_REC_ROOT = DATA_DIR / "Bank Rec"
SETTINGS_JSON = DATA_DIR / "settings.json"
ENV_FILE = HERE / ".env"

# --- Where the one-time migration reads the current live data from (READ-ONLY) ---
# Kept here so migrate_to_db.py has a single place to find the original files. The running
# app must never write anything under here.
ORIGINAL_TOOL_DIR = HERE.parent / "invoice_processor"
ORIGINAL_XLSX = ORIGINAL_TOOL_DIR / "invoices.xlsx"
ORIGINAL_PROPERTIES_CSV = ORIGINAL_TOOL_DIR / "properties.csv"
ORIGINAL_VENDORS_CSV = ORIGINAL_TOOL_DIR / "vendors.csv"

PORT = 5057

# File types accepted for upload + shown as pending. Must stay in step with the types the
# processor can actually read (processor.EXTENSION_MAP).
PDF_EXTS = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".gif", ".webp"}
