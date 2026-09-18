"""HEALTH-2 — office gym program (all Precor).

Canonical office inventory, the 9/16-11/03 schedule (phase 1, weeks 1-7), the
block builders, a structural validator, and the transactional writer shared by
scripts/reseed_health_plan_v2.py --office and scripts/validate_health_plan.py.

GO-LIVE reset: weeks run **Wed-Tue**, anchored on Wed 2026-09-16, so training
starts the morning after the seed. Rest days are Thu and Sat (was Wed and Sat)
— that avoids back-to-back strength days and keeps every training day on a
weekday, when the office gym is open. The pre-week-1 ramp-up days are gone:
9/16 IS week 1 day 1.

Location is PLAN DATA: every row carries blocks.location (and blocks.equipment),
which artemis.health.resolve_equipment_and_location prefers over its static
fallback map. The home gym still exists; the rower and outdoor bike are retired
from the plan.

Rows are written INSERT ... ON CONFLICT (plan_date) DO UPDATE in ONE transaction
with an acos.audit_log row, so plan_ids are stable and a session logged against a
date mid-run is never orphaned. generated_by='manual' (CHECK-legal); week_num is
1-7 (CHECK 1..19).
"""

import copy
import json
from datetime import date, timedelta

LOCATION = "office gym"
PHASE = 1
GENERATED_BY = "manual"

OFFICE_START = date(2026, 9, 16)   # Wed — week 1, day 1
WEEK1_START = date(2026, 9, 16)    # weeks run Wed..Tue from here
OFFICE_END = date(2026, 11, 3)     # Tue — last day of week 7

# ── Canonical office inventory (PB-009) ─────────────────────────────────────
EQ_LEG_PRESS = "leg press"
EQ_CALF_PRESS = "calf press"
EQ_PULLDOWN = "pulldown"
EQ_SEATED_ROW = "seated row"
EQ_LEG_CURL = "leg curl"
EQ_LEG_EXT = "leg extension"
EQ_REAR_DELT = "rear delt fly"
EQ_PEC_FLY = "pec fly"
EQ_AB = "ab machine"
EQ_CABLE = "functional trainer"
EQ_CABLE_ROPE = "functional trainer (rope)"
EQ_SMITH = "Smith machine"
EQ_DBS = "DBs"
EQ_FLAT_BENCH = "flat bench"
EQ_ADJ_BENCH = "adjustable bench"
EQ_CAPTAINS = "captain's chair"
# The Precor Abdominal / Back Extension machine (seated) — there is no 45°
# back extension / roman chair in the office gym.
EQ_BACK_EXT = "back extension machine"
EQ_TREADMILL = "treadmill"
EQ_ELLIPTICAL = "elliptical"
EQ_UPRIGHT = "upright bike"
EQ_RECUMBENT = "recumbent bike"
EQ_STEPMILL = "stepmill"
EQ_STRETCH = "Stretch Trainer"
EQ_MAT = "mat"

# session_type -> equipment list. artemis.health._EQUIPMENT_MAP is built from
# this, so the fallback map and the seeded blocks can't drift apart.
SESSION_EQUIPMENT: dict[str, list[str]] = {
    "strength_a": [EQ_LEG_PRESS, EQ_DBS, EQ_FLAT_BENCH, EQ_PULLDOWN, EQ_LEG_CURL,
                   EQ_CABLE_ROPE, EQ_CAPTAINS],
    "strength_b": [EQ_DBS, EQ_SEATED_ROW, EQ_ADJ_BENCH, EQ_LEG_EXT, EQ_REAR_DELT,
                   EQ_CABLE, EQ_BACK_EXT],
    "strength_c": [EQ_DBS, EQ_PEC_FLY, EQ_CABLE, EQ_ADJ_BENCH, EQ_CALF_PRESS, EQ_AB],
    "cardio_z2": [EQ_TREADMILL, EQ_ELLIPTICAL, EQ_RECUMBENT, EQ_UPRIGHT],
    "cardio_intervals": [EQ_STEPMILL, EQ_UPRIGHT],
    "walk": ["walking shoes"],
    "rest_mobility": [EQ_MAT, EQ_STRETCH],
    "recovery_flow": [EQ_MAT, EQ_STRETCH],
}

FIRST_LIFT = {"strength_a": "Leg press", "strength_b": "DB goblet squat",
              "strength_c": "DB Romanian deadlift"}

# Retired home-gym tokens that must never appear in an office row.
FORBIDDEN_TOKENS = ("rower", "bike on trainer", "road bike", "indoor trainer",
                    "powerblock", "trx", "walking pad")

WARMUP = "5 min elliptical, easy"
COOLDOWN = "5 min Stretch Trainer"
Z2_NOTES = "treadmill incline walk, elliptical, recumbent, or upright bike — conversational pace"
MACHINE_NOTE = "log seat + pin setting"

# ── Exercise lists: (name, rep range label, target_reps top, per_side, machine)
_EXERCISES: dict[str, list[tuple[str, str, int, bool, bool]]] = {
    "strength_a": [
        ("Leg press", "10-12", 12, False, True),
        ("DB bench press", "8-12", 12, False, False),
        ("Lat pulldown", "10-12", 12, False, True),
        ("Seated leg curl", "10-12", 12, False, True),
        ("Cable face pull (rope)", "12-15", 15, False, False),
        ("Captain's chair knee raise", "8-12", 12, False, False),
    ],
    "strength_b": [
        ("DB goblet squat", "8-12", 12, False, False),
        ("Seated cable row", "10-12", 12, False, True),
        ("Incline DB press", "8-12", 12, False, False),
        ("Leg extension", "10-12", 12, False, True),
        ("Rear delt fly", "12-15", 15, False, True),
        ("Cable Pallof press", "10", 10, True, False),
        ("Seated back extension", "10-12", 12, False, True),
    ],
    "strength_c": [
        ("DB Romanian deadlift", "8-12", 12, False, False),
        ("Pec fly", "10-12", 12, False, True),
        ("Single-arm cable row", "10-12", 12, True, False),
        ("Seated DB shoulder press", "8-12", 12, False, False),
        ("Calf press", "12-15", 15, False, True),
        ("Ab machine crunch", "10-12", 12, False, True),
    ],
}

# Explicit blocks.exercises[].equipment_class for names the keyword rules
# once read differently ("back extension" used to mean bodyweight).
EQUIPMENT_CLASS = {"Seated back extension": "machine"}

_DISPLAY = {
    "strength_a": "Office Strength A",
    "strength_b": "Office Strength B",
    "strength_c": "Office Strength C",
    "cardio_z2": "Zone 2 Cardio",
    "walk": "Walk",
    "rest_mobility": "Rest / Mobility",
    "recovery_flow": "Recovery Flow",
}

# Mon=0 .. Sun=6. Recovery Flow Thu (office) + Sat (home) — YOGA-1; no two
# strength days adjacent.
WEEKLY_PATTERN = {0: "strength_c", 1: "cardio_z2", 2: "strength_a",
                  3: "recovery_flow", 4: "strength_b", 5: "recovery_flow", 6: "walk"}
FLOW_LOCATION = {3: LOCATION, 5: "home"}   # weekday -> blocks.location

# week_num -> (sets, target_rpe, z2 minutes (lo, hi))
RAMP = {
    1: (2, 6.0, (20, 20)),
    2: (2, 6.0, (20, 20)),
    3: (3, 7.0, (30, 30)),
    4: (3, 7.0, (30, 30)),
    5: (3, 7.5, (35, 40)),
    6: (3, 7.5, (35, 40)),
    7: (2, 6.0, (30, 30)),
}

# ============================================================================
# Block builders
# ============================================================================

def _exercise(name, rng, top, per_side, machine, sets, week_num, *, wk0=False) -> dict:
    notes = [f"{sets}×{rng}" + (" each side" if per_side else "")]
    if machine and (week_num == 1 or wk0):
        notes.append(MACHINE_NOTE)
    if name == "DB goblet squat" and week_num in (5, 6):
        notes.append("alt: Smith squat")
    ex = {"name": name, "format": "reps", "target_reps": top, "rest_after_sec": 60,
          "notes": "; ".join(notes)}
    if name in EQUIPMENT_CLASS:
        ex["equipment_class"] = EQUIPMENT_CLASS[name]
    if week_num <= 2:
        ex["target_load_lbs"] = None  # finding weights
    return ex


def _strength(session_type: str, week_num: int, *, wk0: bool = False):
    sets, rpe, _ = RAMP[week_num]
    exercises = [_exercise(*e, sets, week_num, wk0=wk0) for e in _EXERCISES[session_type]]
    setup = []
    if week_num <= 2:
        setup.append("Weeks 1-2: finding weights — stop 3-4 reps shy of failure.")
    elif week_num == 7:
        setup.append("Week 7 deload — 2 sets, easy.")
    else:
        setup.append("Progression: +1 rep or next pin once all sets hit the top of the range.")
    blocks = {
        "type": "circuit",
        "display_name": _DISPLAY[session_type],
        "location": LOCATION,
        "rounds": sets,
        "warmup": WARMUP,
        "cooldown": COOLDOWN,
        "rest_between_rounds_sec": 90,
        "equipment": list(SESSION_EQUIPMENT[session_type]),
        "exercises": exercises,
        "setup_notes": setup,
    }
    minutes = 10 + round(sets * len(exercises) * 2.5)
    if session_type == "strength_c" and week_num in (5, 6):
        blocks["finisher"] = {
            "type": "intervals",
            "display_name": "Conditioning finisher",
            "rounds": 6,
            "exercises": [{"name": "Stepmill or upright bike", "format": "duration",
                           "duration_sec": 30, "rest_after_sec": 90,
                           "notes": "30s hard / 90s easy"}],
        }
        minutes += 12
    return blocks, rpe, 3, minutes


def _z2(week_num: int):
    lo, hi = RAMP[week_num][2]
    blocks = {
        "type": "steady",
        "display_name": _DISPLAY["cardio_z2"],
        "location": LOCATION,
        "duration_min": hi,
        "intensity": "Zone 2",
        "equipment": list(SESSION_EQUIPMENT["cardio_z2"]),
        "setup_notes": [Z2_NOTES],
    }
    if lo != hi:
        blocks["target_range_min"] = [lo, hi]
    return blocks, 4.0, 2, hi


def _walk(week_num: int, *, recovery: bool = False):
    blocks = {
        "type": "steady",
        "display_name": "Recovery Walk" if recovery else _DISPLAY["walk"],
        "location": "outside",
        "duration_min": 30,
        "intensity": "easy",
        "equipment": ["walking shoes"],
        "setup_notes": ["30 min walk outside"],
    }
    return blocks, None, None, 30


def _rest(week_num: int):
    blocks = {
        "type": "mobility",
        "display_name": _DISPLAY["rest_mobility"],
        "notes": "20 min mobility (mat or Stretch Trainer) or full rest",
        "intensity": "gentle",
        "duration_min": 20,
    }
    return blocks, None, None, 20


# ── Recovery Flow (YOGA-1) ──────────────────────────────────────────────────
# (step, name, side, hold s, mirror_group, side label, cue, easier option)
# Holds are round 1. Round 2 repeats every step and doubles steps 10-16.
# Cues are our own wording.
FLOW_STEPS = [
    ("1", "Child's pose", None, 30, None, None,
     "Knees wide, hips back toward your heels, arms long.", None),
    ("2", "Cobra", None, 30, None, None,
     "Hips stay down; press through the palms and lift the chest gently.", None),
    ("3", "Downward dog", None, 60, None, None,
     "Hips high, heels reaching down, long spine.", "Dolphin — forearms down"),
    ("4", "Standing forward bend", None, 30, None, None,
     "Soft knees, let your head hang heavy.", None),
    ("5", "High lunge", "R", 30, "lunge-unit", "Right leg forward",
     "Front knee over ankle, back heel lifted, arms up.", "Knee down"),
    ("6", "Crescent lunge", "R", 30, "lunge-unit", "Right leg forward",
     "Square the hips, sink a little deeper, reach up.", "Knee down"),
    ("7", "Extended puppy", None, 30, None, None,
     "From hands and knees, walk the hands forward, chest toward the mat.", None),
    ("8", "High lunge", "L", 30, "lunge-unit", "Left leg forward",
     "Front knee over ankle, back heel lifted, arms up.", "Knee down"),
    ("9", "Crescent lunge", "L", 30, "lunge-unit", "Left leg forward",
     "Square the hips, sink a little deeper, reach up.", "Knee down"),
    ("10", "Bridge", None, 30, None, None,
     "Feet hip-width, press through the heels, lift the hips.", None),
    ("11a", "Supine twist", "R", 30, "twist-supine", "Right side",
     "Knees drop to one side, both shoulders stay down.", None),
    ("11b", "Supine twist", "L", 30, "twist-supine", "Left side",
     "Knees drop to one side, both shoulders stay down.", None),
    ("12a", "Wind release", "R", 30, "wind", "Right knee",
     "Hug one knee to the chest, the other leg long.", None),
    ("12b", "Wind release", "L", 30, "wind", "Left knee",
     "Hug one knee to the chest, the other leg long.", None),
    ("13a", "Seated side bend", "L", 30, "side-bend", "Lean left",
     "Sit tall, reach the opposite arm overhead and lean.", None),
    ("13b", "Seated side bend", "R", 30, "side-bend", "Lean right",
     "Sit tall, reach the opposite arm overhead and lean.", None),
    ("14a", "Seated twist", "L", 30, "twist-seated", "Twist left",
     "Lengthen up first, then turn from the ribs.", None),
    ("14b", "Seated twist", "R", 30, "twist-seated", "Twist right",
     "Lengthen up first, then turn from the ribs.", None),
    ("15", "Seated mountain", None, 30, None, None,
     "Sit tall, arms overhead, slow breaths.", None),
    ("16", "Easy pose", None, 30, None, None,
     "Cross-legged, hands on knees, breathe slowly.", None),
]
FLOW_ROUNDS = 2
FLOW_DOUBLE_ROUND = 2            # round whose holds double …
FLOW_DOUBLE_STEPS = (10, 16)     # … on steps 10-16 (by step number)
FLOW_CLOSE = {"name": "Easy pose breathing", "side": None, "duration_sec": 180,
              "cue": "Easy pose. Slow, even breaths."}
FLOW_STRETCH_TRAINER = {"name": "Stretch Trainer", "side": None, "duration_sec": 480,
                        "cue": "Follow the 8 placard stretches"}
FLOW_PREVIEW_SEC = 5
FLOW_TARGET_RPE = 2.0


def _step_no(step: str) -> int:
    return int("".join(ch for ch in step if ch.isdigit()))


def flow_step_holds(blocks: dict, round_num: int) -> list[int]:
    """Hold seconds for every flow step in `round_num` (1-based)."""
    lo, hi = blocks.get("double_steps") or FLOW_DOUBLE_STEPS
    dbl = blocks.get("double_round", FLOW_DOUBLE_ROUND)
    return [s["duration_sec"] * (2 if round_num == dbl and lo <= _step_no(s["step"]) <= hi else 1)
            for s in blocks["flow"]]


def flow_total_sec(blocks: dict) -> int:
    pre = sum(p["duration_sec"] for p in blocks.get("pre") or [])
    rounds = sum(sum(flow_step_holds(blocks, r)) for r in range(1, int(blocks["rounds"]) + 1))
    close = (blocks.get("close") or {}).get("duration_sec", 0)
    return pre + rounds + close


class FlowError(ValueError):
    """A recovery flow that fails the side validator."""


def validate_flow(blocks: dict) -> None:
    """Every mirror_group needs R and L entries with equal total hold, in every
    round. Groups compare as a whole (a lunge unit is high + crescent lunge)."""
    flow = blocks.get("flow") or []
    if not flow:
        raise FlowError("flow has no steps")
    groups: dict[str, list[int]] = {}
    for i, st in enumerate(flow):
        g = st.get("mirror_group")
        if g is None:
            if st.get("side") is not None:
                raise FlowError(f"step {st.get('step')} has a side but no mirror_group")
            continue
        if st.get("side") not in ("R", "L"):
            raise FlowError(f"step {st.get('step')} in {g} needs side R or L")
        groups.setdefault(g, []).append(i)
    for r in range(1, int(blocks.get("rounds") or 1) + 1):
        holds = flow_step_holds(blocks, r)
        for g, idx in groups.items():
            sides = {"R": 0, "L": 0}
            seen = set()
            for i in idx:
                sides[flow[i]["side"]] += holds[i]
                seen.add(flow[i]["side"])
            if seen != {"R", "L"}:
                raise FlowError(f"{g}: missing side {sorted({'R', 'L'} - seen)[0]}")
            if sides["R"] != sides["L"]:
                raise FlowError(f"{g}: R {sides['R']}s ≠ L {sides['L']}s (round {r})")


def _flow_step(spec) -> dict:
    step, name, side, hold, group, label, cue, easier = spec
    return {"step": step, "name": name, "side": side, "side_label": label,
            "duration_sec": hold, "mirror_group": group, "cue": cue, "easier": easier}


def _recovery_flow(weekday: int):
    location = FLOW_LOCATION.get(weekday, LOCATION)
    office = location == LOCATION
    blocks = {
        "type": "recovery_flow",
        "display_name": _DISPLAY["recovery_flow"],
        "location": location,
        "rounds": FLOW_ROUNDS,
        "double_round": FLOW_DOUBLE_ROUND,
        "double_steps": list(FLOW_DOUBLE_STEPS),
        "preview_sec": FLOW_PREVIEW_SEC,
        "pre": [dict(FLOW_STRETCH_TRAINER)] if office else [],
        "flow": [_flow_step(s) for s in FLOW_STEPS],
        "close": dict(FLOW_CLOSE),
        "equipment": [EQ_MAT, EQ_STRETCH] if office else [EQ_MAT],
    }
    total = flow_total_sec(blocks)
    blocks["total_sec"] = total
    blocks["notes"] = (("8 min Stretch Trainer, then " if office else "")
                       + f"2 rounds of {len({_step_no(s[0]) for s in FLOW_STEPS})} poses (round 2 holds 2× from bridge on), "
                       + "3 min easy-pose breathing")
    validate_flow(blocks)
    return blocks, FLOW_TARGET_RPE, None, -(-total // 60)


def _build(session_type: str, week_num: int, *, wk0: bool = False, recovery: bool = False,
           weekday: int | None = None):
    if session_type == "recovery_flow":
        return _recovery_flow(weekday if weekday is not None else 3)
    if session_type.startswith("strength"):
        return _strength(session_type, week_num, wk0=wk0)
    if session_type == "cardio_z2":
        return _z2(week_num)
    if session_type == "walk":
        return _walk(week_num, recovery=recovery)
    return _rest(week_num)


# ============================================================================
# Schedule
# ============================================================================

def build_schedule() -> list[dict]:
    """Ordered specs {plan_date, session_type, week_num, wk0} for 9/16-11/03.

    Weeks are Wed-Tue blocks counted off WEEK1_START, so week 1 is
    9/16..9/22 and week 7 is 10/28..11/03.
    """
    specs: list[dict] = []
    d = WEEK1_START
    while d <= OFFICE_END:
        week_num = (d - WEEK1_START).days // 7 + 1
        specs.append({"plan_date": d, "session_type": WEEKLY_PATTERN[d.weekday()],
                      "week_num": week_num, "wk0": False})
        d += timedelta(days=1)
    return specs


def build_row(spec: dict) -> dict:
    wk0 = spec["wk0"]
    week_num = spec["week_num"]
    session_type = spec["session_type"]
    blocks, rpe, zone, est = _build(session_type, week_num, wk0=wk0,
                                    recovery=wk0 and session_type == "walk",
                                    weekday=spec["plan_date"].weekday())
    blocks = copy.deepcopy(blocks)
    tag = "office wk0 ramp-up" if wk0 else f"office wk{week_num}"
    return {
        "plan_date": spec["plan_date"],
        "phase": PHASE,
        "week_num": week_num,
        "session_type": session_type,
        "blocks": blocks,
        "target_rpe": rpe,
        "target_hr_zone": zone,
        "est_duration_min": est,
        "generated_by": GENERATED_BY,
        "notes": f"{blocks['display_name']} | {tag}",
    }


def build_rows() -> list[dict]:
    return [build_row(s) for s in build_schedule()]


# ============================================================================
# Validation
# ============================================================================

LEGAL_SESSION_TYPES = {"strength_a", "strength_b", "strength_c", "cardio_intervals",
                       "cardio_z2", "walk", "rest_mobility", "recovery_flow"}


def forbidden_hits(blocks) -> list[str]:
    """Retired home-gym tokens found anywhere in a blocks payload."""
    blob = json.dumps(blocks if not isinstance(blocks, str) else json.loads(blocks)).lower()
    return [t for t in FORBIDDEN_TOKENS if t in blob]


def validate_rows(rows: list[dict]) -> None:
    """Structural asserts so a bad edit fails loudly."""
    dates = [r["plan_date"] for r in rows]
    assert len(dates) == len(set(dates)), "duplicate plan_date"
    expected = [OFFICE_START + timedelta(days=i) for i in range((OFFICE_END - OFFICE_START).days + 1)]
    assert sorted(dates) == expected, "office rows must cover every day 9/16..11/03"
    for r in rows:
        b = r["blocks"]
        assert r["session_type"] in LEGAL_SESSION_TYPES, r["session_type"]
        assert 1 <= r["week_num"] <= 7, r["week_num"]
        assert b.get("display_name"), "blocks must carry a display_name"
        assert b["type"] in ("circuit", "steady", "mobility", "recovery_flow"), b["type"]
        assert not forbidden_hits(b), f"{r['plan_date']}: retired equipment {forbidden_hits(b)}"
        if r["session_type"].startswith("strength"):
            assert b.get("location") == LOCATION
            assert b.get("warmup") == WARMUP and b.get("cooldown") == COOLDOWN
            assert b["exercises"] and all("name" in e and "format" in e for e in b["exercises"])
        if r["session_type"] == "recovery_flow":
            assert b["type"] == "recovery_flow"
            validate_flow(b)


# ============================================================================
# Writer — caller owns the transaction
# ============================================================================

_UPSERT_SQL = """
INSERT INTO health.plan
    (plan_date, phase, week_num, session_type, blocks,
     target_rpe, target_hr_zone, est_duration_min, generated_by, notes)
VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s)
ON CONFLICT (plan_date) DO UPDATE SET
    phase            = EXCLUDED.phase,
    week_num         = EXCLUDED.week_num,
    session_type     = EXCLUDED.session_type,
    blocks           = EXCLUDED.blocks,
    target_rpe       = EXCLUDED.target_rpe,
    target_hr_zone   = EXCLUDED.target_hr_zone,
    est_duration_min = EXCLUDED.est_duration_min,
    generated_by     = EXCLUDED.generated_by,
    notes            = EXCLUDED.notes,
    is_skipped       = FALSE,
    is_override      = FALSE,
    skip_reason      = NULL
"""


# The current program, as the Status page reads it (acos.system_state
# `health_program`, STATUS-1). Written with every office reseed.
PROGRAM_STATE_KEY = "health_program"
DELOAD_WEEK = 7


def program_state() -> dict:
    weeks = max(RAMP)
    return {"name": "Foundation", "phase": PHASE, "anchor": WEEK1_START.isoformat(),
            "weeks_total": weeks, "deload_week": DELOAD_WEEK, "end": OFFICE_END.isoformat()}


def write_program_state(cur) -> None:
    cur.execute(
        "INSERT INTO acos.system_state (key, value, updated_at) VALUES (%s, %s, now()) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
        (PROGRAM_STATE_KEY, json.dumps(program_state())))


def write_rows(cur, rows: list[dict]) -> int:
    """UPSERT every office row and audit it through the same cursor. Does NOT
    commit."""
    validate_rows(rows)
    for r in rows:
        cur.execute(_UPSERT_SQL, (
            r["plan_date"], r["phase"], r["week_num"], r["session_type"],
            json.dumps(r["blocks"]), r["target_rpe"], r["target_hr_zone"],
            r["est_duration_min"], r["generated_by"], r["notes"],
        ))
    cur.execute(
        "INSERT INTO acos.audit_log (agent, persona, action, domain, confidence, "
        "outcome, token_count, api_cost_usd, metadata) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)",
        ("health_office", None, "office_plan_reseed", "health", None, "executed", 0, 0.0,
         json.dumps({"from": OFFICE_START.isoformat(), "to": OFFICE_END.isoformat(),
                     "rows": len(rows)})),
    )
    write_program_state(cur)
    return len(rows)
