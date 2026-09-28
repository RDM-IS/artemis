"""COGNITION-1 — `manual_gap`: "Ryan did this by hand and Artemis had no path."

A structural fact, not a judgement about quality, and set by RULE ONLY. No LLM
reads or writes this column (§3, the statistics-vs-semantics wall). An LLM may
later *summarise* the gap list for a human to read — description, not authorship —
but the flag itself only ever comes from the two rules below.

THE TWO RULES, exactly as merged in the §6 entry. There is no third setter.

1. **An email disposition corrected with no active rule covering it.**
   `domain = 'email'`, a non-null `correction`, and no active `acos.playbook_rules`
   row whose `action` equals the corrected-to action. Evaluated where the
   correction is appended — the outcome job.

   Scoped to email ON PURPOSE, because that is the only domain where a
   data-driven automation rule can exist: `acos.playbook_rules` matches on
   sender/subject/body and its `action` is CHECK-constrained to
   `archive | spam | file` (migration 023). Asked about any other action it
   answers "no rule", which would set the flag on every health and nutrition
   correction ever made — backwards, since those sites HAVE automation that was
   overridden, which is what `correction` is for. A gap list containing every
   correction is not a gap list.

   **It has no eligible rows yet, and that is worth saying plainly rather than
   hiding behind the code being present.** Email dispositions write audit rows,
   not DECISION rows — they carry no `assumptions` — so the outcome job never sees
   them and this rule cannot fire until email disposition becomes a decision site.
   It is implemented and tested so that it fires the day it can.

2. **`source = 'script'`.** Set at WRITE TIME by the script itself, which is the
   only place that knows. The wording this replaced — "a script writing an audit
   row for something no scheduled job does" — was not computable: nothing joins an
   action string to an inventory of what the scheduled jobs do.

3. Everything else is **false, explicitly** — never NULL-as-maybe.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

logger = logging.getLogger(__name__)

#: `gaps` periods, in trailing days. Trailing rather than calendar-aligned so the
#: answer never depends on which day of the week it is asked.
PERIODS: dict[str, int] = {"week": 7, "month": 30}
DEFAULT_PERIOD = "week"


def is_gap_by_script(source: str | None) -> bool:
    """Rule 2. Pure, so the scripts can call it and the tests can pin it."""
    return source == "script"


def is_gap_by_email_correction(cur, *, domain: str | None, correction: dict | None,
                               corrected_to_action: str | None) -> bool:
    """Rule 1. False for every non-email domain by design — see the module docstring.

    Raises nothing: an unreadable `playbook_rules` returns False, because marking
    a gap on the strength of a failed read would put a fact in the ledger that was
    never established. A missed gap is recoverable; an invented one is not.
    """
    if domain != "email" or not correction:
        return False
    if not corrected_to_action:
        return False
    try:
        cur.execute("SELECT 1 FROM acos.playbook_rules "
                    "WHERE active AND action = %s LIMIT 1", (corrected_to_action,))
        return cur.fetchone() is None
    except Exception:                                           # noqa: BLE001
        logger.warning("manual_gap rule 1: could not read acos.playbook_rules — "
                       "not marking a gap", exc_info=True)
        return False


# ---------------------------------------------------------------------------
# The `gaps` listing — read-only
# ---------------------------------------------------------------------------

def gaps(cur, period: str = DEFAULT_PERIOD, *, today: date | None = None) -> list[dict]:
    """manual_gap rows in the trailing period, grouped by action.

    The window is anchored to the ACTIVE timezone (CLAUDE.md): `today` comes from
    `quiet_hours.local_today()`, never from `current_date`, which is a day ahead of
    Ryan after ~7pm because RDS runs UTC.
    """
    if period not in PERIODS:
        raise ValueError(f"unknown period {period!r}; expected one of {sorted(PERIODS)}")
    if today is None:
        from artemis.quiet_hours import local_today
        today = local_today()
    since = today - timedelta(days=PERIODS[period] - 1)
    cur.execute(
        "SELECT action, count(*) AS n, max(created_at) AS latest "
        "  FROM acos.audit_log "
        " WHERE manual_gap "
        "   AND (created_at AT TIME ZONE %s)::date >= %s "
        " GROUP BY action ORDER BY n DESC, action",
        (_tz(), since))
    rows = cur.fetchall()
    out = []
    for r in rows:
        action, n, latest = (r["action"], r["n"], r["latest"]) if isinstance(r, dict) else r
        out.append({"action": action, "count": n, "latest": latest})
    return out


def _tz() -> str:
    from artemis.quiet_hours import get_active_timezone
    return get_active_timezone()


def render(rows: list[dict], period: str) -> str:
    """The reply. Deterministic text, no advice and no interpretation — a gap is a
    structural fact, and what to do about one is Ryan's call."""
    label = f"the last {PERIODS.get(period, '?')} days"
    if not rows:
        return f"No manual gaps recorded in {label}."
    total = sum(r["count"] for r in rows)
    lines = [f"**Manual gaps — {label}** ({total} row{'s' if total != 1 else ''}):"]
    for r in rows:
        latest = r["latest"]
        when = latest.date().isoformat() if hasattr(latest, "date") else str(latest)[:10]
        lines.append(f"  `{r['action']}` — {r['count']}, most recent {when}")
    return "\n".join(lines)
