"""acos.email_index — a queryable MIRROR of the Gmail working set (Phase E1).

Per docs/EMAIL_MODEL.md: **Gmail is the source of truth; labels are the state.**
This module maintains a Postgres mirror of the *active* working set so Artemis
can see/list/match the WHOLE inbox in one fast query, instead of paginating the
Gmail API 20 at a time (the "Artemis only sees the last 5" blind spot). The
mirror is **never acted through** — Artemis decides from the index, then acts
against Gmail and corrects the index to match verified Gmail reality.

Phase E1 is **read-only**: sync + paginated reads. No dispositions, no labels,
no sends, and NO audit entries — indexing is observation, not action (audit_log
is reserved for state-changing actions). Path/playbook matching, snooze
resurfacing, and the disposition command surface are later phases.

Working set (v1): `in:inbox is:unread`. sync_from_gmail() lists the full set
from Gmail, hydrates lightweight metadata, upserts, and prunes rows that have
left the working set — EXCEPT rows parked in state 'snoozed'/'pending', which
must survive a sync that no longer sees them (they resurface later). The mirror
is rebuilt every cycle, so a transient empty read self-heals on the next sync.
"""

import logging

from knowledge.db import execute_one, execute_query, execute_write

logger = logging.getLogger(__name__)

# Working-set query (EMAIL_MODEL.md "Surfacing at scale" v1): unread + in inbox
# = "needs action". Read-in-inbox is a deferred lower tier.
WORKING_SET_QUERY = "in:inbox is:unread"

# States that constitute the active working set (the mirror only ever holds
# these; terminal dispositions are pruned). 'snoozed'/'pending' are protected
# from the remove-stale prune because they will resurface.
_WORKING_STATES = ("inbox", "snoozed", "pending")
_PROTECTED_STATES = ("snoozed", "pending")


def _received_at_param(internal_date) -> float | None:
    """Gmail internalDate is epoch milliseconds (string). Convert to epoch
    seconds for SQL to_timestamp(); None when absent/unparseable."""
    if internal_date is None:
        return None
    try:
        return int(internal_date) / 1000.0
    except (TypeError, ValueError):
        return None


def _sender_domain(from_email: str) -> str:
    """Lowercased domain of an address, '' if none. Mirrors inbox.upsert_thread."""
    if from_email and "@" in from_email:
        return from_email.split("@")[-1].lower().strip().rstrip(">")
    return ""


def _upsert(meta: dict) -> None:
    """Upsert one message's metadata into the mirror.

    On conflict we refresh only the OBSERVED fields (Gmail-sourced metadata) and
    deliberately PRESERVE path / pb_match / state / snooze_until — those are
    set by dispositions in later phases and must not be clobbered by a re-sync
    of metadata. In Phase E1 they are always the inserted defaults anyway.
    """
    label_ids = meta.get("label_ids") or []
    execute_write(
        """
        INSERT INTO acos.email_index
            (message_id, thread_id, sender, sender_domain, subject, snippet,
             received_at, is_unread, current_labels,
             path, pb_match, state, snooze_until, indexed_at)
        VALUES
            (%s, %s, %s, %s, %s, %s,
             to_timestamp(%s), %s, %s,
             %s, %s, %s, %s, now())
        ON CONFLICT (message_id) DO UPDATE SET
            thread_id      = EXCLUDED.thread_id,
            sender         = EXCLUDED.sender,
            sender_domain  = EXCLUDED.sender_domain,
            subject        = EXCLUDED.subject,
            snippet        = EXCLUDED.snippet,
            received_at    = EXCLUDED.received_at,
            is_unread      = EXCLUDED.is_unread,
            current_labels = EXCLUDED.current_labels,
            indexed_at     = now()
        """,
        (
            meta["id"],
            meta.get("thread_id", ""),
            meta.get("from", ""),
            _sender_domain(meta.get("from_email", "")),
            meta.get("subject", ""),
            meta.get("snippet", ""),
            _received_at_param(meta.get("internal_date")),
            "UNREAD" in label_ids,
            label_ids,
            # Phase E1 defaults: everything is Path 1 (Ryan's attention), no
            # playbook claim, plain 'inbox' state, no snooze.
            1,
            None,
            "inbox",
            None,
        ),
    )


def _prune_stale(working_ids: list[str]) -> int:
    """Remove rows whose message_id is no longer in the working set (terminal-
    disposed / left inbox), EXCEPT rows in a protected state ('snoozed'/'pending')
    — those will resurface and must survive. Returns rows deleted.

    With an empty working_ids this deletes all non-protected rows: correct for a
    genuine inbox-zero, and self-healing for a transient empty read (the next
    sync re-adds them). Protected rows are never touched either way.
    """
    row = execute_write(
        """
        WITH deleted AS (
            DELETE FROM acos.email_index
            WHERE state NOT IN %s
              AND NOT (message_id = ANY(%s::text[]))
            RETURNING 1
        )
        SELECT count(*) AS n FROM deleted
        """,
        (_PROTECTED_STATES, working_ids),
    )
    return int(row["n"]) if row else 0


def sync_from_gmail(gmail) -> dict:
    """Sync the mirror from Gmail's working set. Read-only against the system of
    record — observes Gmail, writes only the mirror, no audit entry.

    Steps: list ALL working-set IDs (full pagination) → batch-hydrate metadata →
    upsert each → prune rows that left the working set (protecting snoozed/
    pending). Returns a summary dict: {listed, fetched, upserted, pruned,
    working_set}.
    """
    if not gmail or not getattr(gmail, "service", None):
        logger.warning("email_index sync skipped — Gmail not authenticated")
        return {"listed": 0, "fetched": 0, "upserted": 0, "pruned": 0,
                "working_set": count_working_set()}

    ids = gmail.list_inbox_message_ids(WORKING_SET_QUERY)
    metadata = gmail.get_message_metadata(ids)

    upserted = 0
    for meta in metadata:
        try:
            _upsert(meta)
            upserted += 1
        except Exception:
            logger.exception("email_index upsert failed for %s", meta.get("id"))

    pruned = _prune_stale(ids)

    summary = {
        "listed": len(ids),
        "fetched": len(metadata),
        "upserted": upserted,
        "pruned": pruned,
        "working_set": count_working_set(),
    }
    logger.info(
        "email_index sync: listed=%d fetched=%d upserted=%d pruned=%d working_set=%d",
        summary["listed"], summary["fetched"], summary["upserted"],
        summary["pruned"], summary["working_set"],
    )
    return summary


def query_working_set(limit: int = 20, offset: int = 0) -> list[dict]:
    """Paginated read of the active working set, newest first. NULL received_at
    (rare — metadata gap) sorts last so real mail leads the page."""
    return execute_query(
        """
        SELECT * FROM acos.email_index
        WHERE state IN %s
        ORDER BY received_at DESC NULLS LAST, message_id
        LIMIT %s OFFSET %s
        """,
        (_WORKING_STATES, limit, offset),
    )


def count_working_set() -> int:
    """Total rows in the active working set (for the "23 need action" header)."""
    row = execute_one(
        "SELECT count(*) AS n FROM acos.email_index WHERE state IN %s",
        (_WORKING_STATES,),
    )
    return int(row["n"]) if row else 0
