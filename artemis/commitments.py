"""Work-deadline commitment tracker (RDS-backed) with CLI.

Standalone deadlines with effort-based start alerts and a free-text client —
backed by acos.commitments (Postgres/RDS, migration 020) via knowledge.db. This
is distinct from the CRM API's public.commitments (contact-scoped, status='open'),
which crm_query / the scheduler radar / `crm status` read; see migration 020.

No SQLite remains: this was the last module on the shared SQLite hub, so after it
artemis.db has no writers. The two audit helpers that historically lived here on
that hub (log_claude_call, log_calendar_action) keep their import surface but now
write the existing acos audit tables (acos.audit_log / acos.calendar_audit).

"Today"-relative due-date logic is anchored to America/Chicago — the box runs UTC
(a day ahead of CT after ~19:00), so bare current_date would flag due/overdue a
day early.
"""

import argparse
import difflib
import logging
import re

from knowledge.db import execute_one, execute_query, execute_write

logger = logging.getLogger(__name__)

# CT anchor for every "today"-relative due-date comparison.
_CT_TODAY_SQL = "(now() AT TIME ZONE 'America/Chicago')::date"


# ---------------------------------------------------------------------------
# Commitment CRUD
# ---------------------------------------------------------------------------


def add_commitment(
    title: str,
    due_date: str | None,
    effort_days: int = 1,
    client: str = "",
    status: str = "active",
    dossier_id: int | None = None,
    meeting_id: int | None = None,
) -> int:
    """Insert a commitment. Returns the new id.

    PB-010 extensions (all backward-compatible defaults):
      * status — 'draft' for dossier-extracted to-dos awaiting a bless; 'active'
        (default) for explicit/immediate ones. Drafts are invisible to the
        reminder radar (get_due_soon/get_start_alerts filter status='active').
      * dossier_id / meeting_id — soft provenance so a to-do attributes to the
        person and the meeting it came from. Nullable; free-standing commitments
        pass neither.
      * due_date may be None (undated to-do) — migration 024 dropped the NOT NULL.
    """
    row = execute_write(
        "INSERT INTO acos.commitments "
        "(title, due_date, effort_days, client, status, dossier_id, meeting_id) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (title, due_date or None, effort_days, client, status, dossier_id, meeting_id),
    )
    return row["id"] if row else 0


def get_commitment(commitment_id: int) -> dict | None:
    """Fetch a single commitment row by id (re-read for confirm-from-row)."""
    return execute_one(
        "SELECT * FROM acos.commitments WHERE id = %s", (commitment_id,)
    )


def activate_commitment(commitment_id: int) -> dict | None:
    """Flip a draft commitment to active (the bless transition). Returns the
    re-read row so the caller confirms from written state, never an LLM claim."""
    execute_write(
        "UPDATE acos.commitments SET status = 'active' WHERE id = %s AND status = 'draft'",
        (commitment_id,),
    )
    return get_commitment(commitment_id)


def update_commitment_title(commitment_id: int, title: str) -> None:
    """Replace a commitment's title (the `edit N: <text>` review action)."""
    execute_write(
        "UPDATE acos.commitments SET title = %s WHERE id = %s",
        (title, commitment_id),
    )


def delete_commitment(commitment_id: int) -> None:
    """Hard-delete a commitment (the `drop N` review action — only ever used on a
    draft that Ryan rejected before it entered the record)."""
    execute_write("DELETE FROM acos.commitments WHERE id = %s", (commitment_id,))


def list_commitments(status: str = "active") -> list[dict]:
    return execute_query(
        "SELECT * FROM acos.commitments WHERE status = %s ORDER BY due_date",
        (status,),
    )


def update_status(commitment_id: int, status: str) -> None:
    execute_write(
        "UPDATE acos.commitments SET status = %s WHERE id = %s",
        (status, commitment_id),
    )


def get_due_soon(days: int = 3) -> list[dict]:
    """Get active commitments due within `days` days (CT-anchored)."""
    return execute_query(
        f"""SELECT * FROM acos.commitments
            WHERE status = 'active'
              AND due_date <= {_CT_TODAY_SQL} + %s
            ORDER BY due_date""",
        (days,),
    )


def get_start_alerts() -> list[dict]:
    """Get commitments where remaining days <= effort_days (should start now).

    CT-anchored: remaining days = due_date - CT today (date - date = int days).
    """
    return execute_query(
        f"""SELECT * FROM acos.commitments
            WHERE status = 'active'
              AND (due_date - {_CT_TODAY_SQL}) <= effort_days
            ORDER BY due_date"""
    )


def get_commitments_for_client(client: str) -> list[dict]:
    # ILIKE preserves SQLite LIKE's case-insensitive substring match.
    return execute_query(
        "SELECT * FROM acos.commitments WHERE status = 'active' AND client ILIKE %s "
        "ORDER BY due_date",
        (f"%{client}%",),
    )


# ---------------------------------------------------------------------------
# Close commitment with fuzzy matching
# ---------------------------------------------------------------------------

def close_commitment(title_query: str) -> dict:
    """Close a commitment by fuzzy-matching the title.

    Returns a result dict:
      {"status": "closed", "title": str, "id": int}
      {"status": "ambiguous", "matches": list[dict]}
      {"status": "not_found", "open": list[dict]}
    """
    open_commitments = list_commitments(status="active")
    if not open_commitments:
        return {"status": "not_found", "open": []}

    titles = [c["title"] for c in open_commitments]
    matches = difflib.get_close_matches(title_query, titles, n=5, cutoff=0.6)

    if len(matches) == 1:
        matched_title = matches[0]
        matched = next(c for c in open_commitments if c["title"] == matched_title)
        execute_write(
            "UPDATE acos.commitments SET status = 'closed', closed_at = now() WHERE id = %s",
            (matched["id"],),
        )
        logger.info("Closed commitment #%d: %s", matched["id"], matched_title)
        return {"status": "closed", "title": matched_title, "id": matched["id"]}

    if len(matches) > 1:
        matched_items = [c for c in open_commitments if c["title"] in matches]
        return {"status": "ambiguous", "matches": matched_items}

    return {"status": "not_found", "open": open_commitments}


def format_close_result(result: dict) -> str:
    """Format the close_commitment result for Mattermost."""
    if result["status"] == "closed":
        return f"✅ Commitment closed: *{result['title']}*"

    if result["status"] == "ambiguous":
        lines = ["Found multiple matches — which did you mean?"]
        for i, c in enumerate(result["matches"], 1):
            lines.append(f"{i}. {c['title']}")
        return "\n".join(lines)

    # not_found
    open_items = result.get("open", [])
    if not open_items:
        return "No open commitments."
    lines = ["No match found. Open commitments:"]
    for c in open_items:
        lines.append(f"• {c['title']}")
    return "\n".join(lines)


def format_commitments_list(commitments: list[dict]) -> str:
    """Format a list of commitments for Mattermost."""
    if not commitments:
        return "No open commitments."
    lines = [f"✅ **Open commitments ({len(commitments)}):**"]
    for c in commitments:
        # created_at is a TIMESTAMPTZ (datetime); due_date a DATE. str()[:10] → the date.
        created = str(c.get("created_at") or "")[:10]
        client = c.get("client", "")
        client_str = f" ({client})" if client else ""
        lines.append(f"• **{c['title']}**{client_str} — due {c['due_date']}, created {created}")
    return "\n".join(lines)


def parse_close_title(text: str) -> str | None:
    """Extract a title from 'close commitment "TITLE"' or 'close "TITLE"'.

    Also handles without quotes: 'close commitment TITLE'.
    Returns the extracted title or None.
    """
    # Try quoted extraction first
    m = re.search(r'close\s+(?:commitment\s+)?"([^"]+)"', text, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    # Try unquoted: everything after 'close commitment' or 'close'
    m = re.search(r'close\s+commitment\s+(.+)', text, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    m = re.search(r'close\s+(.+)', text, re.IGNORECASE)
    if m:
        val = m.group(1).strip()
        # Don't match bare 'close' with no argument
        if val and val.lower() not in ("commitment", "commitments"):
            return val
    return None


# ---------------------------------------------------------------------------
# Audit helpers — kept here for their import surface, repointed off the SQLite
# hub to the existing acos audit tables. Both best-effort: an audit-write hiccup
# must never break the caller's flow (LLM call / calendar write).
# ---------------------------------------------------------------------------


def log_claude_call(model: str, prompt_hash: str, response_length: int) -> None:
    """Record an LLM call to acos.audit_log (was the SQLite audit_log table)."""
    try:
        from knowledge.db import log_audit
        log_audit(
            agent="claude",
            action="llm_call",
            metadata={
                "model": model,
                "prompt_hash": prompt_hash,
                "response_length": response_length,
            },
        )
    except Exception:
        logger.debug("log_claude_call audit write failed", exc_info=True)


def log_calendar_action(
    action: str,
    event_id: str,
    summary: str = "",
    attendees: str = "",
    user_approved: bool = False,
    auto_created: bool = False,
    notes: str = "",
) -> None:
    """Record a calendar write/lifecycle action to acos.calendar_audit (was the
    SQLite calendar_audit_log table).

    Maps summary→title and the comma-string attendees→the JSONB list; user_approved
    → approved_by. acos.calendar_audit has no notes/auto_created columns, so those
    descriptive fields are not persisted (flagged in the migration PR). Captures the
    'draft'/'cancelled' lifecycle actions that _audit_calendar_write does not.
    """
    try:
        from knowledge.db import log_calendar_audit
        attendee_list = [a.strip() for a in attendees.split(",") if a.strip()] if attendees else []
        log_calendar_audit(
            action=action,
            event_id=event_id,
            title=summary,
            attendees=attendee_list,
            approved_by="ryan" if user_approved else None,
        )
    except Exception:
        logger.debug("log_calendar_action audit write failed", exc_info=True)


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
