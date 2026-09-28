"""COGNITION-1 — the outcome of a decision, APPENDED as its own row.

A decision row says "this was chosen on these assumptions". An outcome row says
what became of it. Ryan's decision (a), 2026-09-28: the outcome is a SECOND ROW
referencing the first (`metadata.decides` = the decision's id). **A decision row
is never UPDATEd** — append-only survives, and the outcome becomes a dated fact
in its own right rather than a silent overwrite of one.

Deterministic only. Every field here is read out of RDS and compared; nothing is
inferred, scored or summarised, and no LLM is involved at any point (§3, the
statistics-vs-semantics wall). An outcome this module cannot compute is one it
does not write.

Three answers, and they are different on purpose:

* a **Resolution** — the outcome is knowable now, so write it;
* **None** — NOT YET knowable (the day hasn't ended, the week hasn't finished,
  the nutrition day isn't locked). Write nothing and try again tomorrow. This is
  the common case for a decision made today, not an error;
* **OutcomeUnreadable** — the source could not be read, or the decision row is
  missing the field its own outcome needs. Write nothing, log it, move on
  (FAIL-CLOSED-RESOLVERS): a guessed outcome is worse than a missing one,
  because it is indistinguishable from a real one afterwards.

Only actions with a resolver are eligible. That is deliberate rather than
tidiness: a decision the job can never close would otherwise be re-read every
night forever, and writing a fake `not_applicable` outcome to silence it would
put a row in the ledger that means nothing happened.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta

logger = logging.getLogger(__name__)


class OutcomeUnreadable(RuntimeError):
    """The source could not be read. NOT "there is no outcome"."""


@dataclass
class Resolution:
    outcome: str
    metadata: dict = field(default_factory=dict)
    correction: dict | None = None


# The columns the runner selects, in order. Tuple cursors, like the rest of the
# box code — named once so the SELECT and the unpacking cannot drift.
_COLS = ("id", "action", "domain", "outcome", "assumptions", "metadata", "created_at")

_PENDING_SQL = f"""
SELECT {', '.join('d.' + c for c in _COLS)}
FROM acos.audit_log d
WHERE d.assumptions IS NOT NULL
  AND d.action = ANY(%s)
  AND NOT EXISTS (
        SELECT 1 FROM acos.audit_log o
         WHERE o.action = d.action || '.outcome'
           AND o.metadata->>'decides' = d.id::text)
ORDER BY d.created_at
LIMIT %s
"""

# The same NOT EXISTS, for one decision, re-checked immediately before writing.
# The list query is evaluated before any of this run's writes, so it alone cannot
# make a second call in the same transaction idempotent. This can.
_ALREADY_SQL = """
SELECT 1 FROM acos.audit_log
 WHERE action = %s AND metadata->>'decides' = %s
 LIMIT 1
"""


def _scalar(cur):
    row = cur.fetchone()
    if row is None:
        return None
    return list(row.values())[0] if isinstance(row, dict) else row[0]


def _as_date(value) -> date | None:
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _logged(cur, plan_id) -> bool:
    """A real log — `inferred` rows are the nightly backstop, not him training."""
    cur.execute("SELECT count(*) FROM health.session_log "
                "WHERE plan_id = %s AND logged_via <> 'inferred'", (plan_id,))
    return (_scalar(cur) or 0) > 0


# ---------------------------------------------------------------------------
# Site 1 — the check-in decision
# ---------------------------------------------------------------------------

#: Decision outcomes that mean the ladder changed nothing, so "as adjusted"
#: would be a false distinction.
_NO_CHANGE = frozenset({"no_change", "no_change_light_day", "no_adjust_sets_logged",
                        "flag_off"})


def resolve_checkin(cur, row: dict, today: date) -> Resolution | None:
    a = row["assumptions"] or {}
    plan_id, plan_date = a.get("plan_id"), _as_date(a.get("plan_date"))
    if plan_id is None or plan_date is None:
        raise OutcomeUnreadable("the decision carries no plan_id/plan_date")
    if plan_date >= today:
        return None                       # the day hasn't ended; ask again tomorrow

    logged = _logged(cur, plan_id)
    # "As adjusted or as original" is answered by whether he replied `original`,
    # which leaves its own audit row. Reading the plan row's blocks instead would
    # be reading mutable state that anything since could have changed.
    cur.execute("SELECT count(*) FROM acos.audit_log "
                "WHERE action = 'checkin_restore' AND metadata->>'plan_id' = %s "
                "AND created_at >= %s", (str(plan_id), row["created_at"]))
    restored = (_scalar(cur) or 0) > 0
    adjusted = row["outcome"] not in _NO_CHANGE

    if not logged:
        outcome = "not_logged"
    elif restored:
        outcome = "logged_as_original"
    elif adjusted:
        outcome = "logged_as_adjusted"
    else:
        outcome = "logged"
    correction = None
    if restored:
        correction = {"what": "replied `original` — ran the session as written",
                      "source": "acos.audit_log checkin_restore"}
    return Resolution(outcome, {"plan_id": plan_id, "plan_date": plan_date.isoformat(),
                                "logged": logged, "restored": restored}, correction)


# ---------------------------------------------------------------------------
# Site 2 — the makeup swap
# ---------------------------------------------------------------------------

def resolve_makeup(cur, row: dict, today: date) -> Resolution | None:
    m = row["metadata"] or {}
    rest_plan_id, on = m.get("rest_plan_id"), _as_date(m.get("on"))
    if rest_plan_id is None or on is None:
        raise OutcomeUnreadable("the decision carries no rest_plan_id/on")
    if on >= today:
        return None

    logged = _logged(cur, rest_plan_id)
    # Ryan's spec: "if he skips it, that is the correction."
    correction = (None if logged else
                  {"what": "skipped — the swapped-in session was never logged on the rest day"})
    return Resolution("logged" if logged else "not_logged",
                      {"rest_plan_id": rest_plan_id, "on": on.isoformat(), "logged": logged},
                      correction)


# ---------------------------------------------------------------------------
# Site 3 — the week repeat
# ---------------------------------------------------------------------------

def resolve_repeat(cur, row: dict, today: date) -> Resolution | None:
    a = row["assumptions"] or {}
    repeat_start = _as_date(a.get("repeat_start"))
    if repeat_start is None:
        raise OutcomeUnreadable("the decision carries no repeat_start")
    week_end = repeat_start + timedelta(days=6)
    if week_end >= today:
        return None                       # the repeated week hasn't finished

    from artemis import program_repeat
    try:
        not_done = program_repeat.not_done_in(repeat_start)
    except Exception as exc:              # noqa: BLE001
        raise OutcomeUnreadable(f"could not read the repeated week: {exc}") from exc

    # Ryan's spec: a second repeat of the same week is the strongest correction
    # signal there is.
    cur.execute("SELECT count(*) FROM acos.audit_log "
                "WHERE action = 'repeat_week' AND assumptions->>'repeat_start' = %s "
                "AND created_at > %s", (repeat_start.isoformat(), row["created_at"]))
    repeated_again = (_scalar(cur) or 0) > 0

    correction = ({"what": "the same week was repeated again"} if repeated_again else None)
    return Resolution("week_clean" if not not_done else "week_not_done",
                      {"repeat_start": repeat_start.isoformat(),
                       "not_done_count": len(not_done),
                       "not_done": [f"{m.get('plan_date')} {m.get('display_name')}"
                                    for m in not_done],
                       "repeated_again": repeated_again},
                      correction)


# ---------------------------------------------------------------------------
# Site 4 — the nutrition pre-fill
# ---------------------------------------------------------------------------

def resolve_prefill(cur, row: dict, today: date) -> Resolution | None:
    a = row["assumptions"] or {}
    day = _as_date(a.get("day"))
    if day is None:
        raise OutcomeUnreadable("the decision carries no day")

    cur.execute("SELECT status FROM nutrition.day WHERE day_date = %s", (day,))
    status = _scalar(cur)
    if status is None:
        return None                       # the day row is gone or not written yet
    from artemis.nutrition import DAY_ASSUMED
    if status == DAY_ASSUMED:
        return None                       # still inside the 48h window

    cur.execute("SELECT count(*) FROM nutrition.entry "
                "WHERE day_date = %s AND status = 'corrected'", (day,))
    corrected = _scalar(cur) or 0
    correction = ({"what": f"{corrected} entr{'y' if corrected == 1 else 'ies'} corrected",
                   "corrected_entries": corrected} if corrected else None)
    return Resolution(status, {"day": day.isoformat(), "status_at_lock": status,
                               "corrected_entries": corrected}, correction)


RESOLVERS = {
    "checkin_adjust": resolve_checkin,
    "checkin_adjust_suppressed": resolve_checkin,
    "makeup_swap": resolve_makeup,
    "repeat_week": resolve_repeat,
    "nutrition_prefill": resolve_prefill,
}


# ---------------------------------------------------------------------------
# The runner
# ---------------------------------------------------------------------------

def pending(cur, *, limit: int = 500) -> list[dict]:
    """Decision rows with no outcome row yet, for the actions we can close."""
    cur.execute(_PENDING_SQL, (sorted(RESOLVERS), limit))
    return [dict(zip(_COLS, r)) for r in cur.fetchall()]


def run(cur, *, today: date | None = None, limit: int = 500) -> dict:
    """Append one outcome row per closable decision. Returns a count summary.

    Silent: writes only, posts nothing. Runs inside the caller's transaction.
    """
    from knowledge import cognition
    if today is None:
        from artemis.quiet_hours import local_today
        today = local_today()

    counts = {"considered": 0, "wrote": 0, "not_yet": 0, "unreadable": 0, "raced": 0}
    for row in pending(cur, limit=limit):
        counts["considered"] += 1
        try:
            res = RESOLVERS[row["action"]](cur, row, today)
        except OutcomeUnreadable as exc:
            counts["unreadable"] += 1
            logger.warning("outcome for %s %s unreadable: %s", row["action"], row["id"], exc)
            continue
        except Exception:                                   # noqa: BLE001
            counts["unreadable"] += 1
            logger.exception("outcome for %s %s failed", row["action"], row["id"])
            continue
        if res is None:
            counts["not_yet"] += 1
            continue
        # Idempotence, re-checked against the rows this run has already written —
        # the list query was evaluated before any of them existed.
        cur.execute(_ALREADY_SQL, (cognition.outcome_action(row["action"]), str(row["id"])))
        if cur.fetchone() is not None:
            counts["raced"] += 1
            continue
        cognition.log_outcome(cur, decides=row["id"], action=row["action"],
                              domain=row["domain"] or "health", outcome=res.outcome,
                              metadata=res.metadata, correction=res.correction)
        counts["wrote"] += 1
    return counts
