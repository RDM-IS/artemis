"""Day phases, quiet-hours state, and the active timezone (WAKE-1).

State-based phase system with manual goodnight/morning, working-session
overrides with inactivity timers, and timezone overrides. Backed by RDS
(acos.system_state / acos.quiet_state / acos.timezone_overrides, migration 019)
via knowledge.db; no SQLite remains in this module.

DAY PHASES — local wall-clock in the ACTIVE timezone:

    quiet  QUIET_HOURS_START (17:00) .. WAKE_TIME (04:30)  nothing proactive
    wake   WAKE_TIME (04:30) .. OPEN_TIME (06:30)          health + pre-departure
    open   OPEN_TIME (06:30) .. QUIET_HOURS_START          everything

Saturday and Sunday use the WEEKEND_* times (wake 07:30, open 08:30, quiet
22:30). Each boundary belongs to the local date it falls on, so Friday night
goes quiet at 17:00 and Saturday wakes at 07:30; Sunday night goes quiet at
22:30 and Monday wakes at 04:30.

Timezone handling is the module's own: get_active_timezone() resolves the
override (if set and unexpired) else config.HOME_TIMEZONE, and every wall-clock
window check goes through it. Stored instants are TIMESTAMPTZ, so override-expiry
and inactivity-elapsed comparisons are absolute and unambiguous.

local_tz() / local_today() / local_now() are THE date helpers for anything that
means "today for Ryan" — the schedule, health day math, and SQL date anchors all
follow the override. Only genuinely fixed anchors (config defaults, historical
migrations) keep a literal zone.
"""

import logging
import re
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from artemis import config
from knowledge.db import execute_one, execute_write

logger = logging.getLogger(__name__)

PHASE_QUIET = "quiet"
PHASE_WAKE = "wake"
PHASE_OPEN = "open"

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
    "são paulo": "America/Sao_Paulo",
    "rio": "America/Sao_Paulo",
    "rio de janeiro": "America/Sao_Paulo",
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

# Country → PRIMARY zone. Multi-zone countries are flagged so the reply names the
# zone and Ryan can correct it with an exact IANA name.
_COUNTRY_TIMEZONES: dict[str, str] = {
    "brazil": "America/Sao_Paulo",
    "france": "Europe/Paris",
    "spain": "Europe/Madrid",
    "italy": "Europe/Rome",
    "germany": "Europe/Berlin",
    "uk": "Europe/London",
    "england": "Europe/London",
    "united kingdom": "Europe/London",
    "ireland": "Europe/Dublin",
    "portugal": "Europe/Lisbon",
    "netherlands": "Europe/Amsterdam",
    "holland": "Europe/Amsterdam",
    "japan": "Asia/Tokyo",
    "mexico": "America/Mexico_City",
    "canada": "America/Toronto",
}

# US zone words.
_US_ZONE_WORDS: dict[str, str] = {
    "central": "America/Chicago",
    "eastern": "America/New_York",
    "mountain": "America/Denver",
    "pacific": "America/Los_Angeles",
}

# Countries spanning several zones — the reply names the chosen primary zone.
_MULTI_ZONE_PLACES = {
    "brazil", "canada", "mexico", "us", "usa", "united states", "australia", "russia",
}

# Words that carry no zone information; stripped before lookup so
# "central chicago" → chicago and "paris france" → paris.
_FILLER_WORDS = {"the", "city", "of", "in", "time", "timezone", "tz", "zone"}


def _parse_time(t: str) -> time:
    """Parse 'HH:MM' to time object."""
    parts = t.strip().split(":")
    return time(int(parts[0]), int(parts[1]))


# ---------------------------------------------------------------------------
# Active timezone + local date helpers (THE anchors for "today")
# ---------------------------------------------------------------------------


def get_active_timezone() -> str:
    """Return the active timezone — override if set and not expired, else HOME_TIMEZONE.

    The not-expired check is an absolute-instant comparison done in SQL
    (expires_at > now()) on the TIMESTAMPTZ column — tz-agnostic and unambiguous.
    """
    try:
        row = execute_one(
            "SELECT timezone FROM acos.timezone_overrides WHERE id = 1 AND expires_at > now()"
        )
        if row:
            return row["timezone"]
    except Exception:
        logger.debug("Failed to check timezone override", exc_info=True)

    return config.HOME_TIMEZONE


def local_tz() -> ZoneInfo:
    """ZoneInfo for the active timezone (falls back to HOME on a bad name)."""
    name = get_active_timezone()
    try:
        return ZoneInfo(name)
    except (KeyError, ValueError):
        logger.warning("Unknown active timezone %r — falling back to HOME", name)
        return ZoneInfo(config.HOME_TIMEZONE)


def local_now() -> datetime:
    """Now, as a tz-aware datetime in the active timezone."""
    return datetime.now(local_tz())


def local_today() -> date:
    """Today's date for Ryan — in the ACTIVE timezone, not the box's."""
    return local_now().date()


def get_tz_abbrev(tz_name: str | None = None) -> str:
    """Get timezone abbreviation (e.g., CDT, CET) for the active or specified timezone."""
    tz_name = tz_name or get_active_timezone()
    try:
        tz = ZoneInfo(tz_name)
        return datetime.now(tz).strftime("%Z")
    except Exception:
        return "???"


# ---------------------------------------------------------------------------
# Place / date parsing for the timezone command
# ---------------------------------------------------------------------------


def _normalize_place(text: str) -> str:
    s = (text or "").strip().lower()
    s = s.strip(".,!?;:")
    s = re.sub(r"\s+", " ", s)
    return s


def _lookup_place(token: str) -> tuple[str | None, bool]:
    """Exact lookup in city → country → US word → raw IANA. Returns (tz, multi)."""
    if not token:
        return None, False
    if token in _CITY_TIMEZONES:
        return _CITY_TIMEZONES[token], token in _MULTI_ZONE_PLACES
    if token in _COUNTRY_TIMEZONES:
        return _COUNTRY_TIMEZONES[token], token in _MULTI_ZONE_PLACES
    if token in _US_ZONE_WORDS:
        return _US_ZONE_WORDS[token], False
    try:
        ZoneInfo(token)
        return token, False
    except (KeyError, ValueError):
        pass
    # IANA names are case-sensitive; try the original casing of a slashed token.
    return None, False


def resolve_place_timezone(text: str) -> tuple[str | None, str, bool]:
    """Resolve a place to (iana_tz | None, label, is_multi_zone).

    Order: city map → country map → US zone word → raw IANA. Filler words are
    stripped and multi-word inputs are narrowed ("central chicago" → chicago,
    "paris france" → paris) so a qualifier never blocks the match.
    """
    raw = (text or "").strip()
    if not raw:
        return None, "", False

    # Raw IANA first, preserving case ("America/Manaus").
    try:
        ZoneInfo(raw)
        return raw, raw, False
    except (KeyError, ValueError):
        pass

    norm = _normalize_place(raw)
    tz, multi = _lookup_place(norm)
    if tz:
        return tz, norm, multi

    words = [w for w in norm.split(" ") if w and w not in _FILLER_WORDS]
    # Longest contiguous runs first, then single words (last word wins for
    # "paris france"-style inputs; a leading US zone word yields to the city).
    for size in range(len(words), 0, -1):
        for start in range(0, len(words) - size + 1):
            token = " ".join(words[start:start + size])
            tz, multi = _lookup_place(token)
            if tz:
                return tz, token, multi
    return None, norm, False


_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}


def parse_date_token(text: str, today: date | None = None) -> date | None:
    """Parse 9/23, 9/23/26, 9/23/2026, 2026-09-23, 'Sep 23', 'September 23'.

    A year-less date rolls forward to the next occurrence on or after `today`.
    Returns None when nothing parses.
    """
    s = _normalize_place(text)
    if not s:
        return None
    today = today or local_today()

    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None

    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})(?:/(\d{2}|\d{4}))?", s)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        year_s = m.group(3)
        try:
            if year_s:
                year = int(year_s)
                if year < 100:
                    year += 2000
                return date(year, month, day)
            return _roll_forward(month, day, today)
        except ValueError:
            return None

    m = re.fullmatch(r"([a-z]+)\.?\s+(\d{1,2})(?:,?\s*(\d{4}))?", s)
    if m and m.group(1) in _MONTHS:
        month, day = _MONTHS[m.group(1)], int(m.group(2))
        try:
            if m.group(3):
                return date(int(m.group(3)), month, day)
            return _roll_forward(month, day, today)
        except ValueError:
            return None

    # Day-first: "23 Sep", "23 September 2026".
    m = re.fullmatch(r"(\d{1,2})\s+([a-z]+)\.?(?:,?\s*(\d{4}))?", s)
    if m and m.group(2) in _MONTHS:
        day, month = int(m.group(1)), _MONTHS[m.group(2)]
        try:
            if m.group(3):
                return date(int(m.group(3)), month, day)
            return _roll_forward(month, day, today)
        except ValueError:
            return None
    return None


def _roll_forward(month: int, day: int, today: date) -> date:
    candidate = date(today.year, month, day)
    if candidate < today:
        candidate = date(today.year + 1, month, day)
    return candidate


def expires_at_for_through(through: date, tz_name: str) -> datetime:
    """Midnight starting the day AFTER `through`, in the override's timezone.

    'through 9/23' must cover all of 9/23 local-away; 9/24 runs on home time.
    """
    tz = ZoneInfo(tz_name)
    return datetime.combine(through + timedelta(days=1), time(0, 0), tzinfo=tz)


def expires_at_for_days(days: int, tz_name: str, today: date | None = None) -> datetime:
    """Midnight local-away on today+days."""
    tz = ZoneInfo(tz_name)
    base = today or datetime.now(tz).date()
    return datetime.combine(base + timedelta(days=days), time(0, 0), tzinfo=tz)


def resolve_city_timezone(city_or_tz: str) -> str | None:
    """Back-compat wrapper — returns just the IANA zone, or None."""
    tz, _label, _multi = resolve_place_timezone(city_or_tz)
    return tz


# ---------------------------------------------------------------------------
# System state (generic key-value store)
# ---------------------------------------------------------------------------


def get_system_value(key: str) -> str | None:
    """Read a value from the system_state table."""
    try:
        row = execute_one("SELECT value FROM acos.system_state WHERE key = %s", (key,))
        return row["value"] if row else None
    except Exception:
        logger.debug("Failed to read system_state[%s]", key, exc_info=True)
        return None


def set_system_value(key: str, value: str) -> None:
    """Upsert a value into the system_state table."""
    try:
        execute_write(
            "INSERT INTO acos.system_state (key, value, updated_at) VALUES (%s, %s, now()) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
            (key, value),
        )
    except Exception:
        logger.exception("Failed to write system_state[%s]", key)


# ---------------------------------------------------------------------------
# Quiet state management
# ---------------------------------------------------------------------------


def _get_quiet_row() -> dict | None:
    """Read the quiet_state singleton row. Returns dict or None."""
    try:
        return execute_one("SELECT * FROM acos.quiet_state WHERE id = 1")
    except Exception:
        logger.debug("Failed to read quiet_state", exc_info=True)
        return None


def _upsert_quiet_state(**kwargs) -> None:
    """Insert or update the quiet_state singleton (id = 1) via ON CONFLICT.

    updated_at is always server-side now(); only the columns the caller passes
    are written (others keep their value on update, or take their default on
    first insert). last_interaction is passed as a tz-aware datetime by callers.
    """
    cols = list(kwargs.keys())
    insert_cols = ["id"] + cols + ["updated_at"]
    insert_vals = ["1"] + ["%s"] * len(cols) + ["now()"]
    set_parts = [f"{c} = EXCLUDED.{c}" for c in cols] + ["updated_at = now()"]
    sql = (
        f"INSERT INTO acos.quiet_state ({', '.join(insert_cols)}) "
        f"VALUES ({', '.join(insert_vals)}) "
        f"ON CONFLICT (id) DO UPDATE SET {', '.join(set_parts)}"
    )
    try:
        execute_write(sql, tuple(kwargs.values()))
    except Exception:
        logger.exception("Failed to upsert quiet_state")


def is_weekend(d: date) -> bool:
    """Calendar weekend. NOT a schedule input any more — CYCLE-1 made the day
    boundaries depend on location and day type, not on Sat/Sun. Kept only for
    callers that genuinely mean "is it the weekend" (availability heuristics).
    """
    return d.weekday() in config.WEEKEND_DAYS


def wake_time_on(d: date) -> time:
    """Wake boundary on local date `d`. CYCLE-1: follows the day's LOCATION."""
    from artemis import cycle
    return cycle.wake_on(d)


def open_time_on(d: date) -> time:
    """Business-open boundary on local date `d`. CYCLE-1: follows the DAY TYPE."""
    from artemis import cycle
    return cycle.open_on(d)


def quiet_start_on(d: date) -> time:
    """Quiet-hours start on local date `d`. CYCLE-1: follows the DAY TYPE."""
    from artemis import cycle
    return cycle.quiet_on(d)


def next_wake(now: datetime | None = None) -> datetime:
    """The next scheduled wake instant (local), weekend-aware."""
    now = now or local_now()
    for offset in (0, 1, 2):
        d = now.date() + timedelta(days=offset)
        cand = datetime.combine(d, wake_time_on(d), tzinfo=now.tzinfo)
        if cand > now:
            return cand
    raise AssertionError("unreachable")


def next_open(now: datetime | None = None) -> datetime:
    """The next business-open instant (local), weekend-aware."""
    now = now or local_now()
    for offset in (0, 1, 2):
        d = now.date() + timedelta(days=offset)
        cand = datetime.combine(d, open_time_on(d), tzinfo=now.tzinfo)
        if cand > now:
            return cand
    raise AssertionError("unreachable")


def _is_in_time_window() -> bool:
    """True when the local wall clock is inside the QUIET window.

    Quiet runs from the evening boundary of date D to the wake boundary of D+1,
    so for a given local date it is: before that date's wake, or at/after that
    date's quiet start."""
    now = local_now()
    t, d = now.time(), now.date()
    return t < wake_time_on(d) or t >= quiet_start_on(d)


def _time_phase() -> str:
    """Phase from the clock alone (no manual/working-session overrides)."""
    if _is_in_time_window():
        return PHASE_QUIET
    now = local_now()
    return PHASE_WAKE if now.time() < open_time_on(now.date()) else PHASE_OPEN


def _boundaries(state: dict | None, d: date | None = None) -> list[time]:
    """Phase boundaries on local date `d` (default today), including a
    goodnight's custom wake time."""
    d = d or local_today()
    bounds = [wake_time_on(d), open_time_on(d), quiet_start_on(d)]
    wake = (state or {}).get("wake_time")
    if wake:
        try:
            bounds.append(_parse_time(wake))
        except (ValueError, IndexError):
            pass
    return sorted(set(bounds))


def _manual_expired(state: dict) -> bool:
    """True when a phase boundary has passed since the manual state was set.

    A goodnight must not stick past the wake time, and a good-morning must not
    hold the wake phase past 06:30.
    """
    set_at = state.get("updated_at")
    if not set_at:
        return False
    tz = local_tz()
    try:
        if isinstance(set_at, str):
            set_at = datetime.fromisoformat(set_at)
        if set_at.tzinfo is None:
            set_at = set_at.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return False

    set_local = set_at.astimezone(tz)
    now_local = local_now()
    # Any boundary strictly after set_at that has already passed. Boundaries
    # are per local date (weekends differ), so check the set day and the next.
    for offset in (0, 1):
        d = set_local.date() + timedelta(days=offset)
        for b in _boundaries(state, d):
            candidate = datetime.combine(d, b, tzinfo=tz)
            if set_local < candidate <= now_local:
                return True
    return False


def get_phase() -> str:
    """Current day phase: "quiet" | "wake" | "open" (active timezone).

    Priority:
      1. working session (override_active)     → open
      2. manual goodnight / good-morning       → until the next phase boundary
      3. the clock
    """
    state = _get_quiet_row()
    if state:
        if state.get("override_active"):
            return PHASE_OPEN
        if state.get("manual_override") and not _manual_expired(state):
            if state.get("is_quiet"):
                return PHASE_QUIET
            # Good morning: wake until the day's open time, then open.
            now = local_now()
            return PHASE_WAKE if now.time() < open_time_on(now.date()) else PHASE_OPEN
    return _time_phase()


def is_quiet() -> bool:
    """True only in the quiet phase (nothing proactive posts)."""
    state = _get_quiet_row()
    if state:
        if state.get("override_active"):
            return False  # Working session overrides everything
        if state.get("manual_override") and not _manual_expired(state):
            return bool(state.get("is_quiet"))
    # Fall through to time-based check
    return _is_in_time_window()


def is_open() -> bool:
    """True when business-tier posting is allowed."""
    return get_phase() == PHASE_OPEN


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
            f"\U0001f319 Goodnight — going quiet. Jobs paused. "
            f"I'll resume at {wake_display} {tz_abbrev} or when you say good morning."
        )

    wake_display = next_wake().strftime("%I:%M %p").lstrip("0")
    if manual:
        return (
            f"\U0001f319 Goodnight — going quiet. Jobs paused. "
            f"I'll resume at {wake_display} {tz_abbrev} or when you say good morning."
        )

    # Automatic (cron-triggered)
    return (
        f"\U0001f319 Artemis entering quiet hours — scheduled jobs paused "
        f"until {wake_display} {tz_abbrev}."
    )


def exit_quiet() -> str:
    """Exit quiet mode. Called by the wake job or manually via good morning.

    Returns an announcement string (caller adds the wake/overnight content).
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
        last_interaction=datetime.now(timezone.utc),
    )

    if until_time:
        tz_abbrev = get_tz_abbrev()
        display = _parse_time(until_time).strftime("%I:%M %p").lstrip("0")
        return f"⚡ Active until {display} {tz_abbrev}. Let's work."

    timeout = config.OVERRIDE_TIMEOUT_MINUTES
    return (
        f"⚡ Working session started. I'll go quiet after {timeout} minutes "
        f"of inactivity. Say `@artemis extend` to reset the timer or "
        f"`@artemis goodnight` when done."
    )


def extend_override() -> str:
    """Reset the inactivity timer on the working session override."""
    _upsert_quiet_state(last_interaction=datetime.now(timezone.utc))
    timeout = config.OVERRIDE_TIMEOUT_MINUTES
    return f"⏱ Timer reset — going quiet after {timeout} min of inactivity."


def check_override_expiry() -> str | None:
    """Check if a working session override has expired due to inactivity or time limit.

    Returns announcement text if expired, None otherwise.
    Called by the 1-minute scheduler job.
    """
    state = _get_quiet_row()
    if not state or not state.get("override_active"):
        return None

    # Check time-based override limit (override until X) — wall-clock comparison
    # in the ACTIVE timezone.
    until = state.get("override_until")
    if until:
        local_now_time = local_now().time()
        until_time = _parse_time(until)
        if local_now_time >= until_time:
            _upsert_quiet_state(override_active=0, override_until=None, is_quiet=1)
            return (
                f"\U0001f319 Working session ended (reached {until}). Going quiet. "
                f"Say `@artemis override` to keep working."
            )

    # Check inactivity timeout — absolute elapsed time. last_interaction is a
    # TIMESTAMPTZ (psycopg2 → tz-aware datetime); compare against aware now.
    last = state.get("last_interaction")
    if last:
        try:
            if isinstance(last, str):
                last = datetime.fromisoformat(last)
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            elapsed = (datetime.now(timezone.utc) - last).total_seconds() / 60
            if elapsed >= config.OVERRIDE_TIMEOUT_MINUTES:
                _upsert_quiet_state(override_active=0, override_until=None, is_quiet=1)
                return (
                    f"\U0001f319 No activity for {config.OVERRIDE_TIMEOUT_MINUTES} minutes "
                    f"— going quiet. Say `@artemis override` to keep working."
                )
        except (ValueError, TypeError):
            pass

    return None


def update_last_interaction() -> None:
    """Record an @mention interaction for inactivity tracking."""
    state = _get_quiet_row()
    if state and state.get("override_active"):
        _upsert_quiet_state(last_interaction=datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# Status display
# ---------------------------------------------------------------------------


def phase_summary() -> str:
    """One line describing the phase windows in the active timezone."""
    tz_abbrev = get_tz_abbrev()
    f = lambda v: _parse_time(v).strftime("%H:%M")  # noqa: E731
    return (f"Weekdays: wake {f(config.WAKE_TIME)} · business {f(config.OPEN_TIME)} · "
            f"quiet {f(config.QUIET_HOURS_START)} — Sat/Sun: wake {f(config.WEEKEND_WAKE_TIME)} · "
            f"business {f(config.WEEKEND_OPEN_TIME)} · quiet {f(config.WEEKEND_QUIET_HOURS_START)} "
            f"({tz_abbrev})")


def quiet_hours_status() -> str:
    """Return a formatted status string for phase, session state, and timezone."""
    tz_name = get_active_timezone()
    tz_abbrev = get_tz_abbrev(tz_name)
    phase = get_phase()
    state = get_quiet_state()
    now_str = local_now().strftime("%H:%M")

    icon = {PHASE_QUIET: "\U0001f319", PHASE_WAKE: "\U0001f305", PHASE_OPEN: "☀️"}[phase]
    lines = [f"{icon} Phase: **{phase}** — {now_str} {tz_abbrev}. {phase_summary()}."]

    if state.get("override_active"):
        until = state.get("override_until")
        if until:
            display = _parse_time(until).strftime("%I:%M %p").lstrip("0")
            lines.append(f"⚡ Working session active until {display} {tz_abbrev}.")
        else:
            lines.append(
                f"⚡ Working session active — "
                f"{config.OVERRIDE_TIMEOUT_MINUTES}-min inactivity timer running."
            )
    elif state.get("manual_override"):
        wake = state.get("wake_time")
        if wake:
            wake_display = _parse_time(wake).strftime("%I:%M %p").lstrip("0")
            lines.append(f"\U0001f319 Manual goodnight — wake at {wake_display} {tz_abbrev}.")
        elif state.get("is_quiet"):
            lines.append("\U0001f319 Manual goodnight — until the next phase boundary.")
        else:
            lines.append("☀️ Manual good-morning — until the next phase boundary.")

    # Timezone override — show only if still active (expiry filtered in SQL).
    try:
        row = execute_one(
            "SELECT timezone, city_name, expires_at FROM acos.timezone_overrides "
            "WHERE id = 1 AND expires_at > now()"
        )
        if row:
            label = (row["city_name"] or row["timezone"]).title()
            through = (row["expires_at"].astimezone(ZoneInfo(row["timezone"]))
                       - timedelta(days=0)).date() - timedelta(days=1)
            lines.append(
                f"\U0001f30d Timezone: {label} ({row['timezone']}) through "
                f"{through.strftime('%a %b %d')}."
            )
        else:
            lines.append(f"\U0001f3e0 Timezone: home ({config.HOME_TIMEZONE}).")
    except Exception:
        pass

    try:
        from artemis.posting import holds_summary
        held = holds_summary()
        if held:
            lines.append(held)
    except Exception:
        logger.debug("holds summary unavailable", exc_info=True)

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Timezone override CRUD
# ---------------------------------------------------------------------------

DEFAULT_OVERRIDE_DAYS = 7

# A year-less date is rolled FORWARD (9/23 typed in January means this year;
# 1/5 typed in September means next January). That rule and "a past date is
# rejected" collide on a date just behind today: "through 9/14" typed on 9/15
# rolls to 9/14 NEXT year and would silently pin the schedule away for a year.
# Beyond this horizon we assume a typo for a nearby past date and say so.
MAX_OVERRIDE_DAYS = 180


def get_timezone_override() -> dict | None:
    """The active override row, or None."""
    try:
        return execute_one(
            "SELECT timezone, city_name, expires_at, set_at FROM acos.timezone_overrides "
            "WHERE id = 1 AND expires_at > now()"
        )
    except Exception:
        logger.debug("Failed to read timezone override", exc_info=True)
        return None


def set_timezone_override(tz_name: str, label: str = "", expires_at: datetime | None = None) -> str:
    """Set a timezone override that ends at the absolute instant `expires_at`.

    `label` is the human place name (stored in city_name). When expires_at is
    omitted the default 7-day window applies, counted in the override's own zone.
    """
    if expires_at is None:
        expires_at = expires_at_for_days(DEFAULT_OVERRIDE_DAYS, tz_name)

    try:
        execute_write(
            "INSERT INTO acos.timezone_overrides (id, timezone, expires_at, city_name) "
            "VALUES (1, %s, %s, %s) "
            "ON CONFLICT (id) DO UPDATE SET timezone = EXCLUDED.timezone, "
            "expires_at = EXCLUDED.expires_at, city_name = EXCLUDED.city_name, set_at = now()",
            (tz_name, expires_at, label or tz_name),
        )
    except Exception:
        logger.exception("Failed to set timezone override")
        return "⚠️ Failed to set timezone override — check logs."

    return ""  # the caller (main._handle_timezone_command) renders the reply


def clear_timezone_override() -> str:
    """Clear the active timezone override."""
    try:
        execute_write("DELETE FROM acos.timezone_overrides WHERE id = 1")
    except Exception:
        logger.exception("Failed to clear timezone override")
        return "⚠️ Failed to clear timezone override — check logs."

    home_abbrev = get_tz_abbrev(config.HOME_TIMEZONE)
    return (
        f"\U0001f3e0 Timezone reset to {config.HOME_TIMEZONE} ({home_abbrev}). "
        f"{phase_summary()}. Welcome back!"
    )


def check_expired_overrides() -> str | None:
    """Check if the timezone override has expired. Returns announcement or None.

    Atomic: delete the row iff it is expired (expires_at <= now(), an absolute
    instant comparison in SQL) and announce only when a row was actually removed.
    """
    try:
        row = execute_write(
            "DELETE FROM acos.timezone_overrides WHERE id = 1 AND expires_at <= now() "
            "RETURNING timezone, city_name"
        )
        if row:
            home_abbrev = get_tz_abbrev(config.HOME_TIMEZONE)
            return (
                f"\U0001f30d Timezone override expired — reverting to {config.HOME_TIMEZONE} "
                f"({home_abbrev}). {phase_summary()}. If you're still traveling, let me know."
            )
    except Exception:
        logger.exception("Failed to check timezone override expiry")

    return None
