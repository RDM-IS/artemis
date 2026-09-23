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
import logging
from datetime import date, time, timedelta

logger = logging.getLogger(__name__)

LOCATION = "office gym"
PHASE = 1
GENERATED_BY = "manual"

OFFICE_START = date(2026, 9, 16)   # Wed — week 1, day 1
WEEK1_START = date(2026, 9, 16)    # Wed — week 1 is the 9/16..9/19 partial stub
# SCHEDULE-2 (Ryan, 2026-09-19): program weeks are Sun..Sat from 9/20, matching
# the CYCLE-1 pay period. Week 1 stays the 4-day stub; week 2 starts Sun 9/20.
WEEK2_START = date(2026, 9, 20)    # Sun — first full Sun..Sat program week
OFFICE_END = date(2026, 10, 31)    # Sat — last day of week 7 (a week boundary)

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
    "rest_mobility": [EQ_MAT, EQ_STRETCH],
    "rest": [],                      # EVENING-1: a rest day needs nothing
    "recovery_flow": [EQ_MAT, EQ_STRETCH],
}

FIRST_LIFT = {"strength_a": "Leg press", "strength_b": "DB goblet squat",
              "strength_c": "DB Romanian deadlift"}

# Retired home-gym tokens that must never appear in an office row.
FORBIDDEN_TOKENS = ("rower", "bike on trainer", "road bike", "indoor trainer",
                    "powerblock", "trx", "walking pad")

WARMUP = "5 min elliptical, easy"
COOLDOWN = "5 min Stretch Trainer"
COOLDOWN_MIN = 5
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

# ── The equipment class of every exercise, explicitly ───────────────────────
#
# EXERCISE-CLASS (Ryan, 2026-09-23): the class travels ON THE ROW for every
# exercise, not just for the handful the keyword rules once misread. Inference
# from the NAME stays only as a fallback for rows seeded before this.
#
# Why: the rules were written for the office and misread any other gym's
# vocabulary — "Band pulldown" reads as `machine` (and would offer a 10 lb
# stack step), "TRX row" as `machine`, "Ball hamstring curl" as `dumbbell`,
# "Lying leg raise" as `dumbbell` rather than bodyweight. LOCATION-1 makes that
# vocabulary real, so the guessing has to stop first.
#
# Every name this generator can emit, and every name already in RDS, is here.
# `class_for` raises on anything unknown: a new exercise without a class is a
# build error, not a silent `dumbbell`.
EQUIPMENT_CLASS: dict[str, str] = {
    # office — machines
    "Leg press": "machine",
    "Lat pulldown": "machine",
    "Seated leg curl": "machine",
    "Leg extension": "machine",
    "Pec fly": "machine",
    "Rear delt fly": "machine",
    "Calf press": "machine",
    "Ab machine crunch": "machine",
    "Seated back extension": "machine",
    # the Pulldown/Seated Row machine, despite "cable" in the name
    "Seated cable row": "machine",
    # office — functional trainer
    "Cable face pull (rope)": "cable",
    "Cable Pallof press": "cable",
    "Single-arm cable row": "cable",
    # office — dumbbells
    "DB bench press": "dumbbell",
    "Incline DB press": "dumbbell",
    "DB goblet squat": "dumbbell",
    "DB Romanian deadlift": "dumbbell",
    "Seated DB shoulder press": "dumbbell",
    # bodyweight
    "Captain's chair knee raise": "bodyweight",
    # ── pre-office history (the home-gym baseline, 5/06-9/15). Kept so the
    # backfill can class every row in RDS, not only the current program.
    "Band chest press": "bands",
    "Band pull-apart": "bands",
    "TRX row": "trx",
    "TRX single-leg DL": "trx",
    "DB floor press": "dumbbell",
    "DB RDL": "dumbbell",
    "Goblet squat": "dumbbell",
    "Bicep curl": "dumbbell",
    "Reverse lunge": "bodyweight",
    # core / finisher work, office and home: no load of its own
    "Plank": "bodyweight",
    "Side plank": "bodyweight",
    "Hollow hold": "bodyweight",
    "Dead bug": "bodyweight",
    "Bird dog": "bodyweight",
    "Ball plank": "bodyweight",
    "TRX fallout": "trx",
    # the home-gym Pallof was a band; the office one is "Cable Pallof press"
    "Pallof press": "bands",
    # a legacy interval block, not a lift: duration work with no load. `cardio`
    # is a no-numeric-load class like bands and trx.
    "Stepmill or upright bike": "cardio",
}


def class_for(name: str) -> str:
    """The exercise's equipment class. Raises on an unknown name — a new
    exercise without a class is a build error, never a silent `dumbbell`."""
    try:
        return EQUIPMENT_CLASS[name]
    except KeyError:
        raise KeyError(
            f"{name!r} has no equipment_class. Add it to health_office."
            "EQUIPMENT_CLASS — guessing from the name is what LOCATION-1 removes."
        ) from None

_DISPLAY = {
    "strength_a": "Office Strength A",
    "strength_b": "Office Strength B",
    "strength_c": "Office Strength C",
    "cardio_z2": "Zone 2 Cardio",
    "rest_mobility": "Rest / Mobility",
    "rest": "Rest",
    "recovery_flow": "Recovery Flow",
}

# ── CYCLE-1 ────────────────────────────────────────────────────────────────
# The cycle lives in artemis.cycle — ONE definition, shared with the scheduler
# and the quiet_hours boundary helpers. Nothing here re-declares day types or
# locations (tests/test_cycle.py asserts there is no second copy).
from artemis.cycle import (  # noqa: E402
    CYCLE_LEN,
    cycle_pos,
    day_type,
)
from artemis import cycle as _cycle  # noqa: E402

CYCLE_ANCHOR = _cycle.DEFAULT_ANCHOR
DAY_OFF_WORK = {5}                 # the wi Friday is a day off work


def day_location(d: date, slot: str = "morning") -> str:
    """Where a seeded row happens, as the display name the rows carry.

    The MORNING is the day's anchor — where he wakes. Never the 00:00 segment:
    an msp_work day starts at msp_home and the session is at the office.

    The EVENING is where he is when the evening session happens (EVENING-1),
    which on a work day is home, not the office gym."""
    key = (_cycle.anchor_location(d, use_overrides=False) if slot == "morning"
           else _cycle.location_at(d, EVENING_AT, use_overrides=False))
    return (_cycle.DEFAULT_LOCATIONS.get(key) or {}).get("display", key)


def evening_is_possible(d: date) -> bool:
    """False when he is on the road: no session is ever placed in a transit
    segment — it is reported, never relocated (CYCLE-1)."""
    return not _cycle.is_transit(_cycle.location_at(d, EVENING_AT, use_overrides=False))


def is_office_day(d: date) -> bool:
    return day_type(d, use_overrides=False) == "msp_work"


# SCHEDULE-2: lift on the 1st, 3rd and 4th OFFICE day of each cycle week —
# wk 1 Mon/Wed/Thu, wk 2 Tue/Thu/Fri — as A, B, C in order. That puts B and C
# back to back once a week; Ryan accepted that deliberately (test asserts
# exactly 6 such pairs, one per program week).
LIFT_SLOTS = (1, 3, 4)             # 1-based office-day positions within a cycle week
LIFT_ORDER = ("strength_a", "strength_b", "strength_c")
# Ryan, 2026-09-19: Z2 sits on the office non-lift days, where the equipment is
# reliable; the flows sit on the wi days because a mat travels. Richfield's
# rower and trainer stay available for extra cardio — the SEEDED session there
# is a flow.
# EVENING-1 (Ryan, 2026-09-23): the flows moved to the EVENING, so the days
# that carried them are rest mornings now. Mornings are strength / cardio /
# rest; evenings are yoga.
NON_LIFT = {
    0: "rest",            # Sun, msp_home — flow moved to the evening
    2: "cardio_z2",       # Tue, office — treadmill / elliptical
    5: "rest",            # Fri, Richfield — flow moved to the evening
    6: "rest",            # Sat, Brown Deer — flow moved to the evening
    7: "rest",            # Sun, Brown Deer — flow moved to the evening
    8: "rest",            # Mon, travel — flow moved to the evening
    10: "cardio_z2",      # Wed, office — treadmill / elliptical
    13: "rest",           # Sat, msp_home — flow moved to the evening
}

# EVENING-1: FOUR evenings a week — the six days whose morning flow moved,
# plus one training day in each program week (Tue of week 1, Wed of week 2).
# Position 4 is the Thursday of week 1: he drives to the farm after work, and
# NO SESSION IS EVER PLACED IN A TRANSIT SEGMENT, so it never gets one.
EVENING_POS: frozenset[int] = frozenset({0, 2, 5, 6, 7, 8, 10, 13})
EVENING_SESSION = "recovery_flow"
#: When "the evening" is, for resolving where he is. After the work-day move
#: home (17:00) and after the wi Friday's move to Brown Deer (16:00).
EVENING_AT = time(19, 0)


def session_for(d: date) -> str:
    """The session type for a date under SCHEDULE-2."""
    pos = cycle_pos(d)
    week_start = pos - (pos % 7)                       # 0 or 7
    office = [p for p in range(week_start, week_start + 7)
              if _cycle.DAY_TYPES[p] == "msp_work"]
    if pos in office:
        slot = office.index(pos) + 1                   # 1-based office day
        if slot in LIFT_SLOTS:
            return LIFT_ORDER[LIFT_SLOTS.index(slot)]
    return NON_LIFT[pos]


def flow_variant_location(d: date) -> str:
    """Recovery Flow location. Only the office has the Stretch Trainer."""
    return day_location(d)

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
          "notes": "; ".join(notes), "equipment_class": class_for(name)}
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


def _z2(week_num: int, location: str = LOCATION):
    lo, hi = RAMP[week_num][2]
    office = location == LOCATION
    blocks = {
        "type": "steady",
        "display_name": _DISPLAY["cardio_z2"],
        "location": location,
        "duration_min": hi,
        "intensity": "Zone 2",
        "equipment": (list(SESSION_EQUIPMENT["cardio_z2"]) if office
                      else ["rower", "bike on trainer"]),
        "setup_notes": [Z2_NOTES],
    }
    if office:
        # Ryan, 2026-09-19: keeps the Stretch Trainer in the program now that
        # the flows travel. Same cooldown the strength days use.
        blocks["cooldown"] = COOLDOWN
        blocks["equipment"].append(EQ_STRETCH)
    if lo != hi:
        blocks["target_range_min"] = [lo, hi]
    est = hi + (COOLDOWN_MIN if office else 0)
    return blocks, 4.0, 2, est


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
# YOGA-4 (Ryan, 2026-09-20) — the flow, in play order.
#
# Every hold is 40 s in BOTH rounds; the old round-2 doubling is gone. The
# `transition_sec` on each step is the time to move INTO it from the step
# before, and this table is now the ONLY source of transition lengths — the
# posture-derived 3 s / 5 s rule is deleted. `posture` is kept because it
# describes the pose, not because anything computes from it.
#
# Order notes:
#   * the four lunge poses run R, R, L, L, so the side switch lands at the top
#     of the crescent rather than between two different poses;
#   * extended puppy follows all four lunges and leads into bridge;
#   * easy pose closes round 1 only — round 2 ends at seated mountain and goes
#     straight to savasana.
#
# child's pose takes 5 s in both rounds: after the meditation (or the Stretch
# Trainer at the office) in round 1, and after easy pose in round 2.
FLOW_HOLD_SEC = 40

FLOW_STEPS = [
    {"step": "1", "name": "Child's pose", "transition_sec": 5,
     "cue": "Knees wide, hips back toward your heels, arms long."},
    {"step": "2", "name": "Cobra", "transition_sec": 3,
     "cue": "Hips stay down; press through the palms and lift the chest gently."},
    {"step": "3", "name": "Downward dog", "transition_sec": 3,
     "cue": "Hips high, heels reaching down, long spine.", "easier": "Dolphin — forearms down"},
    {"step": "4", "name": "Standing forward bend", "transition_sec": 3,
     "cue": "Soft knees, let your head hang heavy."},
    {"step": "5", "name": "High lunge", "side": "R", "mirror_group": "lunge-unit",
     "side_label": "Right leg forward", "transition_sec": 5,
     "cue": "Front knee over ankle, back heel lifted, arms up.", "easier": "Knee down"},
    {"step": "6", "name": "Crescent lunge", "side": "R", "mirror_group": "lunge-unit",
     "side_label": "Right leg forward", "transition_sec": 3,
     "cue": "Square the hips, sink a little deeper, reach up.", "easier": "Knee down"},
    {"step": "7", "name": "Crescent lunge", "side": "L", "mirror_group": "lunge-unit",
     "side_label": "Left leg forward", "transition_sec": 3,
     "cue": "Square the hips, sink a little deeper, reach up.", "easier": "Knee down"},
    {"step": "8", "name": "High lunge", "side": "L", "mirror_group": "lunge-unit",
     "side_label": "Left leg forward", "transition_sec": 3,
     "cue": "Front knee over ankle, back heel lifted, arms up.", "easier": "Knee down"},
    {"step": "9", "name": "Extended puppy", "transition_sec": 5,
     "cue": "From hands and knees, walk the hands forward, chest toward the mat."},
    {"step": "10", "name": "Bridge", "transition_sec": 4,
     "cue": "Feet hip-width, press through the heels, lift the hips."},
    {"step": "11", "name": "Supine twist", "side": "R", "mirror_group": "twist-supine",
     "side_label": "Right side", "transition_sec": 3,
     "cue": "Knees drop to one side, both shoulders stay down."},
    {"step": "12", "name": "Supine twist", "side": "L", "mirror_group": "twist-supine",
     "side_label": "Left side", "transition_sec": 3,
     "cue": "Knees drop to one side, both shoulders stay down."},
    {"step": "13", "name": "Wind release", "side": "R", "mirror_group": "wind",
     "side_label": "Right knee", "transition_sec": 4,
     "cue": "Hug one knee to the chest, the other leg long."},
    {"step": "14", "name": "Wind release", "side": "L", "mirror_group": "wind",
     "side_label": "Left knee", "transition_sec": 3,
     "cue": "Hug one knee to the chest, the other leg long."},
    {"step": "15", "name": "Seated side bend", "side": "L", "mirror_group": "side-bend",
     "side_label": "Lean left", "transition_sec": 5,
     "cue": "Sit tall, reach the opposite arm overhead and lean."},
    {"step": "16", "name": "Seated twist", "side": "L", "mirror_group": "twist-seated",
     "side_label": "Twist left", "transition_sec": 3,
     "cue": "Lengthen up first, then turn from the ribs."},
    {"step": "17", "name": "Seated twist", "side": "R", "mirror_group": "twist-seated",
     "side_label": "Twist right", "transition_sec": 3,
     "cue": "Lengthen up first, then turn from the ribs."},
    {"step": "18", "name": "Seated side bend", "side": "R", "mirror_group": "side-bend",
     "side_label": "Lean right", "transition_sec": 3,
     "cue": "Sit tall, reach the opposite arm overhead and lean."},
    {"step": "19", "name": "Seated mountain", "transition_sec": 3,
     "cue": "Sit tall, arms overhead, slow breaths."},
    {"step": "20", "name": "Easy pose", "transition_sec": 3, "rounds": [1],
     "cue": "Cross-legged, hands on knees, breathe slowly."},
]
FLOW_ROUNDS = 2
# YOGA-3 (Ryan, 9/19): open with seated meditation; close with savasana.
FLOW_MEDITATION = {"name": "Seated meditation", "side": None, "duration_sec": 60,
                   "transition_sec": 5,
                   "cue": "Sit tall and comfortable, eyes soft, slow breaths.", "posture": "seated",
                   "cue_mid": None}
FLOW_CLOSE = {"name": "Savasana", "side": None, "duration_sec": 180, "transition_sec": 5,
              "cue": "Lie on your back, arms by your sides, let everything go.",
              "posture": "supine", "sanskrit": "Shavasana",
              "sanskrit_spoken": "shah-VAH-sah-nah", "cue_mid": None}
FLOW_STRETCH_TRAINER = {"name": "Stretch Trainer", "side": None, "duration_sec": 480,
                        "transition_sec": 5,
                        "cue": "Follow the 8 placard stretches", "posture": "standing"}
FLOW_TARGET_RPE = 2.0

# Body position of every pose. Descriptive only since YOGA-4 — transitions come
# from the table above, not from this.
FLOW_POSTURE = {
    "Child's pose": "kneeling", "Cobra": "prone", "Downward dog": "quadruped",
    "Standing forward bend": "standing", "High lunge": "standing", "Crescent lunge": "standing",
    "Extended puppy": "kneeling", "Bridge": "supine", "Supine twist": "supine",
    "Wind release": "supine", "Seated side bend": "seated", "Seated twist": "seated",
    "Seated mountain": "seated", "Easy pose": "seated",
}
# YOGA-5 mid-hold cues (Ryan, 2026-09-20). ONE short line per pose, spoken
# 12 s into the hold and then nothing, so the rest of the hold is silent.
# Ryan's framing: what helps in week one is noise by week six — hence the
# toggle on the setup screen, default on.
#
# Meditation and savasana get NOTHING (Ryan, 2026-09-20): those two are silent
# for the whole of their timer. The lead-in and the move cue still happen
# before the hold starts; once it is running, nothing speaks.
FLOW_CUE_MID = {
    "Child's pose":          "Let your forehead rest, widen your knees",
    "Cobra":                 "Draw your shoulders down and back",
    "Downward dog":          "Press the floor away, let your heels sink",
    "Standing forward bend": "Soften your knees, let your head hang",
    "High lunge":            "Front knee over the ankle, back leg long",
    "Crescent lunge":        "Lift through your ribs, reach up",
    "Extended puppy":        "Melt your chest toward the floor",
    "Bridge":                "Press through your heels, open your chest",
    "Supine twist":          "Let the top shoulder drop toward the mat",
    "Wind release":          "Draw the knee closer, relax your neck",
    "Seated side bend":      "Lengthen first, then lean",
    "Seated twist":          "For a deeper stretch, look over your shoulder",
    "Seated mountain":       "Relax your shoulders, sit tall",
    "Easy pose":             "Settle in, soften your jaw",
    # Savasana is deliberately absent — silence, not a line at the start.
}
# How long into a hold the mid cue is spoken. Never collides with the 7 s
# lead-in: the shortest hold is 40 s, so the gap is 21 s.
FLOW_CUE_MID_SEC = 12

FLOW_POSTURES = ("standing", "kneeling", "quadruped", "prone", "supine", "seated")
# How the voice says a pose, where it differs from the written name.
FLOW_SPOKEN = {"Downward dog": "Downward facing dog"}

# YOGA-5 (Ryan, 2026-09-20) — the Sanskrit name of every pose, twice:
#   sanskrit         the display spelling, shown small beneath the English name
#   sanskrit_spoken  a phonetic respelling, for speechSynthesis ONLY, because
#                    the engine mangles the proper spelling
# The transition cue speaks the Sanskrit with no side ("Move to Ashta
# Chandrasana") — the 7 s lead-in just gave the side in English and the screen
# shows it. Meditation and the Stretch Trainer have no meaningful Sanskrit and
# fall back to English; every POSE must resolve, and validate_flow fails if one
# does not.
FLOW_SANSKRIT = {
    "Child's pose":          ("Balasana", "bah-LAH-sah-nah"),
    "Cobra":                 ("Bhujangasana", "boo-jang-GAH-sah-nah"),
    "Downward dog":          ("Adho Mukha Svanasana", "AH-doh MOO-kah shvah-NAH-sah-nah"),
    "Standing forward bend": ("Uttanasana", "oo-tah-NAH-sah-nah"),
    "High lunge":            ("Utthita Ashwa Sanchalanasana",
                              "oo-TEE-tah ASH-wah sahn-chah-lah-NAH-sah-nah"),
    "Crescent lunge":        ("Ashta Chandrasana", "AHSH-tah chahn-DRAH-sah-nah"),
    "Extended puppy":        ("Uttana Shishosana", "oo-TAH-nah shih-SHOH-sah-nah"),
    "Bridge":                ("Setu Bandha Sarvangasana", "SEH-too BAHN-dah sar-vahn-GAH-sah-nah"),
    "Supine twist":          ("Supta Matsyendrasana", "SOOP-tah mahts-yen-DRAH-sah-nah"),
    "Wind release":          ("Pawanmuktasana", "pah-wahn-mook-TAH-sah-nah"),
    "Seated side bend":      ("Parsva Sukhasana", "PARSH-vah soo-KAH-sah-nah"),
    "Seated twist":          ("Parivrtta Sukhasana", "pah-ree-VRIT-tah soo-KAH-sah-nah"),
    "Seated mountain":       ("Parvatasana", "par-vah-TAH-sah-nah"),
    "Easy pose":             ("Sukhasana", "soo-KAH-sah-nah"),
    "Savasana":              ("Shavasana", "shah-VAH-sah-nah"),
}
# YOGA-4: the lead-in moves to 7 s before the hold ends, and there is no chime
# in front of it any more — the words start at 7 s exactly.
FLOW_LEADIN_SEC = 7
FLOW_START_POSTURE = "standing"   # at the iPad when Start is tapped


def _step_no(step: str) -> int:
    return int("".join(ch for ch in step if ch.isdigit()))


def flow_steps_for_round(blocks: dict, round_num: int) -> list[dict]:
    """The steps played in `round_num`. A step may name the rounds it belongs
    to (`"rounds": [1]`); most belong to all of them."""
    return [s for s in blocks.get("flow") or []
            if round_num in (s.get("rounds") or range(1, int(blocks.get("rounds") or 1) + 1))]


def flow_step_holds(blocks: dict, round_num: int) -> list[int]:
    """Hold seconds for every step played in `round_num` (1-based). Since
    YOGA-4 every hold is the same in every round — nothing doubles."""
    return [s["duration_sec"] for s in flow_steps_for_round(blocks, round_num)]


def flow_sequence(blocks: dict) -> list[dict]:
    """Every timed item in play order — pre, each round, close — with its hold
    and the transition before it, both read straight from the data."""
    items = [dict(p) for p in blocks.get("pre") or []]
    for r in range(1, int(blocks.get("rounds") or 1) + 1):
        for st in flow_steps_for_round(blocks, r):
            items.append({**st, "round": r})
    if blocks.get("close"):
        items.append(dict(blocks["close"]))
    return items


def flow_hold_sec(blocks: dict) -> int:
    return sum(i["duration_sec"] for i in flow_sequence(blocks))


def flow_transition_total_sec(blocks: dict) -> int:
    return sum(i["transition_sec"] for i in flow_sequence(blocks))


def flow_total_sec(blocks: dict) -> int:
    """Holds plus the transitions between them — the time the flow really takes."""
    return sum(i["duration_sec"] + i["transition_sec"] for i in flow_sequence(blocks))


class FlowError(ValueError):
    """A recovery flow that fails the side validator."""


def validate_flow(blocks: dict) -> None:
    """Every mirror_group needs R and L entries with equal total hold, in every
    round. Groups compare as a whole (a lunge unit is high + crescent lunge)."""
    flow = blocks.get("flow") or []
    if not flow:
        raise FlowError("flow has no steps")
    for r in range(1, int(blocks.get("rounds") or 1) + 1):
        steps = flow_steps_for_round(blocks, r)
        groups: dict[str, dict[str, int]] = {}
        for st in steps:
            g = st.get("mirror_group")
            if g is None:
                if st.get("side") is not None:
                    raise FlowError(f"step {st.get('step')} has a side but no mirror_group")
                continue
            if st.get("side") not in ("R", "L"):
                raise FlowError(f"step {st.get('step')} in {g} needs side R or L")
            sides = groups.setdefault(g, {"R": 0, "L": 0})
            sides[st["side"]] += st["duration_sec"]
        for g, sides in groups.items():
            missing = [k for k, v in sides.items() if v == 0]
            if missing:
                raise FlowError(f"{g}: missing side {missing[0]} (round {r})")
            if sides["R"] != sides["L"]:
                raise FlowError(f"{g}: R {sides['R']}s ≠ L {sides['L']}s (round {r})")
    for it in flow_sequence(blocks):
        if it.get("posture") not in FLOW_POSTURES:
            raise FlowError(f"{it.get('step') or it.get('name')}: posture {it.get('posture')!r} "
                            f"is not one of {', '.join(FLOW_POSTURES)}")
        if not isinstance(it.get("transition_sec"), int) or it["transition_sec"] <= 0:
            raise FlowError(f"{it.get('step') or it.get('name')}: transition_sec "
                            f"{it.get('transition_sec')!r} is not a positive whole number")
    # YOGA-5: every pose resolves to a Sanskrit entry. The pre blocks
    # (meditation, Stretch Trainer) have none and fall back to English.
    for st in flow:
        if not st.get("sanskrit") or not st.get("sanskrit_spoken"):
            raise FlowError(f"step {st.get('step')} ({st.get('name')}): no Sanskrit entry — "
                            f"add it to FLOW_SANSKRIT")
        # YOGA-5: every pose resolves to a mid-hold cue or explicitly to none.
        # The key must be PRESENT either way, so "nobody wrote one" and "this
        # one deliberately has none" stay different facts.
        if "cue_mid" not in st:
            raise FlowError(f"step {st.get('step')} ({st.get('name')}): no cue_mid key — "
                            f"add it to FLOW_CUE_MID, or set it to None on purpose")


def _flow_step(spec: dict) -> dict:
    """One FLOW_STEPS entry as the JSONB row gym-display reads."""
    name = spec["name"]
    out = {"step": spec["step"], "name": name, "side": spec.get("side"),
           "side_label": spec.get("side_label"), "duration_sec": FLOW_HOLD_SEC,
           "transition_sec": spec["transition_sec"], "mirror_group": spec.get("mirror_group"),
           "cue": spec.get("cue"), "easier": spec.get("easier"),
           "posture": FLOW_POSTURE.get(name)}
    if spec.get("rounds"):
        out["rounds"] = list(spec["rounds"])
    if name in FLOW_SPOKEN:
        out["spoken"] = FLOW_SPOKEN[name]
    sans = FLOW_SANSKRIT.get(name)
    if sans:
        out["sanskrit"], out["sanskrit_spoken"] = sans
    out["cue_mid"] = FLOW_CUE_MID.get(name)
    return out


def _recovery_flow(location: str = LOCATION):
    office = location == LOCATION
    blocks = {
        "type": "recovery_flow",
        "display_name": _DISPLAY["recovery_flow"],
        "location": location,
        "rounds": FLOW_ROUNDS,
        "hold_sec": FLOW_HOLD_SEC,
        "leadin_sec": FLOW_LEADIN_SEC,
        "cue_mid_sec": FLOW_CUE_MID_SEC,
        "start_posture": FLOW_START_POSTURE,
        "pre": [dict(FLOW_MEDITATION)] + ([dict(FLOW_STRETCH_TRAINER)] if office else []),
        "flow": [_flow_step(s) for s in FLOW_STEPS],
        "close": dict(FLOW_CLOSE),
        "equipment": [EQ_MAT, EQ_STRETCH] if office else [EQ_MAT],
    }
    total = flow_total_sec(blocks)
    blocks["total_sec"] = total
    r1 = len(flow_steps_for_round(blocks, 1))
    r2 = len(flow_steps_for_round(blocks, 2))
    med = -(-FLOW_MEDITATION["duration_sec"] // 60)
    blocks["notes"] = (f"{med} min seated meditation, "
                       + ("8 min Stretch Trainer, " if office else "")
                       + f"round 1 of {r1} poses, round 2 of {r2}, every hold "
                       + f"{FLOW_HOLD_SEC} s, 3 min savasana; 3–5 s to move between poses")
    validate_flow(blocks)
    return blocks, FLOW_TARGET_RPE, None, -(-total // 60)


def _build(session_type: str, week_num: int, *, wk0: bool = False,
           location: str | None = None):
    if session_type == "recovery_flow":
        return _recovery_flow(location or LOCATION)
    if session_type.startswith("strength"):
        return _strength(session_type, week_num, wk0=wk0)
    if session_type == "cardio_z2":
        return _z2(week_num, location or LOCATION)
    if session_type == "rest":
        return _rest_day()
    return _rest(week_num)


def _rest_day():
    """EVENING-1: a planned rest morning. A REAL row — "no plan" and "rest
    today" are different facts, and only one of them is a data problem."""
    return ({"type": "rest", "display_name": _DISPLAY["rest"], "equipment": [],
             "notes": "Rest. Nothing planned this morning."}, None, None, 0)


# ============================================================================
# Schedule
# ============================================================================

def week_num_for(d: date) -> int:
    """Program week. Week 1 is the 9/16..9/19 stub; weeks 2+ run Sun..Sat from
    WEEK2_START, matching the CYCLE-1 pay period (SCHEDULE-2)."""
    if d < WEEK2_START:
        return 1
    return 2 + (d - WEEK2_START).days // 7


def build_schedule() -> list[dict]:
    """Ordered specs {plan_date, session_type, week_num, location, wk0} from
    WEEK2_START to OFFICE_END.

    The 9/16..9/19 week-1 stub is NOT regenerated — it is logged history and
    stays as seeded. Weeks 2..7 are Sun..Sat and the session for each day comes
    from SCHEDULE-2's office-day rule over the CYCLE-1 day types.
    """
    specs: list[dict] = []
    d = WEEK2_START
    while d <= OFFICE_END:
        specs.append({"plan_date": d, "slot": "morning", "session_type": session_for(d),
                      "week_num": week_num_for(d), "location": day_location(d),
                      "day_type": day_type(d), "wk0": False})
        # EVENING-1: four evenings a week, and never one in a transit segment.
        if cycle_pos(d) in EVENING_POS and evening_is_possible(d):
            specs.append({"plan_date": d, "slot": "evening", "session_type": EVENING_SESSION,
                          "week_num": week_num_for(d),
                          "location": day_location(d, "evening"),
                          "day_type": day_type(d), "wk0": False})
        d += timedelta(days=1)
    return specs


def build_row(spec: dict) -> dict:
    wk0 = spec["wk0"]
    week_num = spec["week_num"]
    session_type = spec["session_type"]
    location = spec.get("location") or LOCATION
    blocks, rpe, zone, est = _build(session_type, week_num, wk0=wk0, location=location)
    blocks = copy.deepcopy(blocks)
    # CYCLE-1: every row carries where it happens and the day type it came from.
    blocks["location"] = location
    if spec.get("day_type"):
        blocks["day_type"] = spec["day_type"]
    tag = f"{spec.get('day_type', 'office')} wk{week_num}"
    return {
        "plan_date": spec["plan_date"],
        "slot": spec.get("slot", "morning"),
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

# ── TIME-CAP (Ryan, 2026-09-19): 45 min is a target, not a limit ────────────
# 45-59 min is fine and gets a note (reseed diff + log); never auto-cut. 60+
# is rejected — except the program slots in CALIBRATION_PENDING, whose
# estimate (10 + sets x exercises x 2.5 min) is 60+ while weeks 3-6 stay as
# planned. They warn until ~2 weeks of logged sessions recalibrate the
# per-set estimate (the reports show planned vs logged span); empty the set
# then and the hard reject applies to them too.
TARGET_MIN = 45
HARD_MAX_MIN = 60
CALIBRATION_PENDING = {("strength_b", w) for w in (3, 4, 5, 6)} | {("strength_c", 5), ("strength_c", 6)}


def duration_verdict(session_type: str, week_num, est) -> tuple[str, str]:
    """("ok" | "note" | "pending" | "reject", message) for one planned session."""
    if est is None:
        return "ok", ""
    est = int(est)
    if est >= HARD_MAX_MIN:
        if (session_type, week_num) in CALIBRATION_PENDING:
            return "pending", (f"~{est} min — {HARD_MAX_MIN}+ allowed until the estimate "
                               f"is recalibrated")
        return "reject", f"~{est} min — {HARD_MAX_MIN}+ is rejected"
    if est >= TARGET_MIN:
        return "note", f"~{est} min — target {TARGET_MIN} min"
    return "ok", ""


def duration_findings(rows) -> tuple[list[str], list[str]]:
    """(rejects, notes) for planned rows, each "YYYY-MM-DD Dow type wkN: message"."""
    rejects, notes = [], []
    for r in rows:
        kind, msg = duration_verdict(r["session_type"], r.get("week_num"), r.get("est_duration_min"))
        if kind == "ok":
            continue
        line = f"{r['plan_date']} {r['plan_date']:%a} {r['session_type']} wk{r.get('week_num')}: {msg}"
        (rejects if kind == "reject" else notes).append(line)
    return rejects, notes


def format_estimate(minutes) -> str:
    """"40 min", or "~52 min" once it's over the 45 min target. No warnings."""
    if minutes is None:
        return ""
    m = int(minutes)
    return f"~{m} min" if m > TARGET_MIN else f"{m} min"


LEGAL_SESSION_TYPES = {"strength_a", "strength_b", "strength_c", "cardio_intervals",
                       "cardio_z2", "rest", "rest_mobility", "recovery_flow"}


def forbidden_hits(blocks) -> list[str]:
    """Retired home-gym tokens found anywhere in a blocks payload.

    The ban is about the OFFICE program (HEALTH-2 retired the rower and the
    outdoor bike from it). A row at another location is checked against that
    location's own inventory instead — Richfield really does have a rower.
    """
    b = blocks if not isinstance(blocks, str) else json.loads(blocks)
    if (b or {}).get("location") not in (None, LOCATION):
        return []
    blob = json.dumps(b).lower()
    return [t for t in FORBIDDEN_TOKENS if t in blob]


def validate_rows(rows: list[dict]) -> list[str]:
    """Structural asserts so a bad edit fails loudly. Returns the TIME-CAP
    notes (45-59 min, and the CALIBRATION_PENDING 60+ rows); a 60+ row
    outside CALIBRATION_PENDING fails the assert."""
    keys = [(r["plan_date"], r.get("slot", "morning")) for r in rows]
    assert len(keys) == len(set(keys)), "duplicate (plan_date, slot)"
    mornings = sorted(r["plan_date"] for r in rows if r.get("slot", "morning") == "morning")
    expected = [WEEK2_START + timedelta(days=i) for i in range((OFFICE_END - WEEK2_START).days + 1)]
    assert mornings == expected, "every day 9/20..10/31 needs a MORNING row"
    for r in rows:
        b = r["blocks"]
        assert r["session_type"] in LEGAL_SESSION_TYPES, r["session_type"]
        assert 1 <= r["week_num"] <= 7, r["week_num"]
        assert b.get("display_name"), "blocks must carry a display_name"
        assert b["type"] in ("circuit", "steady", "mobility", "recovery_flow", "rest"), b["type"]
        assert not forbidden_hits(b), f"{r['plan_date']}: retired equipment {forbidden_hits(b)}"
        # SCHEDULE-2: a strength session only ever lands on an office day.
        if r["session_type"].startswith("strength"):
            assert b.get("day_type") == "msp_work", \
                f"{r['plan_date']}: {r['session_type']} on a {b.get('day_type')} day"
            assert b.get("location") == LOCATION
            assert b.get("warmup") == WARMUP and b.get("cooldown") == COOLDOWN
            assert b["exercises"] and all("name" in e and "format" in e for e in b["exercises"])
            # EXERCISE-CLASS: the class travels on the row, always.
            for e in b["exercises"]:
                assert e.get("equipment_class") == class_for(e["name"]), \
                    f"{r['plan_date']}: {e['name']} carries {e.get('equipment_class')!r}"
        if r["session_type"] == "recovery_flow":
            assert b["type"] == "recovery_flow"
            validate_flow(b)
    # SCHEDULE-2: exactly 3 lifts per program week, and every row's location is
    # the one CYCLE-1 derives for that date.
    from collections import Counter
    lifts = Counter(r["week_num"] for r in rows if r["session_type"].startswith("strength"))
    for wk in sorted({r["week_num"] for r in rows}):
        assert lifts[wk] == 3, f"week {wk} has {lifts[wk]} lifts, expected 3"
    # EVENING-1: evenings are yoga, four a week, and never on the road.
    evenings = [r for r in rows if r.get("slot") == "evening"]
    for r in evenings:
        assert r["session_type"] == EVENING_SESSION, \
            f"{r['plan_date']}: evening is {r['session_type']}, not {EVENING_SESSION}"
        assert evening_is_possible(r["plan_date"]), \
            f"{r['plan_date']}: an evening session cannot be placed in a transit segment"
        assert r["blocks"].get("location") == day_location(r["plan_date"], "evening"), \
            f"{r['plan_date']}: evening location {r['blocks'].get('location')!r}"
    ev_per_week = Counter(r["week_num"] for r in evenings)
    full_weeks = {r["week_num"] for r in rows
                  if week_num_for(WEEK2_START) < r["week_num"] < week_num_for(OFFICE_END)}
    for wk in sorted(full_weeks):
        assert ev_per_week[wk] == 4, f"week {wk} has {ev_per_week[wk]} evenings, expected 4"

    for r in rows:
        assert r["blocks"].get("day_type") == day_type(r["plan_date"])
        assert r["session_type"] != "walk", \
            f"{r['plan_date']}: walks are activity, never planned sessions"
        # CYCLE-1: no session is ever placed in a transit segment. A seeded row
        # carries the day's anchor, which is never transit, so this can only
        # fire if the anchor table gains one.
        assert not _cycle.is_transit(_cycle.anchor_location(r["plan_date"],
                                                            use_overrides=False)), \
            f"{r['plan_date']}: a session cannot be placed on the road"
        want = day_location(r["plan_date"], r.get("slot", "morning"))
        got = r["blocks"].get("location")
        assert got == want, f"{r['plan_date']}: location {got!r}, cycle says {want!r}"
    rejects, notes = duration_findings(rows)
    assert not rejects, "est_duration_min >= 60: " + "; ".join(rejects)
    for n in notes:
        logger.warning("TIME-CAP: %s", n)
    return notes


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


def write_rows(cur, rows: list[dict], validate: list[dict] | None = None) -> int:
    """UPSERT every office row and audit it through the same cursor. Does NOT
    commit.

    `validate` is the row set to check — pass the WHOLE program when `rows` is
    a targeted subset (reseed --only), since validate_rows asserts full-window
    coverage and the per-week lift counts.
    """
    duration_notes = validate_rows(validate if validate is not None else rows)
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
                     "rows": len(rows), "duration_notes": duration_notes})),
    )
    write_program_state(cur)
    return len(rows)
