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


# ── Where the gate is actually applied ──────────────────────────────────────
#
# ONE evaluation point: the wake job, once per local day, which REWRITES that
# day's row. Everything else -- the wake post, the card, the week view -- reads
# the row. That is the whole reason it is done here and not at card fetch:
#
#   * A row is seeded weeks ahead, so the gate cannot be evaluated when the row
#     is built. Something has to resolve it on the day.
#   * If the card evaluated the gate itself, the box and the Lambda would each
#     run their own evaluation against their own read at their own moment, and
#     the post and the card could disagree about the same session. Persisting the
#     answer makes them agree by construction, because there is only one answer.
#   * A cognition row per fetch would be noise. Per evaluation day is the unit
#     the decision is actually made in.
#
# The card needs no gate logic at all, which is also why this change ships no
# Lambda code.

#: acos.system_state key prefix for "this day has been resolved".
RESOLVED_PREFIX = "intervals_resolved:"


def _resolved_key(day: date) -> str:
    return f"{RESOLVED_PREFIX}{day.isoformat()}"


def _already_resolved(cur, day: date) -> bool:
    """Raises rather than swallowing. An unreadable marker must not silently
    become a second evaluation and a second cognition row."""
    cur.execute("SELECT value FROM acos.system_state WHERE key = %s", (_resolved_key(day),))
    return cur.fetchone() is not None


def _mark_resolved(cur, day: date, result: GateResult) -> None:
    cur.execute(
        "INSERT INTO acos.system_state (key, value, updated_at) VALUES (%s, %s, now()) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
        (_resolved_key(day), "intervals" if result.ok else "z2_variant"))


def _has_real_log(cur, plan_id: int) -> bool:
    cur.execute("SELECT 1 FROM health.session_log WHERE plan_id = %s "
                "AND logged_via <> 'inferred' LIMIT 1", (plan_id,))
    return cur.fetchone() is not None


def resolve_day(cur, day: date) -> dict:
    """Resolve `day`'s `cardio_intervals` rows against the gate. Idempotent.

    Returns {"resolved": bool, "outcome": str|None, "reason": str|None,
             "rows": [plan_date…], "skipped": [...]}. `resolved` is False when
    there was nothing to do -- no interval row, or already done today.

    Called from the wake job. It does NOT swallow database errors: a failure
    leaves the row as it was, and a row that has not been resolved is the Z2
    variant, so failing loudly here still fails closed for Ryan.
    """
    from artemis import health_office as office

    specs = [s for s in office.build_schedule()
             if s["plan_date"] == day and s["session_type"] == "cardio_intervals"]
    if not specs:
        return {"resolved": False, "outcome": None, "reason": None, "rows": [], "skipped": []}
    if _already_resolved(cur, day):
        logger.info("interval gate: %s already resolved — not re-evaluating", day)
        return {"resolved": False, "outcome": None, "reason": None, "rows": [], "skipped": []}

    result = evaluate(cur, day)
    written, skipped = [], []
    for spec in specs:
        slot = spec.get("slot", "morning")
        cur.execute("SELECT plan_id FROM health.plan WHERE plan_date = %s AND slot = %s",
                    (day, slot))
        existing = cur.fetchone()
        if existing is not None:
            plan_id = existing["plan_id"] if isinstance(existing, dict) else existing[0]
            if _has_real_log(cur, plan_id):
                # He already trained it. Rewriting a session that happened would
                # replace what he did with what the gate now thinks.
                skipped.append(f"{day} {slot} (already logged)")
                continue
        row = office.build_row({**spec, "gate": {"ok": result.ok, "reason": result.reason}})
        cur.execute(office._UPSERT_SQL, office.upsert_params(row))
        written.append(f"{day} {slot}")

    record(cur, result, day, specs[0].get("location_key", "office"))
    _mark_resolved(cur, day, result)
    logger.info("interval gate %s: %s (%s)", day,
                "intervals" if result.ok else "z2_variant", result.reason or "all conditions pass")
    return {"resolved": True, "outcome": "intervals" if result.ok else "z2_variant",
            "reason": result.reason, "rows": written, "skipped": skipped}
