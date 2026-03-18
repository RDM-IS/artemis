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

# Day priority for slot selection: Monday first, then Wed/Thu afternoons
_DAY_PRIORITY = [0, 2, 3, 1, 4, 5, 6]  # Mon, Wed, Thu, Tue, Fri, Sat, Sun


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


def _get_tz_abbrev() -> str:
    """Get the current timezone abbreviation (e.g., CDT, CST)."""
    tz = ZoneInfo(config.TIMEZONE)
    now = datetime.now(tz)
    return now.strftime("%Z")


def is_focus_block(summary: str) -> bool:
    """Check if an event title is a focus/deep-work block."""
    lower = summary.lower()
    for kw in config.FOCUS_BLOCK_KEYWORDS:
        if kw.lower() in lower:
            return True
    return False


def get_available_days(start: date, end: date) -> list[date]:
    """Return dates between start and end (inclusive) that have availability windows.

    Uses per-day config (AVAILABILITY_MONDAY etc.) to skip unavailable days.
    """
    days = []
    current = start
    while current <= end:
        window = config.get_day_availability(current.weekday())
        if window is not None:
            days.append(current)
        current += timedelta(days=1)
    return days


def get_business_days(start: date, end: date) -> list[date]:
    """Return dates between start and end (inclusive) that are preferred meeting days.

    Delegates to get_available_days which uses per-day availability config.
    """
    return get_available_days(start, end)


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

    # "this week" — rest of the current week (today through Sunday)
    if "this week" in lower:
        days_until_sunday = (6 - today.weekday()) % 7
        end = today + timedelta(days=days_until_sunday) if days_until_sunday > 0 else today
        return today, end

    # "next week"
    if "next week" in lower:
        days_until_monday = (7 - today.weekday()) % 7
        if days_until_monday == 0:
            days_until_monday = 7
        start = today + timedelta(days=days_until_monday)
        end = start + timedelta(days=6)  # through Sunday
        return start, end

    # "next N days" / "next N business days"
    m = re.search(r"next\s+(\d+)\s+(business\s+)?days?", lower)
    if m:
        n = int(m.group(1))
        if m.group(2):  # business days
            d, count = today + timedelta(days=1), 0
            while count < n:
                if config.get_day_availability(d.weekday()) is not None:
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

    # Default: next 5 business days (days with availability windows)
    d, count = today + timedelta(days=1), 0
    end = d
    while count < 5:
        if config.get_day_availability(d.weekday()) is not None:
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

    Uses per-day availability config. Returns empty list if day is unavailable.
    Returns list of {"date": date, "start": time, "end": time, "day_name": str}.
    """
    tz = ZoneInfo(config.TIMEZONE)

    # Check per-day availability window
    day_window = config.get_day_availability(target_date.weekday())
    if day_window is None:
        return []  # Day is unavailable

    # Use per-day window, with optional overrides
    start_t = _parse_time(hours_start) if hours_start else _parse_time(day_window[0])
    end_t = _parse_time(hours_end) if hours_end else _parse_time(day_window[1])
    duration = slot_duration or config.DEFAULT_SLOT_DURATION
    buffer = buffer_minutes if buffer_minutes is not None else config.MEETING_BUFFER_MINUTES

    # Build occupied intervals as (start_minutes, end_minutes) from midnight
    occupied: list[tuple[int, int]] = []
    for event in events:
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

        # Convert to minutes from midnight (with buffer)
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
            gap_start = cursor
            gap_end = min(block_start, window_end)
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
                gap_start += duration
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
    num_slots: int | None = None,
) -> list[dict]:
    """Find open meeting slots across a date range.

    Returns up to num_slots slots spread across the range, prioritizing
    Monday first, then Wed/Thu afternoons. Uses per-day availability windows.
    Each slot: {"date": date, "start": time, "end": time, "day_name": str}
    """
    duration = slot_duration or config.DEFAULT_SLOT_DURATION
    target_slots = num_slots if num_slots is not None else config.DEFAULT_NUM_SLOTS
    available_days = get_available_days(start_date, end_date)
    if not available_days:
        return []

    # Fetch all events for the range
    events = calendar_client.get_events_in_range(start_date, end_date)

    # Collect slots per day
    slots_by_day: dict[date, list[dict]] = {}
    for day in available_days:
        day_slots = find_open_slots(events, day, slot_duration=duration)
        if day_slots:
            slots_by_day[day] = day_slots

    if not slots_by_day:
        return []

    # Sort days by priority: Monday (0) first, then Wed (2), Thu (3)
    sorted_days = sorted(
        slots_by_day.keys(),
        key=lambda d: (_DAY_PRIORITY.index(d.weekday()) if d.weekday() in _DAY_PRIORITY else 99, d),
    )

    # Pick slots: 1 per day first (spreading across days), then fill
    picked: list[dict] = []

    # Round 1: first slot from each day in priority order
    for day in sorted_days:
        if len(picked) >= target_slots:
            break
        picked.append(slots_by_day[day][0])

    # Round 2: if still need more, take additional from priority days
    if len(picked) < target_slots:
        for day in sorted_days:
            if len(picked) >= target_slots:
                break
            for slot in slots_by_day[day][1:]:
                if slot not in picked:
                    picked.append(slot)
                    if len(picked) >= target_slots:
                        break

    # Sort final picks chronologically
    picked.sort(key=lambda s: (s["date"], s["start"]))
    return picked[:target_slots]


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
    tz_abbrev = _get_tz_abbrev()
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
        parts.append(f"{i}. {slot['day_name']} {date_str} — {start_str}-{end_str} {tz_abbrev}")

    link = booking_link or config.BOOKING_LINK
    if link:
        parts.append(f"\nBooking link: {link}")

    if sender_email:
        parts.append(f"\nReply: `send 1,2,3` or `send all` to draft a reply. `edit` to modify. `cancel` to discard.")

    return "\n".join(parts)


def format_slots_email(
    slots: list[dict],
    sender_first_name: str = "",
    booking_link: str = "",
) -> str:
    """Format slots as an email reply body matching the standard template.

    Template:
        Hi [first name],

        Happy to connect. Here are a few times that work on my end:

        - Monday, March 24 — 10:00 AM CDT
        - Wednesday, March 26 — 4:30 PM CDT
        - Thursday, March 27 — 4:30 PM CDT

        If none of those work, feel free to grab a time directly from my calendar:
        [booking link]

        Looking forward to it,
        Ryan
    """
    if not slots:
        return "I checked my calendar but don't have openings in that timeframe."

    tz_abbrev = _get_tz_abbrev()
    link = booking_link or config.BOOKING_LINK

    lines = []

    # Greeting
    first_name = sender_first_name.strip() if sender_first_name else ""
    if first_name:
        lines.append(f"Hi {first_name},")
    else:
        lines.append("Hi,")

    lines.append("")
    lines.append("Happy to connect. Here are a few times that work on my end:")
    lines.append("")

    for slot in slots:
        date_str = slot["date"].strftime("%A, %B %d")
        start_str = slot["start"].strftime("%I:%M %p").lstrip("0")
        lines.append(f"- {date_str} — {start_str} {tz_abbrev}")

    lines.append("")
    if link:
        lines.append("If none of those work, feel free to grab a time directly from my calendar:")
        lines.append(link)
        lines.append("")

    lines.append("Looking forward to it,")
    lines.append("Ryan")

    return "\n".join(lines)
