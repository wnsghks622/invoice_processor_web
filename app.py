"""Flask app for the invoice-processor web UI (SQLite-backed, fully self-contained).

Localhost single-user app - no auth, dev server is fine. Data lives in the SQLite DB
(core/db.py) and the data/ folder. Long-running jobs shell out to the vendored core/ modules
and stream their output over SSE (runner.py)."""
from __future__ import annotations

import datetime
import shutil
from pathlib import Path
from urllib.parse import urlparse

from flask import (
    Flask, render_template, request, redirect, url_for, jsonify, Response,
    stream_with_context, flash, send_file, abort,
)

import config
import state
from core import db
from runner import stream_module

app = Flask(__name__)
app.secret_key = "invoice-processor-local"        # only for flash messages on localhost
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024

db.init()   # ensure schema exists on startup


@app.before_request
def _reject_cross_site():
    """Localhost-only app, but any web page the user visits can still make their browser fire
    requests at 127.0.0.1:5057 - a drive-by <img>/EventSource/form-POST could run jobs or
    delete records. Browsers label such requests (Sec-Fetch-Site: cross-site, or a foreign
    Origin header); reject them. Same-origin use and curl (no such headers) are unaffected."""
    if request.headers.get("Sec-Fetch-Site", "").lower() == "cross-site":
        abort(403)
    origin = request.headers.get("Origin")
    if origin:
        host = (urlparse(origin).hostname or "").lower()
        if host not in ("127.0.0.1", "localhost"):
            abort(403)


@app.context_processor
def inject_shell():
    """The sidebar badge and period pill are part of base.html, so every page needs them.
    `file_manager` is here too so no template has to test the platform itself."""
    return {
        "shell_needs_review": db.counts()["needs_review"],
        "shell_month": state.load_settings()["month"],
        "file_manager": state.FILE_MANAGER,
    }


# =========================================================================== dashboard

@app.route("/")
def dashboard():
    return render_template(
        "dashboard.html",
        counts=state.dashboard_counts(),
        pending_invoices=state.count_pending_invoices(),
        properties=state.per_property_pending(),
        settings=state.load_settings(),
        api_key_present=state.api_key_present(),
    )


# =========================================================================== process invoices

@app.route("/process")
def process_page():
    return render_template("process.html", files=state.list_pending_invoices())


@app.route("/api/upload", methods=["POST"])
def api_upload():
    config.INVOICES_TO_PROCESS.mkdir(parents=True, exist_ok=True)
    saved, skipped = [], []
    for f in request.files.getlist("files"):
        if not f.filename:
            continue
        name = Path(f.filename).name
        if not name:
            continue
        if Path(name).suffix.lower() not in config.PDF_EXTS:
            skipped.append(name)         # not a type the processor reads - don't save it
            continue
        dest = config.INVOICES_TO_PROCESS / name
        i = 1
        while dest.exists():
            dest = config.INVOICES_TO_PROCESS / f"{Path(name).stem} ({i}){Path(name).suffix}"
            i += 1
        f.save(dest)
        saved.append(dest.name)
    return jsonify({"saved": saved, "count": len(saved), "skipped": skipped})


@app.route("/api/pending-files")
def api_pending_files():
    return jsonify(state.list_pending_invoices())


@app.route("/api/run/process")
def api_run_process():
    return Response(stream_with_context(stream_module("processor")),
                    mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# =========================================================================== invoices

INVOICES_PAGE_SIZE = 100


@app.route("/invoices")
def invoices_page():
    prop = request.args.get("property") or None
    filt = request.args.get("filter", "")
    sort = request.args.get("sort", state.DEFAULT_SORT)
    imonth = request.args.get("imonth", "")
    pmonth = request.args.get("pmonth", "")
    amin = _parse_money_arg(request.args.get("amin", ""))
    amax = _parse_money_arg(request.args.get("amax", ""))
    kwargs = {}
    if filt == "pending":
        kwargs["pending_only"] = True
    elif filt == "review":
        kwargs["needs_review"] = True
    elif filt == "duplicate":
        kwargs["duplicates_only"] = True
    rows = db.list_invoices(property=prop, search=request.args.get("q", ""), **kwargs)
    rows = state.sort_and_filter_invoices(rows, sort, imonth, pmonth, amin, amax)

    # Paginate BEFORE the per-row annotation below - duplicate lookups and the on-disk
    # file checks are the expensive part, so only the visible page pays for them.
    total_rows = len(rows)
    pages = max(1, -(-total_rows // INVOICES_PAGE_SIZE))
    try:
        page = int(request.args.get("page", 1))
    except ValueError:
        page = 1
    page = min(max(page, 1), pages)
    rows = rows[(page - 1) * INVOICES_PAGE_SIZE : page * INVOICES_PAGE_SIZE]

    state.annotate_duplicates(rows)      # attach duplicate_of + matched_fields to DUPLICATE rows
    for r in rows:                       # flag which rows have an openable file on disk
        r["has_file"] = state.resolve_invoice_file(r) is not None
    base_args = {k: v for k, v in request.args.items() if k != "page"}
    return render_template("invoices.html", invoices=rows,
                           properties=db.all_properties(),
                           current_property=prop, current_filter=filt,
                           q=request.args.get("q", ""),
                           sort_options=state.SORT_OPTIONS, current_sort=sort,
                           invoice_months=state.distinct_months("invoice_date"),
                           processed_months=state.distinct_months("date_processed"),
                           current_imonth=imonth, current_pmonth=pmonth,
                           amin=request.args.get("amin", ""), amax=request.args.get("amax", ""),
                           page=page, pages=pages, total_rows=total_rows,
                           page_size=INVOICES_PAGE_SIZE, base_args=base_args)


def _parse_money_arg(s: str):
    """Parse a money filter value ('1,000', '$500.50', '') to a float, or None if blank/invalid."""
    s = (s or "").replace(",", "").replace("$", "").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


@app.route("/invoices/<int:invoice_id>/file")
def invoice_file(invoice_id):
    """Open the invoice's filed PDF (inline in a new tab). Reconciled invoices resolve to the
    Bank Rec staged copy, others to processed/ — see state.resolve_invoice_file."""
    inv = db.get_invoice(invoice_id)
    if not inv:
        abort(404)
    path = state.resolve_invoice_file(inv)
    if not path:
        name = inv.get("stored_file") or "(no filename recorded)"
        return render_template(
            "error.html",
            message=f"No saved file found on disk for this invoice ({name}). It may not have been "
                    f"filed as a PDF, or the file was moved.",
        ), 404
    return send_file(str(path))          # inferred mimetype; PDFs open inline in the browser


@app.route("/invoices/<int:invoice_id>/reveal", methods=["POST"])
def invoice_reveal(invoice_id):
    """Open Windows File Explorer at this invoice's file (selected). If the file isn't on disk,
    fall back to opening its property folder in processed/ so the user can look."""
    inv = db.get_invoice(invoice_id)
    if not inv:
        return jsonify({"ok": False, "error": "Invoice not found."}), 404
    path = state.resolve_invoice_file(inv)
    if path:
        state.reveal_in_explorer(path)
        return jsonify({"ok": True, "mode": "file", "path": str(path)})
    folder = state.property_folder(inv)
    if folder.is_dir():
        state.open_folder(folder)
        return jsonify({"ok": True, "mode": "folder", "path": str(folder),
                        "note": "The file isn't on disk; opened its property folder instead."})
    return jsonify({"ok": False,
                    "error": f"No file or folder found for this invoice "
                             f"(property '{inv.get('property') or '—'}')."}), 404


@app.route("/api/invoices/<int:invoice_id>/yardi", methods=["POST"])
def api_toggle_yardi(invoice_id):
    entered = request.json.get("entered", False) if request.is_json else False
    db.set_entered_in_yardi(invoice_id, bool(entered))
    return jsonify({"ok": True})


@app.route("/invoices/<int:invoice_id>/delete", methods=["POST"])
def delete_invoice(invoice_id):
    """Delete an invoice record. The filed PDF is deliberately left on disk (re-processing the
    file can recreate the record; a deleted PDF is gone). Refreshes the sidecars so the invoice
    drops out of the pending pool / next month's staging."""
    inv = db.get_invoice(invoice_id)
    if not inv:
        flash("That invoice no longer exists.")
        return redirect(url_for("invoices_page"))
    db.delete_invoice(invoice_id)
    db.export_amount_sidecars(config.PROCESSED)
    label = f"{inv['vendor_name']} #{inv['invoice_number'] or '--'}"
    kept = f" Its file ({inv['stored_file']}) was left on disk." if inv.get("stored_file") else ""
    flash(f"Deleted invoice: {label}.{kept}")
    return redirect(request.referrer or url_for("invoices_page"))


@app.route("/invoices/<int:invoice_id>/replace-file", methods=["POST"])
def replace_file(invoice_id):
    """Swap the filed PDF for a better copy, keeping the invoice record and its stored_file name
    (so all references stay valid). Overwrites every on-disk copy (processed/ + any Bank Rec
    copies) and backs the old one up under a _replaced/ subfolder first."""
    inv = db.get_invoice(invoice_id)
    if not inv:
        flash("That invoice no longer exists.")
        return redirect(url_for("invoices_page"))
    stored = (inv.get("stored_file") or "").strip()
    if not stored:
        flash("This invoice has no filed PDF to replace.")
        return redirect(request.referrer or url_for("invoices_page"))

    f = request.files.get("file")
    if not f or not f.filename:
        flash("Choose a file to upload.")
        return redirect(request.referrer or url_for("invoices_page"))

    # Keep the same name/extension, so require the upload to match the stored extension.
    stored_ext = Path(stored).suffix.lower()
    if Path(f.filename).suffix.lower() != stored_ext:
        flash(f"The new file must be a {stored_ext or 'matching'} file (to keep the filed name "
              f"'{stored}'). You uploaded '{f.filename}'.")
        return redirect(request.referrer or url_for("invoices_page"))

    data = f.read()
    if not data:
        flash("That file was empty — nothing replaced.")
        return redirect(request.referrer or url_for("invoices_page"))

    locations = state.all_file_locations(inv)
    if not locations:                                  # nothing on disk yet — write to processed/
        dest = state.property_folder(inv) / stored
        dest.parent.mkdir(parents=True, exist_ok=True)
        locations = [dest]

    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    replaced, locked = [], []
    for p in locations:
        if p.is_file():                                # back up the old copy before overwriting
            bak_dir = p.parent / "_replaced"
            try:
                bak_dir.mkdir(exist_ok=True)
                shutil.copy2(p, bak_dir / f"{p.stem} (replaced {stamp}){p.suffix}")
            except OSError:
                pass
        try:
            p.write_bytes(data)
            replaced.append(p)
        except OSError:                                # open in a viewer / locked
            locked.append(p)

    if not replaced:
        flash(f"Could not replace '{stored}' - the file is open in a viewer or locked. "
              f"Close it and try again; the PDF was not replaced.")
        return redirect(request.referrer or url_for("invoices_page"))

    # A multi-bill PDF is shared by several invoice rows — replacing it updates all of them.
    shared = [i for i in db.list_invoices()
              if (i["stored_file"] or "").strip().lower() == stored.lower()
              and i["property"] == inv["property"]]
    shared_note = f" This file is shared by {len(shared)} invoice rows." if len(shared) > 1 else ""
    locked_note = (f" NOTE: {len(locked)} location(s) were locked and NOT updated - "
                   f"close viewers and replace again." if locked else "")
    flash(f"Replaced the PDF for {inv['vendor_name']} #{inv['invoice_number'] or '--'} "
          f"in {len(replaced)} location(s); the old copy was backed up.{shared_note}{locked_note}")
    return redirect(request.referrer or url_for("invoices_page"))


@app.route("/invoices/<int:invoice_id>/not-duplicate", methods=["POST"])
def not_duplicate(invoice_id):
    """Manually clear a wrong DUPLICATE flag: flip the row to OK and refresh the sidecars so it
    re-enters the pending pool and can be reconciled."""
    inv = db.get_invoice(invoice_id)
    if not inv:
        flash("That invoice no longer exists.")
        return redirect(url_for("invoices_page"))
    if inv["status"] == "DUPLICATE":
        db.update_invoice(invoice_id, status="OK")
        db.export_amount_sidecars(config.PROCESSED)
        flash(f"Marked as NOT a duplicate: {inv['vendor_name']} #{inv['invoice_number'] or '--'} "
              f"— it's back in the pending pool.")
    return redirect(request.referrer or url_for("invoices_page"))


@app.route("/invoices/<int:invoice_id>/edit", methods=["POST"])
def edit_invoice(invoice_id):
    from core import processor as ip
    inv = db.get_invoice(invoice_id)
    if not inv:
        flash("That invoice no longer exists.")
        return redirect(request.referrer or url_for("invoices_page"))
    fields = {k: request.form[k] for k in (
        "vendor_name", "invoice_number", "unit", "invoice_date", "amount_text",
        "property", "check_number") if k in request.form}

    # A property change is a reassignment, same as the fixer: move the filed PDF into the new
    # property's folder (out of Needs Review / the old folder) and keep the review flag in
    # step - otherwise the record points at a folder the file isn't in.
    if "property" in fields and fields["property"] != (inv["property"] or ""):
        new_prop = fields["property"]
        newname = state.move_invoice_file(inv, new_prop or ip.PROPERTY_REVIEW_TAB)
        if newname is False:
            flash(f"'{inv.get('stored_file')}' is open in a viewer - close it and retry. "
                  f"No changes were saved.")
            return redirect(request.referrer or url_for("invoices_page"))
        if newname and newname != inv.get("stored_file"):
            fields["stored_file"] = newname          # collision-renamed: keep the join key in sync
        fields["needs_review"] = 0 if new_prop else 1

    # amount: allow editing the numeric value via amount_text -> parse
    if "amount_text" in fields:
        amt = ip._parse_amount(fields["amount_text"])
        fields["amount"] = amt
        fields["amount_text"] = "" if amt is not None else fields["amount_text"]
    db.update_invoice(invoice_id, **fields)
    db.export_amount_sidecars(config.PROCESSED)      # amount/check#/property all feed the sidecars
    flash("Invoice updated.")
    return redirect(request.referrer or url_for("invoices_page"))


# =========================================================================== properties

@app.route("/properties")
def properties_page():
    pending = db.pending_by_property()
    props = db.all_properties()
    for p in props:
        p["pending"] = pending.get(p["name"], 0)
    return render_template("properties.html", properties=props)


@app.route("/properties/add", methods=["POST"])
def add_property():
    name = request.form.get("canonical_name", "").strip()
    if name:
        try:
            db.add_property(name, request.form.get("property_code", ""),
                            request.form.get("aliases", ""))
            flash(f"Added property '{name}'.")
        except Exception as e:
            flash(f"Could not add: {e}")
    return redirect(url_for("properties_page"))


@app.route("/properties/<int:prop_id>/edit", methods=["POST"])
def edit_property(prop_id):
    db.update_property(prop_id, request.form.get("canonical_name", ""),
                       request.form.get("property_code", ""), request.form.get("aliases", ""))
    flash("Property updated.")
    return redirect(url_for("properties_page"))


@app.route("/properties/<int:prop_id>/delete", methods=["POST"])
def delete_property(prop_id):
    db.delete_property(prop_id)
    flash("Property deleted.")
    return redirect(url_for("properties_page"))


# =========================================================================== vendors

@app.route("/vendors")
def vendors_page():
    return render_template("vendors.html", vendors=db.all_vendors())


@app.route("/vendors/add", methods=["POST"])
def add_vendor():
    short = request.form.get("short_name", "").strip()
    if short:
        try:
            db.add_vendor(short, request.form.get("aliases", ""))
            flash(f"Added vendor '{short}'.")
        except Exception as e:
            flash(f"Could not add: {e}")
    return redirect(url_for("vendors_page"))


@app.route("/vendors/<int:vendor_id>/edit", methods=["POST"])
def edit_vendor(vendor_id):
    db.update_vendor(vendor_id, request.form.get("short_name", ""), request.form.get("aliases", ""))
    flash("Vendor updated.")
    return redirect(url_for("vendors_page"))


@app.route("/vendors/<int:vendor_id>/delete", methods=["POST"])
def delete_vendor(vendor_id):
    db.delete_vendor(vendor_id)
    flash("Vendor deleted.")
    return redirect(url_for("vendors_page"))


# =========================================================================== needs-review fixer

@app.route("/fixer")
def fixer():
    reviews = db.review_invoices()
    for r in reviews:                    # let each row link to its PDF (lives in Needs Review/)
        r["has_file"] = state.resolve_invoice_file(r) is not None
    return render_template("fixer.html", reviews=reviews, properties=db.all_properties())


@app.route("/fixer/<int:invoice_id>/assign", methods=["POST"])
def fixer_assign(invoice_id):
    chosen = request.form.get("property", "").strip()
    if not chosen:
        flash("Pick a property.")
        return redirect(url_for("fixer"))
    inv = db.get_invoice(invoice_id)
    if not inv:
        flash("That invoice no longer exists.")
        return redirect(url_for("fixer"))
    from core import reassign_review as rr
    newname = rr.move_file(config.PROCESSED, str(inv.get("stored_file") or ""), chosen)
    if newname is False:
        flash(f"'{inv.get('stored_file')}' is open in a viewer - close it and retry.")
        return redirect(url_for("fixer"))
    new_stored = newname if (newname and newname != inv.get("stored_file")) else None
    db.reassign_property(invoice_id, chosen, new_stored)
    db.export_amount_sidecars(config.PROCESSED)
    flash(f"Assigned to {chosen}.")
    return redirect(url_for("fixer"))


# =========================================================================== month-end wizard

@app.route("/wizard")
def wizard():
    return render_template("wizard.html",
                           settings=state.load_settings(),
                           properties=state.per_property_pending(),
                           months=state.month_options(),
                           bank_rec_root=str(config.BANK_REC_ROOT))


@app.route("/api/run/stage")
def api_run_stage():
    month = request.args.get("month", "").strip()
    prop = request.args.get("property", "").strip()
    args = ["--month", month, "--dest", str(config.BANK_REC_ROOT)]
    if prop:
        args += ["--property", prop]
    return Response(stream_with_context(stream_module("stage_month", *args)),
                    mimetype="text/event-stream")


@app.route("/api/run/assemble")
def api_run_assemble():
    month = request.args.get("month", "").strip()
    month_root = config.BANK_REC_ROOT / f"{month} Bank Rec"
    outdir = month_root / "_output"
    prop = request.args.get("property", "").strip()
    if prop:
        from core import processor as ip
        folder = month_root / ip._safe_folder_name(prop)
        args = [str(folder), "--outdir", str(outdir), "--month", month]
    else:
        args = ["--batch", str(month_root), "--outdir", str(outdir), "--month", month]
    return Response(stream_with_context(stream_module("bankrec", *args)),
                    mimetype="text/event-stream")


@app.route("/api/reports")
def api_reports():
    """Assembled reports available for a month (drives the wizard's report list)."""
    return jsonify(state.list_assembled_reports(request.args.get("month", "").strip()))


@app.route("/reports/view")
def report_view():
    """Serve an assembled report (PDF or manifest txt) inline in the browser. The file must be a
    real file inside that month's _output folder (guards against path traversal)."""
    month = request.args.get("month", "").strip()
    fname = Path(request.args.get("file", "").strip()).name        # basename only, no traversal
    if not month or fname.lower().rsplit(".", 1)[-1] not in ("pdf", "txt", "csv"):
        abort(404)
    outdir = state.assembled_output_dir(month).resolve()
    path = (outdir / fname).resolve()
    if outdir not in path.parents or not path.is_file():
        abort(404)
    return send_file(str(path))


@app.route("/api/open-folder/bankrec", methods=["POST"])
def api_open_bankrec_folder():
    """Open the staged month folder in File Explorer so the user can drop bank documents in.
    Opens the specific property subfolder when one is selected, else the month root."""
    month = request.args.get("month", "").strip()
    prop = request.args.get("property", "").strip()
    folder = config.BANK_REC_ROOT / f"{month} Bank Rec"
    if prop:
        from core import processor as ip
        folder = folder / ip._safe_folder_name(prop)
    if folder.is_dir():
        state.open_folder(folder)
        return jsonify({"ok": True, "path": str(folder)})
    return jsonify({"ok": False,
                    "error": f"That folder doesn't exist yet — run Stage first.\n{folder}"}), 404


@app.route("/api/run/reconcile")
def api_run_reconcile():
    month = request.args.get("month", "").strip()
    outdir = config.BANK_REC_ROOT / f"{month} Bank Rec" / "_output"
    args = [str(outdir), "--month", month]
    return Response(stream_with_context(stream_module("reconcile", *args)),
                    mimetype="text/event-stream")


# =========================================================================== settings

@app.route("/settings")
def settings_page():
    return render_template("settings.html",
                           settings=state.load_settings(),
                           api_key_present=state.api_key_present(),
                           api_key_from_env=state.api_key_from_env_var(),
                           env_file=str(config.ENV_FILE),
                           data_dir=str(config.DATA_DIR))


@app.route("/settings/save", methods=["POST"])
def save_settings_route():
    s = state.load_settings()
    s["month"] = request.form.get("month", s["month"]).strip() or s["month"]
    state.save_settings(s)
    flash("Settings saved.")
    return redirect(url_for("settings_page"))


@app.route("/settings/api-key", methods=["POST"])
def save_api_key_route():
    """Save the Anthropic API key into .env. The value is never echoed back to the page,
    put in a flash message, or logged - only whether a key is now saved."""
    # Check for an outside override BEFORE saving: save_api_key() updates os.environ itself,
    # which would hide the evidence that a shell/system variable was supplying a different key.
    had_override = state.api_key_from_env_var()
    try:
        state.save_api_key(request.form.get("api_key", ""))
    except ValueError as e:
        flash(str(e))
        return redirect(url_for("settings_page"))
    except OSError as e:
        flash(f"Could not write the .env file: {e}")
        return redirect(url_for("settings_page"))
    if had_override:
        flash("API key saved and in use now. Note: ANTHROPIC_API_KEY is also set as a system "
              "environment variable on this machine — remove it, or that one will take over "
              "again the next time the app restarts.")
    else:
        flash("API key saved. The next run will use it (no restart needed).")
    return redirect(url_for("settings_page"))


@app.route("/api/export-xlsx", methods=["POST"])
def api_export_xlsx():
    try:
        path = db.export_to_xlsx()
    except OSError as e:                 # typically: the export is open in Excel
        flash(f"Could not write the export - is invoices_export.xlsx open in Excel? "
              f"Close it and try again. ({e})")
        return redirect(url_for("settings_page"))
    flash(f"Exported to {path.name} (in the data folder).")
    return redirect(url_for("settings_page"))


@app.errorhandler(404)
def not_found(e):
    return render_template("error.html", message="Page not found."), 404


@app.errorhandler(403)
def forbidden(e):
    return render_template(
        "error.html",
        message="Blocked: this request came from another website, not from the app itself.",
    ), 403


def main():
    print(f"invoice_processor_web  ->  http://127.0.0.1:{config.PORT}/")
    print(f"database:  {config.DB_PATH}")
    try:
        b = db.backup_db()
        if b:
            print(f"backup:    {b.name}  (daily copy in data\\backups, newest 14 kept)")
    except Exception as e:                         # a failed backup must never block startup
        print(f"  [!] Daily DB backup failed: {e}")
    if db.is_empty():
        print("  [!] The database is empty - run  python migrate_to_db.py  to import your data.")
    app.run(host="127.0.0.1", port=config.PORT, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
