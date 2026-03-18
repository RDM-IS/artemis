"""Quiet hours logic — silence scheduled jobs during off-hours.

Manages quiet hours checking and timezone overrides stored in SQLite.
"""

import logging
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from artemis import config
from artemis.commitments import get_db

logger = logging.getLogger(__name__)

# Common city → IANA timezone map for natural-language overrides
_CITY_TIMEZONES: dict[str, str] = {
    "paris": "Europe/Paris",
    "london": "Europe/London",
    "berlin": "Europe/Berlin",
    "amsterdam": "Europe/Amsterdam",
    "rome": "Europe/Rome",
    "madrid": "Europe/Madrid",
    "lisbon": "Europe/Lisbon",
    "dublin": "Europe/Dublin",
    "zurich": "Europe/Zurich",
    "vienna": "Europe/Vienna",
    "brussels": "Europe/Brussels",
    "prague": "Europe/Prague",
    "warsaw": "Europe/Warsaw",
    "stockholm": "Europe/Stockholm",
    "oslo": "Europe/Oslo",
    "copenhagen": "Europe/Copenhagen",
    "helsinki": "Europe/Helsinki",
    "athens": "Europe/Athens",
    "istanbul": "Europe/Istanbul",
    "tokyo": "Asia/Tokyo",
    "seoul": "Asia/Seoul",
    "shanghai": "Asia/Shanghai",
    "beijing": "Asia/Shanghai",
    "hong kong": "Asia/Hong_Kong",
    "singapore": "Asia/Singapore",
    "bangkok": "Asia/Bangkok",
    "mumbai": "Asia/Kolkata",
    "delhi": "Asia/Kolkata",
    "dubai": "Asia/Dubai",
    "sydney": "Australia/Sydney",
    "melbourne": "Australia/Melbourne",
    "auckland": "Pacific/Auckland",
    "toronto": "America/Toronto",
    "new york": "America/New_York",
    "nyc": "America/New_York",
    "boston": "America/New_York",
    "washington": "America/New_York",
    "dc": "America/New_York",
    "miami": "America/New_York",
    "atlanta": "America/New_York",
    "chicago": "America/Chicago",
    "milwaukee": "America/Chicago",
    "dallas": "America/Chicago",
    "houston": "America/Chicago",
    "austin": "America/Chicago",
    "minneapolis": "America/Chicago",
    "denver": "America/Denver",
    "phoenix": "America/Phoenix",
    "salt lake city": "America/Denver",
    "los angeles": "America/Los_Angeles",
    "la": "America/Los_Angeles",
    "san francisco": "America/Los_Angeles",
    "sf": "America/Los_Angeles",
    "seattle": "America/Los_Angeles",
    "portland": "America/Los_Angeles",
    "vancouver": "America/Vancouver",
    "mexico city": "America/Mexico_City",
    "sao paulo": "America/Sao_Paulo",
    "buenos aires": "America/Argentina/Buenos_Aires",
    "santiago": "America/Santiago",
    "bogota": "America/Bogota",
    "lima": "America/Lima",
    "honolulu": "Pacific/Honolulu",
    "anchorage": "America/Anchorage",
    "cairo": "Africa/Cairo",
    "johannesburg": "Africa/Johannesburg",
    "nairobi": "Africa/Nairobi",
    "lagos": "Africa/Lagos",
    "tel aviv": "Asia/Jerusalem",
    "jerusalem": "Asia/Jerusalem",
    "riyadh": "Asia/Riyadh",
    "doha": "Asia/Qatar",
    "taipei": "Asia/Taipei",
    "manila": "Asia/Manila",
    "jakarta": "Asia/Jakarta",
    "kuala lumpur": "Asia/Kuala_Lumpur",
    "hanoi": "Asia/Ho_Chi_Minh",
    "reykjavik": "Atlantic/Reykjavik",
}


def _parse_time(t: str) -> time:
    """Parse 'HH:MM' to time object."""
    parts = t.strip().split(":")
    return time(int(parts[0]), int(parts[1]))


def resolve_city_timezone(city_or_tz: str) -> str | None:
    """Resolve a city name or IANA timezone string to an IANA timezone.

    Returns None if unrecognized.
    """
    normalized = city_or_tz.strip().lower()

    # Check city map first
    if normalized in _CITY_TIMEZONES:
        return _CITY_TIMEZONES[normalized]

    # Try as raw IANA timezone
    try:
        ZoneInfo(city_or_tz.strip())
        return city_or_tz.strip()
    except (KeyError, ValueError):
        pass

    return None


def get_active_timezone() -> str:
    """Return the active timezone — override if set and not expired, else HOME_TIMEZONE."""
    try:
        conn = get_db()
        row = conn.execute(
            "SELECT timezone, expires_at FROM timezone_overrides WHERE id = 1"
        ).fetchone()
        if row:
            expires_at = datetime.fromisoformat(row["expires_at"])
            if datetime.utcnow() < expires_at:
                return row["timezone"]
            # Expired — clean up silently (scheduler handles announcement)
    except Exception:
        logger.debug("Failed to check timezone override", exc_info=True)

    return config.HOME_TIMEZONE


def get_tz_abbrev(tz_name: str | None = None) -> str:
    """Get timezone abbreviation (e.g., CDT, CET) for the active or specified timezone."""
    tz_name = tz_name or get_active_timezone()
    try:
        tz = ZoneInfo(tz_name)
        return datetime.now(tz).strftime("%Z")
    except Exception:
        return "???"


def is_quiet_hours() -> bool:
    """Check if the current time is within the quiet hours window.

    Uses the active timezone (override or home). Handles midnight-spanning
    windows like 20:00-05:00.
    """
    tz_name = get_active_timezone()
    try:
        tz = ZoneInfo(tz_name)
    except (KeyError, ValueError):
        tz = ZoneInfo(config.HOME_TIMEZONE)

    now = datetime.now(tz).time()
    start = _parse_time(config.QUIET_HOURS_START)
    end = _parse_time(config.QUIET_HOURS_END)

    if start <= end:
        # Same-day window (e.g., 01:00-05:00)
        return start <= now < end
    else:
        # Midnight-spanning window (e.g., 20:00-05:00)
        return now >= start or now < end


def quiet_hours_status() -> str:
    """Return a formatted status string for quiet hours."""
    tz_name = get_active_timezone()
    tz_abbrev = get_tz_abbrev(tz_name)
    start_str = _parse_time(config.QUIET_HOURS_START).strftime("%I:%M %p").lstrip("0")
    end_str = _parse_time(config.QUIET_HOURS_END).strftime("%I:%M %p").lstrip("0")

    if is_quiet_hours():
        status = f"\U0001f319 Quiet hours active ({start_str} - {end_str} {tz_abbrev}). Scheduled jobs paused."
    else:
        status = f"\u2600\ufe0f Outside quiet hours ({start_str} - {end_str} {tz_abbrev})."

    # Check for active timezone override
    try:
        conn = get_db()
        row = conn.execute(
            "SELECT timezone, city_name, expires_at FROM timezone_overrides WHERE id = 1"
        ).fetchone()
        if row:
            expires_at = datetime.fromisoformat(row["expires_at"])
            if datetime.utcnow() < expires_at:
                city = row["city_name"] or row["timezone"]
                expires_str = expires_at.strftime("%b %d")
                status += f"\n\U0001f30d Timezone override: {city} ({row['timezone']}) until {expires_str}."
    except Exception:
        pass

    return status


def set_timezone_override(tz_name: str, city_name: str = "", days: int = 7) -> str:
    """Set a timezone override that expires after `days` days.

    Returns a confirmation message string.
    """
    expires_at = datetime.utcnow() + timedelta(days=days)
    tz_abbrev = get_tz_abbrev(tz_name)
    start_str = _parse_time(config.QUIET_HOURS_START).strftime("%I:%M %p").lstrip("0")
    end_str = _parse_time(config.QUIET_HOURS_END).strftime("%I:%M %p").lstrip("0")
    expires_str = expires_at.strftime("%B %d")

    try:
        conn = get_db()
        conn.execute(
            "INSERT OR REPLACE INTO timezone_overrides (id, timezone, expires_at, city_name) "
            "VALUES (1, ?, ?, ?)",
            (tz_name, expires_at.isoformat(), city_name or tz_name),
        )
        conn.commit()
    except Exception:
        logger.exception("Failed to set timezone override")
        return f"\u26a0\ufe0f Failed to set timezone override — check logs."

    display_city = city_name.title() if city_name else tz_name
    return (
        f"\U0001f30d Got it \u2014 quiet hours adjusted to {start_str} - {end_str} {tz_abbrev} "
        f"through {expires_str}. All times in your briefs will reflect {display_city} time."
    )


def clear_timezone_override() -> str:
    """Clear the active timezone override. Returns confirmation message."""
    try:
        conn = get_db()
        conn.execute("DELETE FROM timezone_overrides WHERE id = 1")
        conn.commit()
    except Exception:
        logger.exception("Failed to clear timezone override")
        return "\u26a0\ufe0f Failed to clear timezone override — check logs."

    home_abbrev = get_tz_abbrev(config.HOME_TIMEZONE)
    return f"\U0001f3e0 Timezone reset to {config.HOME_TIMEZONE} ({home_abbrev}). Welcome back!"


def check_expired_overrides() -> str | None:
    """Check if the timezone override has expired. If so, delete it and return announcement text.

    Returns None if no override expired. Called by scheduler daily.
    """
    try:
        conn = get_db()
        row = conn.execute(
            "SELECT timezone, city_name, expires_at FROM timezone_overrides WHERE id = 1"
        ).fetchone()
        if not row:
            return None

        expires_at = datetime.fromisoformat(row["expires_at"])
        if datetime.utcnow() >= expires_at:
            city = row["city_name"] or row["timezone"]
            conn.execute("DELETE FROM timezone_overrides WHERE id = 1")
            conn.commit()
            home_abbrev = get_tz_abbrev(config.HOME_TIMEZONE)
            return (
                f"\U0001f30d Timezone override expired \u2014 reverting to {config.HOME_TIMEZONE} "
                f"({home_abbrev}). If you're still traveling, let me know."
            )
    except Exception:
        logger.exception("Failed to check timezone override expiry")

    return None
