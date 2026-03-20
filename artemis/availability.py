"""Availability slot finder — calendar analysis for scheduling.

Two modes:
- MEETING: external, requires another person. Respects per-day meeting windows,
  warns about avoid days (Tue/Fri), never suggests unavailable days.
- WORK_BLOCK: internal, solo. Full day availability (07:00-22:00 by default).
  Respects existing calendar events, DO NOT SCHEDULE blocks, and quiet hours.

Used by PB-006 (email trigger, always MEETING mode) and @mention commands.
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


# Availability modes
MODE_MEETING = "meeting"
MODE_WORK_BLOCK = "work_block"


def get_available_days(
    start: date, end: date, mode: str = MODE_MEETING
) -> list[date]:
    """Return dates between start and end (inclusive) that have availability windows.

    mode: "meeting" or "work_block".
    For meeting mode, avoid days ARE included (caller handles warnings).
    """
    days = []
    current = start
    while current <= end:
        window = config.get_day_availability(current.weekday(), mode=mode)
        if window is not None:
            days.append(current)
        current += timedelta(days=1)
    return days


def get_business_days(start: date, end: date) -> list[date]:
    """Return dates between start and end (inclusive) that are preferred meeting days."""
    return get_available_days(start, end, mode=MODE_MEETING)


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
    mode: str = MODE_MEETING,
) -> list[dict]:
    """Find open time slots on a single day given existing events.

    mode: "meeting" or "work_block".
    For work_block mode, also respects quiet hours (caps end at QUIET_HOURS_START).
    Returns list of {"date": date, "start": time, "end": time, "day_name": str, "is_avoid_day": bool}.
    """
    tz = ZoneInfo(config.TIMEZONE)

    # Check per-day availability window
    day_window = config.get_day_availability(target_date.weekday(), mode=mode)
    if day_window is None:
        return []  # Day is unavailable

    # Use per-day window, with optional overrides
    start_t = _parse_time(hours_start) if hours_start else _parse_time(day_window[0])
    end_t = _parse_time(hours_end) if hours_end else _parse_time(day_window[1])

    # Work block mode: cap end at quiet hours start
    if mode == MODE_WORK_BLOCK:
        quiet_start = _parse_time(config.QUIET_HOURS_START)
        if end_t > quiet_start:
            end_t = quiet_start
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

    # Check if this is a meeting avoid day
    avoid_day = (mode == MODE_MEETING and config.is_meeting_avoid_day(target_date.weekday()))

    # Find gaps in the availability window
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
                    "is_avoid_day": avoid_day,
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
                "is_avoid_day": avoid_day,
            })
            gap_start += duration

    return slots


_MIN_SLOT_GAP_MINUTES = 120  # minimum 2 hours between offered slots


def _slot_minutes(slot: dict) -> int:
    """Convert a slot to absolute minutes (days * 1440 + time) for gap checks."""
    # Use days since epoch for absolute ordering
    d = slot["date"]
    day_offset = d.toordinal()
    return day_offset * 1440 + slot["start"].hour * 60 + slot["start"].minute


def _respects_gap(candidate: dict, picked: list[dict]) -> bool:
    """Check that candidate is at least _MIN_SLOT_GAP_MINUTES from all picked slots."""
    c_min = _slot_minutes(candidate)
    for p in picked:
        if abs(c_min - _slot_minutes(p)) < _MIN_SLOT_GAP_MINUTES:
            return False
    return True


def _collect_slots_for_range(
    calendar_client,
    start_date: date,
    end_date: date,
    duration: int,
    mode: str = MODE_MEETING,
) -> dict[date, list[dict]]:
    """Fetch events and compute open slots per available day in the range."""
    available_days = get_available_days(start_date, end_date, mode=mode)
    if not available_days:
        return {}

    events = calendar_client.get_events_in_range(start_date, end_date)

    slots_by_day: dict[date, list[dict]] = {}
    for day in available_days:
        day_slots = find_open_slots(events, day, slot_duration=duration, mode=mode)
        if day_slots:
            slots_by_day[day] = day_slots
    return slots_by_day


def _pick_slots(
    slots_by_day: dict[date, list[dict]],
    target_slots: int,
    mode: str = MODE_MEETING,
) -> list[dict]:
    """Pick slots following the preferred spread pattern.

    Meeting mode spread (for 3 slots):
      1st: soonest available Monday
      2nd: soonest available Wednesday or Thursday
      3rd: next Monday or Wed/Thu after that
      Avoid days (Tue/Fri) only used if no other slots available.

    Work block mode preferences:
      - Weekday evenings first (7pm-10pm) for small tasks
      - Weekend mornings (8am-12pm) for large blocks
      - Saturday for 3+ hour blocks

    Rules:
    - Never two slots within 2 hours of each other
    - Spread across different days first
    - Only put multiple slots on the same day as a last resort
    """
    if not slots_by_day:
        return []

    if mode == MODE_WORK_BLOCK:
        return _pick_work_block_slots(slots_by_day, target_slots)

    # Meeting mode: prefer non-avoid days, then use avoid days as fallback
    _tier = {0: 0, 2: 1, 3: 2}  # Mon, Wed, Thu preferred

    # Separate avoid days from preferred days
    preferred_days = sorted(
        [d for d in slots_by_day.keys() if not config.is_meeting_avoid_day(d.weekday())],
        key=lambda d: (_tier.get(d.weekday(), 3), d),
    )
    avoid_days = sorted(
        [d for d in slots_by_day.keys() if config.is_meeting_avoid_day(d.weekday())],
        key=lambda d: d,
    )

    picked: list[dict] = []
    used_dates: set[date] = set()

    # Round 1: Preferred days first, one slot per day.
    for day in preferred_days:
        if len(picked) >= target_slots:
            break
        candidate = slots_by_day[day][0]
        if _respects_gap(candidate, picked):
            picked.append(candidate)
            used_dates.add(day)

    # Round 2: Still short — try more slots on unused preferred days.
    if len(picked) < target_slots:
        for day in preferred_days:
            if day in used_dates:
                continue
            if len(picked) >= target_slots:
                break
            for slot in slots_by_day[day]:
                if _respects_gap(slot, picked):
                    picked.append(slot)
                    used_dates.add(day)
                    break

    # Round 3: Last resort on preferred days — second slot same day.
    if len(picked) < target_slots:
        for day in preferred_days:
            if len(picked) >= target_slots:
                break
            for slot in slots_by_day[day]:
                if slot in picked:
                    continue
                if _respects_gap(slot, picked):
                    picked.append(slot)
                    if len(picked) >= target_slots:
                        break

    # Round 4: Avoid days — only if still can't fill.
    if len(picked) < target_slots and avoid_days:
        for day in avoid_days:
            if len(picked) >= target_slots:
                break
            for slot in slots_by_day[day]:
                if _respects_gap(slot, picked):
                    picked.append(slot)
                    used_dates.add(day)
                    break

    # Sort final picks chronologically
    picked.sort(key=lambda s: (s["date"], s["start"]))
    return picked[:target_slots]


def _pick_work_block_slots(
    slots_by_day: dict[date, list[dict]],
    target_slots: int,
) -> list[dict]:
    """Pick work block slots with preference for evenings and weekend mornings."""

    def _slot_preference(slot: dict) -> tuple[int, int]:
        """Lower = more preferred."""
        weekday = slot["date"].weekday()
        hour = slot["start"].hour
        is_weekend = weekday >= 5

        # Weekday evening (19:00-22:00) — best for small tasks
        if not is_weekend and 19 <= hour < 22:
            return (0, hour)
        # Weekend morning (8:00-12:00) — great for large blocks
        if is_weekend and 8 <= hour < 12:
            return (1, hour)
        # Saturday afternoon — good for longer work
        if weekday == 5 and 12 <= hour < 18:
            return (2, hour)
        # Weekday morning/afternoon (available but less preferred)
        if not is_weekend:
            return (3, hour)
        # Everything else
        return (4, hour)

    all_slots = []
    for day_slots in slots_by_day.values():
        all_slots.extend(day_slots)

    # Sort by preference, then pick with gap enforcement
    all_slots.sort(key=_slot_preference)

    picked: list[dict] = []
    used_dates: set[date] = set()

    # First pass: different days, preferred order
    for slot in all_slots:
        if len(picked) >= target_slots:
            break
        if slot["date"] in used_dates:
            continue
        if _respects_gap(slot, picked):
            picked.append(slot)
            used_dates.add(slot["date"])

    # Second pass: same day if needed
    if len(picked) < target_slots:
        for slot in all_slots:
            if len(picked) >= target_slots:
                break
            if slot in picked:
                continue
            if _respects_gap(slot, picked):
                picked.append(slot)

    picked.sort(key=lambda s: (s["date"], s["start"]))
    return picked[:target_slots]


def get_availability(
    calendar_client,
    start_date: date,
    end_date: date,
    slot_duration: int | None = None,
    num_slots: int | None = None,
    mode: str = MODE_MEETING,
) -> list[dict]:
    """Find open slots across a date range.

    mode: "meeting" (external, per-day windows) or "work_block" (internal, full days).

    Returns up to num_slots slots spread across the range. Each slot:
    {"date": date, "start": time, "end": time, "day_name": str, "is_avoid_day": bool}
    """
    duration = slot_duration or config.DEFAULT_SLOT_DURATION
    target_slots = num_slots if num_slots is not None else config.DEFAULT_NUM_SLOTS

    slots_by_day = _collect_slots_for_range(calendar_client, start_date, end_date, duration, mode=mode)

    # Check if we have enough distinct days
    distinct_days = len(slots_by_day)
    if distinct_days < target_slots:
        extended_end = start_date + timedelta(days=14)
        if extended_end > end_date:
            extra_slots = _collect_slots_for_range(
                calendar_client,
                end_date + timedelta(days=1),
                extended_end,
                duration,
                mode=mode,
            )
            slots_by_day.update(extra_slots)
            if len(slots_by_day) > distinct_days:
                logger.info(
                    "Extended availability search to %s — found %d days (was %d)",
                    extended_end, len(slots_by_day), distinct_days,
                )

    if not slots_by_day:
        return []

    return _pick_slots(slots_by_day, target_slots, mode=mode)


def has_avoid_day_slots(slots: list[dict]) -> bool:
    """Check if any picked slots are on meeting-avoid days."""
    return any(s.get("is_avoid_day") for s in slots)


def format_avoid_day_warning(slots: list[dict]) -> str:
    """Generate a warning string for avoid-day slots."""
    avoid_names = set()
    for s in slots:
        if s.get("is_avoid_day"):
            avoid_names.add(s["day_name"])
    if not avoid_names:
        return ""
    days_str = "/".join(sorted(avoid_names))
    return (
        f"\u26a0\ufe0f {days_str} {'is a' if len(avoid_names) == 1 else 'are'} protected day"
        f"{'s' if len(avoid_names) > 1 else ''}. "
        f"Suggesting anyway because no other slots available this week. Confirm?"
    )


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

    # Avoid-day warning for meeting mode
    if has_avoid_day_slots(slots):
        parts.append(f"\n{format_avoid_day_warning(slots)}")

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
