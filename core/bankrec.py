#!/usr/bin/env python3
"""
bankrec.py - Amount-verified monthly Bank Reconciliation report assembler.

For one property folder it:
  1. Classifies every PDF (bank rec report, bank statement, cover page,
     financial reports, deposit slips, invoices).
  2. Parses the Bank Rec Report for the cleared transactions (the truth set:
     date / tran# / notes / amount) and the reconciliation difference.
  3. Parses the bank statement into ordered transaction lines (deposits, other
     credits, other debits, electronic, cleared checks), excluding summary and
     daily-balance blocks.
  4. Reads each deposit slip and invoice's amount - from the invoice processor's
     _amounts.csv sidecar when present (verified, no OCR needed), otherwise the text
     layer or OCR - and places each supporting document at the bank statement line it
     belongs to, so the slips and invoices follow the statement line by line. Main
     placement signals, strongest first: a user-entered check number, the verified
     amount matching a statement line, a batched "Settlement" line, an aggregate total
     (subset-sum), then vendor grouping.
  5. Merges the cleared documents into one final PDF (outstanding invoices not on the
     statement are left OUT of the PDF but flagged), writes a manifest (.txt) flagging
     every match high / medium / low, and writes a matched.csv that reconcile.py reads to
     mark cleared invoices and carry the rest forward.

Usage:
    python3 bankrec.py "<property folder>" [--outdir DIR] [--month "May 2026"]
            [--out OUT.pdf] [--order grouped|interleaved] [--ocr auto|on|off] [--strict]
    python3 bankrec.py --batch "<parent folder>" [...]
"""

import os, re, io, sys, csv, argparse, datetime, logging

from pypdf import PdfReader, PdfWriter

logging.getLogger("pypdf").setLevel(logging.ERROR)  # silence recoverable-PDF noise

# ---------------------------------------------------------------- OCR (optional)
# OCR is used only for scanned slips/invoices that have no text layer. The tool
# runs fine without it (those files fall back to the filename amount + a flag).
try:
    import fitz                      # PyMuPDF, renders pages to images
    import pytesseract
    from PIL import Image
    _OCR_LIBS = True
except Exception:
    _OCR_LIBS = False

def _find_tesseract():
    import shutil
    p = shutil.which("tesseract")
    if p:
        return p
    for c in (r"C:\Program Files\Tesseract-OCR\tesseract.exe",
              r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
              os.path.expandvars(r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe")):
        if os.path.exists(c):
            return c
    return None

_TESS = _find_tesseract()
if _OCR_LIBS and _TESS:
    pytesseract.pytesseract.tesseract_cmd = _TESS
OCR_AVAILABLE = bool(_OCR_LIBS and _TESS)

# ---------------------------------------------------------------- classification
FINANCIAL_KEYS = [
    "balancesheet", "balance sheet", "cashflow", "cash flow", "incomestatement",
    "income statement", "transactionregisters", "transaction register",
    "bankregisterreport", "bank register", "trialbalance", "trial balance",
    "generalledger", "general ledger", "aranalytics", "ar analytics",
    "rentroll", "rent roll", "receiveable", "receivable", "12monthscashflow",
    "check register", "deposit register", "receipt register", "receivable summary",
]
STATEMENT_KEYS = ["statement", "stmt", "transaction history"]
# Bank names that identify a statement file named only by its bank. NOTE: do not
# add vendor names here - "Athens Services" (a trash hauler) is an invoice vendor,
# not a bank, so it must stay out of this list.
BANK_HINTS = ["hanmi", "bank of hope", "preferred bank",
              "city national", "chase", "wells", "us bank", "citibank"]

# A bank line whose description contains one of these is a batched settlement:
# one ACH line that actually covers several invoices (debit) or slips (credit).
SETTLEMENT_KEYS = ["settlement"]

# Generic words ignored when matching a vendor name across statement/rec/invoice.
VENDOR_STOP = {
    "slip", "deposit", "despoit", "invoice", "page", "bank", "payment", "payments",
    "settlement", "direct", "directpay", "ach", "wips", "epay", "epayv", "epayw",
    "epayz", "corp", "corporation", "incorporated", "company", "the", "real",
    "estate", "services", "service", "rent", "vendor", "paid", "online", "credit",
    "card", "deposits", "llc", "inc", "dba", "for", "and", "with", "from", "cleared",
}

def norm(s):
    return re.sub(r"\s+", " ", s.lower()).strip()

def classify(fname):
    n = norm(os.path.basename(fname))
    base = n.replace(" ", "")
    if "bankrec" in base or "bank rec" in n or "rec report" in n:
        return "rec"
    if "cover page" in n or "coverpage" in base:
        return "cover"
    for k in FINANCIAL_KEYS:
        if k in n:
            return "financial"
    for k in STATEMENT_KEYS:
        if k in n:
            return "statement"
    for k in BANK_HINTS:
        if k in n:
            return "statement"
    return "support"

def list_pdfs(folder):
    out = []
    for root, _dirs, files in os.walk(folder):
        for f in files:
            if f.lower().endswith(".pdf"):
                out.append(os.path.join(root, f))
    return sorted(out)

# ---------------------------------------------------------------- PDF text / OCR
AMT = r"-?[\d,]+\.\d{2}"
_text_cache, _ocr_cache = {}, {}

def _fixed_bytes(path):
    """File bytes, healing a wrong leading header (some vendor PDFs start with
    '%%BILLID...' instead of '%PDF') so readers don't choke."""
    with open(path, "rb") as fh:
        data = fh.read()
    if not data.startswith(b"%PDF"):
        i = data.find(b"%PDF")
        if i > 0:
            data = data[i:]
    return data

def _reader(path):
    return PdfReader(io.BytesIO(_fixed_bytes(path)))

def text_layer(path):
    if path in _text_cache:
        return _text_cache[path]
    try:
        t = "\n".join((pg.extract_text() or "") for pg in _reader(path).pages)
    except Exception:
        t = ""
    _text_cache[path] = t
    return t

def ocr_text(path, dpi=300):
    if not OCR_AVAILABLE:
        return ""
    if path in _ocr_cache:
        return _ocr_cache[path]
    try:
        doc = fitz.open(stream=_fixed_bytes(path), filetype="pdf")
        parts = []
        for pg in doc:
            pix = pg.get_pixmap(dpi=dpi)
            parts.append(pytesseract.image_to_string(Image.open(io.BytesIO(pix.tobytes("png")))))
        t = "\n".join(parts)
    except Exception:
        t = ""
    _ocr_cache[path] = t
    return t

def content_text(path, ocr_mode):
    """(text, ocr_used). Uses the embedded text layer; if there is essentially
    none and OCR is allowed, falls back to OCR."""
    t = text_layer(path)
    if len(t.strip()) >= 20:
        return t, False
    if ocr_mode != "off":
        ot = ocr_text(path)
        if ot.strip():
            return ot, True
    return t, False

def amounts_in(text):
    out = set()
    for x in re.findall(AMT, text):
        try:
            out.add(round(abs(float(x.replace(",", ""))), 2))
        except ValueError:
            pass
    return out

def slip_total(text):
    m = re.search(r"Total Deposit\s*\$?\s*([\d,]+\.\d{2})", text, re.I)
    return round(float(m.group(1).replace(",", "")), 2) if m else None

def slip_date(text):
    """The deposit date printed on a deposit slip, as a datetime (or None). Prefers the
    explicit 'Deposit Date MM/DD/YYYY' field; otherwise the first full (4-digit-year) date
    on the slip. The 4-digit-year requirement skips the print-time footer stamp (e.g.
    '7/2/26, 9:41 AM'). Used to line same-amount slips up with the right statement line."""
    if not text:
        return None
    m = re.search(r"deposit date\s*:?\s*(\d{1,2}/\d{1,2}/\d{4})", text, re.I)
    if not m:
        m = re.search(r"\b(\d{1,2}/\d{1,2}/\d{4})\b", text)
    if m:
        try:
            return datetime.datetime.strptime(m.group(1), "%m/%d/%Y")
        except ValueError:
            pass
    return None

def deposit_number(text):
    """The 'Deposit Number NNN' printed on a deposit slip, as an int (or None). Two slips that
    share a deposit number are the same physical deposit (used to skip duplicate scans)."""
    if not text:
        return None
    m = re.search(r"deposit\s*(?:number|no\.?|#)\s*[:#]?\s*(\d+)", text, re.I)
    return int(m.group(1)) if m else None

def vendor_keys(text):
    toks = re.findall(r"[a-z]+", (text or "").lower())
    return {t for t in toks if len(t) >= 4 and t not in VENDOR_STOP}

# ---------------------------------------------------------------- rec report
def parse_rec(path):
    txt = text_layer(path)
    if not txt:
        return {"checks": [], "deposits": [], "difference": None, "error": "no text"}
    section, checks, deposits, difference = None, [], [], None
    for raw in txt.splitlines():
        ln = raw.strip()
        low = ln.lower()
        if low.startswith("difference"):
            m = re.search(AMT, ln)
            if m:
                difference = m.group(0)
        if low.startswith("cleared checks"):
            section = "checks"; continue
        if low.startswith("cleared deposits"):
            section = "deposits"; continue
        if low.startswith("cleared other") or low.startswith("total cleared") \
           or low.startswith("outstanding") or low.startswith("plus:") \
           or low.startswith("less:") or low.startswith("reconciled"):
            if low.startswith("cleared other"):
                section = "other"
            elif section in ("checks", "deposits") and low.startswith("total cleared"):
                section = None
            continue
        if section in ("checks", "deposits"):
            m = re.match(r"(\d{2}/\d{2}/\d{4})\s+(\S+)\s+(.*?)\s*(" + AMT + r")(\s+\d{2}/\d{2}/\d{4})?\s*$", ln)
            if m:
                rec = {
                    "date": m.group(1),
                    "tran": m.group(2),
                    "notes": m.group(3).strip(),
                    "amount": round(float(m.group(4).replace(",", "")), 2),
                    "type": "deposit" if section == "deposits" else "check",
                }
                (deposits if section == "deposits" else checks).append(rec)
    return {"checks": checks, "deposits": deposits, "difference": difference, "error": None}

def to_date(s):
    try:
        return datetime.datetime.strptime(s, "%m/%d/%Y")
    except Exception:
        return datetime.datetime.max

def _parse_mdy(s):
    """Parse a MM/DD/YYYY (or M/D/YY) date string to a datetime, or None."""
    s = (s or "").strip()
    for fmt in ("%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None

def period_last_day(period):
    """Last calendar day of a reconciliation-period label ('June 2026', '06/2026',
    '2026-06') as a datetime, or None if it can't be read (then no period filtering)."""
    s = (period or "").strip()
    if not s:
        return None
    dt = None
    for fmt in ("%B %Y", "%b %Y", "%m/%Y", "%Y-%m", "%m-%Y", "%B, %Y"):
        try:
            dt = datetime.datetime.strptime(s, fmt); break
        except ValueError:
            continue
    if dt is None:
        return None
    nxt = datetime.datetime(dt.year + (dt.month == 12), (dt.month % 12) + 1, 1)
    return nxt - datetime.timedelta(days=1)

# ---------------------------------------------------------------- bank statement
class StmtLine:
    __slots__ = ("seq", "amount", "sign", "section", "date", "desc", "check_no", "is_settlement", "pos")
    def __init__(self, seq, amount, sign, section, date, desc, check_no, is_settlement, pos=None):
        self.seq, self.amount, self.sign, self.section = seq, amount, sign, section
        self.date, self.desc, self.check_no, self.is_settlement = date, desc, check_no, is_settlement
        # `pos` = ordering key (character offset in the statement text). Lets real
        # parsed lines and rec-fallback pseudo-lines sort into one sequence.
        self.pos = pos if pos is not None else seq

def stmt_pos(raw, amount):
    """Character position where an amount first appears in the raw statement text
    (bank-format-independent). Large number if absent, so it sorts last."""
    if not raw:
        return 10 ** 9
    amt = abs(round(amount, 2))
    best = 10 ** 9
    for c in ("{:,.2f}".format(amt), "{:.2f}".format(amt)):
        j = raw.find(c)
        if j >= 0:
            best = min(best, j)
    return best

def synth_lines_from_rec(txns, raw=""):
    """Fallback statement model when the statement can't be parsed into line items
    (unfamiliar/scanned layout) or is missing. Order the way a real statement reads:
    deposits, then ACH/electronic (tran 9999) debits, then NUMBERED checks (whose
    "Checks" section comes last). Within a group, by where the amount appears in the
    statement text, with date as the tie-break; numbered checks go by date. Files
    then match these exactly as they would real lines."""
    def tier(t):
        if t["type"] == "deposit":
            return 0
        return 1 if t["tran"] == "9999" else 2     # ACH debits before numbered checks
    def within(t):
        return to_date(t["date"]).toordinal() if tier(t) == 2 else min(stmt_pos(raw, t["amount"]), 10 ** 7 - 1)
    ordered = sorted(txns, key=lambda t: (tier(t), within(t), to_date(t["date"])))
    out = []
    for seq, t in enumerate(ordered, 1):
        out.append(StmtLine(seq, t["amount"],
                            "credit" if t["type"] == "deposit" else "debit",
                            "rec", t["date"], t["notes"],
                            re.sub(r"\D", "", t["tran"]) or None, False,
                            pos=tier(t) * 10 ** 7 + within(t)))
    return out

def parse_statement(paths):
    """Walk the statement body section by section into ordered transaction lines.
    Excludes account-summary and daily-balance blocks so totals/balances are
    never mistaken for a transaction."""
    txt = ""
    for p in paths:
        txt += text_layer(p) + "\n"
    section = sign = None
    seq = 0
    out = []
    lines = txt.split("\n")
    offsets, _o = [], 0
    for L in lines:
        offsets.append(_o); _o += len(L) + 1
    for li, raw in enumerate(lines):
        char_pos = offsets[li]
        ln = raw.strip()
        low = ln.lower()
        if not ln:
            continue
        # --- section headers ---
        if low.startswith("other credits"):
            section, sign = "other_credit", "credit"; continue
        if low.startswith("other debits"):
            section, sign = "other_debit", "debit"; continue
        if low.startswith("deposits"):
            section, sign = "deposit", "credit"; continue
        if low.startswith("electronic"):
            # "Electronic Credits" are deposits (credit side); "Electronic Debits" are
            # payments. Same leading keyword, opposite sign - so read the credit/debit word
            # instead of assuming debit (otherwise deposits here can't match their slips and
            # fall back to date ordering instead of statement order).
            section, sign = ("electronic", "credit") if "credit" in low else ("electronic", "debit")
            continue
        if low.startswith("checks cleared") or low == "checks" or low.startswith("checks "):
            section, sign = "check", "debit"; continue
        # --- blocks that end the transaction listing ---
        if low.startswith("daily balance") or low.startswith("account summary") \
           or low.startswith("summary of accounts"):
            section = sign = None; continue
        # --- header / boilerplate rows to skip ---
        if low.startswith("date description") or low.startswith("check nbr") \
           or low.startswith("date amount") or low.startswith("* indicates") \
           or "beginning balance" in low or "ending balance" in low \
           or "this period" in low:
            continue
        if section is None:
            continue
        m = re.search(r"\$(" + AMT + r")", ln)
        if not m:
            continue
        amount = round(abs(float(m.group(1).replace(",", ""))), 2)
        dm = re.search(r"(\d{2}/\d{2}/\d{4})", ln)
        date = dm.group(1) if dm else None
        check_no = None
        if section == "check":
            cm = re.match(r"(\d+)\*?\s", ln)
            if cm:
                check_no = cm.group(1)
        seq += 1
        out.append(StmtLine(seq, amount, sign, section, date, ln,
                            check_no, any(k in low for k in SETTLEMENT_KEYS),
                            pos=char_pos))
    return out

# ---------------------------------------------------------------- support docs
class Doc:
    __slots__ = ("path", "is_slip", "fname_ints", "fname_moneys", "content_amounts",
                 "slip_total", "slip_date", "deposit_no", "doc_date", "vendor_keys",
                 "ocr_used", "verified", "sidecar_total", "check_numbers")

def file_number_tokens(path):
    name = os.path.splitext(os.path.basename(path))[0]
    name = re.sub(r"\(.*?\)", " ", name)            # drop "(1)" duplicate markers
    ints, moneys = set(), set()
    for tok in re.findall(r"\d+(?:\.\d+)?", name):
        if "." in tok:
            moneys.add(round(float(tok), 2))
        else:
            ints.add(int(tok))
    return ints, moneys

def looks_like_slip(path, text):
    nn = norm(os.path.basename(path))
    folder = norm(os.path.basename(os.path.dirname(path)))
    if any(k in nn for k in ("slip", "deposit", "despoit")):
        return True
    if any(k in folder for k in ("slip", "deposit")):
        return True
    return slip_total(text) is not None

SIDECAR_NAME = "_amounts.csv"

def load_amount_sidecar(folder):
    """Read _amounts.csv (written by the invoice processor) into
    {stored_filename_lower: {"amounts": {float,...}, "total": float|None, "checks": {int,...},
     "date": datetime|None}}.
    Several rows for one file (a multi-bill PDF) merge into one amount set; `total` is their
    sum. `checks` holds any check numbers the user typed in (the strongest match signal for a
    cleared check). A file with neither a usable amount nor a check is omitted. {} if absent."""
    path = os.path.join(folder, SIDECAR_NAME)
    if not os.path.exists(path):
        return {}
    out = {}
    try:
        with open(path, newline="", encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                key = (row.get("stored_file") or "").strip().lower()
                if not key:
                    continue
                slot = out.setdefault(key, {"amounts": set(), "_list": [], "checks": set(), "date": None})
                raw = (row.get("amount") or "").strip().replace(",", "").replace("$", "")
                try:
                    amt = round(abs(float(raw)), 2)
                    slot["amounts"].add(amt); slot["_list"].append(amt)
                except ValueError:
                    pass
                chk = re.sub(r"\D", "", row.get("check_number") or "")
                if chk:
                    slot["checks"].add(int(chk))
                idate = _parse_mdy(row.get("invoice_date"))
                if idate and (slot["date"] is None or idate > slot["date"]):
                    slot["date"] = idate
    except (OSError, csv.Error):
        return {}
    for slot in out.values():
        slot["total"] = round(sum(slot.pop("_list")), 2) if slot["amounts"] else None
    return {k: v for k, v in out.items() if v["amounts"] or v["checks"]}


def _doc_period_date(path, sc):
    """The invoice's own date, for judging whether it belongs to the rec period. Prefers the
    sidecar invoice_date (verified); else the _MM_YYYY stamped in the stored filename; else
    None (unknown -> never held back)."""
    if sc and sc.get("date"):
        return sc["date"]
    m = re.search(r"_(\d{1,2})_(\d{4})(?:\D|$)", os.path.basename(path))
    if m:
        mo, yr = int(m.group(1)), int(m.group(2))
        if 1 <= mo <= 12:
            try:
                return datetime.datetime(yr, mo, 1)
            except ValueError:
                pass
    return None


def profile_support(paths, ocr_mode, sidecar=None):
    sidecar = sidecar or {}
    docs = []
    for p in paths:
        sc = sidecar.get(os.path.basename(p).lower())
        d = Doc()
        d.path = p
        d.check_numbers = set(sc["checks"]) if sc else set()   # user-entered check #s, if any
        if sc and sc["amounts"]:
            text = text_layer(p)                          # verified amount -> text only, skip OCR
            d.content_amounts = set(sc["amounts"])        # verified, authoritative amount(s)
            d.fname_ints, d.fname_moneys = set(), set()   # suppress MM/YYYY filename-token noise
            d.slip_total = slip_total(text)
            d.ocr_used, d.verified, d.sidecar_total = False, True, sc["total"]
        else:
            text, ocr_used = content_text(p, ocr_mode)    # no verified amount -> read it (OCR ok)
            d.content_amounts = amounts_in(text)
            d.fname_ints, d.fname_moneys = file_number_tokens(p)
            d.slip_total = slip_total(text)
            d.ocr_used, d.verified, d.sidecar_total = ocr_used, False, None
        d.is_slip = looks_like_slip(p, text)
        d.slip_date = slip_date(text) if d.is_slip else None
        d.deposit_no = deposit_number(text) if d.is_slip else None
        d.doc_date = _doc_period_date(p, sc)
        d.vendor_keys = vendor_keys(text) | vendor_keys(os.path.basename(p))
        docs.append(d)
    return docs

# ---------------------------------------------------------------- subset-sum
def _subset_sum(values, target, maxn=8):
    """Indices of one minimal-size subset of `values` (floats) summing to target
    (cents-exact), or None. Dynamic programming over reachable cent-sums (each value used
    at most once), so a busy statement can't hang the assembler the way trying every
    combination could (C(40,8) is tens of millions). Bails out with None if the sum table
    explodes - a real statement never gets near the cap."""
    cents = [round(v * 100) for v in values]
    tc = round(target * 100)
    if tc <= 0:
        return None
    LIMIT = 50000                       # reachable-sum cap (memory + time bound)
    best = {0: ()}                      # cent-sum -> smallest index-tuple reaching it
    for i, c in enumerate(cents):
        if c <= 0 or c > tc:
            continue
        for s, idx in list(best.items()):   # snapshot: item i joins each chain at most once
            if len(idx) >= maxn:
                continue
            ns = s + c
            if ns > tc:
                continue
            cur = best.get(ns)
            if cur is None or len(idx) + 1 < len(cur):
                best[ns] = idx + (i,)
        if len(best) > LIMIT:
            return None
    hit = best.get(tc)
    return list(hit) if hit else None

# ---------------------------------------------------------------- placement
def _doc_amt(d):
    """A support doc's single representative amount - a slip's deposit total, an invoice's
    verified (possibly multi-bill) total, or its lone content amount - or None if ambiguous."""
    if d.slip_total is not None:
        return round(d.slip_total, 2)
    if d.verified and d.sidecar_total is not None:
        return round(d.sidecar_total, 2)
    if len(d.content_amounts) == 1:
        return round(next(iter(d.content_amounts)), 2)
    return None

def _doc_total(d):
    """A doc's total amount for settlement-member matching: its verified/single amount, or - for
    a raw multi-amount invoice the processor never cleaned up - the largest figure on it (an
    invoice's total is its largest number; subtotal + tax add up to it). None if it has none."""
    a = _doc_amt(d)
    if a is not None:
        return a
    pos = [x for x in d.content_amounts if x > 0]
    return round(max(pos), 2) if pos else None

def score_doc_line(d, sl):
    """How strongly support doc `d` evidences statement line `sl`."""
    amt = sl.amount
    s, why = 0, []
    amount_hit = False
    if d.slip_total is not None and d.slip_total == amt:
        s += 6; why.append("slip-total"); amount_hit = True
    elif amt in d.content_amounts:
        s += 6; why.append("content"); amount_hit = True
    # Deposit-date tie-break: once the amount matches, pull a dated slip toward the
    # statement line whose POSTED date is closest. This separates two same-amount slips
    # deposited on different days so each lands on (and is ordered by) its own line
    # instead of by filename order. Never a match on its own - only ranks amount matches.
    if amount_hit and d.is_slip and d.slip_date is not None and sl.date:
        ld = to_date(sl.date)
        if ld is not datetime.datetime.max:
            gap = abs((d.slip_date - ld).days)
            if gap == 0:
                s += 3; why.append("date")
            elif gap <= 3:
                s += 2; why.append("date~")
            elif gap <= 7:
                s += 1; why.append("date~")
    if amt in d.fname_moneys:
        s += 4; why.append("file$")
    elif int(amt) in d.fname_ints:
        s += 3; why.append("file#")
    if sl.check_no and sl.check_no.isdigit():
        cn = int(sl.check_no)
        if cn in d.check_numbers:        # user-entered check # -> strongest single signal
            s += 8; why.append("check#")
        elif cn in d.fname_ints:         # legacy: the number happened to be in the filename
            s += 4; why.append("check#")
    if vendor_keys(sl.desc) & d.vendor_keys:
        s += 2; why.append("vendor")
    return s, why

def assign_docs(docs, stmt, txns=None, raw="", period_end=None):
    """Map each support doc to the statement line it belongs to.
    Returns doc_line, reason, conf (all keyed by doc path) and covered (set of
    statement seqs that a doc was placed on). `period_end`, when given, holds out any
    invoice dated after it - a future bill can't have cleared this period."""
    # Settlements (one bank line covering several cleared rec items) with their members, from the
    # posted rec report - used by pass 1b to group the supporting files onto them.
    settlements = [(sl, ms) for sl, ms in settlement_rec_members(stmt, txns) if len(ms) >= 2] if txns else []

    # A bill dated AFTER the reconciliation period is held out of this month's report - it can't
    # have cleared in it. Amount is NOT enough to override the date: a recurring vendor bills the
    # same amount every month, so a July invoice can read identical to June's cleared charge (and
    # its clean verified amount would even out-match the real June invoice). The correct in-month
    # invoice, once run through the processor so it has a verified amount, matches on its own.
    future = [d for d in docs
              if period_end and not d.is_slip and d.doc_date and d.doc_date > period_end]
    if future:
        _fp = {d.path for d in future}
        docs = [d for d in docs if d.path not in _fp]
    # split settlements by sign: a slip can only join a CREDIT (deposit)
    # settlement, an invoice only a DEBIT one - so a slip's filename number can't
    # be mis-grouped onto a same-numbered debit line.
    settle_credit, settle_debit = {}, {}
    for sl in stmt:
        if sl.is_settlement:
            (settle_credit if sl.sign == "credit" else settle_debit).setdefault(int(sl.amount), sl)
    individual = [sl for sl in stmt if not sl.is_settlement]

    doc_line, reason, conf = {}, {}, {}
    used_line = set()

    # pass 1 - settlement-named files (your convention): a file named with a
    # settlement's dollar amount belongs to that settlement (grouped there).
    for d in docs:
        settle = settle_credit if d.is_slip else settle_debit
        keys = [k for k in d.fname_ints if k in settle]
        if keys:
            sl = settle[keys[0]]
            doc_line[d.path] = sl; reason[d.path] = ["batch-name"]; conf[d.path] = "grouped"

    # pass 1b - batched SETTLEMENT lines by member amount: a settlement is one bank line covering
    # several cleared items (per the posted rec). A slip/invoice whose amount equals one of those
    # member amounts belongs on that settlement line, sitting with its siblings at the settlement's
    # position - e.g. two deposits 8,879.96 + 39,757.99 posting as one 48,637.95 settlement, or
    # several vendor bills netted into one ACH settlement. EXACT amount only: a member whose
    # matching invoice isn't in the folder stays unfilled rather than pulling in a wrong same-
    # vendor bill. (Single-member settlements are left to pass 2 - full amount + date.)
    for sl, members in settlements:
        if sl.seq in used_line:
            continue
        want_slip = (sl.sign == "credit")
        member_amts = [round(t["amount"], 2) for t in members]
        landed = False
        for d in docs:
            if d.path in doc_line or d.is_slip != want_slip:
                continue
            a = _doc_total(d)          # verified amount, or a raw invoice's total (its largest figure)
            if a is not None and a in member_amts:
                doc_line[d.path] = sl; reason[d.path] = ["settle-member"]; conf[d.path] = "grouped"
                member_amts.remove(a); landed = True
        if landed:
            used_line.add(sl.seq)

    # pass 2 - individual lines by evidence, greedy by score, 1 doc : 1 line.
    # Also match any "Settlement"-labelled line pass 1 did NOT group files onto: a settlement
    # whose FULL amount equals a single deposit/invoice is really that one item (e.g. a KORUS
    # rent "Settlement" line that is a single slip), not a multi-file batch - so it should place
    # and order like any other line instead of being held out for filename grouping only.
    grouped_settle = {sl.seq for sl in doc_line.values() if sl.is_settlement}
    pass2_lines = individual + [sl for sl in stmt if sl.is_settlement and sl.seq not in grouped_settle]
    pairs = []
    for d in docs:
        if d.path in doc_line:
            continue
        for sl in pass2_lines:
            if (sl.sign == "credit") != d.is_slip:   # slips->credit, invoices->debit
                continue
            sc, why = score_doc_line(d, sl)
            if sc > 0 and (set(why) - {"vendor", "date", "date~"}):   # need amount/number evidence, not vendor/date alone
                pairs.append((sc, d, sl, why))
    pairs.sort(key=lambda x: -x[0])
    for sc, d, sl, why in pairs:
        if d.path in doc_line or sl.seq in used_line:
            continue
        doc_line[d.path] = sl; used_line.add(sl.seq); reason[d.path] = why
        if {"slip-total", "content", "check#"} & set(why):
            conf[d.path] = "high"
        elif {"file$", "file#", "vendor"} & set(why):
            conf[d.path] = "med"
        else:
            conf[d.path] = "low"

    # pass 3 - aggregate files: a multi-deposit slip / multi-invoice total that
    # equals the SUM of several still-unused statement lines (subset-sum).
    for d in docs:
        if d.path in doc_line:
            continue
        total = d.slip_total
        if total is None and len(d.fname_moneys) == 1:
            total = next(iter(d.fname_moneys))
        if total is None and d.verified and d.sidecar_total:
            total = d.sidecar_total          # multi-bill invoice file: sum of its bills
        if total is None:
            continue
        want_credit = d.is_slip
        pool = [sl for sl in individual
                if (sl.sign == "credit") == want_credit and sl.seq not in used_line]
        idx = _subset_sum([sl.amount for sl in pool], total)
        if idx and len(idx) >= 2:
            chosen = [pool[i] for i in idx]
            first = min(chosen, key=lambda x: x.seq)
            doc_line[d.path] = first; reason[d.path] = ["agg-sum(%d lines)" % len(chosen)]
            conf[d.path] = "high"
            for sl in chosen:
                used_line.add(sl.seq)

    # pass 3b - reverse aggregate: several unplaced files whose amounts SUM to one still-
    # unmatched statement line the bank combined into a single posting. CREDIT side: deposit
    # slips clearing as one deposit (e.g. 12,427.60 + 24,447.39 = one 36,874.99 DEPOSIT).
    # DEBIT side: same-vendor invoices paid as one lump (e.g. several LADWP account bills as a
    # single autopay). Group them onto that line so they sit together at its statement position.
    for sl in sorted(stmt, key=lambda s: s.pos):
        if sl.seq in used_line or sl.is_settlement:
            continue
        want_slip = (sl.sign == "credit")         # credit line <- slips, debit line <- invoices
        line_vendor = vendor_keys(sl.desc)
        pool = []
        for d in docs:
            if d.path in doc_line or d.is_slip != want_slip:
                continue
            # Debit side: require a vendor shared with the line, so unrelated invoices that
            # merely happen to sum to a debit total aren't grouped by coincidence. Slips carry
            # no vendor identity, so the credit side groups on the exact sum alone.
            if not want_slip and not (line_vendor & d.vendor_keys):
                continue
            a = _doc_amt(d)
            if a is not None:
                pool.append((a, d))
        if len(pool) < 2:
            continue
        idx = _subset_sum([a for a, _d in pool], sl.amount, maxn=min(len(pool), 12))
        if idx and len(idx) >= 2:
            chosen = [pool[i][1] for i in idx]
            kind = "slips" if want_slip else "invoices"
            for d in chosen:
                doc_line[d.path] = sl
                reason[d.path] = ["agg-group(%d %s)" % (len(chosen), kind)]
                conf[d.path] = "high"
            used_line.add(sl.seq)

    # pass 4 - rec fallback: a doc that matched no statement line (the bank netted
    # or omitted that amount) is attached to a cleared rec item it evidences, and
    # ordered by where that amount sits in the statement text. Catches the items a
    # structured parse misses without leaving anything unplaced.
    if txns:
        # sign-aware coverage: a deposit is only "covered" by a CREDIT line, a
        # check only by a DEBIT line. So a deposit the bank listed in a debit
        # ("electronic") section still gets a credit fallback line for its slip.
        cred_amts = {round(sl.amount, 2) for sl in stmt if sl.sign == "credit"}
        debit_amts = {round(sl.amount, 2) for sl in stmt if sl.sign == "debit"}
        faux = []
        for t in txns:
            amts = cred_amts if t["type"] == "deposit" else debit_amts
            if round(t["amount"], 2) in amts:
                continue
            # order fallback items by DATE, placed after every cleanly-listed line
            # (real-line positions are small char offsets; this base sits beyond
            # them). Matches how hand collation lists the bank-buried items.
            faux_pos = 10 ** 8 + to_date(t["date"]).toordinal()
            faux.append(StmtLine(10 ** 6 + len(faux), t["amount"],
                                 "credit" if t["type"] == "deposit" else "debit",
                                 "rec", t["date"], t["notes"],
                                 re.sub(r"\D", "", t["tran"]) or None, False,
                                 pos=faux_pos))
        pairs = []
        for d in docs:
            if d.path in doc_line:
                continue
            for sl in faux:
                if (sl.sign == "credit") != d.is_slip:
                    continue
                sc, why = score_doc_line(d, sl)
                if sc > 0 and (set(why) - {"vendor", "date", "date~"}):   # amount/number evidence, not vendor/date alone
                    pairs.append((sc, d, sl, why))
        pairs.sort(key=lambda x: -x[0])
        used_faux = set()
        for sc, d, sl, why in pairs:
            if d.path in doc_line or id(sl) in used_faux:
                continue
            doc_line[d.path] = sl; used_faux.add(id(sl))
            reason[d.path] = why + ["rec-fallback"]
            conf[d.path] = "high" if ({"slip-total", "content", "check#"} & set(why)) else "med"

    # pass 5 - vendor grouping: an invoice still unplaced is grouped onto a
    # cleared CHECK that shares its vendor name (several invoices paid by one
    # check, e.g. legal bills whose amounts don't individually match). They sit
    # together at that check's position.
    if txns:
        # Cleared amounts already explained by a placed file - so vendor-grouping can't
        # re-attach a different (often outstanding) invoice to a check already matched. This
        # is what kept an outstanding SoCalGas bill off the rec while a same-vendor cleared
        # payment was already covered by its own invoice.
        placed_amts = {round(doc_line[d.path].amount, 2) for d in docs if d.path in doc_line}
        debit_pos = {}
        for sl in stmt:
            if sl.sign == "debit":
                debit_pos.setdefault(round(sl.amount, 2), sl.pos)
        for d in docs:
            if d.path in doc_line or d.is_slip:
                continue
            match, grp_reason, grp_conf = None, None, None
            if d.check_numbers:          # share a cleared check by its (user-entered) number
                match = next((t for t in txns if t["type"] == "check"
                              and re.sub(r"\D", "", t["tran"]).isdigit()
                              and int(re.sub(r"\D", "", t["tran"])) in d.check_numbers), None)
                if match:
                    grp_reason, grp_conf = "check#-group", "high"
            if match is None and d.vendor_keys:      # fall back to sharing a vendor name, but
                match = next((t for t in txns if t["type"] == "check"   # not onto an already-matched check
                              and (d.vendor_keys & vendor_keys(t["notes"]))
                              and round(t["amount"], 2) not in placed_amts), None)
                if match:
                    grp_reason, grp_conf = "vendor-group", "med"
            if match is None:
                continue
            pos = debit_pos.get(round(match["amount"], 2),
                                10 ** 8 + to_date(match["date"]).toordinal())
            doc_line[d.path] = StmtLine(10 ** 6 + 500 + len(doc_line), match["amount"],
                                        "debit", "rec", match["date"], match["notes"],
                                        None, False, pos=pos)
            reason[d.path] = [grp_reason]; conf[d.path] = grp_conf

    for d in docs:
        if d.path not in doc_line:
            conf[d.path] = "low"; reason[d.path] = reason.get(d.path, ["unmatched"])
    for d in future:                     # dated beyond the period -> flagged, carried forward
        reason[d.path] = ["next-period"]; conf[d.path] = "future"

    covered = {sl.seq for p, sl in doc_line.items()}
    return doc_line, reason, conf, covered

def settlement_rec_members(stmt, txns):
    """For the manifest: which cleared rec items subset-sum to each settlement
    line (the trust check that a batch is complete)."""
    out = []
    used = set()
    for sl in sorted([s for s in stmt if s.is_settlement], key=lambda s: s.seq):
        want = "deposit" if sl.sign == "credit" else "check"
        pool = [t for t in txns if t["type"] == want and id(t) not in used]
        idx = _subset_sum([t["amount"] for t in pool], sl.amount)
        members = [pool[i] for i in idx] if idx else []
        for t in members:
            used.add(id(t))
        out.append((sl, members))
    return out

# ---------------------------------------------------------------- ordering
# Order the financial appendix the way the reports are collated by hand, not
# alphabetically: rent roll (first, when present), bank register, cash flow,
# 12-month cash flow, transaction registers, balance sheet, income statement,
# trial balance, GL, AR analytics. Unknown reports keep their name order at the end.
def fin_rank(path):
    n = os.path.basename(path).lower().replace(" ", "")
    if "rentroll" in n: return 0           # rent roll leads the financials when present
    if "bankregister" in n: return 1
    if "12month" in n: return 3            # 12-month cash flow (after the regular one)
    if "cashflow" in n: return 2
    if "transactionregister" in n: return 4
    if "balancesheet" in n: return 5
    if "incomestatement" in n: return 6
    if "trialbalance" in n: return 7
    if "generalledger" in n: return 8
    if "aranalytics" in n or "receivable" in n: return 9
    return 99

def _natural_name(path):
    """Sort key that puts the base file before its duplicates and orders the
    duplicates numerically: 21928.pdf, 21928 (1).pdf, 21928 (2).pdf, ...
    (matches how the files sort in Windows Explorer / were collated by hand)."""
    b = os.path.splitext(os.path.basename(path))[0]
    m = re.search(r"\((\d+)\)", b)
    suffix = int(m.group(1)) if m else 0
    base = re.sub(r"\s*\(\d+\)\s*", " ", b).strip().lower()
    return (base, suffix)

def _first_amount(path):
    """First dollar figure near the top of the file (the rent-income line on a
    cash flow), used to collate cash-flow variants higher-figure-first."""
    m = re.search(AMT, text_layer(path)[:400])
    return float(m.group(0).replace(",", "")) if m else 0.0

def fin_sort_key(path):
    """Collation order for the financial appendix. Within the cash-flow families
    (which often ship as two variants), the higher-figure version comes first,
    matching how the reports are collated by hand; everything else is base-before-
    duplicates within its report type."""
    r = fin_rank(path)
    if r in (1, 2):
        return (r, -_first_amount(path), _natural_name(path))
    return (r, 0.0, _natural_name(path))

# Report-type aliases: substrings (letters/digits only, lowercased) that identify a report in
# either a file name or a cover-page contents line - so the financial appendix can be ordered
# to match the cover page's table of contents when one is present. "AR Analytics" and
# "Receivable Detail" are the same report named two ways, so both map to `receivable`.
_REPORT_TYPES = [
    ("12monthcashflow",     ("12month",)),
    ("rentroll",            ("rentroll",)),
    ("receivable",          ("receivable", "receiveable", "aranalytic", "aranalysis", "ardetail")),
    ("trialbalance",        ("trialbalance",)),
    ("incomestatement",     ("incomestatement",)),
    ("cashflow",            ("cashflow",)),
    ("generalledger",       ("generalledger",)),
    ("bankregister",        ("bankregister",)),
    ("transactionregister", ("transactionregister",)),
    ("balancesheet",        ("balancesheet",)),
]

def _report_type(text):
    """The financial report type named in a string (a file name or a cover-page line), or None."""
    tc = re.sub(r"[^a-z0-9]", "", (text or "").lower())
    for name, aliases in _REPORT_TYPES:
        if any(a in tc for a in aliases):
            return name
    return None

def _file_report_type(path):
    rt = _report_type(os.path.basename(path))
    if rt is None:
        rt = _report_type(text_layer(path)[:600])   # fall back to the report's own first page
    return rt

def cover_report_order(cover_paths):
    """Financial report types in the order a cover-page table of contents lists them, e.g.
    ['rentroll','receivable','trialbalance',...]. Empty if there's no cover page or it names no
    recognizable reports (then the built-in fin_rank order is used)."""
    order = []
    for p in cover_paths:
        for ln in text_layer(p).splitlines():
            rt = _report_type(ln)
            if rt and rt not in order:
                order.append(rt)
    return order

def fin_cover_sort_key(path, cover_types):
    """Collation key that orders a financial report by its position on the cover page; a report
    the cover doesn't list falls after the listed ones, in the built-in fin_rank order. Within a
    type the higher-figure variant leads (matches the by-hand collation of two-variant reports)."""
    rt = _file_report_type(path)
    primary = cover_types.index(rt) if rt in cover_types else len(cover_types) + fin_rank(path)
    lead = -_first_amount(path) if rt in ("cashflow", "bankregister", "12monthcashflow") else 0.0
    return (primary, lead, _natural_name(path))

# Some managers collate the financial appendix in a fixed order that isn't the default and has
# no cover-page table of contents - notably the KORUS/Yardi template used for 6281-6301 Beach
# Blvd and 10630 Santa Monica: bank register, both Cash Flow bases, both 12-month Cash Flow
# bases, the check/deposit/receipt registers, the balance sheet, then the receivable summary.
# Reports are identified by their content footer title (fin_report_tag), so this holds whatever
# the saved files happen to be named.
_KORUS_FIN_ORDER = ["bankregister", "cashflow_cash", "cashflow_accrual",
                    "cashflow12_cash", "cashflow12_accrual",
                    "checkregister", "depositregister", "receiptregister",
                    "balancesheet", "receivablesummary"]
# Solair's deposits-only appendix: just the Check Transaction and Deposit Transaction registers
# and the Receivable Summary (no cash flow / balance sheet).
_SOLAIR_FIN_ORDER = ["checkregister", "depositregister", "receivablesummary"]

# Per-property overrides, keyed on a distinctive part of the property name (lowercased). A
# property not listed here uses the defaults (cover-page or built-in financial order, invoices
# matched normally).
#   fin_order     - a fixed financial-appendix order (tags per fin_report_tag), for managers
#                   whose packet has no cover-page table of contents.
#   deposits_only - the packet intentionally carries no expense invoices (expenses are detailed
#                   in the Check register instead), so cleared checks with no file are EXPECTED,
#                   not flagged for review.
PROPERTY_CONFIG = {
    "beach blvd":   {"fin_order": _KORUS_FIN_ORDER},
    "santa monica": {"fin_order": _KORUS_FIN_ORDER},
    "solair":       {"fin_order": _SOLAIR_FIN_ORDER, "deposits_only": True},
    "sherman":      {"deposits_only": True},   # JH Lee (Sherman)
    "irolo":        {"deposits_only": True},   # Irolo & Vermont
    "bronson":      {"deposits_only": True},   # Bronson & Victoria
}

def _property_config(name):
    n = re.sub(r"[^a-z0-9]+", " ", (name or "").lower())
    cfg = {}
    for key, c in PROPERTY_CONFIG.items():
        if key in n:
            cfg.update(c)
    return cfg

def property_fin_order(name):
    """The fixed financial-appendix order for a property collated by a template, or None to fall
    back to the cover-page / built-in order."""
    return _property_config(name).get("fin_order")

def property_deposits_only(name):
    """True if the property's packet intentionally has no expense invoices (cleared checks with
    no supporting file are then expected, not flagged for review)."""
    return bool(_property_config(name).get("deposits_only"))

def fin_report_tag(path):
    """Fine-grained financial-report tag from a report's own content. These Yardi/KORUS reports
    print '<Title> Page N' plus 'Book = Cash/Accrual' in every page footer, so the report type
    (and cash vs accrual basis) is read from the content, not the file name. None if the file
    isn't one of these reports."""
    low = re.sub(r"\s+", " ", text_layer(path).lower())
    if not low.strip():
        return None
    basis = "accrual" if "book = accrual" in low else "cash"
    if "receivable summary page" in low:
        return "receivablesummary"
    if "12 months cash flow statement page" in low:
        return "cashflow12_" + basis
    if "cash flow statement page" in low:
        return "cashflow_" + basis
    if "bank register page" in low:
        return "bankregister"
    if "check register page" in low:
        return "checkregister"
    if "deposit register page" in low:
        return "depositregister"
    if "receipt register page" in low:
        return "receiptregister"
    if "transaction register page" in low:
        return "transactionregister"
    if "balance sheet page" in low:
        return "balancesheet"
    return None

def fin_property_sort_key(path, order):
    """Collation key ordering a financial report by its position in a property's fixed template;
    a report not in the template falls after the listed ones, in the built-in fin_rank order."""
    tag = fin_report_tag(path)
    if tag in order:
        return (order.index(tag), _natural_name(path))
    return (len(order) + fin_rank(path), _natural_name(path))

def order_docs(docs, doc_line, mode):
    placed = [d for d in docs if d.path in doc_line]
    unplaced = [d for d in docs if d.path not in doc_line]
    def key(d):
        sl = doc_line[d.path]
        return (sl.pos, sl.seq, _natural_name(d.path))
    if mode == "interleaved":
        seq = sorted(placed, key=lambda d: (doc_line[d.path].pos,
                                            0 if d.is_slip else 1,
                                            doc_line[d.path].seq, _natural_name(d.path)))
        return seq, unplaced
    # Section is decided by the document's own kind (slip vs invoice), NOT by the
    # bank statement's section sign - so deposit slips always sit in the slips
    # group even when the bank lists the deposit under "electronic".
    slips = sorted([d for d in placed if d.is_slip], key=key)
    invs = sorted([d for d in placed if not d.is_slip], key=key)
    return slips + invs, unplaced

# ---------------------------------------------------------------- build
def write_matched_csv(out_path, docs, doc_line, conf, reason, prop_name, period=""):
    """Machine-readable record of what was placed, for the processor's reconcile step. One
    row per support file: stored_file (the join key back to invoices.xlsx), kind, whether it
    matched, the confidence grade, the statement amount/date it landed on, whether the amount
    was sidecar-verified, the reason tokens, the property and the period."""
    csv_path = os.path.splitext(out_path)[0] + " - matched.csv"
    cols = ["stored_file", "kind", "matched", "confidence", "amount",
            "statement_date", "verified", "reason", "property", "period"]
    try:
        with open(csv_path, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(cols)
            for d in sorted(docs, key=lambda x: os.path.basename(x.path).lower()):
                sl = doc_line.get(d.path)
                placed = sl is not None
                w.writerow([
                    os.path.basename(d.path),
                    "slip" if d.is_slip else "invoice",
                    "yes" if placed else "no",
                    conf.get(d.path, "unplaced") if placed else "unplaced",
                    ("%0.2f" % sl.amount) if placed else "",
                    (sl.date or "") if placed else "",
                    "yes" if d.verified else "no",
                    "+".join(reason.get(d.path, [])),
                    prop_name,
                    period,
                ])
    except OSError as e:
        print("  ! could not write matched.csv (%s)" % e)
    return csv_path


def looks_like_statement_text(text):
    low = (text or "").lower()
    return sum(k in low for k in ("beginning balance", "ending balance", "account summary",
                                  "other credits", "checks cleared", "daily balance",
                                  "deposits", "statement ending")) >= 3

def build(folder, out_path=None, out_dir=None, order="grouped", ocr_mode="auto",
          strict=False, verbose=True, period=""):
    pdfs = [p for p in list_pdfs(folder) if "ASSEMBLED" not in p]
    buckets = {"rec": [], "statement": [], "cover": [], "financial": [], "support": []}
    for p in pdfs:
        buckets[classify(p)].append(p)
    # Safety net: if no file's NAME looks like a statement (e.g. it's just
    # "JHR IV.pdf"), promote the support PDF whose CONTENT reads like one.
    if not buckets["statement"]:
        best, best_hits = None, 0
        for p in buckets["support"]:
            low = text_layer(p).lower()
            hits = sum(k in low for k in ("beginning balance", "ending balance",
                       "account summary", "other credits", "checks cleared",
                       "daily balance", "statement ending"))
            if hits > best_hits:
                best, best_hits = p, hits
        if best and best_hits >= 3:
            buckets["support"].remove(best)
            buckets["statement"].append(best)
    # Also promote any support PDF that is really a financial report (recognized by its content
    # footer title, e.g. "Cash Flow Statement Page 1"), so a report saved under a plain name
    # still lands in the financial appendix instead of being mistaken for an invoice.
    for p in list(buckets["support"]):
        if fin_report_tag(p):
            buckets["support"].remove(p); buckets["financial"].append(p)
    log = []
    def say(*a):
        line = " ".join(str(x) for x in a)
        log.append(line)
        if verbose:
            print(line)

    name = os.path.basename(folder.rstrip("/\\"))
    say("=== %s ===" % name)
    say("    OCR: %s" % ("available" if OCR_AVAILABLE
                         else "NOT available (scanned files fall back to filename amount)"))
    if not buckets["rec"]:
        say("  ! No Bank Rec Report found - cannot determine order. Skipping.")
        return None, log, 1
    rec_path = buckets["rec"][0]
    parsed = parse_rec(rec_path)
    if parsed["error"]:
        say("  ! Error parsing rec report:", parsed["error"])
    txns = parsed["checks"] + parsed["deposits"]

    stmt = parse_statement(buckets["statement"])
    raw_stmt = "".join(text_layer(p) for p in buckets["statement"])
    # Is the parse usable? Count how many cleared amounts show up among the parsed
    # line amounts. If few do, this bank's layout wasn't understood - fall back to
    # ordering by raw-text position (works for any statement format).
    line_amts = {round(s.amount, 2) for s in stmt}
    cov = sum(1 for t in txns if round(t["amount"], 2) in line_amts)
    if not stmt or (txns and cov < 0.5 * len(txns)):
        if buckets["statement"] and raw_stmt.strip():
            say("  ! Statement layout not parsed into lines - ordering supports by position in the statement text.")
        else:
            say("  ! No readable bank statement - ordering by rec-report date.")
        stmt = synth_lines_from_rec(txns, raw_stmt)
    sidecar = load_amount_sidecar(folder)
    if sidecar:
        say("    Sidecar: %d invoice file(s) with verified amounts (OCR skipped for these)"
            % len(sidecar))
    docs = profile_support(buckets["support"], ocr_mode, sidecar)
    period_end = period_last_day(period)
    doc_line, reason, conf, covered = assign_docs(docs, stmt, txns, raw_stmt, period_end)
    # Only STRONG evidence (verified amount, check number, or an exact sum) earns a place in the
    # PDF. Pull back any weak "closest guess" invoice - one fit to a line by vendor name or
    # filename alone, with no amount confirmation - so it's left out of the PDF, flagged, and
    # carried forward rather than asserting a clearance the numbers don't support.
    weak_line = {}
    for d in docs:
        if d.path in doc_line and not d.is_slip and conf.get(d.path) not in ("high", "grouped"):
            weak_line[d.path] = doc_line[d.path]
            reason[d.path] = reason.get(d.path, []) + ["weak-left-out"]
            del doc_line[d.path]
    future_docs = [d for d in docs if reason.get(d.path, [None])[0] == "next-period"]
    if future_docs and not property_deposits_only(name):
        say("    Holding %d invoice(s) dated after %s for next month: %s"
            % (len(future_docs), period or "the period",
               ", ".join(sorted(os.path.basename(d.path) for d in future_docs))))
    ordered, unplaced = order_docs(docs, doc_line, order)
    # Deposits-only property (e.g. Solair): its packet carries no expense invoices - the expenses
    # are detailed in the Check register - so keep every invoice OUT of the PDF even if it matched.
    # Only the deposit slips (plus the rec / statement / financial reports) belong.
    deposits_only_dropped = []
    if property_deposits_only(name):
        deposits_only_dropped = [d for d in ordered if not d.is_slip]
        ordered = [d for d in ordered if d.is_slip]

    # assemble final page order
    order_list = []
    order_list += buckets["cover"]
    order_list += [rec_path]
    order_list += buckets["statement"]
    order_list += [d.path for d in ordered]
    unplaced_sorted = sorted(unplaced, key=lambda d: (0 if d.is_slip else 1, norm(os.path.basename(d.path))))
    # Unplaced INVOICES aren't on the statement (e.g. an outstanding bill), so they're left OUT
    # of the assembled PDF - still flagged in the manifest below. Unplaced slips are kept so a
    # missing deposit stays visible - EXCEPT a slip that just duplicates one already in the
    # report (same deposit number, or same amount+date), which would otherwise print the same
    # deposit twice (e.g. two scans of deposit #404).
    def _slip_sig(d):
        if d.deposit_no:
            return ("no", d.deposit_no)
        if d.slip_total is not None:
            return ("amt", round(d.slip_total, 2), d.slip_date.date() if d.slip_date else None)
        return None
    seen_slips = {_slip_sig(d) for d in ordered if d.is_slip}
    seen_slips.discard(None)
    dup_slips, kept_unplaced_slips = [], []
    for d in unplaced_sorted:
        if not d.is_slip:
            continue
        sig = _slip_sig(d)
        if sig is not None and sig in seen_slips:
            dup_slips.append(d)
        else:
            if sig is not None:
                seen_slips.add(sig)
            kept_unplaced_slips.append(d)
    order_list += [d.path for d in kept_unplaced_slips]
    if dup_slips:
        say("    Skipped %d duplicate slip(s) - same deposit already in the report: %s"
            % (len(dup_slips), ", ".join(sorted(os.path.basename(d.path) for d in dup_slips))))
    # Financial appendix order, most specific first: a property's fixed template (KORUS/Yardi,
    # by content footer title), else the cover page's table of contents, else built-in fin_rank.
    prop_order = property_fin_order(name)
    cover_types = cover_report_order(buckets["cover"])
    if prop_order and buckets["financial"]:
        fins = sorted(buckets["financial"], key=lambda p: fin_property_sort_key(p, prop_order))
        fin_src = "property template order"
    elif cover_types and buckets["financial"]:
        fins = sorted(buckets["financial"], key=lambda p: fin_cover_sort_key(p, cover_types))
        fin_src = "cover-page order"
    else:
        fins = sorted(buckets["financial"], key=fin_sort_key)
        fin_src = "default order (no cover page)"
    order_list += fins

    # ---- manifest ----
    def cbucket(c):
        return "high" if c in ("high", "grouped") else c
    counts = {"high": 0, "med": 0, "low": 0}
    for d in docs:
        if d.path in doc_line:
            counts[cbucket(conf[d.path])] += 1
    say("  Reconciliation difference: %s" % parsed["difference"])
    say("  Cleared checks: %d  deposits: %d" % (len(parsed["checks"]), len(parsed["deposits"])))
    say("  Support files: %d  placed: %d  unplaced: %d" %
        (len(docs), len(docs) - len(unplaced), len(unplaced)))
    say("  Verification: %d high  %d medium  %d low" %
        (counts["high"], counts["med"], counts["low"]))

    members = settlement_rec_members(stmt, txns)
    batches = [(sl, m) for sl, m in members if len(m) >= 2]
    if batches:
        say("  --- batched settlements (one bank line = several cleared items) ---")
        for sl, m in batches:
            gsum = round(sum(t["amount"] for t in m), 2)
            ok = "==" if abs(gsum - sl.amount) < 0.005 else "!="
            files_here = [d for d in ordered if doc_line[d.path].seq == sl.seq]
            say("    %s  %s %0.2f  = %d cleared items (sum %0.2f %s)  | %d file(s) grouped here"
                % (sl.date or "", "credit" if sl.sign == "credit" else "debit",
                   sl.amount, len(m), gsum, ok, len(files_here)))
            for t in m:
                say("        member: %-7s %-30s %10.2f" % (t["tran"], t["notes"][:30], t["amount"]))

    def line_tag(d):
        sl = doc_line[d.path]
        s = "%s %-12s %10.2f" % (sl.date or "  -       ", sl.section, sl.amount)
        if sl.is_settlement:
            s += " [BATCH]"
        return s
    def flag(c):
        return {"high": "OK ", "grouped": "GRP", "med": "med", "low": "!! "}.get(c, "?")
    def extracted(d):
        sl = doc_line.get(d.path)
        if sl is None:
            return ""
        if d.slip_total is not None and d.slip_total == sl.amount:
            return "%0.2f" % d.slip_total
        if sl.amount in d.content_amounts:
            return "%0.2f" % sl.amount
        if sl.amount in d.fname_moneys or int(sl.amount) in d.fname_ints:
            return "filename"
        if reason.get(d.path, [""])[0].startswith("agg") or conf.get(d.path) == "grouped":
            return "group"
        if "vendor-group" in reason.get(d.path, []):
            return "vendor"
        return "?"

    say("  --- supporting documents, in bank-statement order ---")
    for d in ordered:
        tag = ("(OCR)" if d.ocr_used else "") + ("(verified)" if d.verified else "")
        say("    %s  ->  %-26s %10s  [%s]%s  {%s}"
            % (line_tag(d), os.path.basename(d.path)[:26], extracted(d),
               flag(conf[d.path]), tag,
               "+".join(reason.get(d.path, []))))

    if property_deposits_only(name):
        excluded = [d for d in docs if not d.is_slip]
        if excluded:
            say("  --- deposits-only property: %d invoice(s) in the folder are NOT in the PDF "
                "(expenses are detailed in the Check register, not individual invoices) ---"
                % len(excluded))
            for d in sorted(excluded, key=lambda x: os.path.basename(x.path).lower()):
                say("    %s" % os.path.basename(d.path))

    # cleared items that have no supporting document of their own. A rec item is
    # "explained" if a settlement/aggregate that has files covers it, or an
    # individually-placed file matches its amount.
    placed_seqs = {doc_line[d.path].seq for d in ordered if d.path in doc_line}
    explained_ids = set()
    # 1. settlement lines that actually have a file grouped on them
    for sl, m in members:
        if sl.seq in placed_seqs:
            for t in m:
                explained_ids.add(id(t))
    # 2. aggregate files (multi-deposit slip / multi-invoice total)
    for d in docs:
        if d.path in doc_line and reason.get(d.path, [""])[0].startswith("agg"):
            total = d.slip_total
            if total is None and len(d.fname_moneys) == 1:
                total = next(iter(d.fname_moneys))
            if total is None:
                continue
            want = "deposit" if d.is_slip else "check"
            pool = [t for t in txns if t["type"] == want and id(t) not in explained_ids]
            idx = _subset_sum([t["amount"] for t in pool], total)
            for i in (idx or []):
                explained_ids.add(id(pool[i]))
    # 2b. reverse-aggregate: files grouped onto one combined statement line explain the rec
    # item(s) of matching kind that sum to that LINE's amount (a combined deposit, or a vendor
    # autopay covering several bills). Line-based, so it also covers the debit side where the
    # grouped invoices have no single per-file total to trace back individually.
    agg_group_seqs = {doc_line[d.path].seq for d in docs
                      if d.path in doc_line and reason.get(d.path, [""])[0].startswith("agg-group")}
    for sl in stmt:
        if sl.seq not in agg_group_seqs:
            continue
        want = "deposit" if sl.sign == "credit" else "check"
        pool = [t for t in txns if t["type"] == want and id(t) not in explained_ids]
        idx = _subset_sum([t["amount"] for t in pool], sl.amount)
        for i in (idx or []):
            explained_ids.add(id(pool[i]))
    # 3. individually-placed files match one rec item by amount + kind
    for d in docs:
        if d.path not in doc_line:
            continue
        sl = doc_line[d.path]
        if sl.is_settlement or reason.get(d.path, [""])[0].startswith("agg"):
            continue
        want = "deposit" if sl.sign == "credit" else "check"
        for t in txns:
            if id(t) not in explained_ids and t["type"] == want \
               and round(t["amount"], 2) == round(sl.amount, 2):
                explained_ids.add(id(t)); break
    # 4. vendor-grouped invoices explain the check they share a vendor with
    for d in docs:
        if d.path in doc_line and "vendor-group" in reason.get(d.path, []):
            for t in txns:
                if t["type"] == "check" and (vendor_keys(t["notes"]) & d.vendor_keys):
                    explained_ids.add(id(t)); break
    # 5. a placed invoice explains the cleared check sharing its (user-entered) number
    for d in docs:
        if d.path in doc_line and getattr(d, "check_numbers", None):
            for t in txns:
                tn = re.sub(r"\D", "", t["tran"])
                if t["type"] == "check" and tn.isdigit() and int(tn) in d.check_numbers:
                    explained_ids.add(id(t))
    no_file = [t for t in txns if id(t) not in explained_ids]
    if no_file:
        if property_deposits_only(name):
            say("  --- cleared checks with no individual invoice (expected for this property - "
                "expenses are detailed in the Check Transaction register) ---")
        else:
            say("  --- cleared items with no direct supporting file (refunds/fees/transfers, or part of a batch) ---")
        for t in no_file:
            say("    %-7s %-7s %-30s %10.2f" % (t["type"], t["tran"], t["notes"][:30], t["amount"]))

    # A deposits-only property's invoices are covered by the note above, not treated as review.
    review_unplaced = [d for d in unplaced_sorted
                       if not (property_deposits_only(name) and not d.is_slip)]
    if review_unplaced:
        say("  --- files NOT on this statement (review) ---")
        dup_paths = {d.path for d in dup_slips}
        for d in review_unplaced:
            if d.path in dup_paths:
                tag = "  [duplicate of a slip already in the report - left OUT]"
            elif d.path in weak_line:
                tag = "  [weak match only (~%.2f, no amount confirmation) - left OUT, carried forward]" % weak_line[d.path].amount
            elif reason.get(d.path, [""])[0] == "next-period":
                tag = "  [dated after %s - left OUT, carried to next month]" % (period or "the period")
            elif d.is_slip:
                tag = "  [kept in PDF - slip]"
            else:
                tag = "  [left OUT of PDF - invoice]"
            say("    %s%s%s" % (os.path.basename(d.path), tag, "  (OCR tried)" if d.ocr_used else ""))

    review = [d for d in docs if d.path in doc_line and cbucket(conf[d.path]) in ("med", "low")]
    if review or review_unplaced:
        say("  REVIEW: %d filename/date-only match(es) + %d unplaced - check the flagged lines."
            % (len(review), len(review_unplaced)))

    if buckets["financial"]:
        say("  --- financial reports, in %s ---" % fin_src)
        for p in fins:
            say("    %s" % os.path.basename(p))

    if out_path is None:
        target_dir = out_dir if out_dir else folder
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(target_dir, "%s - ASSEMBLED.pdf" % name)
    writer = PdfWriter()
    for p in order_list:
        try:
            for pg in _reader(p).pages:
                writer.add_page(pg)
        except Exception as e:
            say("  ! could not add", os.path.basename(p), e)
    wrote_pdf = False
    try:
        with open(out_path, "wb") as fh:
            writer.write(fh)
        wrote_pdf = True
        say("  -> wrote %s (%d pages)" % (out_path, len(writer.pages)))
    except OSError as e:
        say("  ! could NOT write %s (%s). Is it open in a viewer? Manifest still written." %
            (os.path.basename(out_path), e))
    man = os.path.splitext(out_path)[0] + " - manifest.txt"
    with open(man, "w", encoding="utf-8") as fh:
        fh.write("\n".join(log))
    write_matched_csv(out_path, docs, doc_line, conf, reason, name, period)

    if not wrote_pdf:
        # The manifest updating while the PDF didn't is exactly what makes a locked
        # (open-in-a-viewer) PDF look like a silent success. Shout, and exit non-zero so the
        # menu shows a failure instead of a clean finish.
        say("  " + "*" * 68)
        say("  ** PDF NOT UPDATED - '%s - ASSEMBLED.pdf' is locked (open in a viewer?)." % name)
        say("  ** Close it and run this property again. The manifest above is current.")
        say("  " + "*" * 68)
        return out_path, log, 3
    rc = 2 if (strict and (review or unplaced_sorted)) else 0
    return out_path, log, rc

# ---------------------------------------------------------------- cli
def main():
    ap = argparse.ArgumentParser(description="Amount-verified Bank Rec assembler")
    ap.add_argument("folder")
    ap.add_argument("--out")
    ap.add_argument("--order", choices=["interleaved", "grouped"], default="grouped")
    ap.add_argument("--ocr", choices=["auto", "on", "off"], default="auto")
    ap.add_argument("--strict", action="store_true",
                    help="exit non-zero if any match is unverified / unplaced")
    ap.add_argument("--outdir", help="write all outputs (and manifests) into this folder")
    ap.add_argument("--batch", action="store_true")
    ap.add_argument("--month", default="",
                    help='reconciliation period echoed into _matched.csv, e.g. "May 2026"')
    args = ap.parse_args()
    rc = 0
    if args.batch:
        for d in sorted(os.listdir(args.folder)):
            full = os.path.join(args.folder, d)
            if d.startswith("_") or d.startswith("."):
                continue                      # skip helper/output folders
            if os.path.isdir(full):
                res = build(full, out_dir=args.outdir, order=args.order,
                            ocr_mode=args.ocr, strict=args.strict, period=args.month)
                rc = rc or res[2]
                print()
    else:
        res = build(args.folder, out_path=args.out, out_dir=args.outdir,
                    order=args.order, ocr_mode=args.ocr, strict=args.strict, period=args.month)
        rc = res[2]
    sys.exit(rc)

if __name__ == "__main__":
    main()
