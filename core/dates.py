# -*- coding: utf-8 -*-
"""Invoice-date parsing and normalization.

Every timing decision in the app reads `invoices.invoice_date_iso`, which is produced
here. Nothing re-parses the raw `invoice_date` text at read time.

Two rules this module exists to make explicit:

1. Failure is visible. `parse_invoice_date` returns None and `to_iso` returns "", and
   the caller routes the row to the date review queue. A row that cannot be parsed must
   never be silently dropped - a dropped row looks exactly like a vendor who skipped a
   month, which is the signal the whole system exists to detect.

2. Ambiguous NN-NN-YYYY input is MM-DD-YYYY. These are US invoices. Note there is
   deliberately no %d/%m/... format in the list below, so the preference is structural
   rather than a matter of ordering luck.
"""
import datetime
import re
from typing import Optional

# Ordered most-specific first. Formats with separators are tried before the bare-digit
# form so "06262026" is the only thing that can reach %m%d%Y.
_FORMATS = (
    "%m/%d/%Y", "%m-%d-%Y", "%m.%d.%Y",
    "%Y-%m-%d", "%Y/%m/%d",
    "%m/%d/%y", "%m-%d-%y",
    "%B %d, %Y", "%b %d, %Y",
    "%B %d %Y", "%b %d %Y",
    "%d %B %Y", "%d %b %Y",
    "%d-%b-%Y", "%d-%B-%Y",
    "%d-%b-%y", "%d-%B-%y",          # 13-Jul-26
    "%b/%d/%Y", "%B/%d/%Y",
    "%b/%d/%y", "%B/%d/%y",          # Jun/01/26
    "%m%d%Y",                        # 06262026 - bare digits, tried last
)

# Values that mean "no date" even when they are non-empty text.
_MISSING = frozenset({
    "", "null", "none", "nil", "n/a", "na", "n.a.", "-", "--",
    "not available", "not found", "not provided", "unknown", "tbd",
})


def parse_invoice_date(raw: Optional[str]) -> Optional[datetime.date]:
    """Parse an invoice date to a `date`, or None if it cannot be read.

    Returning None is a real outcome, not an error to swallow: the caller sends the row
    to the date review queue so a human can resolve it.
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if text.lower() in _MISSING:
        return None
    # Excel datetime cells stringify with a time part; drop it before matching.
    text = re.sub(r"\s+\d{1,2}:\d{2}(:\d{2})?(\.\d+)?$", "", text).strip()
    if not text:
        return None
    for fmt in _FORMATS:
        try:
            return datetime.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def to_iso(raw: Optional[str]) -> str:
    """Normalize an invoice date to 'YYYY-MM-DD', or '' when it cannot be parsed.

    Empty string rather than None so it drops straight into a TEXT column and the
    review-queue query can select on COALESCE(invoice_date_iso, '') = ''.
    """
    parsed = parse_invoice_date(raw)
    return parsed.isoformat() if parsed else ""
