import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _list(val: str) -> list[str]:
    return [v.strip() for v in val.split(",") if v.strip()]


def _domain_expiry_map(val: str) -> dict[str, str]:
    """Parse 'domain:date,domain:date' into a dict."""
    result = {}
    for pair in _list(val):
        if ":" in pair:
            domain, date = pair.split(":", 1)
            result[domain.strip()] = date.strip()
    return result


# Anthropic
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# Mattermost
MATTERMOST_URL = os.environ.get("MATTERMOST_URL", "http://localhost:8065")
MATTERMOST_BOT_TOKEN = os.environ.get("MATTERMOST_BOT_TOKEN", "")
MATTERMOST_TEAM_ID = os.environ.get("MATTERMOST_TEAM_ID", "")
CHANNEL_OPS = os.environ.get("CHANNEL_OPS", "artemis-ryan")
CHANNEL_BRIEFS = os.environ.get("CHANNEL_BRIEFS", "artemis-ryan")
CHANNEL_COMMITMENTS = os.environ.get("CHANNEL_COMMITMENTS", "artemis-ryan")

# Gmail
GMAIL_CREDENTIALS_PATH = Path(os.environ.get("GMAIL_CREDENTIALS_PATH", "credentials.json"))
GMAIL_TOKEN_PATH = Path(os.environ.get("GMAIL_TOKEN_PATH", "token.json"))

# Calendar
CALENDAR_CREDENTIALS_PATH = Path(os.environ.get("CALENDAR_CREDENTIALS_PATH", "credentials.json"))
CALENDAR_TOKEN_PATH = Path(os.environ.get("CALENDAR_TOKEN_PATH", "token.json"))

# Timezone
TIMEZONE = os.environ.get("TIMEZONE", "America/Chicago")

# Scheduling
BRIEF_LEAD_TIME_MINUTES = int(os.environ.get("BRIEF_LEAD_TIME_MINUTES", "90"))
MORNING_BRIEF_TIME = os.environ.get("MORNING_BRIEF_TIME", "07:30")

# Monitoring
MONITORED_DOMAINS = _list(os.environ.get("MONITORED_DOMAINS", ""))
DOMAIN_EXPIRY_DATES = _domain_expiry_map(os.environ.get("DOMAIN_EXPIRY_DATES", ""))

# Priority contacts
PRIORITY_CONTACTS = _list(os.environ.get("PRIORITY_CONTACTS", ""))

# Focus client (e.g. Titanium/TTI)
FOCUS_CLIENT = os.environ.get("FOCUS_CLIENT", "")
FOCUS_KEYWORDS = _list(os.environ.get("FOCUS_KEYWORDS", ""))

# Startup
STARTUP_RETRY_COUNT = int(os.environ.get("STARTUP_RETRY_COUNT", "10"))
STARTUP_RETRY_DELAY = int(os.environ.get("STARTUP_RETRY_DELAY", "15"))

# Tailscale
TAILSCALE_HOSTNAME = os.environ.get("TAILSCALE_HOSTNAME", "")

# CRM API
CRM_API_URL = os.environ.get("CRM_API_URL", "")
CRM_API_KEY = os.environ.get("CRM_API_KEY", "")

# Playbooks
PLAYBOOKS_PATH = Path(os.environ.get("PLAYBOOKS_PATH", "PLAYBOOKS.md"))

# Availability / Meeting Preferences (PB-006)
MEETING_HOURS_START = os.environ.get("MEETING_HOURS_START", "09:00")
MEETING_HOURS_END = os.environ.get("MEETING_HOURS_END", "17:00")
MEETING_BUFFER_MINUTES = int(os.environ.get("MEETING_BUFFER_MINUTES", "15"))
PREFERRED_MEETING_DAYS = _list(os.environ.get("PREFERRED_MEETING_DAYS", "Mon,Tue,Wed,Thu,Fri"))
FOCUS_BLOCK_KEYWORDS = _list(os.environ.get("FOCUS_BLOCK_KEYWORDS", "focus,deep work,work session"))
BOOKING_LINK = os.environ.get("BOOKING_LINK", "https://calendar.app.google/W21n5XJQ1CUcGkLM9")
DEFAULT_SLOT_DURATION = int(os.environ.get("DEFAULT_SLOT_DURATION", "30"))
DEFAULT_NUM_SLOTS = int(os.environ.get("DEFAULT_NUM_SLOTS", "3"))

# Per-day MEETING availability (external, requires another person)
# "HH:MM-HH:MM", "avoid" (use with warning), or "unavailable"
MEETING_MONDAY = os.environ.get("MEETING_MONDAY", "07:00-18:00")
MEETING_TUESDAY = os.environ.get("MEETING_TUESDAY", "avoid")
MEETING_WEDNESDAY = os.environ.get("MEETING_WEDNESDAY", "15:00-18:00")
MEETING_THURSDAY = os.environ.get("MEETING_THURSDAY", "15:00-18:00")
MEETING_FRIDAY = os.environ.get("MEETING_FRIDAY", "avoid")
MEETING_SATURDAY = os.environ.get("MEETING_SATURDAY", "unavailable")
MEETING_SUNDAY = os.environ.get("MEETING_SUNDAY", "unavailable")
MEETING_AVOID_DAYS = _list(os.environ.get("MEETING_AVOID_DAYS", "Tue,Fri"))

# Per-day WORK BLOCK availability (internal, solo)
WORK_BLOCK_START = os.environ.get("WORK_BLOCK_START", "07:00")
WORK_BLOCK_END = os.environ.get("WORK_BLOCK_END", "22:00")
WORK_BLOCK_DAYS = _list(os.environ.get("WORK_BLOCK_DAYS", "Mon,Tue,Wed,Thu,Fri,Sat,Sun"))

# Legacy aliases — per-day availability windows (used by get_day_availability)
AVAILABILITY_MONDAY = os.environ.get("AVAILABILITY_MONDAY", MEETING_MONDAY)
AVAILABILITY_TUESDAY = os.environ.get("AVAILABILITY_TUESDAY", MEETING_TUESDAY)
AVAILABILITY_WEDNESDAY = os.environ.get("AVAILABILITY_WEDNESDAY", MEETING_WEDNESDAY)
AVAILABILITY_THURSDAY = os.environ.get("AVAILABILITY_THURSDAY", MEETING_THURSDAY)
AVAILABILITY_FRIDAY = os.environ.get("AVAILABILITY_FRIDAY", MEETING_FRIDAY)
AVAILABILITY_SATURDAY = os.environ.get("AVAILABILITY_SATURDAY", MEETING_SATURDAY)
AVAILABILITY_SUNDAY = os.environ.get("AVAILABILITY_SUNDAY", MEETING_SUNDAY)

_DAY_ABBR_TO_INT = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def get_day_availability(weekday: int, mode: str = "meeting") -> tuple[str, str] | None:
    """Return (start, end) hours for a weekday (Mon=0), or None if unavailable.

    mode: "meeting" uses per-day meeting windows; "work_block" uses full work block hours.
    For meeting mode, "avoid" days return the default meeting window (caller handles warning).
    """
    if mode == "work_block":
        # Work blocks available every configured day
        day_names = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
        allowed = {_DAY_ABBR_TO_INT.get(d.lower()[:3], -1) for d in WORK_BLOCK_DAYS}
        if weekday in allowed:
            return WORK_BLOCK_START, WORK_BLOCK_END
        return None

    # Meeting mode
    _meeting_configs = [
        MEETING_MONDAY, MEETING_TUESDAY, MEETING_WEDNESDAY,
        MEETING_THURSDAY, MEETING_FRIDAY, MEETING_SATURDAY,
        MEETING_SUNDAY,
    ]
    val = _meeting_configs[weekday].strip().lower()
    if val == "unavailable":
        return None
    if val == "avoid":
        # Return default meeting window — caller checks is_meeting_avoid_day()
        return MEETING_HOURS_START, MEETING_HOURS_END
    if "-" in val:
        start, end = val.split("-", 1)
        return start.strip(), end.strip()
    return None


def is_meeting_avoid_day(weekday: int) -> bool:
    """Check if a weekday is a meeting-avoid day (Tue/Fri by default)."""
    avoid = {_DAY_ABBR_TO_INT.get(d.lower()[:3], -1) for d in MEETING_AVOID_DAYS}
    return weekday in avoid

# Quiet hours
QUIET_HOURS_START = os.environ.get("QUIET_HOURS_START", "20:00")
QUIET_HOURS_END = os.environ.get("QUIET_HOURS_END", "05:00")
HOME_TIMEZONE = os.environ.get("HOME_TIMEZONE", "America/Chicago")
OVERRIDE_TIMEOUT_MINUTES = int(os.environ.get("OVERRIDE_TIMEOUT_MINUTES", "30"))

# Database
SQLITE_PATH = Path(os.environ.get("SQLITE_PATH", "artemis.db"))

# Weekly staples (grocery)
WEEKLY_STAPLES = os.environ.get("WEEKLY_STAPLES", "")

# Logging
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
