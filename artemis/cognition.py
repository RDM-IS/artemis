"""COGNITION-1 bronze — the one place that writes a decision row.

A decision row is an `acos.audit_log` row that carries `assumptions`: what the
code believed when it chose. The difference from every other audit row is not
the columns, it is that a decision row is REVISITED when its outcome is known
(docs/ARTEMIS_STATE.md §6 COGNITION-1). Bronze is this table plus three
columns, never a second store — one system of record.

Why a helper rather than three more inline INSERTs: the column list lives once,
so the next field is one edit instead of a grep, and every site is guaranteed to
write `manual_gap` explicitly. `assumptions` is the highest-value column — it is
what makes a wrong decision diagnosable instead of merely visible — and a site
that writes it inconsistently is worth less than one that does not write it.

Outcomes ATTACH BY APPEND, never by UPDATE (Ryan, 2026-09-28): a later row
references this one through `metadata.decides` = the returned id. That is why
this returns the id, and why nothing here is mutable.

It lives in `artemis/` and not in `knowledge/db.py` on purpose. `knowledge/` is
a hashed Lambda path (PACKAGE-IDENTITY), and the Lambda calls none of this yet —
shipping it there would move the package hash for code that cannot run. Site 2
(the makeup swap, `api/app/routers/health.py`) is the one Lambda decision site,
and when it is instrumented this moves deliberately, with the redeploy that
implies.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

# The column list, written once. `token_count` and `api_cost_usd` are 0 for a
# decision that cost no tokens; they are NOT NULL-ish by convention here because
# every existing writer sets them.
_INSERT = (
    "INSERT INTO acos.audit_log "
    "(agent, persona, action, domain, confidence, outcome, token_count, "
    "api_cost_usd, metadata, assumptions, manual_gap) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s) "
    "RETURNING id"
)


def _jsonb(value: dict | None) -> str:
    """jsonb or nothing. `default=str` so a date or Decimal cannot turn a
    decision record into an exception at the write."""
    return json.dumps(value or {}, default=str, sort_keys=True)


def log_decision(cur, *, agent: str, action: str, domain: str, outcome: str,
                 assumptions: dict, metadata: dict | None = None,
                 manual_gap: bool = False, persona: str | None = None,
                 confidence: float | None = None) -> int | None:
    """Write one decision row on `cur`; return its id, or None if the driver
    gave none back.

    `assumptions` is required and must be a dict: a decision row without it is
    just an audit row, and this helper exists to stop that happening by accident.
    `manual_gap` must be a real bool — never NULL-as-maybe (§6 COGNITION-1 rule
    3). Both sites in this slice pass False explicitly: the automation existed
    and ran, so there is no gap by definition. The rules that set it true are a
    later slice and are queries over this table, not judgements made here.

    Raises nothing the caller should catch — it runs inside the caller's
    transaction, so a failure here rolls back the decision it describes. That is
    the correct coupling: a decision that could not be recorded should not stand.
    """
    if not isinstance(assumptions, dict):
        raise TypeError(f"assumptions must be a dict, got {type(assumptions).__name__}")
    if not isinstance(manual_gap, bool):
        raise TypeError(f"manual_gap must be a bool, got {manual_gap!r}")
    cur.execute(_INSERT, (agent, persona, action, domain, confidence, outcome, 0, 0.0,
                          _jsonb(metadata), _jsonb(assumptions), manual_gap))
    row = cur.fetchone()
    if row is None:
        return None
    if isinstance(row, dict):
        return row.get("id")
    return row[0]
