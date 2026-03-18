"""Quiet hours session management — silence scheduled jobs during off-hours.

State-based quiet system with manual goodnight/morning, working session
overrides with inactivity timers, and timezone overrides stored in SQLite.
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


def _now_utc_iso() -> str:
    """Current UTC time as ISO string."""
    return datetime.utcnow().isoformat()


# ---------------------------------------------------------------------------
# City / timezone resolution
# ---------------------------------------------------------------------------


def resolve_city_timezone(city_or_tz: str) -> str | None:
    """Resolve a city name or IANA timezone string to an IANA timezone.

    Returns None if unrecognized.
    """
    normalized = city_or_tz.strip().lower()

    if normalized in _CITY_TIMEZONES:
        return _CITY_TIMEZONES[normalized]

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


# ---------------------------------------------------------------------------
# System state (generic key-value store)
# ---------------------------------------------------------------------------


def get_system_value(key: str) -> str | None:
    """Read a value from the system_state table."""
    try:
        conn = get_db()
        row = conn.execute(
            "SELECT value FROM system_state WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else None
    except Exception:
        logger.debug("Failed to read system_state[%s]", key, exc_info=True)
        return None


def set_system_value(key: str, value: str) -> None:
    """Upsert a value into the system_state table."""
    try:
        conn = get_db()
        conn.execute(
            "INSERT OR REPLACE INTO system_state (key, value, updated_at) VALUES (?, ?, ?)",
            (key, value, _now_utc_iso()),
        )
        conn.commit()
    except Exception:
        logger.exception("Failed to write system_state[%s]", key)


# ---------------------------------------------------------------------------
# Quiet state management
# ---------------------------------------------------------------------------


def _get_quiet_row() -> dict | None:
    """Read the quiet_state singleton row. Returns dict or None."""
    try:
        conn = get_db()
        row = conn.execute("SELECT * FROM quiet_state WHERE id = 1").fetchone()
        return dict(row) if row else None
    except Exception:
        logger.debug("Failed to read quiet_state", exc_info=True)
        return None


def _upsert_quiet_state(**kwargs) -> None:
    """Insert or update the quiet_state singleton row."""
    kwargs["updated_at"] = _now_utc_iso()
    try:
        conn = get_db()
        row = conn.execute("SELECT id FROM quiet_state WHERE id = 1").fetchone()
        if row:
            sets = ", ".join(f"{k} = ?" for k in kwargs)
            conn.execute(
                f"UPDATE quiet_state SET {sets} WHERE id = 1",
                list(kwargs.values()),
            )
        else:
            kwargs["id"] = 1
            cols = ", ".join(kwargs.keys())
            placeholders = ", ".join("?" for _ in kwargs)
            conn.execute(
                f"INSERT INTO quiet_state ({cols}) VALUES ({placeholders})",
                list(kwargs.values()),
            )
        conn.commit()
    except Exception:
        logger.exception("Failed to upsert quiet_state")


def _is_in_time_window() -> bool:
    """Check if current time is within the configured quiet hours window."""
    tz_name = get_active_timezone()
    try:
        tz = ZoneInfo(tz_name)
    except (KeyError, ValueError):
        tz = ZoneInfo(config.HOME_TIMEZONE)

    now = datetime.now(tz).time()
    start = _parse_time(config.QUIET_HOURS_START)
    end = _parse_time(config.QUIET_HOURS_END)

    if start <= end:
        return start <= now < end
    else:
        return now >= start or now < end


def is_quiet() -> bool:
    """Check if Artemis should be in quiet mode.

    Priority:
    1. override_active=1 → NOT quiet (working session)
    2. manual_override=1 AND is_quiet=1 → quiet (user said goodnight)
    3. manual_override=1 AND is_quiet=0 → NOT quiet (user said good morning)
    4. No manual override → check time-based window
    """
    state = _get_quiet_row()
    if state:
        if state.get("override_active"):
            return False  # Working session overrides everything
        if state.get("manual_override"):
            return bool(state.get("is_quiet"))
    # Fall through to time-based check
    return _is_in_time_window()


# Backward compatibility alias
is_quiet_hours = is_quiet


def get_quiet_state() -> dict:
    """Get the full quiet state as a dict."""
    state = _get_quiet_row()
    if state:
        return state
    return {
        "is_quiet": 0,
        "manual_override": 0,
        "wake_time": None,
        "override_active": 0,
        "override_until": None,
        "last_interaction": None,
    }


def enter_quiet(manual: bool = False, wake_time: str | None = None) -> str:
    """Enter quiet mode. Called by cron job or manually via goodnight.

    Returns an announcement string.
    """
    _upsert_quiet_state(
        is_quiet=1,
        manual_override=1 if manual else 0,
        wake_time=wake_time,
        override_active=0,
        override_until=None,
    )

    tz_abbrev = get_tz_abbrev()
    if wake_time:
        wake_display = _parse_time(wake_time).strftime("%I:%M %p").lstrip("0")
        return (
            f"\U0001f319 Goodnight \u2014 going quiet. Jobs paused. "
            f"I'll resume at {wake_display} {tz_abbrev} or when you say good morning."
        )

    end_display = _parse_time(config.QUIET_HOURS_END).strftime("%I:%M %p").lstrip("0")
    if manual:
        return (
            f"\U0001f319 Goodnight \u2014 going quiet. Jobs paused. "
            f"I'll resume at {end_display} {tz_abbrev} or when you say good morning."
        )

    # Automatic (cron-triggered)
    return (
        f"\U0001f319 Artemis entering quiet hours \u2014 scheduled jobs paused "
        f"until {end_display} {tz_abbrev}."
    )


def exit_quiet() -> str:
    """Exit quiet mode. Called by cron job or manually via good morning.

    Returns an announcement string (caller adds overnight summary).
    """
    _upsert_quiet_state(
        is_quiet=0,
        manual_override=0,
        wake_time=None,
        override_active=0,
        override_until=None,
    )
    return ""  # Caller builds the full morning summary


def start_override(until_time: str | None = None) -> str:
    """Start a working session override — suspend quiet hours.

    Returns a confirmation string.
    """
    _upsert_quiet_state(
        override_active=1,
        override_until=until_time,
        last_interaction=_now_utc_iso(),
    )

    if until_time:
        tz_abbrev = get_tz_abbrev()
        display = _parse_time(until_time).strftime("%I:%M %p").lstrip("0")
        return f"\u26a1 Active until {display} {tz_abbrev}. Let's work."

    timeout = config.OVERRIDE_TIMEOUT_MINUTES
    return (
        f"\u26a1 Working session started. I'll go quiet after {timeout} minutes "
        f"of inactivity. Say `@artemis extend` to reset the timer or "
        f"`@artemis goodnight` when done."
    )


def extend_override() -> str:
    """Reset the inactivity timer on the working session override."""
    _upsert_quiet_state(last_interaction=_now_utc_iso())
    timeout = config.OVERRIDE_TIMEOUT_MINUTES
    return f"\u23f1 Timer reset \u2014 going quiet after {timeout} min of inactivity."


def check_override_expiry() -> str | None:
    """Check if a working session override has expired due to inactivity or time limit.

    Returns announcement text if expired, None otherwise.
    Called by the 1-minute scheduler job.
    """
    state = _get_quiet_row()
    if not state or not state.get("override_active"):
        return None

    now = datetime.utcnow()

    # Check time-based override limit (override until X)
    until = state.get("override_until")
    if until:
        tz_name = get_active_timezone()
        try:
            tz = ZoneInfo(tz_name)
        except (KeyError, ValueError):
            tz = ZoneInfo(config.HOME_TIMEZONE)
        local_now = datetime.now(tz).time()
        until_time = _parse_time(until)
        if local_now >= until_time:
            _upsert_quiet_state(override_active=0, override_until=None, is_quiet=1)
            return (
                f"\U0001f319 Working session ended (reached {until}). Going quiet. "
                f"Say `@artemis override` to keep working."
            )

    # Check inactivity timeout
    last = state.get("last_interaction")
    if last:
        try:
            last_dt = datetime.fromisoformat(last)
            elapsed = (now - last_dt).total_seconds() / 60
            if elapsed >= config.OVERRIDE_TIMEOUT_MINUTES:
                _upsert_quiet_state(override_active=0, override_until=None, is_quiet=1)
                return (
                    f"\U0001f319 No activity for {config.OVERRIDE_TIMEOUT_MINUTES} minutes "
                    f"\u2014 going quiet. Say `@artemis override` to keep working."
                )
        except (ValueError, TypeError):
            pass

    return None


def update_last_interaction() -> None:
    """Record an @mention interaction for inactivity tracking."""
    state = _get_quiet_row()
    if state and state.get("override_active"):
        _upsert_quiet_state(last_interaction=_now_utc_iso())


# ---------------------------------------------------------------------------
# Status display
# ---------------------------------------------------------------------------


def quiet_hours_status() -> str:
    """Return a formatted status string for quiet hours and session state."""
    tz_name = get_active_timezone()
    tz_abbrev = get_tz_abbrev(tz_name)
    start_str = _parse_time(config.QUIET_HOURS_START).strftime("%I:%M %p").lstrip("0")
    end_str = _parse_time(config.QUIET_HOURS_END).strftime("%I:%M %p").lstrip("0")
    state = get_quiet_state()

    lines = []

    # Current quiet state
    if state.get("override_active"):
        lines.append(f"\u26a1 Working session active. Quiet hours window: {start_str} - {end_str} {tz_abbrev}.")
        until = state.get("override_until")
        if until:
            display = _parse_time(until).strftime("%I:%M %p").lstrip("0")
            lines.append(f"\u23f1 Active until {display} {tz_abbrev}.")
        else:
            lines.append(f"\u23f1 {config.OVERRIDE_TIMEOUT_MINUTES}-min inactivity timer running.")
    elif is_quiet():
        if state.get("manual_override"):
            lines.append(f"\U0001f319 Quiet (manual goodnight). Window: {start_str} - {end_str} {tz_abbrev}.")
            wake = state.get("wake_time")
            if wake:
                wake_display = _parse_time(wake).strftime("%I:%M %p").lstrip("0")
                lines.append(f"Wake time: {wake_display} {tz_abbrev}.")
        else:
            lines.append(f"\U0001f319 Quiet hours active ({start_str} - {end_str} {tz_abbrev}). Scheduled jobs paused.")
    else:
        lines.append(f"\u2600\ufe0f Outside quiet hours ({start_str} - {end_str} {tz_abbrev}).")

    # Timezone override
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
                lines.append(f"\U0001f30d Timezone: {city} ({row['timezone']}) until {expires_str}.")
    except Exception:
        pass

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Timezone override CRUD (unchanged)
# ---------------------------------------------------------------------------


def set_timezone_override(tz_name: str, city_name: str = "", days: int = 7) -> str:
    """Set a timezone override that expires after `days` days."""
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
        return "\u26a0\ufe0f Failed to set timezone override \u2014 check logs."

    display_city = city_name.title() if city_name else tz_name
    return (
        f"\U0001f30d Got it \u2014 quiet hours adjusted to {start_str} - {end_str} {tz_abbrev} "
        f"through {expires_str}. All times in your briefs will reflect {display_city} time."
    )


def clear_timezone_override() -> str:
    """Clear the active timezone override."""
    try:
        conn = get_db()
        conn.execute("DELETE FROM timezone_overrides WHERE id = 1")
        conn.commit()
    except Exception:
        logger.exception("Failed to clear timezone override")
        return "\u26a0\ufe0f Failed to clear timezone override \u2014 check logs."

    home_abbrev = get_tz_abbrev(config.HOME_TIMEZONE)
    return f"\U0001f3e0 Timezone reset to {config.HOME_TIMEZONE} ({home_abbrev}). Welcome back!"


def check_expired_overrides() -> str | None:
    """Check if the timezone override has expired. Returns announcement or None."""
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
