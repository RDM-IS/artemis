"""Hard guardrails — safety checks that cannot be disabled by any config, env var, or mode.

GUARDRAIL: External attendee protection
No calendar event with external attendees (non @rdm.is, non @gmail.com) may be
created without explicit user approval through the Mattermost confirmation flow.
This guardrail fires regardless of autonomy mode (Learning, Active, Live).
"""

import logging
import sqlite3
from datetime import datetime, timezone

from artemis.commitments import get_db

logger = logging.getLogger(__name__)

# Internal domains — emails on these domains are NOT flagged
_INTERNAL_DOMAINS = frozenset({"rdm.is", "gmail.com"})

# SQLite table for guardrail violation logging
_CREATE_VIOLATIONS = """
CREATE TABLE IF NOT EXISTS guardrail_violations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL DEFAULT (datetime('now')),
    event_summary TEXT NOT NULL,
    external_attendees TEXT NOT NULL,
    outcome TEXT NOT NULL
)
"""


def _ensure_table(db: sqlite3.Connection) -> None:
    db.execute(_CREATE_VIOLATIONS)
    db.commit()


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def log_violation(
    event_summary: str,
    external_attendees: list[str],
    outcome: str,
    db: sqlite3.Connection | None = None,
) -> None:
    """Log a guardrail violation to SQLite. outcome: 'blocked', 'approved', 'denied'."""
    conn = db or get_db()
    _ensure_table(conn)
    conn.execute(
        "INSERT INTO guardrail_violations (timestamp, event_summary, external_attendees, outcome) "
        "VALUES (?, ?, ?, ?)",
        (_now_iso(), event_summary, ", ".join(external_attendees), outcome),
    )
    conn.commit()
    logger.warning(
        "GUARDRAIL VIOLATION [%s]: event='%s', external=%s",
        outcome, event_summary, external_attendees,
    )


def get_external_attendees(attendees: list[str] | None) -> list[str]:
    """Return list of attendee emails whose domain is NOT internal.

    Internal domains: rdm.is, gmail.com
    Empty/None attendees list returns [].
    """
    if not attendees:
        return []
    external = []
    for email in attendees:
        email_lower = email.lower().strip()
        domain = email_lower.split("@")[1] if "@" in email_lower else ""
        if domain and domain not in _INTERNAL_DOMAINS:
            external.append(email_lower)
    return external


def check_external_attendees(
    event_summary: str,
    attendees: list[str] | None,
    user_approved: bool = False,
) -> dict:
    """Check if event has external attendees. This is a HARD guardrail.

    Returns:
        {"allowed": True} — no external attendees, or user explicitly approved
        {"allowed": False, "external": [...], "reason": str} — blocked

    This function CANNOT be bypassed by config, env var, or mode. The only way
    to proceed is with user_approved=True, which requires explicit Mattermost
    confirmation routed through _handle_calendar_confirm().
    """
    external = get_external_attendees(attendees)

    if not external:
        return {"allowed": True}

    if user_approved:
        log_violation(event_summary, external, "approved")
        return {"allowed": True}

    # BLOCKED — log and return
    log_violation(event_summary, external, "blocked")
    return {
        "allowed": False,
        "external": external,
        "reason": (
            f"Event '{event_summary}' has external attendee(s): {', '.join(external)}. "
            f"Calendar write BLOCKED — requires explicit user approval."
        ),
    }


def format_guardrail_block(event_summary: str, external: list[str], event_data: dict) -> str:
    """Format a Mattermost message for a blocked calendar write."""
    date_str = event_data.get("date", "?")
    start = event_data.get("start_time", "?")
    end = event_data.get("end_time", "?")

    lines = [
        "\U0001f6d1 **Calendar write BLOCKED — external attendee guardrail**",
        f"**Event:** {event_summary}",
        f"**When:** {date_str} {start}–{end}",
        f"**External attendee(s):** {', '.join(external)}",
        "",
        "Reply `approve` to create this event with the external attendee(s),",
        "or `deny` to discard.",
    ]
    return "\n".join(lines)
