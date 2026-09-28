"""PROGRAM-2 — the interval GATE. Three conditions, fail-closed.

Zone 4 work is the sharpest thing in the program, and the reasons not to do it on
a given day are medical, structural and physical in that order. So a
`cardio_intervals` row runs as intervals only if ALL of:

  a. **Ryan has been cleared.** He sends `intervals cleared` after his VA provider
     says vigorous exercise is fine. **Artemis never sets this**, under any
     circumstances — it is the Brad Spaits rule applied to his body rather than
     his mailbox. `intervals not cleared` revokes it.
  b. **He is actually training.** ≥ 4 of the last 6 PLANNED cardio sessions carry
     a real log. Intervals on top of a fortnight of missed sessions is how people
     get hurt.
  c. **Nothing hurts.** No check-in pain ≥ 3 in the last 7 days.

FAIL-CLOSED, and that is the whole design: any condition that is false, and any
condition that cannot be READ, sends the day to the Z2 variant. A gate that
answered "go ahead" because the database was down would be the one failure mode
that matters, and "I could not check" and "you are cleared" must never look the
same.

The result is recorded as a COGNITION decision row with the three conditions as
its assumptions, so a day that ran Z2 can be argued with later.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta

logger = logging.getLogger(__name__)

#: acos.system_state key. Human-gated: only a chat command Ryan sends writes it.
CLEARED_KEY = "intervals_cleared"

#: (b) and (c), as numbers rather than magic literals in the SQL below.
RECENT_CARDIO = 6
RECENT_CARDIO_MIN_LOGGED = 4
PAIN_WINDOW_DAYS = 7
PAIN_THRESHOLD = 3


@dataclass
class GateResult:
    ok: bool
    #: The one line a card or a post shows when `ok` is False.
    reason: str | None = None
    #: Each condition: True, False, or None for "could not be read".
    conditions: dict = field(default_factory=dict)
    detail: dict = field(default_factory=dict)


def _cleared(cur) -> bool | None:
    try:
        cur.execute("SELECT value FROM acos.system_state WHERE key = %s", (CLEARED_KEY,))
        row = cur.fetchone()
    except Exception:                                           # noqa: BLE001
        logger.warning("interval gate: could not read %s", CLEARED_KEY, exc_info=True)
        return None
    if row is None:
        return False
    value = row["value"] if isinstance(row, dict) else row[0]
    return str(value).strip().lower() in ("1", "true", "yes", "cleared")


def _training(cur, today: date) -> tuple[bool | None, dict]:
    """≥ RECENT_CARDIO_MIN_LOGGED of the last RECENT_CARDIO planned cardio rows
    carry a real log. `inferred` rows are the nightly backstop, not him training."""
    try:
        cur.execute(
            "SELECT p.plan_date, EXISTS (SELECT 1 FROM health.session_log sl "
            "  WHERE sl.plan_id = p.plan_id AND sl.logged_via <> 'inferred') "
            "FROM health.plan p "
            "WHERE p.session_type IN ('cardio_z2', 'cardio_intervals') "
            "  AND p.plan_date < %s "
            "ORDER BY p.plan_date DESC LIMIT %s", (today, RECENT_CARDIO))
        rows = cur.fetchall()
    except Exception:                                           # noqa: BLE001
        logger.warning("interval gate: could not read recent cardio", exc_info=True)
        return None, {}
    logged = sum(1 for r in rows for v in [r[1] if not isinstance(r, dict)
                                           else list(r.values())[1]] if v)
    return logged >= RECENT_CARDIO_MIN_LOGGED, {
        "recent_cardio_rows": len(rows), "recent_cardio_logged": logged,
        "recent_cardio_needed": RECENT_CARDIO_MIN_LOGGED}


def _no_pain(cur, today: date) -> tuple[bool | None, dict]:
    """No check-in pain ≥ PAIN_THRESHOLD in the last PAIN_WINDOW_DAYS."""
    since = today - timedelta(days=PAIN_WINDOW_DAYS - 1)
    try:
        cur.execute("SELECT state_date, soreness FROM health.daily_state "
                    "WHERE state_date BETWEEN %s AND %s", (since, today))
        rows = cur.fetchall()
    except Exception:                                           # noqa: BLE001
        logger.warning("interval gate: could not read check-in pain", exc_info=True)
        return None, {}
    import json
    worst, worst_day = 0, None
    for r in rows:
        d, sore = (r[0], r[1]) if not isinstance(r, dict) else (r["state_date"], r["soreness"])
        if not sore:
            continue
        if isinstance(sore, str):
            try:
                sore = json.loads(sore)
            except ValueError:
                continue
        for region, score in (sore.get("pain") or {}).items():
            if isinstance(score, (int, float)) and score > worst:
                worst, worst_day = score, f"{d} {region} {score}"
    return worst < PAIN_THRESHOLD, {"worst_pain": worst, "worst_pain_at": worst_day,
                                    "pain_window_days": PAIN_WINDOW_DAYS}


def evaluate(cur, today: date) -> GateResult:
    """All three, fail-closed. Never raises: an exception inside a condition is
    that condition returning None, which is a FAIL."""
    cleared = _cleared(cur)
    training, t_detail = _training(cur, today)
    no_pain, p_detail = _no_pain(cur, today)
    conditions = {"cleared": cleared, "training": training, "no_pain": no_pain}

    failed = []
    if cleared is None:
        failed.append("I couldn't check whether you're cleared")
    elif not cleared:
        failed.append("you haven't sent `intervals cleared` yet")
    if training is None:
        failed.append("I couldn't read your recent cardio")
    elif not training:
        failed.append(f"only {t_detail.get('recent_cardio_logged', 0)} of the last "
                      f"{t_detail.get('recent_cardio_rows', 0)} cardio sessions are logged")
    if no_pain is None:
        failed.append("I couldn't read your recent check-ins")
    elif not no_pain:
        failed.append(f"pain {p_detail.get('worst_pain')} in the last "
                      f"{PAIN_WINDOW_DAYS} days ({p_detail.get('worst_pain_at')})")

    return GateResult(ok=not failed, reason=("; ".join(failed) if failed else None),
                      conditions=conditions, detail={**t_detail, **p_detail})


def record(cur, result: GateResult, day: date, location_key: str) -> None:
    """The gate's answer as a COGNITION decision row.

    Written whichever way it went: "it ran Z2 and I do not know why" is the
    question this exists to answer.
    """
    from knowledge import cognition
    cognition.log_decision(
        cur, agent="health_office", action="interval_gate", domain="health",
        outcome="intervals" if result.ok else "z2_variant", manual_gap=False,
        metadata={"day": day.isoformat(), "location_key": location_key},
        assumptions={
            "rule": "intervals run only if cleared AND training AND pain-free; "
                    "any false or unreadable condition runs the Z2 variant",
            "cleared": result.conditions.get("cleared"),
            "training": result.conditions.get("training"),
            "no_pain": result.conditions.get("no_pain"),
            "reason": result.reason,
            **result.detail,
        })
