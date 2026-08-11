# -*- coding: utf-8 -*-
"""Period and window arithmetic for the month calendar.

A "period" is a calendar month written the way settings.json already writes it:
"%B %Y", e.g. "August 2026". Everything here is a pure function - no database, no
filesystem - because this is the arithmetic every timing decision in the app depends on,
and it needs to be exhaustively testable without fixtures.

Two conventions worth stating, because both have a defensible alternative:

- A "week" is a seven-day block counted from the 1st, so week 3 is the 15th-21st. It is
  NOT "the third Monday". The SOP's calendar is written as "2nd week", "3rd week", and
  the invoices it describes do not care which weekday it is.
- Windows are always clamped to the period. "day:28-30" in February ends on the 28th
  rather than overflowing into March.
"""
import calendar
import datetime
import re
from typing import Optional, Tuple

_MONTHS = {calendar.month_name[i].lower(): i for i in range(1, 13)}


def parse_period(s: Optional[str]) -> Tuple[int, int]:
    """'August 2026' -> (2026, 8). Raises ValueError on anything else."""
    text = (s or "").strip()
    parts = text.split()
    if len(parts) != 2 or parts[0].lower() not in _MONTHS or not parts[1].isdigit():
        raise ValueError(f"not a period: {s!r}")
    return int(parts[1]), _MONTHS[parts[0].lower()]


def format_period(year: int, month: int) -> str:
    """(2026, 8) -> 'August 2026'."""
    return f"{calendar.month_name[month]} {year}"


def period_of(iso_date: str) -> str:
    """'2026-08-12' -> 'August 2026'."""
    d = datetime.date.fromisoformat(iso_date)
    return format_period(d.year, d.month)


def period_bounds(period: str) -> Tuple[datetime.date, datetime.date]:
    """First and last day of the period, inclusive."""
    year, month = parse_period(period)
    last = calendar.monthrange(year, month)[1]
    return datetime.date(year, month, 1), datetime.date(year, month, last)


def _clamp(day: int, year: int, month: int) -> int:
    last = calendar.monthrange(year, month)[1]
    return max(1, min(day, last))


def resolve_window(rule: str, period: str,
                   due_day: Optional[int] = None,
                   due_spread: Optional[int] = None) -> Optional[Tuple[str, str]]:
    """Resolve a window rule to (due_from, due_to) ISO dates inside `period`.

    Returns None when the rule does not apply to this period at all - currently only
    `date:YYYY-MM-DD` outside the period. That is what confines a one-off reminder to a
    single month without any active-flag bookkeeping.
    """
    year, month = parse_period(period)
    first, last = period_bounds(period)

    def iso(day: int) -> str:
        return datetime.date(year, month, _clamp(day, year, month)).isoformat()

    text = (rule or "").strip()

    if text.startswith("date:"):
        when = datetime.date.fromisoformat(text[5:])
        if (when.year, when.month) != (year, month):
            return None
        return when.isoformat(), when.isoformat()

    if text == "month-end":
        return last.isoformat(), last.isoformat()

    if text == "last-week":
        return iso(last.day - 6), last.isoformat()

    if text == "learned":
        if due_day is None:
            return first.isoformat(), last.isoformat()
        spread = due_spread or 0
        return iso(due_day - spread), iso(due_day + spread)

    m = re.fullmatch(r"day:(\d+)(?:-(\d+))?", text)
    if m:
        lo = int(m.group(1))
        hi = int(m.group(2)) if m.group(2) else lo
        return iso(lo), iso(hi)

    m = re.fullmatch(r"week:(\d+)(?:-(\d+))?", text)
    if m:
        lo = int(m.group(1))
        hi = int(m.group(2)) if m.group(2) else lo
        return iso((lo - 1) * 7 + 1), iso(hi * 7)

    raise ValueError(f"unknown window rule: {rule!r}")


# A pair needs this much evidence before any non-monthly cadence is inferred. Below it,
# a pair seen in June and August is equally consistent with even-months, quarterly, and
# two unrelated jobs - so learning stays with monthly or irregular.
CADENCE_GATE_OBSERVATIONS = 4
CADENCE_GATE_SPAN_MONTHS = 4


def _month_index(ym: str) -> int:
    """'2026-08' -> an integer month index, so gaps are plain subtraction."""
    return int(ym[:4]) * 12 + int(ym[5:7])


def classify_cadence(months, recent: int = 2) -> Tuple[str, Optional[int]]:
    """Classify billing cadence from the months a pair was observed in.

    `months` are 'YYYY-MM' strings, any order, duplicates allowed. Returns
    (cadence, anchor) where anchor is the parity for even/odd-months, month % 3 for
    quarterly, and None otherwise.

    Only the most recent `recent` gaps decide the cadence. Older observations still count
    toward the gate and toward confidence, but a vendor that billed quarterly last year and
    monthly since is monthly now - classifying it over the whole record would call it
    irregular and delay its warning to the last week of the month.

    `recent` is 2 because two equal gaps in a row are the smallest thing that is a repeat
    rather than a coincidence, AND because a wider window would not fit the data: no live
    pair has more than five observations, so a 4-gap window spans every pair's entire
    history and quietly degrades into the whole-history rule this exists to replace. See
    spec 6.1 "Why the window is two and not four".
    """
    uniq = sorted({m for m in months if m})
    if len(uniq) < 2:
        return "irregular", None

    idx = [_month_index(m) for m in uniq]
    gaps = [b - a for a, b in zip(idx, idx[1:])]
    span = idx[-1] - idx[0] + 1
    gated = len(uniq) >= CADENCE_GATE_OBSERVATIONS and span >= CADENCE_GATE_SPAN_MONTHS

    recent_gaps = gaps[-recent:]
    distinct = set(recent_gaps)

    if distinct == {1}:
        return "monthly", None
    if gated and distinct == {2}:
        last_month = idx[-1] % 12 or 12
        return ("even-months", 0) if last_month % 2 == 0 else ("odd-months", 1)
    if gated and distinct == {3}:
        last_month = idx[-1] % 12 or 12
        return "quarterly", last_month % 3
    return "irregular", None


def applies_to_period(cadence: str, anchor: Optional[int], period: str) -> bool:
    """Does an obligation with this cadence generate an instance in this period?

    `on-demand` never does, which is what makes a work-order vendor incapable of showing
    up as missing. Off-anchor periods for even/odd/quarterly also generate nothing, rather
    than generating an instance that would immediately read as missing.
    """
    if cadence == "on-demand":
        return False
    _, month = parse_period(period)
    if cadence == "even-months":
        return month % 2 == 0
    if cadence == "odd-months":
        return month % 2 == 1
    if cadence == "quarterly":
        return anchor is not None and month % 3 == anchor
    return True          # monthly, irregular, once
