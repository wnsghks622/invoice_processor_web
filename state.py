"""Read current state for the web UI, sourced from the SQLite database (core/db.py) and the
app's own data/ folder. No dependency on the original invoice_processor/ folder."""
from __future__ import annotations

import datetime
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import config
from core import db
from core import processor as ip


# Sort options offered on the Invoices page: (value, label). Default is the first.
SORT_OPTIONS = [
    ("processed_desc", "Date processed — newest first"),
    ("processed_asc",  "Date processed — oldest first"),
    ("invoice_desc",   "Invoice date — newest first"),
    ("invoice_asc",    "Invoice date — oldest first"),
    ("amount_desc",    "Amount — high to low"),
    ("amount_asc",     "Amount — low to high"),
]
DEFAULT_SORT = SORT_OPTIONS[0][0]   # most recently processed on top


# A few date shapes the tool's _parse_date doesn't cover but that show up in the imported data:
# Excel datetime cells stringified with a time part, and abbreviated-month slash dates.
_EXTRA_DATE_FORMATS = ("%Y-%m-%d %H:%M:%S", "%b/%d/%y", "%b/%d/%Y", "%B/%d/%y", "%B/%d/%Y")


def _parse_date_any(date_str):
    """Parse a stored date string to a datetime, trying the tool's parser first, then a few
    extra shapes seen in the migrated data. Returns None if nothing matches."""
    d = ip._parse_date(date_str)
    if d:
        return d
    t = str(date_str or "").strip()
    if not t:
        return None
    for fmt in _EXTRA_DATE_FORMATS:
        try:
            return datetime.datetime.strptime(t, fmt)
        except ValueError:
            continue
    return None


def _month_key(date_str):
    """('2026-07' sort key, 'July 2026' label) for a stored date string, or (None, None)."""
    d = _parse_date_any(date_str)
    return (d.strftime("%Y-%m"), d.strftime("%B %Y")) if d else (None, None)


def _dup_key(inv: dict) -> str:
    """The duplicate-detection key for a stored invoice, computed exactly like the processor's
    write_invoice did when it flagged the row (so a saved DUPLICATE re-matches its original)."""
    invno = inv.get("invoice_number")
    date_based = ip._is_date_based_number(invno, inv.get("invoice_date"), inv.get("due_date"))
    amount = inv["amount"] if inv.get("amount") is not None else inv.get("amount_text")
    return ip._invoice_key(
        str(inv.get("vendor_name") or ""), str(invno or ""), str(inv.get("unit") or ""),
        property_name=str(inv.get("property") or ""), amount=amount, date_based=date_based,
    )


def _matched_fields(inv: dict) -> list[str]:
    """Human labels for the fields that make up an invoice's duplicate key — i.e. what had to
    match the original for it to be flagged."""
    date_based = ip._is_date_based_number(
        inv.get("invoice_number"), inv.get("invoice_date"), inv.get("due_date"))
    fields = ["vendor"]
    if date_based:
        fields += ["date", "property", "amount"]     # numberless utility bills key on these
    else:
        fields.append("invoice #")
    if str(inv.get("unit") or "").strip():
        fields.append("unit")
    return fields


def annotate_duplicates(rows: list[dict]) -> list[dict]:
    """For each DUPLICATE row in `rows`, attach `duplicate_of` (the original invoice dict, or
    None) and `matched_fields`. The original is the earliest non-duplicate invoice sharing the
    same key, rebuilt live from the whole log (not just the displayed subset)."""
    if not any(r["status"] == "DUPLICATE" for r in rows):
        return rows
    originals: dict[str, dict] = {}
    for inv in db.list_invoices():
        if inv["status"] == "DUPLICATE":
            continue
        k = _dup_key(inv)
        if not k:
            continue
        if k not in originals or inv["id"] < originals[k]["id"]:
            originals[k] = inv          # earliest id with this key = the original
    for r in rows:
        if r["status"] != "DUPLICATE":
            continue
        r["duplicate_of"] = originals.get(_dup_key(r))
        r["matched_fields"] = _matched_fields(r)
    return rows


def _month_dir_sort_key(p: Path) -> datetime.datetime:
    """Chronological key for a '<Month YYYY> Bank Rec' folder. Plain name sorting is
    alphabetical ('May 2026' > 'June 2026', and years don't order at all), so the
    newest-first fallback scan below needs the label parsed as a real date."""
    label = p.name[: -len(" Bank Rec")].strip() if p.name.endswith(" Bank Rec") else p.name
    for fmt in ("%B %Y", "%b %Y"):
        try:
            return datetime.datetime.strptime(label, fmt)
        except ValueError:
            continue
    return datetime.datetime.min


def resolve_invoice_file(inv: dict):
    """Best existing on-disk path for an invoice's filed PDF, or None.

    Reconciled invoices prefer the Bank Rec staged copy — the processed/ archive copy may have
    been removed by a later Cleanup, whereas staging copies into Bank Rec and nothing downstream
    deletes them. Pending invoices live in processed/. Needs-review invoices (and rows whose
    file was never moved out) live in processed/Needs Review/, so that folder is always a
    candidate too. Falls back across all of these so historical reconciled invoices (reconciled
    in the old tool, so no Bank Rec copy here) still resolve to their processed/ copy."""
    stored = os.path.basename((inv.get("stored_file") or "").strip())
    if not stored:
        return None
    prop_safe = ip._safe_folder_name(inv.get("property") or "")
    processed = config.PROCESSED / prop_safe / stored
    reconciled = (inv.get("reconciled") or "").strip()

    candidates = []
    if reconciled:                       # reconciled -> Bank Rec copy first (processed may be gone)
        candidates.append(config.BANK_REC_ROOT / f"{reconciled} Bank Rec" / prop_safe / stored)
    candidates.append(processed)
    review = config.PROCESSED / ip._safe_folder_name(ip.PROPERTY_REVIEW_TAB) / stored
    if review != processed:              # unassigned invoices are filed under Needs Review/
        candidates.append(review)
    if config.BANK_REC_ROOT.exists():    # last resort: any month's staged copy, newest month first
        for monthdir in sorted(config.BANK_REC_ROOT.glob("* Bank Rec"),
                               key=_month_dir_sort_key, reverse=True):
            candidates.append(monthdir / prop_safe / stored)

    for c in candidates:
        try:
            if c.is_file():
                return c
        except OSError:
            continue
    return None


def reveal_in_explorer(path) -> None:
    """Open the OS file manager with `path` selected (server == user on this localhost app).

    On Windows the command line MUST be:  explorer /select,"C:\\full path\\file.pdf"
    with /select, UNquoted and the path quoted on its own. Passing ['explorer', '/select,'+path]
    as a list makes Windows wrap the whole token in quotes -> explorer misreads it and just OPENS
    the file instead of selecting it. So we build the string form directly. A Windows filename
    can't contain a double-quote, so there's no command-injection risk from the quoted path."""
    path = Path(path)
    if sys.platform == "win32":
        norm = os.path.normpath(str(path))
        subprocess.run(f'explorer /select,"{norm}"', check=False)   # returns 1 even on success
    elif sys.platform == "darwin":
        subprocess.run(["open", "-R", str(path)], check=False)
    else:
        subprocess.run(["xdg-open", str(path.parent)], check=False)


def open_folder(folder) -> None:
    """Open a folder in the OS file manager (no file selected)."""
    folder = Path(folder)
    if sys.platform == "win32":
        os.startfile(str(folder))            # noqa: S606 - localhost desktop integration
    elif sys.platform == "darwin":
        subprocess.run(["open", str(folder)], check=False)
    else:
        subprocess.run(["xdg-open", str(folder)], check=False)


def assembled_output_dir(month: str) -> Path:
    """The _output folder the assembler writes ASSEMBLED PDFs / manifests into for a month."""
    return config.BANK_REC_ROOT / f"{month} Bank Rec" / "_output"


def list_assembled_reports(month: str) -> list[dict]:
    """Assembled reconciliation PDFs for a month, one per property, as
    [{property, pdf, manifest, size_kb}] (manifest is None if absent). Newest naming is
    '<property> - ASSEMBLED.pdf' alongside '<property> - manifest.txt'."""
    outdir = assembled_output_dir(month)
    if not outdir.is_dir():
        return []
    suffix = " - ASSEMBLED.pdf"
    out = []
    for pdf in sorted(outdir.glob("*" + suffix)):
        prop = pdf.name[: -len(suffix)]
        # The assembler names the sidecars off the PDF path, so they are
        # '<property> - ASSEMBLED - manifest.txt' / '... - matched.csv' (note the ASSEMBLED).
        manifest = outdir / f"{pdf.stem} - manifest.txt"
        matched = outdir / f"{pdf.stem} - matched.csv"
        out.append({
            "property": prop,
            "pdf": pdf.name,
            "manifest": manifest.name if manifest.exists() else None,
            "matched": matched.name if matched.exists() else None,
            "size_kb": round(pdf.stat().st_size / 1024, 1),
        })
    return out


def property_folder(inv: dict) -> Path:
    """The processed/ folder an invoice's file belongs in: Needs Review/ while it's still
    unassigned, else processed/<property>/."""
    if inv.get("needs_review"):
        return config.PROCESSED / ip._safe_folder_name(ip.PROPERTY_REVIEW_TAB)
    return config.PROCESSED / ip._safe_folder_name(inv.get("property") or "")


def all_file_locations(inv: dict) -> list[Path]:
    """Every existing on-disk copy of this invoice's filed PDF — the processed/ archive (its
    property folder or Needs Review/) plus any staged copies under Bank Rec/<month>/. Used when
    replacing the file so all copies stay in sync."""
    stored = os.path.basename((inv.get("stored_file") or "").strip())
    if not stored:
        return []
    safe = ip._safe_folder_name(inv.get("property") or "")
    review_safe = ip._safe_folder_name(ip.PROPERTY_REVIEW_TAB)
    candidates = [config.PROCESSED / safe / stored]
    if review_safe != safe:
        candidates.append(config.PROCESSED / review_safe / stored)
    if config.BANK_REC_ROOT.exists():
        for monthdir in config.BANK_REC_ROOT.glob("* Bank Rec"):
            candidates.append(monthdir / safe / stored)
    return [p for p in candidates if p.is_file()]


def move_invoice_file(inv: dict, new_property: str):
    """Move an invoice's filed PDF under processed/ into <new_property>'s folder, looking for
    the current copy in the invoice's own folder (property or Needs Review) and then in
    Needs Review explicitly (covers rows whose property was edited without a move). Pass
    ip.PROPERTY_REVIEW_TAB as `new_property` to move a file back into Needs Review.

    Returns the final filename (may differ on a collision), None if there was no file to move
    (record-only update) or it's already in the right folder, or False if the file is locked
    (open in a viewer) so nothing was moved."""
    stored = os.path.basename((inv.get("stored_file") or "").strip())
    if not stored:
        return None
    src_dirs = [property_folder(inv),
                config.PROCESSED / ip._safe_folder_name(ip.PROPERTY_REVIEW_TAB)]
    dest_dir = config.PROCESSED / ip._safe_folder_name(new_property)
    src = None
    for d in src_dirs:
        p = d / stored
        try:
            if p.is_file():
                src = p
                break
        except OSError:
            continue
    if src is None or src.parent == dest_dir:
        return None
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / stored
    stem, suffix, i = dest.stem, dest.suffix, 1
    while dest.exists():                 # same collision rule as reassign_review.move_file
        dest = dest_dir / f"{stem}_{i}{suffix}"
        i += 1
    try:
        shutil.move(str(src), str(dest))
    except (PermissionError, OSError):
        return False
    return dest.name


def distinct_months(field: str) -> list[dict]:
    """Distinct months present across all invoices for a date field ('invoice_date' or
    'date_processed'), newest first, as [{key, label}]."""
    seen = {}
    for r in db.list_invoices():
        key, label = _month_key(r.get(field))
        if key:
            seen[key] = label
    return [{"key": k, "label": seen[k]} for k in sorted(seen, reverse=True)]


def sort_and_filter_invoices(rows: list[dict], sort: str, imonth: str, pmonth: str,
                             amin=None, amax=None) -> list[dict]:
    """Attach month keys, apply the invoice-month / processed-month / amount-range filters, and
    sort. Rows with an unparseable date sort to the bottom; rows with no numeric amount sort to
    the bottom and are excluded whenever an amount range is set."""
    for r in rows:
        ik, _ = _month_key(r.get("invoice_date"))
        pk, _ = _month_key(r.get("date_processed"))
        r["invoice_month_key"] = ik or ""
        r["processed_month_key"] = pk or ""
        r["_isort"] = _parse_date_any(r.get("invoice_date")) or datetime.datetime.min
        r["_psort"] = _parse_date_any(r.get("date_processed")) or datetime.datetime.min
    if imonth:
        rows = [r for r in rows if r["invoice_month_key"] == imonth]
    if pmonth:
        rows = [r for r in rows if r["processed_month_key"] == pmonth]
    if amin is not None:
        rows = [r for r in rows if r["amount"] is not None and r["amount"] >= amin]
    if amax is not None:
        rows = [r for r in rows if r["amount"] is not None and r["amount"] <= amax]
    sorters = {
        "invoice_desc":   (lambda r: r["_isort"], True),
        "invoice_asc":    (lambda r: r["_isort"], False),
        "processed_desc": (lambda r: r["_psort"], True),
        "processed_asc":  (lambda r: r["_psort"], False),
        "amount_desc":    (lambda r: r["amount"] if r["amount"] is not None else float("-inf"), True),
        "amount_asc":     (lambda r: r["amount"] if r["amount"] is not None else float("inf"),  False),
    }
    keyfn, reverse = sorters.get(sort, sorters[DEFAULT_SORT])
    rows.sort(key=keyfn, reverse=reverse)
    return rows


# --------------------------------------------------------------------------- input inbox

def count_pending_invoices() -> int:
    """Files sitting in the drop folder, waiting to be processed."""
    if not config.INVOICES_TO_PROCESS.exists():
        return 0
    return sum(
        1 for p in config.INVOICES_TO_PROCESS.iterdir()
        if p.is_file() and p.suffix.lower() in config.PDF_EXTS
    )


def list_pending_invoices() -> list[dict]:
    if not config.INVOICES_TO_PROCESS.exists():
        return []
    out = []
    for p in sorted(config.INVOICES_TO_PROCESS.iterdir()):
        if p.is_file() and p.suffix.lower() in config.PDF_EXTS:
            out.append({"name": p.name, "size_kb": round(p.stat().st_size / 1024, 1)})
    return out


# --------------------------------------------------------------------------- DB-derived views

def per_property_pending() -> list[dict]:
    """Every property + its pending-invoice count (from the DB), in list order."""
    pending = db.pending_by_property()
    out = []
    for p in db.all_properties():
        out.append({"name": p["name"], "code": p["code"],
                    "pending": pending.get(p["name"], 0)})
    return out


def dashboard_counts() -> dict:
    return db.counts()


# --------------------------------------------------------------------------- settings

def load_settings() -> dict:
    default = {
        "dest_root": str(config.DATA_DIR),
        "month": datetime.date.today().strftime("%B %Y"),
    }
    if config.SETTINGS_JSON.exists():
        try:
            default.update(json.loads(config.SETTINGS_JSON.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            pass
    return default


def save_settings(settings: dict) -> None:
    config.SETTINGS_JSON.parent.mkdir(parents=True, exist_ok=True)
    config.SETTINGS_JSON.write_text(json.dumps(settings, indent=2), encoding="utf-8")


def month_options(n: int = 12) -> list[str]:
    today = datetime.date.today().replace(day=1)
    out, y, m = [], today.year, today.month
    for _ in range(n):
        out.append(datetime.date(y, m, 1).strftime("%B %Y"))
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    return out


def api_key_present() -> bool:
    if config.ENV_FILE.exists():
        try:
            for line in config.ENV_FILE.read_text(encoding="utf-8").splitlines():
                if line.strip().startswith("ANTHROPIC_API_KEY=") and line.split("=", 1)[1].strip():
                    return True
        except OSError:
            pass
    return bool(os.environ.get("ANTHROPIC_API_KEY"))
