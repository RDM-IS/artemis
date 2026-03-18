"""Simple SQLite-backed CRM for Artemis contact tracking."""

import logging
import sqlite3
from datetime import date

from artemis import config

logger = logging.getLogger(__name__)

_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS contacts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    email       TEXT NOT NULL UNIQUE,
    company     TEXT DEFAULT '',
    source      TEXT DEFAULT '',
    status      TEXT DEFAULT 'lead',
    first_seen  DATE,
    last_contact DATE,
    notes       TEXT DEFAULT ''
);
"""


# ---------------------------------------------------------------------------
# DB helpers (mirrors commitments.py patterns)
# ---------------------------------------------------------------------------

def get_db(db_path: str | None = None) -> sqlite3.Connection:
    """Return a connection, creating the table if needed."""
    path = db_path or str(config.SQLITE_PATH)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute(_TABLE_SQL)
    conn.commit()
    return conn


def init_db(db: sqlite3.Connection | None = None) -> None:
    """Ensure the contacts table exists."""
    conn = db or get_db()
    conn.execute(_TABLE_SQL)
    conn.commit()


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def upsert_contact(
    name: str,
    email: str,
    company: str = "",
    source: str = "",
    status: str = "lead",
    *,
    db: sqlite3.Connection | None = None,
) -> int:
    """Insert or update a contact by email.  Returns the row id."""
    conn = db or get_db()
    today = date.today().isoformat()
    conn.execute(
        """
        INSERT INTO contacts (name, email, company, source, status, first_seen, last_contact)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(email) DO UPDATE SET
            name         = COALESCE(NULLIF(excluded.name, ''), contacts.name),
            company      = COALESCE(NULLIF(excluded.company, ''), contacts.company),
            source       = COALESCE(NULLIF(excluded.source, ''), contacts.source),
            status       = excluded.status,
            last_contact = excluded.last_contact
        """,
        (name, email.lower().strip(), company, source, status, today, today),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM contacts WHERE email = ?", (email.lower().strip(),)
    ).fetchone()
    return row["id"] if row else 0


def get_contact(email: str, *, db: sqlite3.Connection | None = None) -> dict | None:
    """Look up a contact by email.  Returns dict or None."""
    conn = db or get_db()
    row = conn.execute(
        "SELECT * FROM contacts WHERE email = ?", (email.lower().strip(),)
    ).fetchone()
    return dict(row) if row else None


def list_contacts(
    status: str | None = None, *, db: sqlite3.Connection | None = None
) -> list[dict]:
    """List contacts, optionally filtered by status."""
    conn = db or get_db()
    if status:
        rows = conn.execute(
            "SELECT * FROM contacts WHERE status = ? ORDER BY last_contact DESC",
            (status,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM contacts ORDER BY last_contact DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def update_last_contact(
    email: str, contact_date: date | None = None, *, db: sqlite3.Connection | None = None
) -> bool:
    """Update the last_contact date for a contact.  Returns True if found."""
    conn = db or get_db()
    d = (contact_date or date.today()).isoformat()
    cur = conn.execute(
        "UPDATE contacts SET last_contact = ? WHERE email = ?",
        (d, email.lower().strip()),
    )
    conn.commit()
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def format_contacts_list(contacts: list[dict]) -> str:
    """Format a list of contacts for Mattermost."""
    if not contacts:
        return "No contacts found."
    lines = []
    for c in contacts:
        parts = [f"**{c['name']}**"]
        if c.get("company"):
            parts.append(f"({c['company']})")
        parts.append(f"— {c['email']}")
        if c.get("status"):
            parts.append(f"[{c['status']}]")
        if c.get("last_contact"):
            parts.append(f"last contact: {c['last_contact']}")
        lines.append(" ".join(parts))
    return "\n".join(f"- {line}" for line in lines)
