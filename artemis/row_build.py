"""ROW-BUILD — the rowing build phase (Ryan, 2026-10-04).

A steady 40 minutes on the rower was not doable: the tempo fell apart after two
or three minutes while heart rate was still low. So a cardio session on the
rower runs as work intervals at a fixed STROKE RATE with a one-minute rest, and
the session it runs is a step on a ladder he set himself:

    step 1   4 min at 22 strokes/min, 1 min stopped        x 8   (40 min)
    step 2   5 min at 22,             1 min stopped        x 7   (42 min)
    step 3   4 min at 22,             1 min easy at 18     x 8
    step 4   5 min at 22,             1 min easy at 18     x 7
    step 5   4 min at 23,             1 min stopped        x 8
    ...                                                    and so on, +1 per four

Every round is followed by its minute; the total lands where it lands.

**The effort is conversational. Heart rate is recorded, not targeted** — there
are no real zones yet (ZONE-1), and the rate is the instruction. That is also why
this is NOT behind the interval gate: the gate guards Zone 4 work, and this is
the same easy session as the Z2 variant, cut into pieces he can finish.

WHICH STEP is derived, never stored: the last row-build session with a real log
decides. Finished -> the next step. Cut short -> the same step again. `row
repeat` holds the step when the clock ran out but the rate did not hold, because
a water rower reports nothing and only he knows.

ONE function shapes the block (`apply`), used by the seeder and by the on-the-day
resolver, so a seeded row and a resolved row cannot describe two different
sessions.
"""

from __future__ import annotations

import copy
import json
import logging
from datetime import date

logger = logging.getLogger(__name__)

START_RATE = 22
REST_SEC = 60
#: The active rest, in strokes per minute. 0 means stopped.
ACTIVE_REST_RATE = 18
#: (work minutes, rounds). Rounds are the count that lands nearest 40 minutes
#: with a rest after every round: 8 x (4+1) = 40, 7 x (5+1) = 42.
SHAPES: tuple[tuple[int, int], ...] = ((4, 8), (5, 7))
#: Four sessions at each rate: both shapes stopped, then both with active rest.
STEPS_PER_RATE = 4

DISPLAY_NAME = "Row build"
#: A session counts as finished when the logged time reaches the last WORK
#: interval's end, less this. The screen skips the final rest, and a timer
#: stopped a few seconds early is not a missed round.
FINISH_TOLERANCE_SEC = 60

#: acos.system_state key. Its value is the plan_date of the session being held:
#: a hold names ONE session, so it cannot outlive the session it was about.
HOLD_KEY = "row_build_hold"


def step_spec(step: int) -> dict:
    """The session a 1-based step runs. Pure."""
    if step < 1:
        raise ValueError(f"row build step must be >= 1, got {step}")
    i = step - 1
    k = i % STEPS_PER_RATE
    work_min, rounds = SHAPES[k % len(SHAPES)]
    return {
        "step": step,
        "rate": START_RATE + i // STEPS_PER_RATE,
        "work_min": work_min,
        "rounds": rounds,
        "rest_sec": REST_SEC,
        "rest_rate": 0 if k < len(SHAPES) else ACTIVE_REST_RATE,
    }


def total_min(spec: dict) -> int:
    """Rest after EVERY round, the last included (Ryan: "always 1 minute rest")."""
    return round(spec["rounds"] * (spec["work_min"] * 60 + spec["rest_sec"]) / 60)


def finish_sec(spec: dict) -> int:
    """Seconds on the clock when the last work interval ends."""
    return spec["rounds"] * (spec["work_min"] * 60 + spec["rest_sec"]) - spec["rest_sec"]


def _rest_words(spec: dict) -> str:
    return ("stopped" if not spec["rest_rate"]
            else f"easy at {spec['rest_rate']} strokes/min")


def describe(spec: dict) -> str:
    return (f"{spec['rounds']} × {spec['work_min']} min at {spec['rate']} strokes/min, "
            f"{spec['rest_sec'] // 60} min {_rest_words(spec)} after each "
            f"— {total_min(spec)} min")


def applies(blocks: dict) -> bool:
    """A cardio session on the rower that is not running Zone 4 intervals."""
    if not isinstance(blocks, dict):
        return False
    if (blocks.get("cardio") or {}).get("modality") != "row":
        return False
    if blocks.get("row_build"):
        return True
    if blocks.get("type") == "steady":
        return True
    # A gate-passed interval day keeps its Zone 4 session; that is the gate's call.
    return False


#: Lines `apply` writes itself, so a second pass does not stack them.
_OWN_NOTES = ("Conversational effort. Heart rate is recorded, not targeted.",)

#: Keys of the steady/Z2 shape that describe a session this replaces.
#: `zones` STAYS: every cardio row carries its work zone (tests/test_zones.py),
#: and ZONE-0 reads it to report time in zone. It is recorded against, not aimed
#: at -- the interval screen does not show it for this shape.
_DROP = ("duration_min", "target_range_min", "intervals", "warmup_min",
         "cooldown_min", "warmup_sec", "cooldown_sec")


def apply(blocks: dict, step: int | None = None, *, note: str | None = None) -> dict:
    """`blocks` reshaped into the row build at `step`. Pure; returns a new dict.

    `step=None` is the SEEDED state: the step cannot be known weeks ahead, so the
    row carries step 1's shape and says the step is set on the day. The resolver
    replaces it on the morning.

    The shape is the one gym-display's interval timer already runs
    (`rounds` + `intervals_template`), so the iPad needs no change.
    """
    provisional = step is None
    spec = step_spec(1 if provisional else step)
    out = copy.deepcopy(blocks)
    for key in _DROP:
        out.pop(key, None)
    work = f"Row at {spec['rate']} strokes/min"
    rest = ("Stop — rest" if not spec["rest_rate"]
            else f"Easy at {spec['rest_rate']} strokes/min")
    out.update({
        "type": "intervals",
        "display_name": DISPLAY_NAME,
        "rounds": spec["rounds"],
        "intervals_template": {
            "work_sec": spec["work_min"] * 60, "work_settings": work,
            "rest_sec": spec["rest_sec"], "rest_settings": rest,
        },
        "intensity": f"{spec['rate']} strokes/min",
        "row_build": {**spec, "provisional": provisional},
    })
    notes = [n for n in (out.get("setup_notes") or [])
             if isinstance(n, str) and "Zone 2" not in n and "conversational pace" not in n
             and not n.startswith("Row build") and not n.startswith("Intervals not today")
             and n not in _OWN_NOTES and not n.startswith("The step is set")]
    head = (f"Row build — {describe(spec)}." if provisional
            else f"Row build step {spec['step']} — {describe(spec)}.")
    lines = [head, "Conversational effort. Heart rate is recorded, not targeted."]
    if provisional:
        lines.append(note or "The step is set on the morning, from your last logged row.")
    out["setup_notes"] = lines + notes
    return out


def est_minutes(blocks: dict) -> int:
    return total_min(blocks["row_build"])


def summary_line(blocks: dict) -> str:
    """One line for the wake post and replies."""
    rb = blocks["row_build"]
    label = "Row build" if rb.get("provisional") else f"Row build step {rb['step']}"
    return f"· {label}: {describe(rb)}"


# ── Which step ──────────────────────────────────────────────────────────────

def _val(row, key: str, idx: int):
    return row[key] if isinstance(row, dict) else row[idx]


def _blocks(value) -> dict:
    if isinstance(value, str):
        return json.loads(value)
    return value or {}


def last_logged(cur, before: date) -> dict | None:
    """The most recent row-build session before `before` that carries a real log.

    Raises on a failed read -- the caller fails closed. `inferred` rows are the
    nightly backstop, not him rowing.
    """
    cur.execute(
        "SELECT p.plan_date, p.blocks, "
        "  (SELECT max(sl.duration_sec) FROM health.session_log sl "
        "    WHERE sl.plan_id = p.plan_id AND sl.logged_via <> 'inferred') AS duration_sec "
        "FROM health.plan p "
        "WHERE p.plan_date < %s AND p.blocks -> 'row_build' IS NOT NULL "
        "  AND EXISTS (SELECT 1 FROM health.session_log sl "
        "    WHERE sl.plan_id = p.plan_id AND sl.logged_via <> 'inferred') "
        "ORDER BY p.plan_date DESC LIMIT 1", (before,))
    row = cur.fetchone()
    if row is None:
        return None
    rb = _blocks(_val(row, "blocks", 1)).get("row_build") or {}
    return {"plan_date": _val(row, "plan_date", 0), "row_build": rb,
            "duration_sec": _val(row, "duration_sec", 2)}


def _held(cur, plan_date: date) -> bool:
    cur.execute("SELECT value FROM acos.system_state WHERE key = %s", (HOLD_KEY,))
    row = cur.fetchone()
    if row is None:
        return False
    return str(_val(row, "value", 0)).strip() == plan_date.isoformat()


def step_for(cur, day: date) -> dict:
    """The step `day` runs, and why. Raises on a failed read.

    {"step": int, "why": str, "last": {...} | None}
    """
    last = last_logged(cur, day)
    if last is None:
        return {"step": 1, "why": "no logged row-build session yet", "last": None}
    prev = int(last["row_build"].get("step") or 1)
    spec = step_spec(prev)
    when = last["plan_date"]
    if _held(cur, when):
        return {"step": prev, "why": f"`row repeat` holds step {prev} ({when})", "last": last}
    done = last["duration_sec"]
    need = finish_sec(spec) - FINISH_TOLERANCE_SEC
    if done is None:
        return {"step": prev, "last": last,
                "why": f"{when} was logged with no duration — step {prev} again"}
    if int(done) < need:
        return {"step": prev, "last": last,
                "why": f"{when} ran {int(done) // 60} of {finish_sec(spec) // 60} min "
                       f"— step {prev} again"}
    return {"step": prev + 1, "why": f"step {prev} finished on {when}", "last": last}


def _has_real_log(cur, plan_id: int) -> bool:
    cur.execute("SELECT 1 FROM health.session_log WHERE plan_id = %s "
                "AND logged_via <> 'inferred' LIMIT 1", (plan_id,))
    return cur.fetchone() is not None


def resolve_day(cur, day: date) -> dict:
    """Set `day`'s rower sessions to the step the logs say. Idempotent.

    Called from the wake job, AFTER the interval gate (which may have rebuilt the
    row). The step is derived, so a second run writes the same thing.

    Does not swallow database errors. A failure leaves the row as seeded -- step
    1's shape, saying the step is not confirmed -- which he can still do.

    Returns {"resolved": bool, "step": int|None, "why": str|None, "rows": [...],
             "skipped": [...]}.
    """
    cur.execute("SELECT plan_id, slot, blocks FROM health.plan WHERE plan_date = %s "
                "ORDER BY slot", (day,))
    todo, skipped = [], []
    for r in cur.fetchall():
        blocks = _blocks(_val(r, "blocks", 2))
        if not applies(blocks):
            continue
        plan_id, slot = _val(r, "plan_id", 0), _val(r, "slot", 1)
        if _has_real_log(cur, plan_id):
            # He already rowed it. Rewriting a session that happened would
            # replace what he did with what the ladder now says.
            skipped.append(f"{day} {slot} (already logged)")
            continue
        todo.append((plan_id, slot, blocks))
    if not todo:
        return {"resolved": False, "step": None, "why": None, "rows": [], "skipped": skipped}

    answer = step_for(cur, day)
    step, written = answer["step"], []
    for plan_id, slot, blocks in todo:
        new = apply(blocks, step)
        cur.execute("UPDATE health.plan SET blocks = %s::jsonb, est_duration_min = %s "
                    "WHERE plan_id = %s", (json.dumps(new), est_minutes(new), plan_id))
        # Assert the intended value, not just "no error": re-read and compare.
        cur.execute("SELECT blocks FROM health.plan WHERE plan_id = %s", (plan_id,))
        got = _blocks(_val(cur.fetchone(), "blocks", 0)).get("row_build") or {}
        if got.get("step") != step or got.get("provisional"):
            raise RuntimeError(f"row build: {day} {slot} did not persist step {step}: {got}")
        written.append(f"{day} {slot}")

    from knowledge import cognition
    spec = step_spec(step)
    cognition.log_decision(
        cur, agent="health_office", action="row_build_step", domain="health",
        outcome=f"step_{step}", manual_gap=False,
        metadata={"day": day.isoformat(), "rows": written},
        assumptions={
            "rule": "the last logged row-build session decides: finished -> next "
                    "step; cut short, no duration, or `row repeat` -> same step",
            "why": answer["why"],
            "session": describe(spec),
            "last_session": (answer["last"]["plan_date"].isoformat()
                             if answer["last"] else None),
            "last_duration_sec": answer["last"]["duration_sec"] if answer["last"] else None,
        })
    logger.info("row build %s: step %s (%s)", day, step, answer["why"])
    return {"resolved": True, "step": step, "why": answer["why"],
            "rows": written, "skipped": skipped}


def hold_last(cur, today: date) -> dict:
    """`row repeat`: hold the step of the last logged session. Raises on a failed
    read or write. {"held": bool, "step": int|None, "plan_date": date|None}"""
    # `today + 1` so a session logged this morning is the one being held.
    from datetime import timedelta
    last = last_logged(cur, today + timedelta(days=1))
    if last is None:
        return {"held": False, "step": None, "plan_date": None}
    when = last["plan_date"]
    cur.execute(
        "INSERT INTO acos.system_state (key, value, updated_at) VALUES (%s, %s, now()) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
        (HOLD_KEY, when.isoformat()))
    return {"held": True, "step": int(last["row_build"].get("step") or 1), "plan_date": when}
