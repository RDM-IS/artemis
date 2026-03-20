"""Rolling calendar cache — 28-day window (-14 to +14 days).

Refreshed every 10 minutes by the pre-meeting brief job.
All calendar reads should go through this cache.
"""

import logging
from datetime import date, datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

_cache: list[dict] = []
_last_refreshed: Optional[datetime] = None


def refresh(calendar_client) -> int:
    """Full replacement pull of -14 to +14 days. Returns event count."""
    global _cache, _last_refreshed
    start = date.today() - timedelta(days=14)
    end = date.today() + timedelta(days=14)
    try:
        events = calendar_client.get_events_in_range(start, end)
        _cache = events
        _last_refreshed = datetime.now()
        logger.info("Calendar cache refreshed: %d events (%s to %s)", len(events), start, end)
        return len(events)
    except Exception:
        logger.exception("Calendar cache refresh failed")
        return 0


def get_events() -> list[dict]:
    """All cached events."""
    return list(_cache)


def get_events_for_date(target: date) -> list[dict]:
    """Events for a specific date."""
    target_str = target.isoformat()
    return [e for e in _cache if e.get("start", "").startswith(target_str)]


def get_events_in_range(start: date, end: date) -> list[dict]:
    """Events between start and end dates inclusive."""
    return [
        e for e in _cache
        if start.isoformat() <= e.get("start", "")[:10] <= end.isoformat()
    ]


def get_upcoming_with_externals(within_minutes: int | None = None) -> list[dict]:
    """Return events with external attendees, optionally filtered to start within N minutes."""
    now = datetime.now()
    result = []
    for e in _cache:
        external = [a for a in e.get("attendees", []) if not a.get("self")]
        if not external:
            continue
        if within_minutes is not None:
            try:
                event_start = datetime.fromisoformat(e["start"])
                if event_start.tzinfo:
                    event_start = event_start.replace(tzinfo=None)
                diff = (event_start - now).total_seconds() / 60
                if diff < 0 or diff > within_minutes:
                    continue
            except (ValueError, TypeError):
                continue
        e["external_attendees"] = external
        result.append(e)
    return result


def status() -> str:
    """Human-readable cache status."""
    if not _last_refreshed:
        return "Calendar cache: not loaded"
    age = int((datetime.now() - _last_refreshed).total_seconds() / 60)
    return f"Calendar cache: {len(_cache)} events, refreshed {age}m ago"
