"""SQLite-backed commitment tracker with CLI."""

import argparse
import logging
import sqlite3
from datetime import date, datetime
from pathlib import Path

from artemis import config

logger = logging.getLogger(__name__)

CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS commitments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    due_date TEXT NOT NULL,
    effort_days INTEGER NOT NULL DEFAULT 1,
    client TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
)
"""

CREATE_AUDIT_LOG = """
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL DEFAULT (datetime('now')),
    model TEXT NOT NULL,
    prompt_hash TEXT NOT NULL,
    response_length INTEGER NOT NULL
)
"""

CREATE_CALENDAR_AUDIT = """
CREATE TABLE IF NOT EXISTS calendar_audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL DEFAULT (datetime('now')),
    action TEXT NOT NULL,
    event_id TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    attendees TEXT NOT NULL DEFAULT '',
    user_approved INTEGER NOT NULL DEFAULT 0,
    auto_created INTEGER NOT NULL DEFAULT 0,
    notes TEXT NOT NULL DEFAULT ''
)
"""

CREATE_TIMEZONE_OVERRIDES = """
CREATE TABLE IF NOT EXISTS timezone_overrides (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    timezone TEXT NOT NULL,
    set_at TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at TEXT NOT NULL,
    city_name TEXT NOT NULL DEFAULT ''
)
"""

CREATE_QUIET_STATE = """
CREATE TABLE IF NOT EXISTS quiet_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    is_quiet INTEGER NOT NULL DEFAULT 0,
    manual_override INTEGER NOT NULL DEFAULT 0,
    wake_time TEXT,
    override_active INTEGER NOT NULL DEFAULT 0,
    override_until TEXT,
    last_interaction TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
)
"""

CREATE_SYSTEM_STATE = """
CREATE TABLE IF NOT EXISTS system_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
)
"""


def get_db(db_path: Path | None = None) -> sqlite3.Connection:
    path = db_path or config.SQLITE_PATH
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute(CREATE_TABLE)
    conn.execute(CREATE_AUDIT_LOG)
    conn.execute(CREATE_CALENDAR_AUDIT)
    conn.execute(CREATE_TIMEZONE_OVERRIDES)
    conn.execute(CREATE_QUIET_STATE)
    conn.execute(CREATE_SYSTEM_STATE)
    conn.commit()
    return conn


def log_calendar_action(
    action: str,
    event_id: str,
    summary: str = "",
    attendees: str = "",
    user_approved: bool = False,
    auto_created: bool = False,
    notes: str = "",
    db: sqlite3.Connection | None = None,
) -> None:
    """Log a calendar write action (create/delete) to the audit trail."""
    conn = db or get_db()
    conn.execute(
        "INSERT INTO calendar_audit_log (action, event_id, summary, attendees, user_approved, auto_created, notes) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (action, event_id, summary, attendees, int(user_approved), int(auto_created), notes),
    )
    conn.commit()


def add_commitment(
    title: str,
    due_date: str,
    effort_days: int = 1,
    client: str = "",
    db: sqlite3.Connection | None = None,
) -> int:
    conn = db or get_db()
    cursor = conn.execute(
        "INSERT INTO commitments (title, due_date, effort_days, client) VALUES (?, ?, ?, ?)",
        (title, due_date, effort_days, client),
    )
    conn.commit()
    return cursor.lastrowid


def list_commitments(
    status: str = "active", db: sqlite3.Connection | None = None
) -> list[dict]:
    conn = db or get_db()
    rows = conn.execute(
        "SELECT * FROM commitments WHERE status = ? ORDER BY due_date", (status,)
    ).fetchall()
    return [dict(r) for r in rows]


def update_status(
    commitment_id: int, status: str, db: sqlite3.Connection | None = None
) -> None:
    conn = db or get_db()
    conn.execute(
        "UPDATE commitments SET status = ? WHERE id = ?", (status, commitment_id)
    )
    conn.commit()


def get_due_soon(days: int = 3, db: sqlite3.Connection | None = None) -> list[dict]:
    """Get active commitments due within `days` days."""
    conn = db or get_db()
    today = date.today().isoformat()
    rows = conn.execute(
        """
        SELECT * FROM commitments
        WHERE status = 'active'
          AND due_date <= date(?, '+' || ? || ' days')
        ORDER BY due_date
        """,
        (today, days),
    ).fetchall()
    return [dict(r) for r in rows]


def get_start_alerts(db: sqlite3.Connection | None = None) -> list[dict]:
    """Get commitments where remaining days <= effort_days (should start now)."""
    conn = db or get_db()
    today = date.today().isoformat()
    rows = conn.execute(
        """
        SELECT * FROM commitments
        WHERE status = 'active'
          AND (julianday(due_date) - julianday(?)) <= effort_days
        ORDER BY due_date
        """,
        (today,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_commitments_for_client(
    client: str, db: sqlite3.Connection | None = None
) -> list[dict]:
    conn = db or get_db()
    rows = conn.execute(
        "SELECT * FROM commitments WHERE status = 'active' AND client LIKE ? ORDER BY due_date",
        (f"%{client}%",),
    ).fetchall()
    return [dict(r) for r in rows]


def log_claude_call(
    model: str,
    prompt_hash: str,
    response_length: int,
    db: sqlite3.Connection | None = None,
) -> None:
    conn = db or get_db()
    conn.execute(
        "INSERT INTO audit_log (model, prompt_hash, response_length) VALUES (?, ?, ?)",
        (model, prompt_hash, response_length),
    )
    conn.commit()


def _cli():
    parser = argparse.ArgumentParser(description="Artemis commitment tracker")
    sub = parser.add_subparsers(dest="command")

    add_p = sub.add_parser("add", aliases=["a"], help="Add a commitment")
    add_p.add_argument("title")
    add_p.add_argument("--due", required=True, help="Due date (YYYY-MM-DD)")
    add_p.add_argument("--effort", type=int, default=1, help="Effort in days")
    add_p.add_argument("--client", default="", help="Client name")

    sub.add_parser("list", aliases=["ls"], help="List active commitments")

    done_p = sub.add_parser("done", aliases=["d"], help="Mark a commitment as done")
    done_p.add_argument("id", type=int)

    block_p = sub.add_parser("block", help="Mark a commitment as blocked")
    block_p.add_argument("id", type=int)

    args = parser.parse_args()

    if args.command in ("add", "a"):
        cid = add_commitment(args.title, args.due, args.effort, args.client)
        print(f"Added commitment #{cid}: {args.title} (due {args.due})")
    elif args.command in ("list", "ls"):
        for c in list_commitments():
            print(
                f"  #{c['id']} [{c['status']}] {c['title']} — due {c['due_date']} "
                f"(effort: {c['effort_days']}d, client: {c['client'] or 'n/a'})"
            )
    elif args.command in ("done", "d"):
        update_status(args.id, "done")
        print(f"Marked #{args.id} as done")
    elif args.command == "block":
        update_status(args.id, "blocked")
        print(f"Marked #{args.id} as blocked")
    else:
        parser.print_help()


if __name__ == "__main__":
    _cli()
