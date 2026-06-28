"""Inbox zero tracking — every email thread gets a state, nothing is silently dropped.

Backend: acos.inbox_threads (Postgres/RDS) via knowledge.db. Migrated off SQLite
(migration 018); no SQLite remains in this module. Every "today" comparison is
anchored to America/Chicago — the box runs UTC, a day ahead of CT after ~19:00,
so bare current_date would resurface snoozes / flag due items a day early.
"""

import logging
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from knowledge.db import execute_one, execute_query, execute_write

logger = logging.getLogger(__name__)

# Thread states
NEEDS_ACTION = "NEEDS_ACTION"
WAITING = "WAITING"
SNOOZED = "SNOOZED"
DONE = "DONE"
NOISE = "NOISE"

VALID_STATES = {NEEDS_ACTION, WAITING, SNOOZED, DONE, NOISE}

# CT anchor. (now() AT TIME ZONE 'America/Chicago')::date is "today in CT" inside
# SQL; _ct_today() is the same value for the DATE columns we write in Python.
_CT = ZoneInfo("America/Chicago")
_CT_TODAY_SQL = "(now() AT TIME ZONE 'America/Chicago')::date"


def _ct_today() -> date:
    return datetime.now(_CT).date()


def _coerce_date(value):
    """Best-effort to a date. psycopg2 returns DATE columns as date objects, but
    format_* may also be handed ISO strings (tests / legacy callers)."""
    if value is None or isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except (ValueError, TypeError):
        return None


def should_keep_in_inbox(state: str) -> bool:
    """Return True only for states that must remain in the Gmail INBOX.

    The one rule: INBOX = a human decision is required. So NEEDS_ACTION keeps;
    WAITING / DONE / NOISE are filed (archived). SNOOZED is keep-on-wake, handled
    by the snooze-resurfacing job — at triage time it archives like the rest, so
    it is NOT kept here.
    """
    return state == NEEDS_ACTION


def state_from_triage(item: dict) -> str:
    """Resolve a triaged email (a triage_emails() result item) to an inbox state.

    Prefers the rubric-assigned `state`; falls back to the legacy sender_type
    mapping when the classifier omits it. Biases to NEEDS_ACTION (INBOX) on
    uncertainty — a false file drops a ball, a false inbox costs a glance.
    """
    state = (item.get("state") or "").strip().upper()
    if state in (NEEDS_ACTION, DONE, NOISE):
        return state
    if item.get("sender_type") == "noise":
        return NOISE
    return NEEDS_ACTION

# Snooze periods: label → timedelta
SNOOZE_PERIODS = {
    "1d": timedelta(days=1),
    "3d": timedelta(days=3),
    "1w": timedelta(weeks=1),
    "2w": timedelta(weeks=2),
}


# ---------------------------------------------------------------------------
# Core CRUD
# ---------------------------------------------------------------------------


def upsert_thread(
    thread_id: str,
    subject: str,
    sender: str,
    state: str = NEEDS_ACTION,
    client: str = "",
) -> bool:
    """Create a thread record if it doesn't already exist. Returns True if created.

    ON CONFLICT DO NOTHING makes the "create-if-absent" atomic; first_seen_at /
    last_updated_at fall to the table's NOW() defaults.
    """
    sender_domain = ""
    if "@" in sender:
        sender_domain = sender.split("@")[-1].lower().rstrip(">")

    row = execute_write(
        """INSERT INTO acos.inbox_threads
               (id, subject, sender, sender_domain, state, client)
           VALUES (%s, %s, %s, %s, %s, %s)
           ON CONFLICT (id) DO NOTHING
           RETURNING id""",
        (thread_id, subject, sender, sender_domain, state, client),
    )
    return row is not None


def get_thread(thread_id: str) -> dict | None:
    return execute_one("SELECT * FROM acos.inbox_threads WHERE id = %s", (thread_id,))


def set_state(thread_id: str, state: str, **kwargs) -> bool:
    """Transition a thread to a new state. Extra kwargs set optional columns."""
    if state not in VALID_STATES:
        logger.error("Invalid state: %s", state)
        return False

    if not get_thread(thread_id):
        logger.warning("Thread %s not found", thread_id)
        return False

    sets = ["state = %s", "last_updated_at = now()"]
    params: list = [state]

    # Clear snooze fields when leaving SNOOZED
    if state != SNOOZED:
        sets.append("snoozed_until = NULL")

    # Clear waiting fields when leaving WAITING
    if state != WAITING:
        sets.append("waiting_on = NULL")
        sets.append("waiting_since = NULL")

    for col in ("snoozed_until", "waiting_on", "waiting_since", "due_date", "client", "notes", "mattermost_post_id"):
        if col in kwargs:
            sets.append(f"{col} = %s")
            params.append(kwargs[col])

    params.append(thread_id)
    execute_write(f"UPDATE acos.inbox_threads SET {', '.join(sets)} WHERE id = %s", params)

    # Audit trail for the state change. Was a piggyback INSERT into the shared
    # SQLite audit_log (severed with commitments.get_db); now routes to the
    # existing acos.audit_log via log_audit. Best-effort — never fail the
    # transition because the audit write hiccuped.
    try:
        from knowledge.db import log_audit
        log_audit(
            agent="inbox",
            action="state_change",
            outcome=state,
            metadata={"thread_id": thread_id, "state": state},
        )
    except Exception:
        logger.debug("inbox state-change audit write failed", exc_info=True)
    return True


def mark_done(thread_id: str) -> bool:
    return set_state(thread_id, DONE)


def mark_noise(thread_id: str) -> bool:
    return set_state(thread_id, NOISE)


def mark_waiting(thread_id: str, waiting_on: str = "") -> bool:
    return set_state(
        thread_id,
        WAITING,
        waiting_on=waiting_on,
        waiting_since=_ct_today(),
    )


def mark_snoozed(thread_id: str, period: str) -> bool:
    """Snooze a thread. period must be one of: 1d, 3d, 1w, 2w."""
    delta = SNOOZE_PERIODS.get(period)
    if not delta:
        logger.error("Invalid snooze period: %s (valid: %s)", period, list(SNOOZE_PERIODS))
        return False
    until = _ct_today() + delta
    return set_state(thread_id, SNOOZED, snoozed_until=until)


def mark_needs_action(thread_id: str) -> bool:
    return set_state(thread_id, NEEDS_ACTION)


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


def list_by_state(state: str) -> list[dict]:
    return execute_query(
        "SELECT * FROM acos.inbox_threads WHERE state = %s ORDER BY last_updated_at DESC",
        (state,),
    )


def get_stale_needs_action(hours: int = 24) -> list[dict]:
    """NEEDS_ACTION threads with no update in `hours` hours.

    NOT CT-anchored: this is an absolute elapsed-time comparison on a TIMESTAMPTZ
    (last_updated_at < now() - interval), which is zone-independent.
    """
    return execute_query(
        """SELECT * FROM acos.inbox_threads
           WHERE state = 'NEEDS_ACTION'
             AND last_updated_at < now() - make_interval(hours => %s)
           ORDER BY last_updated_at ASC""",
        (hours,),
    )


def get_stale_waiting(days: int = 3) -> list[dict]:
    """WAITING threads where waiting_since is older than `days` days.

    CT-anchored: waiting_since is a DATE compared against CT "today" minus days.
    """
    return execute_query(
        f"""SELECT * FROM acos.inbox_threads
           WHERE state = 'WAITING'
             AND waiting_since <= {_CT_TODAY_SQL} - %s
           ORDER BY waiting_since ASC""",
        (days,),
    )


def get_due_today() -> list[dict]:
    """NEEDS_ACTION/WAITING threads due on or before CT today (CT-anchored)."""
    return execute_query(
        f"""SELECT * FROM acos.inbox_threads
           WHERE state IN ('NEEDS_ACTION', 'WAITING')
             AND due_date IS NOT NULL
             AND due_date <= {_CT_TODAY_SQL}
           ORDER BY due_date ASC"""
    )


def get_snoozed_due() -> list[dict]:
    """SNOOZED threads where snoozed_until <= CT today (CT-anchored)."""
    return execute_query(
        f"""SELECT * FROM acos.inbox_threads
           WHERE state = 'SNOOZED'
             AND snoozed_until <= {_CT_TODAY_SQL}
           ORDER BY snoozed_until ASC"""
    )


def can_nudge(thread_id: str, min_hours: int = 12) -> bool:
    """Check if enough time has passed since last nudge.

    NOT CT-anchored: an absolute elapsed-time comparison on last_nudged_at.
    A missing thread or a never-nudged thread returns True (as before).
    """
    row = execute_one(
        """SELECT (last_nudged_at IS NULL
                   OR last_nudged_at < now() - make_interval(hours => %s)) AS can_nudge
           FROM acos.inbox_threads WHERE id = %s""",
        (min_hours, thread_id),
    )
    if row is None:
        return True
    return bool(row["can_nudge"])


def record_nudge(thread_id: str) -> None:
    execute_write(
        "UPDATE acos.inbox_threads SET last_nudged_at = now() WHERE id = %s",
        (thread_id,),
    )


def get_counts() -> dict[str, int]:
    rows = execute_query(
        "SELECT state, COUNT(*) AS cnt FROM acos.inbox_threads GROUP BY state"
    )
    return {r["state"]: r["cnt"] for r in rows}


def set_mattermost_post_id(thread_id: str, post_id: str) -> None:
    execute_write(
        "UPDATE acos.inbox_threads SET mattermost_post_id = %s WHERE id = %s",
        (post_id, thread_id),
    )


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def format_thread_card(t: dict) -> str:
    """Format a single thread as a Mattermost message with action instructions."""
    lines = [
        f"**{t['subject']}**",
        f"From: {t['sender']}",
    ]
    if t.get("client"):
        lines.append(f"Client: {t['client']}")
    if t.get("notes"):
        lines.append(f"Notes: {t['notes']}")
    if t.get("due_date"):
        lines.append(f"Due: {t['due_date']}")

    tid = t["id"][:12]  # short ID for readability
    lines.append("")
    lines.append(f"Reply with: `done {tid}` · `wait {tid}` · `snooze {tid} 3d` · `noise {tid}`")
    return "\n".join(lines)


def format_inbox_status(counts: dict[str, int]) -> str:
    """Format inbox zero status summary."""
    na = counts.get(NEEDS_ACTION, 0)
    w = counts.get(WAITING, 0)
    s = counts.get(SNOOZED, 0)
    d = counts.get(DONE, 0)
    n = counts.get(NOISE, 0)
    return (
        f"**Inbox Zero Status:**\n"
        f"- Needs action: **{na}**\n"
        f"- Waiting: **{w}**\n"
        f"- Snoozed: **{s}**\n"
        f"- Done: {d}\n"
        f"- Noise: {n}"
    )


def format_waiting_list(threads: list[dict]) -> str:
    if not threads:
        return "No threads in WAITING state."
    today = _ct_today()
    lines = ["**Waiting on replies:**"]
    for t in threads:
        days = 0
        ws = _coerce_date(t.get("waiting_since"))
        if ws:
            days = (today - ws).days
        who = t.get("waiting_on") or "unknown"
        lines.append(f"- **{t['subject']}** — waiting on {who} ({days}d)")
    return "\n".join(lines)


def format_snoozed_list(threads: list[dict]) -> str:
    if not threads:
        return "No snoozed threads."
    lines = ["**Snoozed threads:**"]
    for t in threads:
        lines.append(f"- **{t['subject']}** — resurfaces {t.get('snoozed_until', '?')}")
    return "\n".join(lines)


def format_morning_inbox_section() -> str:
    """Format inbox zero section for the morning brief."""
    counts = get_counts()
    na = counts.get(NEEDS_ACTION, 0)
    w = counts.get(WAITING, 0)

    lines = []
    if na > 0:
        lines.append(f"- **{na}** email thread{'s' if na != 1 else ''} need{'s' if na == 1 else ''} action")
    if w > 0:
        waiting = list_by_state(WAITING)
        who_list = [t.get("waiting_on", "someone") for t in waiting if t.get("waiting_on")]
        if who_list:
            lines.append(f"- **{w}** thread{'s' if w != 1 else ''} waiting on: {', '.join(who_list)}")
        else:
            lines.append(f"- **{w}** thread{'s' if w != 1 else ''} waiting on replies")

    due_today = get_due_today()
    if due_today:
        for t in due_today:
            lines.append(f"- **Due today**: {t['subject']} (from {t['sender']})")

    if not lines:
        lines.append("- Inbox zero — no threads need attention")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Command parser for Mattermost replies
# ---------------------------------------------------------------------------

# Match: done <id>, wait <id>, snooze <id> <period>, noise <id>
_CMD_PATTERN = re.compile(
    r"^(done|wait|snooze|noise|inbox|waiting|snoozed)\s*([\w-]*)\s*([\w]*)",
    re.IGNORECASE,
)


def parse_inbox_command(text: str) -> tuple[str, str, str] | None:
    """Parse an inbox command from message text.

    Returns (command, thread_id, extra) or None if not an inbox command.
    """
    text = text.strip()
    m = _CMD_PATTERN.match(text)
    if not m:
        return None
    cmd = m.group(1).lower()
    thread_id = m.group(2) or ""
    extra = m.group(3) or ""
    return (cmd, thread_id, extra)


def resolve_thread_id(short_id: str) -> str | None:
    """Resolve a short thread ID prefix to a full Gmail thread ID."""
    if not short_id:
        return None
    rows = execute_query(
        "SELECT id FROM acos.inbox_threads WHERE id LIKE %s", (f"{short_id}%",)
    )
    if len(rows) == 1:
        return rows[0]["id"]
    if len(rows) > 1:
        logger.warning("Ambiguous thread ID prefix: %s (%d matches)", short_id, len(rows))
    return None
