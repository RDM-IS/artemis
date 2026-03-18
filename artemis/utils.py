"""Shared utility helpers for Artemis."""

from datetime import date, timedelta


def next_business_day(from_date: date | None = None) -> date:
    """Return the next weekday (Mon-Fri) after *from_date*.

    Skips Saturday and Sunday.  No holiday calendar for MVP.
    If *from_date* is None, uses today.
    """
    d = from_date or date.today()
    d += timedelta(days=1)
    while d.weekday() >= 5:          # 5 = Saturday, 6 = Sunday
        d += timedelta(days=1)
    return d
