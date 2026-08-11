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
