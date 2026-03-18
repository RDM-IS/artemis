"""Availability slot finder — calendar analysis for scheduling.

Pure functions that take events and preferences, return open time slots.
Used by both PB-006 (email trigger) and @mention commands.
"""

import logging
import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from artemis import config

logger = logging.getLogger(__name__)

# Day abbreviation → weekday number (Mon=0 … Sun=6)
_DAY_MAP = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def _preferred_weekdays() -> set[int]:
    """Convert config PREFERRED_MEETING_DAYS to a set of weekday ints."""
    days = set()
    for abbr in config.PREFERRED_MEETING_DAYS:
        key = abbr.lower()[:3]
        if key in _DAY_MAP:
            days.add(_DAY_MAP[key])
    return days or {0, 1, 2, 3, 4}  # default Mon-Fri


def _parse_time(t: str) -> time:
    """Parse 'HH:MM' string to time object."""
    parts = t.strip().split(":")
    return time(int(parts[0]), int(parts[1]))


def is_focus_block(summary: str) -> bool:
    """Check if an event title is a focus/deep-work block."""
    lower = summary.lower()
    for kw in config.FOCUS_BLOCK_KEYWORDS:
        if kw.lower() in lower:
            return True
    return False


def get_business_days(start: date, end: date) -> list[date]:
    """Return dates between start and end (inclusive) that are preferred meeting days."""
    preferred = _preferred_weekdays()
    days = []
    current = start
    while current <= end:
        if current.weekday() in preferred:
            days.append(current)
        current += timedelta(days=1)
    return days


def parse_timeframe(text: str) -> tuple[date, date]:
    """Parse a natural-language timeframe into (start_date, end_date).

    Handles: "this week", "next week", "tomorrow", "next X days",
    "March 24", "3/24". Default: next 5 business days.
    """
    today = date.today()
    lower = text.lower().strip()

    # "tomorrow"
    if "tomorrow" in lower:
        d = today + timedelta(days=1)
        return d, d

    # "this week" — rest of the current week (today through Friday)
    if "this week" in lower:
        # Find next Friday (or today if already past)
        days_until_friday = (4 - today.weekday()) % 7
        end = today + timedelta(days=days_until_friday) if days_until_friday > 0 else today
        return today, end

    # "next week"
    if "next week" in lower:
        # Monday of next week
        days_until_monday = (7 - today.weekday()) % 7
        if days_until_monday == 0:
            days_until_monday = 7
        start = today + timedelta(days=days_until_monday)
        end = start + timedelta(days=4)  # Friday
        return start, end

    # "next N days" / "next N business days"
    m = re.search(r"next\s+(\d+)\s+(business\s+)?days?", lower)
    if m:
        n = int(m.group(1))
        if m.group(2):  # business days
            preferred = _preferred_weekdays()
            d, count = today + timedelta(days=1), 0
            while count < n:
                if d.weekday() in preferred:
                    count += 1
                if count < n:
                    d += timedelta(days=1)
            return today + timedelta(days=1), d
        else:
            return today + timedelta(days=1), today + timedelta(days=n)

    # Specific date: "March 24", "Mar 24", "3/24"
    m = re.search(r"(\w+)\s+(\d{1,2})", lower)
    if m:
        month_names = {
            "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
            "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
            "january": 1, "february": 2, "march": 3, "april": 4,
            "june": 6, "july": 7, "august": 8, "september": 9,
            "october": 10, "november": 11, "december": 12,
        }
        month_key = m.group(1).lower()
        if month_key in month_names:
            month = month_names[month_key]
            day = int(m.group(2))
            try:
                d = date(today.year, month, day)
                if d < today:
                    d = date(today.year + 1, month, day)
                return d, d
            except ValueError:
                pass

    m = re.search(r"(\d{1,2})/(\d{1,2})", lower)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        try:
            d = date(today.year, month, day)
            if d < today:
                d = date(today.year + 1, month, day)
            return d, d
        except ValueError:
            pass

    # Default: next 5 business days
    preferred = _preferred_weekdays()
    d, count = today + timedelta(days=1), 0
    end = d
    while count < 5:
        if d.weekday() in preferred:
            count += 1
            end = d
        d += timedelta(days=1)
    return today + timedelta(days=1), end


def find_open_slots(
    events: list[dict],
    target_date: date,
    hours_start: str | None = None,
    hours_end: str | None = None,
    slot_duration: int | None = None,
    buffer_minutes: int | None = None,
) -> list[dict]:
    """Find open time slots on a single day given existing events.

    Returns list of {"date": date, "start": time, "end": time, "day_name": str}.
    """
    tz = ZoneInfo(config.TIMEZONE)
    start_t = _parse_time(hours_start or config.MEETING_HOURS_START)
    end_t = _parse_time(hours_end or config.MEETING_HOURS_END)
    duration = slot_duration or config.DEFAULT_SLOT_DURATION
    buffer = buffer_minutes if buffer_minutes is not None else config.MEETING_BUFFER_MINUTES

    # Build occupied intervals as (start_minutes, end_minutes) from midnight
    occupied: list[tuple[int, int]] = []
    for event in events:
        if is_focus_block(event.get("summary", "")):
            # Focus blocks are fully blocked
            pass  # still add them below

        start_str = event.get("start", "")
        if not start_str:
            continue

        try:
            ev_start = datetime.fromisoformat(start_str)
            if ev_start.tzinfo is None:
                ev_start = ev_start.replace(tzinfo=tz)
            ev_start = ev_start.astimezone(tz)
        except (ValueError, TypeError):
            continue

        # Only consider events on this target_date
        if ev_start.date() != target_date:
            continue

        # Get end time from event or assume 1 hour
        end_str = event.get("end", "")
        if end_str:
            try:
                ev_end = datetime.fromisoformat(end_str)
                if ev_end.tzinfo is None:
                    ev_end = ev_end.replace(tzinfo=tz)
                ev_end = ev_end.astimezone(tz)
            except (ValueError, TypeError):
                ev_end = ev_start + timedelta(hours=1)
        else:
            ev_end = ev_start + timedelta(hours=1)

        # Convert to minutes from midnight
        start_min = ev_start.hour * 60 + ev_start.minute - buffer
        end_min = ev_end.hour * 60 + ev_end.minute + buffer
        occupied.append((max(0, start_min), min(24 * 60, end_min)))

    # Sort and merge overlapping intervals
    occupied.sort()
    merged: list[tuple[int, int]] = []
    for s, e in occupied:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))

    # Find gaps in the meeting window
    window_start = start_t.hour * 60 + start_t.minute
    window_end = end_t.hour * 60 + end_t.minute
    slots = []
    cursor = window_start

    for block_start, block_end in merged:
        if block_start > cursor:
            # There's a gap from cursor to block_start
            gap_start = cursor
            gap_end = min(block_start, window_end)
            # Fill with slots
            while gap_start + duration <= gap_end:
                slot_start = time(gap_start // 60, gap_start % 60)
                slot_end_min = gap_start + duration
                slot_end = time(slot_end_min // 60, slot_end_min % 60)
                slots.append({
                    "date": target_date,
                    "start": slot_start,
                    "end": slot_end,
                    "day_name": target_date.strftime("%A"),
                })
                gap_start += duration  # no overlapping slots
        cursor = max(cursor, block_end)

    # Remaining gap after last block
    if cursor < window_end:
        gap_start = cursor
        while gap_start + duration <= window_end:
            slot_start = time(gap_start // 60, gap_start % 60)
            slot_end_min = gap_start + duration
            slot_end = time(slot_end_min // 60, slot_end_min % 60)
            slots.append({
                "date": target_date,
                "start": slot_start,
                "end": slot_end,
                "day_name": target_date.strftime("%A"),
            })
            gap_start += duration

    return slots


def get_availability(
    calendar_client,
    start_date: date,
    end_date: date,
    slot_duration: int | None = None,
    num_slots: int = 6,
) -> list[dict]:
    """Find open meeting slots across a date range.

    Returns up to num_slots slots spread across the range.
    Each slot: {"date": date, "start": time, "end": time, "day_name": str}
    """
    duration = slot_duration or config.DEFAULT_SLOT_DURATION
    biz_days = get_business_days(start_date, end_date)
    if not biz_days:
        return []

    # Fetch all events for the range
    events = calendar_client.get_events_in_range(start_date, end_date)

    all_slots: list[dict] = []
    for day in biz_days:
        day_slots = find_open_slots(events, day, slot_duration=duration)
        all_slots.extend(day_slots)

    if not all_slots:
        return []

    # Spread picks across days: take up to 2 per day, then fill
    if len(all_slots) <= num_slots:
        return all_slots

    # Group by date, pick first 2 from each day, then fill remaining
    by_date: dict[date, list[dict]] = {}
    for s in all_slots:
        by_date.setdefault(s["date"], []).append(s)

    picked: list[dict] = []
    for day in biz_days:
        day_slots = by_date.get(day, [])
        if day_slots:
            # Pick first and a mid-afternoon slot if available
            picked.append(day_slots[0])
            if len(day_slots) > 1:
                mid = len(day_slots) // 2
                if day_slots[mid] != day_slots[0]:
                    picked.append(day_slots[mid])
        if len(picked) >= num_slots:
            break

    return picked[:num_slots]


def format_slots_mattermost(
    slots: list[dict],
    sender_name: str = "",
    sender_email: str = "",
    subject: str = "",
    original_quote: str = "",
    booking_link: str = "",
) -> str:
    """Format availability slots as a Mattermost message.

    If sender info is provided (email trigger), includes context header.
    Otherwise (direct @mention), just shows slots.
    """
    parts = []

    if sender_email:
        parts.append(f":calendar: **Availability request** from {sender_name} <{sender_email}>")
        if subject:
            parts.append(f"Subject: {subject}")
        if original_quote:
            parts.append(f'> "{original_quote[:200]}"')
        parts.append("")

    if not slots:
        parts.append("No open slots found for the requested timeframe.")
        return "\n".join(parts)

    parts.append("**Available slots:**")
    for i, slot in enumerate(slots, 1):
        date_str = slot["date"].strftime("%b %d")
        start_str = slot["start"].strftime("%I:%M %p").lstrip("0")
        end_str = slot["end"].strftime("%I:%M %p").lstrip("0")
        parts.append(f"{i}. {slot['day_name']} {date_str} — {start_str}-{end_str}")

    if booking_link:
        parts.append(f"\nBooking link: {booking_link}")

    if sender_email:
        parts.append(f"\nReply: `send 1,3,5` or `send all` to draft a reply. `edit` to modify. `cancel` to discard.")

    return "\n".join(parts)


def format_slots_email(
    slots: list[dict],
    booking_link: str = "",
) -> str:
    """Format slots as plain text for an email reply body."""
    if not slots:
        return "I checked my calendar but don't have openings in that timeframe."

    lines = ["Here are some times that work for me:\n"]
    for slot in slots:
        date_str = slot["date"].strftime("%A, %B %d")
        start_str = slot["start"].strftime("%I:%M %p").lstrip("0")
        end_str = slot["end"].strftime("%I:%M %p").lstrip("0")
        lines.append(f"  - {date_str}: {start_str} - {end_str}")

    lines.append("\nLet me know which works best and I'll send a calendar invite.")

    if booking_link:
        lines.append(f"\nOr feel free to book directly: {booking_link}")

    return "\n".join(lines)
