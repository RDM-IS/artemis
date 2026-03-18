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
CHANNEL_OPS = os.environ.get("CHANNEL_OPS", "artemis-ops")
CHANNEL_BRIEFS = os.environ.get("CHANNEL_BRIEFS", "artemis-briefs")
CHANNEL_COMMITMENTS = os.environ.get("CHANNEL_COMMITMENTS", "artemis-commitments")

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

# Playbooks
PLAYBOOKS_PATH = Path(os.environ.get("PLAYBOOKS_PATH", "PLAYBOOKS.md"))

# Availability / Meeting Preferences (PB-006)
MEETING_HOURS_START = os.environ.get("MEETING_HOURS_START", "09:00")
MEETING_HOURS_END = os.environ.get("MEETING_HOURS_END", "17:00")
MEETING_BUFFER_MINUTES = int(os.environ.get("MEETING_BUFFER_MINUTES", "15"))
PREFERRED_MEETING_DAYS = _list(os.environ.get("PREFERRED_MEETING_DAYS", "Mon,Tue,Wed,Thu,Fri"))
FOCUS_BLOCK_KEYWORDS = _list(os.environ.get("FOCUS_BLOCK_KEYWORDS", "focus,deep work,work session"))
BOOKING_LINK = os.environ.get("BOOKING_LINK", "")
DEFAULT_SLOT_DURATION = int(os.environ.get("DEFAULT_SLOT_DURATION", "30"))

# Database
SQLITE_PATH = Path(os.environ.get("SQLITE_PATH", "artemis.db"))

# Logging
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
