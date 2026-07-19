"""Shared utility helpers for Artemis."""

from datetime import date, timedelta


def abbr_date(d: date) -> str:
    """Absolute short date, e.g. ``Jul 22`` — platform-independent (no %-d)."""
    return f"{d.strftime('%b')} {d.day}"


def end_of_this_week(today: date) -> date:
    """POLISH-1 P4 convention — the last day of "this week": *today* through the
    coming Sunday (inclusive). When *today* is itself Sunday, the window runs a
    full seven days to the following Sunday, so a Sunday still has a meaningful
    "this week" ahead of it (the observed bug: on Sun Jul 19 a Wed Jul 22 to-do
    was pushed into "next week" and dropped from the `todos` this-week view)."""
    days_ahead = (6 - today.weekday()) % 7  # Mon=0 … Sun=6 → days to coming Sunday
    return today + timedelta(days=days_ahead or 7)


def describe_due(d: date | None, today: date) -> str:
    """POLISH-1 P2 — a relative phrase that ALWAYS carries its absolute date.

    A wrong "today" anchor must be *visible*, never plausible: every relative
    phrase is rendered beside the concrete date it resolved to, from a single
    ``today`` the caller anchored in America/Chicago.

    Examples (today = Sun Jul 19)::

        describe_due(date(2026, 7, 22), today) -> "this Wednesday (Jul 22)"
        describe_due(date(2026, 7, 20), today) -> "tomorrow (Jul 20)"
        describe_due(date(2026, 7, 30), today) -> "in 11 days (Jul 30)"
        describe_due(None, today)              -> "no date set"
    """
    if d is None:
        return "no date set"
    abs_s = abbr_date(d)
    delta = (d - today).days
    if delta == 0:
        rel = "today"
    elif delta == 1:
        rel = "tomorrow"
    elif delta == -1:
        rel = "yesterday"
    elif delta < 0:
        rel = f"{-delta} days ago"
    elif 2 <= delta <= 6:
        prefix = "this" if d <= end_of_this_week(today) else "next"
        rel = f"{prefix} {d.strftime('%A')}"
    else:
        rel = f"in {delta} days"
    return f"{rel} ({abs_s})"


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
