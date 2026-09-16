"""HEALTH-2 — office gym program (all Precor).

Canonical office inventory, the 9/16-11/08 schedule (ramp-up days + weeks 1-7),
the block builders, a structural validator, and the transactional writer shared
by scripts/reseed_health_plan_v2.py --office and scripts/validate_health_plan.py.

Location is PLAN DATA: every row carries blocks.location (and blocks.equipment),
which artemis.health.resolve_equipment_and_location prefers over its static
fallback map. The home gym still exists; the rower and outdoor bike are retired
from the plan.

Rows are written INSERT ... ON CONFLICT (plan_date) DO UPDATE in ONE transaction
with an acos.audit_log row, so plan_ids are stable and a session logged against a
date mid-run is never orphaned. generated_by='manual' (CHECK-legal); week_num is
1-7 (CHECK 1..19) — the pre-week-1 ramp-up days are week_num=1, tagged 'wk0' in
notes.
"""

import copy
import json
from datetime import date, timedelta

LOCATION = "office gym"
PHASE = 1
GENERATED_BY = "manual"

OFFICE_START = date(2026, 9, 16)   # first ramp-up day
WEEK1_MONDAY = date(2026, 9, 21)
OFFICE_END = date(2026, 11, 8)     # last day of week 7

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
EQ_BACK_EXT = "45° back extension"
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
        ("45° back extension", "10-12", 12, False, False),
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

_DISPLAY = {
    "strength_a": "Office Strength A",
    "strength_b": "Office Strength B",
    "strength_c": "Office Strength C",
    "cardio_z2": "Zone 2 Cardio",
    "walk": "Walk",
    "rest_mobility": "Rest / Mobility",
}

# Mon=0 .. Sun=6
WEEKLY_PATTERN = {0: "strength_a", 1: "cardio_z2", 2: "rest_mobility",
                  3: "strength_b", 4: "strength_c", 5: "rest_mobility", 6: "walk"}

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

# Pre-week-1 ramp-up (HEALTH-2 confirm): 9/16-9/18 ramp-up work, 9/19 recovery
# walk, 9/20 rest. Stored as week_num=1 (CHECK), tagged wk0 in notes.
RAMPUP = {
    date(2026, 9, 16): "cardio_z2",
    date(2026, 9, 17): "strength_a",
    date(2026, 9, 18): "cardio_z2",
    date(2026, 9, 19): "walk",
    date(2026, 9, 20): "rest_mobility",
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


def _build(session_type: str, week_num: int, *, wk0: bool = False, recovery: bool = False):
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
    """Ordered specs {plan_date, session_type, week_num, wk0} for 9/16-11/08."""
    specs = [{"plan_date": d, "session_type": st, "week_num": 1, "wk0": True}
             for d, st in sorted(RAMPUP.items())]
    d = WEEK1_MONDAY
    while d <= OFFICE_END:
        week_num = (d - WEEK1_MONDAY).days // 7 + 1
        specs.append({"plan_date": d, "session_type": WEEKLY_PATTERN[d.weekday()],
                      "week_num": week_num, "wk0": False})
        d += timedelta(days=1)
    return specs


def build_row(spec: dict) -> dict:
    wk0 = spec["wk0"]
    week_num = spec["week_num"]
    session_type = spec["session_type"]
    blocks, rpe, zone, est = _build(session_type, week_num, wk0=wk0,
                                    recovery=wk0 and session_type == "walk")
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
                       "cardio_z2", "walk", "rest_mobility"}


def forbidden_hits(blocks) -> list[str]:
    """Retired home-gym tokens found anywhere in a blocks payload."""
    blob = json.dumps(blocks if not isinstance(blocks, str) else json.loads(blocks)).lower()
    return [t for t in FORBIDDEN_TOKENS if t in blob]


def validate_rows(rows: list[dict]) -> None:
    """Structural asserts so a bad edit fails loudly."""
    dates = [r["plan_date"] for r in rows]
    assert len(dates) == len(set(dates)), "duplicate plan_date"
    expected = [OFFICE_START + timedelta(days=i) for i in range((OFFICE_END - OFFICE_START).days + 1)]
    assert sorted(dates) == expected, "office rows must cover every day 9/16..11/08"
    for r in rows:
        b = r["blocks"]
        assert r["session_type"] in LEGAL_SESSION_TYPES, r["session_type"]
        assert 1 <= r["week_num"] <= 7, r["week_num"]
        assert b.get("display_name"), "blocks must carry a display_name"
        assert b["type"] in ("circuit", "steady", "mobility"), b["type"]
        assert not forbidden_hits(b), f"{r['plan_date']}: retired equipment {forbidden_hits(b)}"
        if r["session_type"].startswith("strength"):
            assert b.get("location") == LOCATION
            assert b.get("warmup") == WARMUP and b.get("cooldown") == COOLDOWN
            assert b["exercises"] and all("name" in e and "format" in e for e in b["exercises"])


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
    return len(rows)
