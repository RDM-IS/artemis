"""COGNITION-1 bronze — the one place that writes a decision row.

A decision row is an `acos.audit_log` row that carries `assumptions`: what the
code believed when it chose. The difference from every other audit row is not
the columns, it is that a decision row is REVISITED when its outcome is known
(docs/ARTEMIS_STATE.md §6 COGNITION-1). Bronze is this table plus three
columns, never a second store — one system of record.

Outcomes ATTACH BY APPEND, never by UPDATE (Ryan, 2026-09-28): a later row
references this one through `metadata.decides` = the returned id. That is why
this returns the id, and why nothing here is mutable.

TWO BIND STYLES, ONE COLUMN LIST. The box writes through psycopg2 (`%s`); the
Lambda writes through SQLAlchemy (`:name`). The two statements are GENERATED
from `_COLUMNS`, so they cannot disagree about which columns exist or what order
they are in — the failure #213 shipped was a statement and its parameters
drifting apart, and this is the shape of change that invites it.

It lived in `artemis/` for slice 2 because the Lambda called none of it and
`knowledge/` is a hashed package path (PACKAGE-IDENTITY) — shipping it there
would have moved the package hash for code that could not run. Site 2 (the
makeup swap) changed that: it IS Lambda code, so the helper moved here and the
package hash moved with it, which is correct now and was not then.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

# The column list, written ONCE. Both statements below are generated from it.
_COLUMNS: tuple[str, ...] = (
    "agent", "persona", "action", "domain", "confidence", "outcome",
    "token_count", "api_cost_usd", "metadata", "assumptions", "correction",
    "manual_gap",
)
_JSONB = frozenset({"metadata", "assumptions", "correction"})


def _sql(placeholder, cast) -> str:
    vals = ", ".join(cast(c) if c in _JSONB else placeholder(c) for c in _COLUMNS)
    return (f"INSERT INTO acos.audit_log ({', '.join(_COLUMNS)}) "
            f"VALUES ({vals}) RETURNING id")


SQL_PG = _sql(lambda _c: "%s", lambda _c: "%s::jsonb")
SQL_SA = _sql(lambda c: f":{c}", lambda c: f"CAST(:{c} AS jsonb)")


def _jsonb(value: dict | None) -> str | None:
    """jsonb, or NULL for a column that genuinely has no value. `default=str`
    so a date or Decimal cannot turn a decision record into an exception at the
    write."""
    if value is None:
        return None
    return json.dumps(value, default=str, sort_keys=True)


def _values(*, agent: str, action: str, domain: str, outcome: str,
            assumptions: dict | None, metadata: dict | None,
            correction: dict | None, manual_gap: bool | None,
            persona: str | None, confidence: float | None) -> dict:
    """Validate once, for both dialects.

    `assumptions` is required for a decision row and must be a dict: a decision
    row without it is just an audit row, and this helper exists to stop that
    happening by accident. An OUTCOME row passes None on purpose — see
    `log_outcome`.

    `manual_gap` must be a real bool on a decision row, never NULL-as-maybe
    (§6 COGNITION-1 rule 3).
    """
    if assumptions is not None and not isinstance(assumptions, dict):
        raise TypeError(f"assumptions must be a dict, got {type(assumptions).__name__}")
    if assumptions is not None and not isinstance(manual_gap, bool):
        raise TypeError(f"a decision row needs manual_gap as a bool, got {manual_gap!r}")
    for name, v in (("metadata", metadata), ("correction", correction)):
        if v is not None and not isinstance(v, dict):
            raise TypeError(f"{name} must be a dict or None, got {type(v).__name__}")
    return {"agent": agent, "persona": persona, "action": action, "domain": domain,
            "confidence": confidence, "outcome": outcome, "token_count": 0,
            "api_cost_usd": 0.0, "metadata": _jsonb(metadata or {}),
            "assumptions": _jsonb(assumptions), "correction": _jsonb(correction),
            "manual_gap": manual_gap}


def _require_decision(assumptions, manual_gap) -> None:
    """A decision row without `assumptions` is just an audit row, and this module
    exists to stop that happening by accident. `_values` tolerates None because
    `log_outcome` passes it deliberately, so the guard lives here, on the two
    decision entry points, rather than there.
    """
    if not isinstance(assumptions, dict):
        raise TypeError("a decision row needs assumptions as a dict, got "
                        f"{type(assumptions).__name__}")
    if not isinstance(manual_gap, bool):
        raise TypeError(f"a decision row needs manual_gap as a bool, got {manual_gap!r}")


def _id(row):
    if row is None:
        return None
    if isinstance(row, dict):
        return row.get("id")
    return row[0]


def log_decision(cur, *, agent: str, action: str, domain: str, outcome: str,
                 assumptions: dict, metadata: dict | None = None,
                 correction: dict | None = None, manual_gap: bool = False,
                 persona: str | None = None, confidence: float | None = None):
    """psycopg2. Write one decision row on `cur`; return its id.

    Raises nothing the caller should catch — it runs inside the caller's
    transaction, so a failure here rolls back the decision it describes. That is
    the correct coupling: a decision that could not be recorded should not stand.
    """
    _require_decision(assumptions, manual_gap)
    v = _values(agent=agent, action=action, domain=domain, outcome=outcome,
                assumptions=assumptions, metadata=metadata, correction=correction,
                manual_gap=manual_gap, persona=persona, confidence=confidence)
    cur.execute(SQL_PG, tuple(v[c] for c in _COLUMNS))
    return _id(cur.fetchone())


def log_decision_sa(db, *, agent: str, action: str, domain: str, outcome: str,
                    assumptions: dict, metadata: dict | None = None,
                    correction: dict | None = None, manual_gap: bool = False,
                    persona: str | None = None, confidence: float | None = None):
    """SQLAlchemy (the Lambda). Same row, same columns, `:name` binds.

    `db` is a Session: this joins the caller's transaction and does NOT commit,
    so the route's existing commit/rollback still decides whether the decision
    and its record stand or fall together.
    """
    from sqlalchemy import text
    _require_decision(assumptions, manual_gap)
    v = _values(agent=agent, action=action, domain=domain, outcome=outcome,
                assumptions=assumptions, metadata=metadata, correction=correction,
                manual_gap=manual_gap, persona=persona, confidence=confidence)
    return _id(db.execute(text(SQL_SA), v).fetchone())


OUTCOME_SUFFIX = ".outcome"


def outcome_action(decision_action: str) -> str:
    """`checkin_adjust` -> `checkin_adjust.outcome`. One spelling, so the query
    that finds decisions without outcomes and the writer that fills them cannot
    disagree."""
    return f"{decision_action}{OUTCOME_SUFFIX}"


def log_outcome(cur, *, decides, action: str, domain: str, outcome: str,
                metadata: dict | None = None, correction: dict | None = None,
                manual_gap: bool | None = None, agent: str = "cognition"):
    """Append the outcome of an earlier decision as its OWN row (decision (a),
    Ryan 2026-09-28) — a decision row is never UPDATEd.

    `assumptions` is deliberately NULL here, and that is load-bearing in two
    ways: an outcome is not a decision, and the query that looks for decisions
    needing outcomes selects on `assumptions IS NOT NULL`, so an outcome row can
    never be mistaken for a decision that itself needs an outcome. Without that,
    the job would append a row for its own output every night, forever.
    """
    meta = dict(metadata or {})
    meta["decides"] = str(decides)
    # `manual_gap` rides on the OUTCOME row, not the decision, because a decision
    # row is never UPDATEd (decision (a)) and the gap is only knowable once the
    # correction exists. The partial index is on audit_log, so either row is found.
    v = _values(agent=agent, action=outcome_action(action), domain=domain,
                outcome=outcome, assumptions=None, metadata=meta,
                correction=correction, manual_gap=manual_gap, persona=None,
                confidence=None)
    cur.execute(SQL_PG, tuple(v[c] for c in _COLUMNS))
    return _id(cur.fetchone())
