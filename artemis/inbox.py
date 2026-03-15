"""Inbox zero tracking — every email thread gets a state, nothing is silently dropped."""

import logging
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone

from artemis import config
from artemis.commitments import get_db as _get_commitments_db

logger = logging.getLogger(__name__)

# Thread states
NEEDS_ACTION = "NEEDS_ACTION"
WAITING = "WAITING"
SNOOZED = "SNOOZED"
DONE = "DONE"
NOISE = "NOISE"

VALID_STATES = {NEEDS_ACTION, WAITING, SNOOZED, DONE, NOISE}

# Snooze periods: label → timedelta
SNOOZE_PERIODS = {
    "1d": timedelta(days=1),
    "3d": timedelta(days=3),
    "1w": timedelta(weeks=1),
    "2w": timedelta(weeks=2),
}

CREATE_INBOX_THREADS = """
CREATE TABLE IF NOT EXISTS inbox_threads (
    id TEXT PRIMARY KEY,
    subject TEXT,
    sender TEXT,
    sender_domain TEXT,
    state TEXT NOT NULL DEFAULT 'NEEDS_ACTION',
    snoozed_until DATE,
    waiting_on TEXT,
    waiting_since DATE,
    due_date DATE,
    client TEXT,
    notes TEXT,
    first_seen_at TIMESTAMP NOT NULL DEFAULT (datetime('now')),
    last_updated_at TIMESTAMP NOT NULL DEFAULT (datetime('now')),
    last_nudged_at TIMESTAMP,
    mattermost_post_id TEXT
)
"""


def get_db() -> sqlite3.Connection:
    """Get a database connection with inbox_threads table ensured."""
    conn = _get_commitments_db()
    conn.execute(CREATE_INBOX_THREADS)
    conn.commit()
    return conn


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _today_iso() -> str:
    return date.today().isoformat()


# ---------------------------------------------------------------------------
# Core CRUD
# ---------------------------------------------------------------------------


def upsert_thread(
    thread_id: str,
    subject: str,
    sender: str,
    state: str = NEEDS_ACTION,
    client: str = "",
    db: sqlite3.Connection | None = None,
) -> bool:
    """Create a thread record if it doesn't already exist. Returns True if created."""
    conn = db or get_db()
    existing = conn.execute(
        "SELECT id FROM inbox_threads WHERE id = ?", (thread_id,)
    ).fetchone()
    if existing:
        return False

    sender_domain = ""
    if "@" in sender:
        sender_domain = sender.split("@")[-1].lower().rstrip(">")

    conn.execute(
        """INSERT INTO inbox_threads
           (id, subject, sender, sender_domain, state, client, first_seen_at, last_updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (thread_id, subject, sender, sender_domain, state, client, _now_iso(), _now_iso()),
    )
    conn.commit()
    return True


def get_thread(thread_id: str, db: sqlite3.Connection | None = None) -> dict | None:
    conn = db or get_db()
    row = conn.execute("SELECT * FROM inbox_threads WHERE id = ?", (thread_id,)).fetchone()
    return dict(row) if row else None


def set_state(
    thread_id: str,
    state: str,
    db: sqlite3.Connection | None = None,
    **kwargs,
) -> bool:
    """Transition a thread to a new state. Extra kwargs set optional columns."""
    if state not in VALID_STATES:
        logger.error("Invalid state: %s", state)
        return False

    conn = db or get_db()
    existing = get_thread(thread_id, db=conn)
    if not existing:
        logger.warning("Thread %s not found", thread_id)
        return False

    sets = ["state = ?", "last_updated_at = ?"]
    params: list = [state, _now_iso()]

    # Clear snooze fields when leaving SNOOZED
    if state != SNOOZED:
        sets.append("snoozed_until = NULL")

    # Clear waiting fields when leaving WAITING
    if state != WAITING:
        sets.append("waiting_on = NULL")
        sets.append("waiting_since = NULL")

    for col in ("snoozed_until", "waiting_on", "waiting_since", "due_date", "client", "notes", "mattermost_post_id"):
        if col in kwargs:
            sets.append(f"{col} = ?")
            params.append(kwargs[col])

    params.append(thread_id)
    conn.execute(f"UPDATE inbox_threads SET {', '.join(sets)} WHERE id = ?", params)
    conn.commit()

    # Log state change to audit_log
    conn.execute(
        "INSERT INTO audit_log (model, prompt_hash, response_length) VALUES (?, ?, ?)",
        ("inbox_state_change", f"{thread_id}:{state}", 0),
    )
    conn.commit()
    return True


def mark_done(thread_id: str, db: sqlite3.Connection | None = None) -> bool:
    return set_state(thread_id, DONE, db=db)


def mark_noise(thread_id: str, db: sqlite3.Connection | None = None) -> bool:
    return set_state(thread_id, NOISE, db=db)


def mark_waiting(
    thread_id: str, waiting_on: str = "", db: sqlite3.Connection | None = None
) -> bool:
    return set_state(
        thread_id,
        WAITING,
        db=db,
        waiting_on=waiting_on,
        waiting_since=_today_iso(),
    )


def mark_snoozed(
    thread_id: str, period: str, db: sqlite3.Connection | None = None
) -> bool:
    """Snooze a thread. period must be one of: 1d, 3d, 1w, 2w."""
    delta = SNOOZE_PERIODS.get(period)
    if not delta:
        logger.error("Invalid snooze period: %s (valid: %s)", period, list(SNOOZE_PERIODS))
        return False
    until = (date.today() + delta).isoformat()
    return set_state(thread_id, SNOOZED, db=db, snoozed_until=until)


def mark_needs_action(thread_id: str, db: sqlite3.Connection | None = None) -> bool:
    return set_state(thread_id, NEEDS_ACTION, db=db)


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


def list_by_state(state: str, db: sqlite3.Connection | None = None) -> list[dict]:
    conn = db or get_db()
    rows = conn.execute(
        "SELECT * FROM inbox_threads WHERE state = ? ORDER BY last_updated_at DESC",
        (state,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_stale_needs_action(hours: int = 24, db: sqlite3.Connection | None = None) -> list[dict]:
    """NEEDS_ACTION threads with no update in `hours` hours."""
    conn = db or get_db()
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    rows = conn.execute(
        """SELECT * FROM inbox_threads
           WHERE state = 'NEEDS_ACTION'
             AND last_updated_at < ?
           ORDER BY last_updated_at ASC""",
        (cutoff,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_stale_waiting(days: int = 3, db: sqlite3.Connection | None = None) -> list[dict]:
    """WAITING threads where waiting_since is older than `days` days."""
    conn = db or get_db()
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    rows = conn.execute(
        """SELECT * FROM inbox_threads
           WHERE state = 'WAITING'
             AND waiting_since <= ?
           ORDER BY waiting_since ASC""",
        (cutoff,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_due_today(db: sqlite3.Connection | None = None) -> list[dict]:
    conn = db or get_db()
    today = _today_iso()
    rows = conn.execute(
        """SELECT * FROM inbox_threads
           WHERE state IN ('NEEDS_ACTION', 'WAITING')
             AND due_date IS NOT NULL
             AND due_date <= ?
           ORDER BY due_date ASC""",
        (today,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_snoozed_due(db: sqlite3.Connection | None = None) -> list[dict]:
    """SNOOZED threads where snoozed_until <= today."""
    conn = db or get_db()
    today = _today_iso()
    rows = conn.execute(
        """SELECT * FROM inbox_threads
           WHERE state = 'SNOOZED'
             AND snoozed_until <= ?
           ORDER BY snoozed_until ASC""",
        (today,),
    ).fetchall()
    return [dict(r) for r in rows]


def can_nudge(thread_id: str, min_hours: int = 12, db: sqlite3.Connection | None = None) -> bool:
    """Check if enough time has passed since last nudge."""
    conn = db or get_db()
    row = conn.execute(
        "SELECT last_nudged_at FROM inbox_threads WHERE id = ?", (thread_id,)
    ).fetchone()
    if not row or not row["last_nudged_at"]:
        return True
    try:
        last = datetime.strptime(row["last_nudged_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - last).total_seconds() >= min_hours * 3600
    except (ValueError, TypeError):
        return True


def record_nudge(thread_id: str, db: sqlite3.Connection | None = None) -> None:
    conn = db or get_db()
    conn.execute(
        "UPDATE inbox_threads SET last_nudged_at = ? WHERE id = ?",
        (_now_iso(), thread_id),
    )
    conn.commit()


def get_counts(db: sqlite3.Connection | None = None) -> dict[str, int]:
    conn = db or get_db()
    rows = conn.execute(
        "SELECT state, COUNT(*) as cnt FROM inbox_threads GROUP BY state"
    ).fetchall()
    return {r["state"]: r["cnt"] for r in rows}


def set_mattermost_post_id(
    thread_id: str, post_id: str, db: sqlite3.Connection | None = None
) -> None:
    conn = db or get_db()
    conn.execute(
        "UPDATE inbox_threads SET mattermost_post_id = ? WHERE id = ?",
        (post_id, thread_id),
    )
    conn.commit()


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
    lines = ["**Waiting on replies:**"]
    for t in threads:
        days = 0
        if t.get("waiting_since"):
            try:
                ws = date.fromisoformat(t["waiting_since"])
                days = (date.today() - ws).days
            except ValueError:
                pass
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


def format_morning_inbox_section(db: sqlite3.Connection | None = None) -> str:
    """Format inbox zero section for the morning brief."""
    conn = db or get_db()
    counts = get_counts(db=conn)
    na = counts.get(NEEDS_ACTION, 0)
    w = counts.get(WAITING, 0)

    lines = []
    if na > 0:
        lines.append(f"- **{na}** email thread{'s' if na != 1 else ''} need{'s' if na == 1 else ''} action")
    if w > 0:
        waiting = list_by_state(WAITING, db=conn)
        who_list = [t.get("waiting_on", "someone") for t in waiting if t.get("waiting_on")]
        if who_list:
            lines.append(f"- **{w}** thread{'s' if w != 1 else ''} waiting on: {', '.join(who_list)}")
        else:
            lines.append(f"- **{w}** thread{'s' if w != 1 else ''} waiting on replies")

    due_today = get_due_today(db=conn)
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


def resolve_thread_id(short_id: str, db: sqlite3.Connection | None = None) -> str | None:
    """Resolve a short thread ID prefix to a full Gmail thread ID."""
    if not short_id:
        return None
    conn = db or get_db()
    rows = conn.execute(
        "SELECT id FROM inbox_threads WHERE id LIKE ?", (f"{short_id}%",)
    ).fetchall()
    if len(rows) == 1:
        return rows[0]["id"]
    if len(rows) > 1:
        logger.warning("Ambiguous thread ID prefix: %s (%d matches)", short_id, len(rows))
    return None
