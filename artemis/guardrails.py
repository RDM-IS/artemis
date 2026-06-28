"""Hard guardrails — safety checks that cannot be disabled by any config, env var, or mode.

GUARDRAIL: External attendee protection
No calendar event with external attendees (non @rdm.is, non @gmail.com) may be
created without explicit user approval through the Mattermost confirmation flow.
This guardrail fires regardless of autonomy mode (Learning, Active, Live).
"""

import logging
import re
from datetime import datetime

from knowledge import db as knowledge_db

logger = logging.getLogger(__name__)

# Internal domains — emails on these domains are NOT flagged
_INTERNAL_DOMAINS = frozenset({"rdm.is", "gmail.com"})

# guardrail_type recorded for the external-attendee guard. Matches the value
# used by the one-time SQLite→Postgres backfill (migrate_sqlite_to_postgres.py).
_EXTERNAL_ATTENDEE_GUARDRAIL = "external_calendar_attendee"


def log_violation(
    event_summary: str,
    external_attendees: list[str],
    outcome: str,
) -> None:
    """Log a guardrail violation to acos.guardrail_violations in RDS.

    outcome: 'blocked', 'approved', 'denied'. created_at is set by the table's
    now() default; external_attendees is stored as a Postgres TEXT[].
    """
    knowledge_db.log_guardrail_violation(
        guardrail_type=_EXTERNAL_ATTENDEE_GUARDRAIL,
        event_summary=event_summary,
        outcome=outcome,
        external_attendees=external_attendees,
    )
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


# ---------------------------------------------------------------------------
# GUARDRAIL: Duplicate-event detection (Brad Spaits incident — guard #2)
#
# Restored after the AWS migration. Runs INDEPENDENTLY of the external-attendee
# approval gate: the user may have already accepted an event and would not
# remember it, so approval alone cannot prevent a duplicate invite going out.
# Deliberately precise to avoid over-blocking — a bare time overlap with an
# unrelated event is NOT a duplicate.
# ---------------------------------------------------------------------------

def _normalize_title(title: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace for fuzzy comparison."""
    t = re.sub(r"[^a-z0-9 ]+", " ", (title or "").lower())
    return re.sub(r"\s+", " ", t).strip()


def _titles_similar(a: str, b: str) -> bool:
    na, nb = _normalize_title(a), _normalize_title(b)
    if not na or not nb:
        return False
    if na == nb or na in nb or nb in na:
        return True
    sa, sb = set(na.split()), set(nb.split())
    if not sa or not sb:
        return False
    return len(sa & sb) / len(sa | sb) >= 0.6  # Jaccard on word sets


def _attendee_emails(attendees) -> set:
    """Normalize attendees (list of email strings OR {'email': ...} dicts) to a set."""
    out = set()
    for a in attendees or []:
        email = a.get("email", "") if isinstance(a, dict) else a
        email = (email or "").lower().strip()
        if email:
            out.add(email)
    return out


def _parse_event_start(value):
    """Parse an event start (ISO datetime or date-only) → datetime, or None."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        try:
            return datetime.strptime(value[:10], "%Y-%m-%d")
        except (ValueError, TypeError):
            return None


def check_duplicate_event(
    summary: str,
    start_dt: datetime,
    attendees,
    existing_events: list[dict],
    window_hours: int = 2,
) -> dict:
    """Classify whether a proposed event duplicates an existing one.

    A CONFIDENT duplicate requires BOTH a time overlap (within ±window_hours of
    the proposed start, same calendar) AND either a similar title or a shared
    attendee. A bare time overlap with an unrelated event is NOT a duplicate —
    it is returned as a soft note only.

    Returns:
        {"duplicate": True,  "match": <existing event>, "reason": "title"|"attendee"}
        {"duplicate": False, "match": None, "soft_note": str|None}
    """
    proposed_att = _attendee_emails(attendees)
    soft = None
    for ev in existing_events or []:
        ev_start = _parse_event_start(ev.get("start"))
        if ev_start is None:
            continue
        # Align tz-awareness before subtracting.
        if ev_start.tzinfo is None and start_dt.tzinfo is not None:
            ev_start = ev_start.replace(tzinfo=start_dt.tzinfo)
        elif ev_start.tzinfo is not None and start_dt.tzinfo is None:
            ev_start = ev_start.replace(tzinfo=None)
        if abs((ev_start - start_dt).total_seconds()) > window_hours * 3600:
            continue  # not within the window — ignore

        if _titles_similar(summary, ev.get("summary", "")):
            return {"duplicate": True, "match": ev, "reason": "title"}
        if proposed_att and (proposed_att & _attendee_emails(ev.get("attendees"))):
            return {"duplicate": True, "match": ev, "reason": "attendee"}
        # Overlapping but unrelated — remember the first as a soft heads-up.
        if soft is None:
            soft = ev

    note = None
    if soft is not None:
        note = f"you have '{soft.get('summary', '(event)')}' around then"
    return {"duplicate": False, "match": None, "soft_note": note}


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
