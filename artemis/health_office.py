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

Rows are written INSERT ... ON CONFLICT (plan_date, slot) DO UPDATE in ONE transaction
with an acos.audit_log row, so plan_ids are stable and a session logged against a
date mid-run is never orphaned. generated_by='manual' (CHECK-legal); week_num is
1-7 (CHECK 1..19).
"""

import copy
import json
import logging
from datetime import date, time, timedelta

from knowledge import lift_recomp as _recomp
from knowledge import warmup as _prep

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

# LOCATION-1 (2026-09-26): warmup and cooldown resolve PER LOCATION from
# knowledge/warmup.py. These three stay as the OFFICE values, read from that
# config rather than duplicated here, because a lot of call sites and tests name
# them. A location with no entry gets the explicit unknown state, never these.
WARMUP = _prep.OFFICE["warmup"]
COOLDOWN = _prep.OFFICE["cooldown"]
COOLDOWN_MIN = _prep.OFFICE["cooldown_min"]
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
# vocabulary — "TRX row" read as `machine` (a 10 lb stack step for a strap),
# and both band exercises as `dumbbell`. Measured against the nine home-gym
# exercises in RDS, five of nine were wrong. LOCATION-1 makes that
# vocabulary real, so the guessing has to stop first.
#
# Every name this generator can emit, and every name already in RDS, is here.
# `class_for` raises on anything unknown: a new exercise without a class is a
# build error, not a silent `dumbbell`.
EQUIPMENT_CLASS: dict[str, str] = {
    # office — machines
    # PROGRAM-2 Richfield Strength B (Ryan, 2026-09-28)
    "Band seated row": "bands",
    "Band incline press": "bands",
    "Band leg extension": "bands",
    "Band rear delt fly": "bands",
    "Band Pallof press": "bands",
    "Stability-ball back extension": "bodyweight",
    # PROGRAM-2 Richfield Strength A (Ryan, 2026-09-28)
    "DB split squat": "dumbbell",
    "Band lat pulldown": "bands",
    "Stability-ball hamstring curl": "bodyweight",
    "Band face pull": "bands",
    "Lying leg raise": "bodyweight",
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
    # hotel substitutes (approved 2026-09-29)
    "Feet-elevated push-up": "bodyweight",
    "Bent-over DB rear delt raise": "dumbbell",
    "Prone back extension": "bodyweight",
    "Dead bug": "bodyweight",
    "Bird dog": "bodyweight",
    "Ball plank": "bodyweight",
    "TRX fallout": "trx",
    # the home-gym Pallof was a band; the office one is "Cable Pallof press"
    "Pallof press": "bands",
    # EXTRAS (2026-09-27, DRAFT pending Ryan's approval): no equipment beyond a mat
    "Glute bridge": "bodyweight",
    "McGill curl-up": "bodyweight",
    "Cat-cow": "bodyweight",
    "90/90 hip switch": "bodyweight",
    "Half-kneeling hip flexor stretch": "bodyweight",
    "Thread the needle": "bodyweight",
    "Ankle rocks": "bodyweight",
    "Child's pose": "bodyweight",
    # YOGA-6 poses (2026-09-28, draft). Flow poses do not currently route through
    # class_for, but class_for RAISES on an unknown name, so an entry here is what
    # keeps a future consumer from being a build error.
    "Chair": "bodyweight",
    "Plank": "bodyweight",
    "Warrior II": "bodyweight",
    "Warrior III": "bodyweight",
    "Low lunge twist": "bodyweight",
    # a legacy interval block, not a lift: duration work with no load. `cardio`
    # is a no-numeric-load class like bands and trx.
    "Stepmill or upright bike": "cardio",
    # LOCATION-1 — Richfield (the farm)
    "DB fly": "dumbbell",
    "1-arm DB row": "dumbbell",
    "Standing DB calf raise": "dumbbell",
    "Stability-ball crunch": "bodyweight",
}


# ── LOCATION-1: the same session, at another gym ────────────────────────────
#
# Substitution is DETERMINISTIC AND FROM A TABLE, never invented. Resolution
# order is LOCATION FIRST, THEN PAIN: the location resolver produces the
# session for that day's inventory, and the pain ladder then applies to the
# RESOLVED session (so a pain removal removes the substitute, and its own
# replacement also comes from that location's pool).
#
# Richfield Strength C, approved by Ryan 2026-09-23. Three of the six are
# native there; these are the three that are not, plus the ab machine.
# (name at the office) -> (name at Richfield, rep-range label, top reps)
# (name at the office) -> (name here, rep-range label, top reps, per_side)
#
# `per_side` joined the tuple on 2026-09-28: it used to be inherited from the
# office movement, which is right for every substitution above but wrong for a
# split squat — Leg press is not per-side, and prescribing "3×10-12" for a
# unilateral lift asks for half the work.
# ── LIFT-RECOMP: the class, pattern and station of every strength movement ──
#
# Three facts per exercise, on the row, for the same reason EXERCISE-CLASS put
# the equipment class there: inference from the name was measured wrong five
# times in nine. `lift_profile` RAISES on an unknown name, so a new exercise
# without a profile is a build error rather than a silent "accessory, push, no
# station".
#
#   class    compound | accessory -> which rep range (12-20 vs 15-25)
#   pattern  push | pull | lower | core -> what may be supersetted with what
#   station  the thing there is exactly ONE of, which must stay occupied for the
#            whole set. None means the movement contends for nothing.
#
# THE STATION IS THE LOAD-BEARING ONE, AND IT IS EASY TO GET WRONG:
#   * "Lat pulldown" and "Seated cable row" are the SAME machine here (the
#     EQUIPMENT_CLASS comment above says so), so they can never be supersetted
#     together even though one is a pulldown and one is a row.
#   * every cable movement contends for the single functional trainer.
#   * "Incline DB press" and "Seated DB shoulder press" both need the one
#     adjustable bench.
#   * ASSUMPTION, FLAGGED FOR RYAN: Richfield's band movements are treated as
#     sharing ONE anchor point. If there are two, density there improves and
#     these estimates are pessimistic. Over-estimating a session's length is the
#     safe direction — the reverse runs him out of the office window.
#: The first week of THIS BLOCK that LIFT-RECOMP governs.
#:
#: Block 1 starts it at week 5 (2026-10-11), because weeks 1-4 are already logged
#: or already in front of him. BLOCK 2 NUMBERS ITS WEEKS 1-6 ALL OVER AGAIN, so a
#: bare `week_num >= 5` would have given block 2's first four weeks the OLD
#: strength-biased prescription and only its last two the new one — silently, and
#: in a block that does not exist yet so nothing would have contradicted it.
#: `block2._build_one` lowers this to 1 inside the same swap it already does for
#: RAMP and INTERVAL_WEEKS, which is where block-2-specific tables belong.
RECOMP_FROM_WEEK = _recomp.FIRST_WEEK


def recomp_applies(week_num) -> bool:
    """True for a week this block applies LIFT-RECOMP to. Fail-closed on junk:
    an unreadable week keeps the prescription it already has."""
    try:
        return int(week_num) >= RECOMP_FROM_WEEK
    except (TypeError, ValueError):
        return False


_LC, _LP = _recomp.COMPOUND, _recomp.ACCESSORY
_PUSH, _PULL, _LOWER, _CORE = (_recomp.PUSH, _recomp.PULL, _recomp.LOWER,
                               _recomp.CORE)

LIFT_PROFILE: dict[str, tuple[str, str, str | None]] = {
    # office — machines, each its own station except the pulldown/row pair
    "Leg press":                     (_LC, _LOWER, "leg press"),
    "Lat pulldown":                  (_LC, _PULL,  "pulldown/row"),
    "Seated cable row":              (_LC, _PULL,  "pulldown/row"),
    "Seated leg curl":               (_LP, _LOWER, "leg curl"),
    "Leg extension":                 (_LP, _LOWER, "leg extension"),
    "Pec fly":                       (_LP, _PUSH,  "pec fly"),
    "Rear delt fly":                 (_LP, _PULL,  "rear delt fly"),
    "Calf press":                    (_LP, _LOWER, "calf press"),
    "Ab machine crunch":             (_LP, _CORE,  "ab machine"),
    "Seated back extension":         (_LP, _CORE,  "back extension"),
    # office — the single functional trainer
    "Cable face pull (rope)":        (_LP, _PULL,  "functional trainer"),
    "Cable Pallof press":            (_LP, _CORE,  "functional trainer"),
    "Single-arm cable row":          (_LP, _PULL,  "functional trainer"),
    # office — dumbbells; the BENCH is the station, the rack is not
    "DB bench press":                (_LC, _PUSH,  "flat bench"),
    "Incline DB press":              (_LC, _PUSH,  "adjustable bench"),
    "Seated DB shoulder press":      (_LC, _PUSH,  "adjustable bench"),
    "DB fly":                        (_LP, _PUSH,  "flat bench"),
    "1-arm DB row":                  (_LC, _PULL,  "flat bench"),
    "DB goblet squat":               (_LC, _LOWER, None),
    "DB Romanian deadlift":          (_LC, _LOWER, None),
    "DB split squat":                (_LC, _LOWER, None),
    "Standing DB calf raise":        (_LP, _LOWER, None),
    # bodyweight
    "Captain's chair knee raise":    (_LP, _CORE,  "captain's chair"),
    "Lying leg raise":               (_LP, _CORE,  None),
    "Stability-ball back extension": (_LP, _CORE,  "stability ball"),
    "Stability-ball crunch":         (_LP, _CORE,  "stability ball"),
    "Stability-ball hamstring curl": (_LP, _LOWER, "stability ball"),
    # hotel — approved 2026-09-29. No station for any of them except the bench
    # movements above, which are shared with the office table.
    "Feet-elevated push-up":         (_LC, _PUSH,  None),
    "Bent-over DB rear delt raise":  (_LP, _PULL,  None),
    "Prone back extension":          (_LP, _CORE,  None),
    "Side plank":                    (_LP, _CORE,  None),
    # Richfield — bands. One anchor assumed; "Band leg extension" is seated with
    # the band under the foot and needs none.
    "Band seated row":               (_LC, _PULL,  "band anchor"),
    "Band lat pulldown":             (_LC, _PULL,  "band anchor"),
    "Band incline press":            (_LC, _PUSH,  "band anchor"),
    "Band face pull":                (_LP, _PULL,  "band anchor"),
    "Band rear delt fly":            (_LP, _PULL,  "band anchor"),
    "Band Pallof press":             (_LP, _CORE,  "band anchor"),
    "Band leg extension":            (_LP, _LOWER, None),
}


def lift_profile(name: str) -> tuple[str, str, str | None]:
    """(class, pattern, station). Raises on an unknown movement — see above."""
    try:
        return LIFT_PROFILE[name]
    except KeyError:
        raise KeyError(
            f"no LIFT_PROFILE for {name!r} — add its class, pattern and station "
            f"in artemis/health_office.py before it can be programmed") from None


def lift_class_for(name: str) -> str:
    return lift_profile(name)[0]


def pattern_for(name: str) -> str:
    return lift_profile(name)[1]


def station_for(name: str) -> str | None:
    return lift_profile(name)[2]


RICHFIELD_SUBS: dict[str, tuple[str, str, int, bool]] = {
    "Pec fly": ("DB fly", "10-12", 12, False),
    "Single-arm cable row": ("1-arm DB row", "10-12", 12, False),
    "Calf press": ("Standing DB calf raise", "12-15", 15, False),
    "Ab machine crunch": ("Stability-ball crunch", "10-12", 12, False),
}

#: Richfield STRENGTH B — APPROVED by Ryan 2026-09-28, exactly as drafted.
#: DB goblet squat is deliberately ABSENT: PowerBlocks are native here.
#:
#: The incline press is the one that needed an argument. There is no incline
#: bench, and the three honest options were a band incline press from a low
#: anchor, a DB floor press and a DB flat press. The band keeps the INCLINE
#: ANGLE, which is the upper-chest bias the movement exists for; either DB option
#: just repeats Strength A's flat bench press and the week would carry two flat
#: presses and no incline. The tension curve is the trade -- a band is hardest at
#: lockout where a dumbbell is hardest at the stretch -- so the RPE does not map
#: across from the office version, and that is a known cost, not an oversight.
#:
#: Four of these need an anchor at a SPECIFIC HEIGHT, which the wall boards give
#: (every 6" from 6" off the floor to the ceiling). CLASS-ATTRIBUTES: `bands` says
#: what the implement is, not that the room can anchor it where the movement
#: needs -- so the height is named in each case rather than assumed.
RICHFIELD_B_SUBS: dict[str, tuple[str, str, int, bool]] = {
    "Seated cable row": ("Band seated row", "10-12", 12, False),
    "Incline DB press": ("Band incline press", "8-12", 12, False),
    "Leg extension": ("Band leg extension", "10-12", 12, False),
    "Rear delt fly": ("Band rear delt fly", "12-15", 15, False),
    "Cable Pallof press": ("Band Pallof press", "10", 10, True),
    "Seated back extension": ("Stability-ball back extension", "10-12", 12, False),
}

#: Richfield STRENGTH A — approved by Ryan 2026-09-28 as part of PROGRAM-2.
#: DB bench press is deliberately ABSENT: the flat bench is native here, so that
#: movement runs unchanged and a substitution row for it would be a lie.
RICHFIELD_A_SUBS: dict[str, tuple[str, str, int, bool]] = {
    "Leg press": ("DB split squat", "10-12", 12, True),
    "Lat pulldown": ("Band lat pulldown", "10-12", 12, False),
    "Seated leg curl": ("Stability-ball hamstring curl", "10-12", 12, False),
    "Cable face pull (rope)": ("Band face pull", "12-15", 15, False),
    "Captain's chair knee raise": ("Lying leg raise", "8-12", 12, False),
}

#: What the gym is called on the row, per location. The office keeps its
#: per-session equipment list; anywhere else names what the session uses.
# ── HOTEL GYM — approved by Ryan 2026-09-29 ("approve hotel") ───────────────
#
# The generic hotel gym, from away.HOTEL_GYM_INVENTORY and deliberately
# conservative: dumbbells 5-50 lb in 5s, ONE FLAT BENCH (no incline), treadmill,
# upright bike. `hotel gym has <items>` replaces the assumption for one stay.
#
# LIFT-RECOMP is what makes this table work at all: under a 5-rep scheme the
# 50 lb cap is a ceiling you grind into, and at 12-20 reps it is simply the load.
#
# THE INCLINE SLOT IS THE ONE THAT NEEDED AN ARGUMENT, and it is the same
# argument as Richfield's. There is no incline bench. A second flat press would
# just repeat Strength A's, leaving the week with two flat presses and no upper
# chest. A FEET-ELEVATED PUSH-UP keeps the upper-chest and front-delt bias the
# incline press exists for (Ryan's instruction, 2026-09-29). The trade, stated:
# the load is bodyweight only, so progression there is reps and foot height
# rather than dumbbells.
#
# BENCH CONTENTION is the real constraint here, not the dumbbell cap. A needs the
# one bench for 2 of 6 movements, B for 1 of 7, C for 3 of 6 — and the superset
# pairer never pairs two movements that need it, which is why C comes out at 42
# min rather than shorter. Assuming one bench is the safe direction; a hotel with
# two only improves it.
HOTEL_A_SUBS: dict[str, tuple[str, str, int, bool]] = {
    "Leg press":                  ("DB goblet squat", "12-20", 20, False),
    "Lat pulldown":               ("1-arm DB row", "12-20", 20, True),
    "Seated leg curl":            ("DB Romanian deadlift", "12-20", 20, False),
    "Cable face pull (rope)":     ("Bent-over DB rear delt raise", "15-25", 25, False),
    "Captain's chair knee raise": ("Lying leg raise", "15-25", 25, False),
    # DB bench press is native — the hotel has dumbbells and a flat bench.
}

HOTEL_B_SUBS: dict[str, tuple[str, str, int, bool]] = {
    "Seated cable row":      ("1-arm DB row", "12-20", 20, True),
    "Incline DB press":      ("Feet-elevated push-up", "12-20", 20, False),
    "Leg extension":         ("DB split squat", "12-20", 20, True),
    "Rear delt fly":         ("Bent-over DB rear delt raise", "15-25", 25, False),
    # A hold: the seconds come from _recomp.HOLD_SECONDS, not from this tuple.
    "Cable Pallof press":    ("Side plank", "30-45s", 45, True),
    "Seated back extension": ("Prone back extension", "15-25", 25, False),
    # DB goblet squat is native.
}

HOTEL_C_SUBS: dict[str, tuple[str, str, int, bool]] = {
    "Pec fly":             ("DB fly", "15-25", 25, False),
    "Single-arm cable row": ("1-arm DB row", "12-20", 20, True),
    "Calf press":          ("Standing DB calf raise", "15-25", 25, False),
    "Ab machine crunch":   ("Lying leg raise", "15-25", 25, False),
    # DB Romanian deadlift and Seated DB shoulder press are native.
}

LOCATION_EQUIPMENT: dict[str, list[str]] = {
    # CONFIRMED by Ryan 2026-09-28 15:14. "to 80 lb" was wrong: they pair to 90.
    "richfield": ["PowerBlocks (to 90 lb)", "flat bench", "stability ball",
                  "resistance bands + wall anchors", "TRX", "curl bar",
                  "bike on trainer", "yoga mat"],
    # ASSUMED, not confirmed: what almost every hotel gym has. `hotel gym has
    # <items>` records a real one for a stay. Assuming a cable stack and being
    # wrong means walking to a machine that is not there; assuming dumbbells and
    # a treadmill and being wrong is recoverable in a way the reverse is not.
    "hotel": ["dumbbells (5-50 lb, 5 lb steps)", "flat bench", "treadmill",
              "upright bike"],
}

#: Which office session each location can hold, and how.
LOCATION_SUBS: dict[str, dict[str, dict[str, tuple[str, str, int, bool]]]] = {
    "richfield": {"strength_a": RICHFIELD_A_SUBS, "strength_b": RICHFIELD_B_SUBS,
                  "strength_c": RICHFIELD_SUBS},
    "hotel": {"strength_a": HOTEL_A_SUBS, "strength_b": HOTEL_B_SUBS,
              "strength_c": HOTEL_C_SUBS},
}


def subs_for(location_key: str, session_type: str) -> dict[str, tuple[str, str, int]]:
    """The substitution table for a session at a location. Empty means the
    session runs as written (the office), or that this location has no table
    for it — `can_hold` is what decides whether it may be seeded at all."""
    return LOCATION_SUBS.get(location_key, {}).get(session_type, {})


def can_hold(location_key: str, session_type: str) -> bool:
    """**"Is there an APPROVED SUBSTITUTION TABLE for this session here?" — that
    is all this answers.** It reads like a general capability question and it is
    not (Ryan, 2026-09-26). Three things follow, and each has bitten:

    * **False does not mean impossible.** It is False for `rest` and
      `recovery_flow` at every non-office location, both of which are seeded
      there constantly and need nothing but a mat. Only the STRENGTH branch of
      `validate_rows` consults this, which is why that is not a bug — but do not
      reuse it as "can this room run this session".
    * **True does not mean every movement is possible.** A table maps names, and
      `equipment_class` describes what an implement IS, not what it can do. An
      exercise can pass both and still be undoable in the room: *Incline DB
      press* is class `dumbbell`, Richfield has dumbbells, and Richfield's bench
      is flat. See CLASS-ATTRIBUTES in docs/ARTEMIS_STATE.md.
    * **It never reads the room's constraints.** `load_config.CONSTRAINTS` holds
      the ceiling heights and `standing_overhead`, and nothing here consults
      them, so an overhead movement at a 6.5 ft location passes.

    The office holds everything it was written for; anywhere else needs a table.
    A location with no recorded inventory holds nothing.
    """
    if location_key == "office":
        return True
    return bool(subs_for(location_key, session_type))


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

# NAMING (Ryan, 2026-09-28 17:17): a session name says WHAT, never WHERE. The
# location is already its own field on the row and its own chip on the screen, so
# baking it into the name both repeats it and lies whenever the two disagree —
# which they did: "Office Strength A" was what the iPad showed for a session at
# Richfield, and had since LOCATION-1 made the office one location among four.
#
# `display_name_for()` below is the ONE derivation. Nothing should build a
# session name by hand, and gym-display reads the row rather than keeping a
# second list that can drift from this one.
_DISPLAY = {
    "strength_a": "Strength A",
    "strength_b": "Strength B",
    "strength_c": "Strength C",
    "cardio_z2": "Zone 2",
    # Without a modality this is the honest generic; with one it becomes
    # "Row intervals" etc. via display_name_for().
    "cardio_intervals": "Intervals",
    "rest_mobility": "Rest / Mobility",
    "rest": "Rest",
    "recovery_flow": "Recovery Flow",
    "core": "Core (easy)",
    "mobility": "Mobility",
    "yoga_strength": "Yoga — Strength & Balance",
}

#: Location display names, so a test can prove none of them reaches a session
#: name. Sourced from the cycle registry rather than typed twice.
def _location_words() -> tuple[str, ...]:
    return tuple((v or {}).get("display", k) for k, v in _cycle.DEFAULT_LOCATIONS.items())


def display_name_for(session_type: str, *, modality: str | None = None) -> str:
    """The name of a session. WHAT it is, never where it is.

    `modality` distinguishes the cardio sessions from each other, because "Zone 2"
    on a rower and "Zone 2" on a treadmill are different sessions to do even
    though they are the same session_type. It is the MODALITY, not the room: the
    rower reads "Zone 2 – Row" wherever the rower happens to live that month.
    """
    base = _DISPLAY.get(session_type, session_type)
    if session_type in ("cardio_z2", "cardio_intervals") and modality:
        from knowledge import cardio as cardio_cfg
        label = cardio_cfg.MODALITY_LABELS.get(modality, modality.title()) \
            if hasattr(cardio_cfg, "MODALITY_LABELS") else modality.title()
        if session_type == "cardio_z2":
            return f"{base} – {label}"
        return f"{label} intervals"
    return base

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
from knowledge import load_config as _load_config  # noqa: E402

CYCLE_ANCHOR = _cycle.DEFAULT_ANCHOR
DAY_OFF_WORK = {5}                 # the wi Friday is a day off work


def day_location_key(d: date, slot: str = "morning") -> str:
    """The location KEY (`office`, `richfield`, …) a row belongs to.

    OVERRIDE-AWARE (2026-09-26). This resolved on the base pattern, which made a
    full reseed silently revert every override: the leave week was rebuilt to
    Richfield and Brown Deer, and `build_rows()` would have put it back to
    office/msp_home without a word. The seeder now resolves the same way the
    scheduler, the wake post and the plan API do. The one thing that must NOT
    follow an override is which SESSION a day gets — `session_for()` reads the
    positional `DAY_TYPES` table, so the program shape is fixed and only the
    location moves.
    """
    return (_cycle.anchor_location(d) if slot == "morning"
            else _cycle.location_at(d, EVENING_AT))


def day_location(d: date, slot: str = "morning") -> str:
    """Where a seeded row happens, as the display name the rows carry.

    The MORNING is the day's anchor — where he wakes. Never the 00:00 segment:
    an msp_work day starts at msp_home and the session is at the office.

    The EVENING is where he is when the evening session happens (EVENING-1),
    which on a work day is home, not the office gym."""
    key = day_location_key(d, slot)
    return (_cycle.DEFAULT_LOCATIONS.get(key) or {}).get("display", key)


def evening_is_possible(d: date) -> bool:
    """False when he is on the road: no session is ever placed in a transit
    segment — it is reported, never relocated (CYCLE-1).

    Override-aware for the same reason as `day_location_key`: an override that
    says he is at the farm all day means he is NOT in a transit segment that
    evening, and the base pattern is not entitled to a vote on that."""
    return not _cycle.is_transit(_cycle.location_at(d, EVENING_AT))


def is_office_day(d: date) -> bool:
    """Override-aware, like every other day-type question. UNUSED as of
    2026-09-26 — `wake.py` has its own `_is_office_day(plan)` that reads the
    plan row. Kept override-aware rather than left on the base pattern so it
    cannot become the next silent reverter if something starts calling it."""
    return day_type(d) == "msp_work"


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
# PROGRAM-2 (Ryan, approved 2026-09-28): four of these were `rest` and are now
# cardio. The LIFT days are untouched -- A/B/C already land on office days 1, 3
# and 4 in both weeks, which is what the approved table says.
NON_LIFT = {
    0: "cardio_intervals",   # Sun, msp_home — row (the rower arrives 10/04)
    2: "cardio_z2",          # Tue, office
    5: "cardio_z2",          # Fri, Richfield — bike on the trainer
    6: "rest",               # Sat, Brown Deer — rest; Yoga S&B suggested
    7: "cardio_z2",          # Sun, Brown Deer — treadmill incline walk
    8: "rest",               # Mon, travel — rest; Mobility suggested
    10: "cardio_z2",         # Wed, office
    13: "cardio_intervals",  # Sat, msp_home — row
}

#: PROGRAM-2 SUGGESTED EXTRA — display only. Core stays an EXTRA and is NOT a
#: planned row and NOT part of the lift: the office window is 05:00-06:00 and B
#: is already 62 min. The row carries a NAME the card and the wake post show as
#: one line, and tapping it opens that library entry. No new rows, and no change
#: to what "extras" means.
SUGGESTED_EXTRA_BY_TYPE: dict[str, str] = {
    "strength_a": "core", "strength_b": "core", "strength_c": "core",
}
#: By cycle position, where the day rather than the session decides.
SUGGESTED_EXTRA_BY_POS: dict[int, str] = {
    6: "yoga_strength",   # Sat, Brown Deer — a mat is all it needs
    8: "mobility",        # Mon, travel
}


def suggested_extra(session_type: str, pos: int | None = None) -> str | None:
    """The extra offered alongside a session, or None. Display only."""
    if pos is not None and pos in SUGGESTED_EXTRA_BY_POS:
        return SUGGESTED_EXTRA_BY_POS[pos]
    return SUGGESTED_EXTRA_BY_TYPE.get(session_type)

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
    # LIFT-RECOMP (from week 5, 2026-10-11): the rep range comes from the lift's
    # CLASS, not from the table. The table's ranges are the old strength-biased
    # ones and they stay in force for weeks 1-4 — that is deliberately how the
    # reseed leaves everything before 10/11 untouched, rather than by filtering
    # dates afterwards and hoping.
    # A HOLD is prescribed in seconds and carries no target_reps. Checked before
    # the rep logic, because a hold has no rep range to compute.
    if _recomp.is_hold(name):
        lo, hi = _recomp.hold_seconds(name)
        notes = [f"{sets}×{lo}-{hi}s" + (" each side" if per_side else "")]
        return {"name": name, "format": "duration", "duration_sec": hi,
                "rest_after_sec": 60, "notes": "; ".join(notes),
                "equipment_class": class_for(name)}
    if recomp_applies(week_num):
        lift_class = lift_class_for(name)
        rng = _recomp.rep_label(lift_class)
        top = _recomp.top_reps(lift_class)
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


def _apply_supersets(exercises: list, pairs: list) -> None:
    """Mark the paired exercises, in place, and set the rests that enact the pair.

    THE RESTS ARE WHAT MAKE THIS WORK ON THE EXISTING iPAD. gym-display renders
    `rest_after_sec` as its rest timer and knows nothing about supersets, so the
    first half of a pair gets the short transition (walk to the partner) and the
    second gets the real rest. Following the timers therefore performs the
    superset correctly with no frontend change at all — and the note names the
    partner so the screen reads as instructions rather than as odd rest values.
    """
    by_name = {e["name"]: e for e in exercises}
    for idx, (a, b) in enumerate(pairs, start=1):
        ea, eb = by_name[a], by_name[b]
        for e, partner, rest in ((ea, b, _recomp.INTRA_PAIR_SEC),
                                 (eb, a, _recomp.REST_BETWEEN_PAIRS_SEC)):
            e["superset"] = idx
            e["superset_partner"] = partner
            e["rest_after_sec"] = rest
        ea["notes"] = f"{ea['notes']}; superset {idx} — straight into {b}"
        eb["notes"] = (f"{eb['notes']}; superset {idx} with {a} — "
                       f"rest {_recomp.REST_BETWEEN_PAIRS_SEC}s, then repeat")
    for e in exercises:
        if "superset" not in e:
            # Unpaired: it has not become denser, so it keeps the old interval.
            e["rest_after_sec"] = _recomp.REST_BETWEEN_SETS_SEC


def _strength(session_type: str, week_num: int, *, wk0: bool = False,
              location: str = LOCATION, location_key: str = "office"):
    sets, rpe, _ = RAMP[week_num]
    subs = subs_for(location_key, session_type)
    specs = []
    for spec in _EXERCISES[session_type]:
        name, label, top, per_side, machine = spec
        if name in subs:
            sub_name, sub_label, sub_top, sub_per_side = subs[name]
            # A substitute is never a machine. `per_side` now comes from the
            # TABLE rather than from the office movement it replaces: a split
            # squat is unilateral where a leg press is not, and inheriting False
            # would have prescribed half the work.
            specs.append((sub_name, sub_label, sub_top, sub_per_side, False))
        else:
            specs.append(spec)
    exercises = [_exercise(*e, sets, week_num, wk0=wk0) for e in specs]
    setup = []
    if week_num <= 2:
        setup.append("Weeks 1-2: finding weights — stop 3-4 reps shy of failure.")
    elif week_num == 7:
        setup.append("Week 7 deload — 2 sets, easy.")
    elif recomp_applies(week_num):
        # LIFT-RECOMP: reps first, then one load step. The trigger is unchanged
        # in kind (every set at the top of the range) and changed in number,
        # because the range itself moved.
        setup.append(_recomp.progression_note(_recomp.COMPOUND)
                     .replace("Add reps first:", "Progression — reps first:"))
        setup.append(_recomp.DENSITY_NOTE)
    else:
        setup.append("Progression: +1 rep or next pin once all sets hit the top of the range.")
    blocks = {
        "type": "circuit",
        "display_name": _DISPLAY[session_type],
        "location": location,
        "rounds": sets,
        "rest_between_rounds_sec": 90,
        "equipment": (list(SESSION_EQUIPMENT[session_type]) if location_key == "office"
                      else list(LOCATION_EQUIPMENT.get(location_key, []))),
        "exercises": exercises,
        "setup_notes": setup,
    }
    # LOCATION-1 (2026-09-26): the warmup and cooldown come from THIS location's
    # config. Absent means absent — the keys are omitted and `prep_unknown` is set,
    # so nothing renders another gym's equipment as this session's warmup.
    _apply_prep(blocks, location_key, location)
    if subs:
        blocks["substituted_from"] = "office"
        blocks["substitutions"] = [{"from": k, "to": v[0]} for k, v in subs.items()]
    # ── duration ─────────────────────────────────────────────────────────
    #
    # The leading 10 is ALREADY the warmup + cooldown allowance for a strength
    # session, so cooldown_min is deliberately NOT added here — doing so pushed
    # every office lift from ~55 to ~60 and tripped the TIME-CAP reject. Only the
    # Z2 estimate adds it, because that one counts work minutes alone.
    #
    # LIFT-RECOMP: from week 5 the session is PAIRED, so the estimate comes from
    # knowledge/lift_recomp.py's named components rather than one flat 2.5
    # min/set. The old constant could not express the change at all: it bundled
    # work, rest, setup and logging together, so halving the number of rests
    # moved the answer by zero. Weeks 1-4 keep the old formula.
    finisher_min = 12 if (session_type == "strength_c" and week_num in (5, 6)) else 0
    if recomp_applies(week_num):
        pairs, singles = _recomp.pair_for_density(
            [e["name"] for e in exercises],
            station_of=station_for, pattern_of=pattern_for)
        _apply_supersets(exercises, pairs)
        blocks["supersets"] = [list(pair) for pair in pairs]
        blocks["unpaired"] = list(singles)
        blocks["rest_between_rounds_sec"] = _recomp.REST_BETWEEN_PAIRS_SEC
        blocks["density_note"] = _recomp.DENSITY_NOTE
        minutes = _recomp.session_minutes(n_pairs=len(pairs), n_singles=len(singles),
                                          sets=sets, finisher_min=finisher_min)
    else:
        minutes = 10 + round(sets * len(exercises) * 2.5) + finisher_min
    if session_type == "strength_c" and week_num in (5, 6):
        blocks["finisher"] = {
            "type": "intervals",
            "display_name": "Conditioning finisher",
            "rounds": 6,
            # LOCATION-1: the finisher's exercise carries its class like every
            # other one. Nothing infers a class from a name any more, and the
            # seeder gate (tests/test_seed_rows.py) found this row unclassed.
            "exercises": [{"name": "Stepmill or upright bike", "format": "duration",
                           "duration_sec": 30, "rest_after_sec": 90,
                           "equipment_class": class_for("Stepmill or upright bike"),
                           "notes": "30s hard / 90s easy"}],
        }
        # minutes already includes finisher_min — adding it here would double it.
    return blocks, rpe, 3, minutes


def _z2(week_num: int, location: str = LOCATION, location_key: str = "office", on=None):
    """CARDIO-LOC (2026-09-25): the modality comes from the LOCATION'S INVENTORY
    via `knowledge.cardio`, in the configured preference order.

    This used to be a two-way branch — the office's equipment list, else the
    literal ["rower", "bike on trainer"] — which is why a Richfield Z2 named a
    rower that `FORBIDDEN_TOKENS` forbids in an office row. Inventory is config
    now, so the rower moving to MSP is one line in `knowledge/cardio.py`.
    """
    from knowledge import cardio as cardio_cfg
    from knowledge import zones

    lo, hi = RAMP[week_num][2]
    office = location == LOCATION
    resolved = cardio_cfg.resolve(location_key, on)
    blocks = {
        "type": "steady",
        "display_name": display_name_for("cardio_z2",
                                         modality=resolved.get("modality")),
        "location": location,
        "location_key": location_key,
        "duration_min": hi,
        "intensity": "Zone 2",
        # PROGRAM-2: the range travels on the row like the modality does. A Z2
        # session that names a zone and not its bpm asks him to remember what Z2
        # means, and the iPad would have to derive it from a second constant.
        "zones": {"work": zones.zone_block("Z2")},
        # The resolved modality travels on the row, like load_config does, so
        # the iPad and the box read one answer instead of deciding separately.
        "cardio": resolved,
        "equipment": [cardio_cfg.label_for(d) for _, d in cardio_cfg.available(location_key, on)],
        "setup_notes": [Z2_NOTES if office else cardio_cfg.describe(resolved),
                        f"{zones.describe('Z2')} — conversational pace"],
    }
    if not resolved.get("modality"):
        # MSP home: an explicit state, never a silent fall-through to another
        # location's equipment. The session still exists and says why.
        blocks["setup_notes"] = [cardio_cfg.describe(resolved),
                                 f"{zones.describe('Z2')} — conversational pace"]
        blocks["no_equipment"] = True
    # Ryan, 2026-09-19: keeps the Stretch Trainer in the program now that the
    # flows travel. Same cooldown the strength days use — and, since 2026-09-26,
    # the same per-location resolution: it appears because the OFFICE config has
    # a cooldown, not because the code says "if office".
    _apply_prep(blocks, location_key, location, warmup=False, add_equipment=True)
    if lo != hi:
        blocks["target_range_min"] = [lo, hi]
    est = hi + _prep.cooldown_min(location_key)
    return blocks, 4.0, 2, est


# ── PROGRAM-2 intervals (Ryan, 2026-09-28) ──────────────────────────────────
#
# `cardio_intervals` existed as a session type but had NO BUILDER: it fell
# through to `_rest`, which is why the 14 rows seeded with it rendered as
# "Rest / Mobility". This is the builder.
#
# Weeks that are NOT in INTERVAL_WEEKS run the **Z2 variant** — the same row,
# steady instead of intervals. That is one concept, not two: the early weeks run
# it because the plan says so, and a gated week runs it because a condition
# failed, and both produce a session he can actually do rather than an absent row.
INTERVAL_WARMUP_MIN = 10
INTERVAL_COOLDOWN_MIN = 5
#: program week -> (reps, work seconds at Z4, easy seconds between)
INTERVAL_WEEKS: dict[int, tuple[int, int, int]] = {
    5: (6, 60, 120),
    6: (5, 120, 120),
}
#: The Z2 variant's work minutes, by week. Week 7 is the deload.
Z2_VARIANT_MIN: dict[int, int] = {7: 30}
Z2_VARIANT_DEFAULT_MIN = 40


def _cardio_common(location: str, location_key: str, on):
    from knowledge import cardio as cardio_cfg
    resolved = cardio_cfg.resolve(location_key, on)
    return resolved, [cardio_cfg.label_for(d)
                      for _, d in cardio_cfg.available(location_key, on)]


def _intervals(week_num: int, location: str = LOCATION, location_key: str = "office",
               on=None, gate: dict | None = None):
    """A `cardio_intervals` session, or its Z2 variant when the week or the gate
    says so.

    `gate` is the GATE's own answer, and **its absence is a FAIL, not a pass.**
    A row is seeded weeks ahead, long before the gate could be evaluated for its
    day, so "nobody has asked yet" has to resolve to the Z2 variant -- otherwise
    every seeded row would ship as intervals and the gate would only ever be able
    to take Zone 4 away on the morning, which is the wrong direction. The only
    thing that turns a row into intervals is a gate that was evaluated and
    passed. A blocked gate names the condition that failed on the card, because
    "why is this Zone 2 today" is the first thing he will ask.
    """
    from knowledge import cardio as cardio_cfg
    from knowledge import zones

    resolved, equipment = _cardio_common(location, location_key, on)
    modality = resolved.get("modality")
    spec = INTERVAL_WEEKS.get(week_num)
    # FAIL-CLOSED: no gate answer is a fail. `gate and gate.get("ok")` -- not
    # `gate and not gate.get("ok")`, which read an absent gate as a pass.
    blocked = not (gate and gate.get("ok"))
    reason = None
    if blocked:
        reason = (gate.get("reason") if gate else
                  "the interval gate has not been evaluated for this day yet")
    elif spec is None:
        reason = (f"week {week_num} is the deload — easy Z2" if week_num == 7
                  else f"week {week_num} runs Z2 by the plan; intervals start in week 5")

    if spec is None or blocked:
        work_min = Z2_VARIANT_MIN.get(week_num, Z2_VARIANT_DEFAULT_MIN)
        blocks = {
            "type": "steady",
            "display_name": display_name_for("cardio_z2", modality=modality),
            "location": location,
            "location_key": location_key,
            "duration_min": work_min,
            "intensity": "Zone 2",
            "zones": {"work": zones.zone_block("Z2")},
            "cardio": resolved,
            "equipment": equipment,
            "ran_as": "z2_variant",
            "z2_variant_reason": reason,
            "setup_notes": [cardio_cfg.describe(resolved),
                            f"{zones.describe('Z2')} — conversational pace",
                            f"Intervals not today: {reason}." if reason else ""],
        }
        blocks["setup_notes"] = [n for n in blocks["setup_notes"] if n]
        if not modality:
            blocks["no_equipment"] = True
        est = INTERVAL_WARMUP_MIN + work_min + INTERVAL_COOLDOWN_MIN
        # target_hr_zone is an INTEGER column -- `_z2` returns 2, not "Zone 2".
        return blocks, None, 2, est

    reps, work_sec, easy_sec = spec
    blocks = {
        "type": "intervals",
        "display_name": display_name_for("cardio_intervals", modality=modality),
        "location": location,
        "location_key": location_key,
        "intervals": {"reps": reps, "work_sec": work_sec, "easy_sec": easy_sec},
        "warmup_min": INTERVAL_WARMUP_MIN,
        "cooldown_min": INTERVAL_COOLDOWN_MIN,
        "intensity": "Zone 4",
        # Both ranges travel on the row: the work target and what "easy" means
        # between reps, so the iPad never has to derive one from the other.
        "zones": {"work": zones.zone_block("Z4"), "easy": zones.zone_block("Z2")},
        "cardio": resolved,
        "equipment": equipment,
        "ran_as": "intervals",
        "setup_notes": [
            cardio_cfg.describe(resolved),
            f"{INTERVAL_WARMUP_MIN} min easy warm-up, then "
            f"{reps} × {work_sec // 60 if work_sec % 60 == 0 else work_sec / 60:g} min "
            f"hard / {easy_sec // 60} min easy, then "
            f"{INTERVAL_COOLDOWN_MIN} min easy cool-down.",
            f"Hard = {zones.describe('Z4')}. Easy = {zones.describe('Z2')}.",
        ],
    }
    if not modality:
        blocks["no_equipment"] = True
    est = INTERVAL_WARMUP_MIN + round(reps * (work_sec + easy_sec) / 60) + INTERVAL_COOLDOWN_MIN
    return blocks, None, 4, est


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
    # YOGA-6 (draft). Every one is an EXISTING posture on purpose: adding a value
    # to FLOW_POSTURES would need the same value added to gym-display's POSTURES
    # union in the same change (ENUM-EXPAND), which is not a draft-content change.
    # "Low lunge twist" is kneeling because the back knee is down; a plank is
    # face-down and supported, which is what prone means.
    "Chair": "standing", "Warrior II": "standing", "Warrior III": "standing",
    "Plank": "prone", "Low lunge twist": "kneeling",
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
    # YOGA-6 (draft).
    "Chair":           "Weight in your heels, chest up, sit back",
    "Plank":           "One line from heels to head, ribs down",
    "Warrior II":      "Front knee over the ankle, arms long and level",
    "Warrior III":     "Reach the back heel away, hips level",
    "Low lunge twist": "Lengthen first, then turn from the ribs",
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
    # YOGA-6 — CONTENT APPROVED by Ryan 2026-09-28.
    "Chair":                 ("Utkatasana", "oot-kah-TAH-sah-nah"),
    "Plank":                 ("Phalakasana", "fah-lah-KAH-sah-nah"),
    "Warrior II":            ("Virabhadrasana II", "veer-ah-bah-DRAH-sah-nah two"),
    "Warrior III":           ("Virabhadrasana III", "veer-ah-bah-DRAH-sah-nah three"),
    "Low lunge twist":       ("Parivrtta Anjaneyasana",
                              "pah-ree-VRIT-tah ahn-jah-nay-AH-sah-nah"),
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


def _flow_step(spec: dict, hold_sec: int = FLOW_HOLD_SEC) -> dict:
    """One flow-step entry as the JSONB row gym-display reads.

    `hold_sec` is a parameter because YOGA-6 holds for less time than the
    Recovery Flow does; a step may also carry its own `duration_sec`. The
    Recovery Flow's default is unchanged, so its rows are byte-identical.
    """
    name = spec["name"]
    out = {"step": spec["step"], "name": name, "side": spec.get("side"),
           "side_label": spec.get("side_label"),
           "duration_sec": spec.get("duration_sec", hold_sec),
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


# ---------------------------------------------------------------------------
# YOGA-6 — Yoga, Strength & Balance. A higher-intensity flow for the Extras list.
#
# CONTENT APPROVED by Ryan 2026-09-28.
#
# Still low-impact and still mat-only — Ryan's rule is that extras are
# yoga/core/mobility — but standing strength and single-leg balance instead of
# the Recovery Flow's restorative shape. Holds are 30 s rather than 40 s and
# there are more of them, so the flow keeps moving.
#
# Order notes:
#   * it opens with a short sun-salutation arc (forward bend → plank → cobra →
#     downward dog) so nothing loaded happens cold;
#   * the eight standing poses run R,R,R,R then L,L,L,L in MIRROR ORDER, so the
#     side switch lands at the low lunge twist rather than between two different
#     poses — the same reason the Recovery Flow runs its lunges R,R,L,L;
#   * chair sits before the single-leg work, while the legs are fresh enough for
#     warrior III to be balance rather than a fight;
#   * bridge then child's pose close each round, which is where the trunk work
#     lands after the standing block rather than before it.
#
# Every posture used here already exists in FLOW_POSTURES. Adding one would mean
# adding the same value to gym-display's POSTURES union in the same change
# (ENUM-EXPAND), and that is not a draft-content change — which is why there is
# no side plank in this draft.
# ---------------------------------------------------------------------------
YOGA6_HOLD_SEC = 30
YOGA6_ROUNDS = 2
YOGA6_TARGET_RPE = 4.5
YOGA6_OPEN = {"name": "Standing centering", "side": None, "duration_sec": 30,
              "transition_sec": 5, "posture": "standing", "cue_mid": None,
              "cue": "Stand tall, feet hip-width, three slow breaths."}
YOGA6_CLOSE = {"name": "Savasana", "side": None, "duration_sec": 180,
               "transition_sec": 5, "posture": "supine", "cue_mid": None,
               "cue": "Lie on your back, arms by your sides, let everything go.",
               "sanskrit": "Shavasana", "sanskrit_spoken": "shah-VAH-sah-nah"}

YOGA6_STEPS = [
    {"step": "1", "name": "Standing forward bend", "transition_sec": 5,
     "cue": "Soft knees, fold from the hips, let your head hang."},
    {"step": "2", "name": "Plank", "transition_sec": 4,
     "cue": "One line from heels to head, hands under the shoulders.",
     "easier": "Knees down"},
    {"step": "3", "name": "Cobra", "transition_sec": 3,
     "cue": "Hips stay down; press the palms and lift the chest gently."},
    {"step": "4", "name": "Downward dog", "transition_sec": 3,
     "cue": "Hips high, heels reaching down, long spine.",
     "easier": "Dolphin — forearms down"},
    {"step": "5", "name": "Chair", "transition_sec": 5,
     "cue": "Feet together, sit back, weight in the heels, arms up.",
     "easier": "Feet hip-width, sit back less"},
    {"step": "6", "name": "High lunge", "side": "R", "mirror_group": "standing-unit",
     "side_label": "Right leg forward", "transition_sec": 5,
     "cue": "Front knee over ankle, back heel lifted, arms up.", "easier": "Knee down"},
    {"step": "7", "name": "Warrior II", "side": "R", "mirror_group": "standing-unit",
     "side_label": "Right leg forward", "transition_sec": 4,
     "cue": "Open the hips, arms long and level, gaze over the front hand.",
     "easier": "Shorten the stance"},
    {"step": "8", "name": "Warrior III", "side": "R", "mirror_group": "standing-unit",
     "side_label": "Standing on the right leg", "transition_sec": 4,
     "cue": "Hinge forward, back leg straight behind, hips level.",
     "easier": "Fingertips to a wall or the floor"},
    {"step": "9", "name": "Low lunge twist", "side": "R", "mirror_group": "standing-unit",
     "side_label": "Right leg forward", "transition_sec": 4,
     "cue": "Back knee down, lengthen up, then turn toward the front leg.",
     "easier": "Hand to the floor, turn less"},
    {"step": "10", "name": "Low lunge twist", "side": "L", "mirror_group": "standing-unit",
     "side_label": "Left leg forward", "transition_sec": 5,
     "cue": "Back knee down, lengthen up, then turn toward the front leg.",
     "easier": "Hand to the floor, turn less"},
    {"step": "11", "name": "Warrior III", "side": "L", "mirror_group": "standing-unit",
     "side_label": "Standing on the left leg", "transition_sec": 4,
     "cue": "Hinge forward, back leg straight behind, hips level.",
     "easier": "Fingertips to a wall or the floor"},
    {"step": "12", "name": "Warrior II", "side": "L", "mirror_group": "standing-unit",
     "side_label": "Left leg forward", "transition_sec": 4,
     "cue": "Open the hips, arms long and level, gaze over the front hand.",
     "easier": "Shorten the stance"},
    {"step": "13", "name": "High lunge", "side": "L", "mirror_group": "standing-unit",
     "side_label": "Left leg forward", "transition_sec": 4,
     "cue": "Front knee over ankle, back heel lifted, arms up.", "easier": "Knee down"},
    {"step": "14", "name": "Bridge", "transition_sec": 5,
     "cue": "Feet hip-width, press through the heels, lift the hips."},
    {"step": "15", "name": "Child's pose", "transition_sec": 4,
     "cue": "Knees wide, hips back toward your heels, arms long."},
]


def _yoga_strength(location: str = LOCATION):
    """YOGA-6. Mat only, so it runs at every location a mat does.

    CONTENT APPROVED by Ryan 2026-09-28.
    """
    blocks = {
        "type": "recovery_flow",          # the RENDERER: gym-display's Flow screen
        "session_type": "yoga_strength",
        "display_name": _DISPLAY["yoga_strength"],
        "location": location,
        "rounds": YOGA6_ROUNDS,
        "hold_sec": YOGA6_HOLD_SEC,
        "leadin_sec": FLOW_LEADIN_SEC,
        "cue_mid_sec": FLOW_CUE_MID_SEC,
        "start_posture": FLOW_START_POSTURE,
        "pre": [dict(YOGA6_OPEN)],
        "flow": [_flow_step(s, YOGA6_HOLD_SEC) for s in YOGA6_STEPS],
        "close": dict(YOGA6_CLOSE),
        "equipment": [EQ_MAT],
        "extra": True,
    }
    total = flow_total_sec(blocks)
    blocks["total_sec"] = total
    n = len(flow_steps_for_round(blocks, 1))
    blocks["notes"] = (f"{YOGA6_ROUNDS} rounds of {n} poses, every hold "
                       f"{YOGA6_HOLD_SEC} s, 3 min savasana; 3–5 s to move between "
                       f"poses. Standing strength and balance — harder than the "
                       f"Recovery Flow, still mat-only.")
    validate_flow(blocks)
    return blocks, YOGA6_TARGET_RPE, None, -(-total // 60)


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
           location: str | None = None, location_key: str = "office",
           on=None, gate: dict | None = None):
    """Every blocks dict carries its location_key: the pain ladder reads it to
    filter the substitution pool, and a row without one is an office row (which
    is what every row seeded before LOCATION-1 is)."""
    blocks, rpe, zone, est = _build_inner(session_type, week_num, wk0=wk0,
                                          location=location, location_key=location_key,
                                          on=on, gate=gate)
    blocks["location_key"] = location_key
    cfg = _load_config.for_location(location_key)
    if cfg:
        blocks["load_config"] = copy.deepcopy(cfg)
    return blocks, rpe, zone, est


# ---------------------------------------------------------------------------
# EXTRAS (Ryan, 2026-09-27): low-impact work on top of the plan, any day, rest
# days included. Never planned rows — the Sessions tab builds them on demand.
# Bodyweight and a mat only, so they run anywhere but on the road.
# CONTENT APPROVED by Ryan 2026-09-27 (#217). The marker said DRAFT until
# 2026-09-28: the content was approved and merged, and only the comment was
# left behind — so the source claimed it was unapproved for a day.
#
# Core stays LIGHT and goes AFTER the day's lift, never before it: the trunk
# braces squats and hinges, and pre-fatiguing it costs stability under the bar.
# ---------------------------------------------------------------------------

# (name, format, target, per_side, rest_after_sec)
_CORE = (
    ("Dead bug",       "reps",     8,  True,  30),
    ("Bird dog",       "reps",     8,  True,  30),
    ("Side plank",     "duration", 20, True,  30),
    ("Glute bridge",   "reps",     12, False, 30),
    ("McGill curl-up", "reps",     5,  False, 45),
)
_MOBILITY = (
    ("Cat-cow",                          "reps",     10, False, 10),
    ("90/90 hip switch",                 "reps",     6,  True,  10),
    ("Half-kneeling hip flexor stretch", "duration", 40, True,  10),
    ("Thread the needle",                "reps",     6,  True,  10),
    ("Ankle rocks",                      "reps",     10, True,  10),
    ("Child's pose",                     "duration", 45, False, 0),
)


def _extra_exercise(name, fmt, target, per_side, rest) -> dict:
    ex = {"name": name, "format": fmt, "rest_after_sec": rest,
          "equipment_class": class_for(name),
          "notes": "each side" if per_side else ""}
    if fmt == "duration":
        ex["duration_sec"] = target
    else:
        ex["target_reps"] = target
    return ex


def _extra(session_type: str, location: str):
    spec, rounds, rpe, notes = {
        "core": (_CORE, 2, 4.0,
                 ["Light by design — after the day's lift, never before squats or hinges.",
                  "Slow and controlled; stop well short of fatigue."]),
        "mobility": (_MOBILITY, 1, 2.0,
                     ["Easy range, no forcing. Breathe through each hold."]),
    }[session_type]
    exercises = [_extra_exercise(*e) for e in spec]
    blocks = {"type": "circuit", "display_name": _DISPLAY[session_type], "location": location,
              "rounds": rounds, "rest_between_rounds_sec": 45, "equipment": ["mat"],
              "exercises": exercises, "setup_notes": notes, "extra": True}
    work = sum((e.get("duration_sec") or e.get("target_reps", 0) * 4)
               * (2 if s[3] else 1) + s[4] for e, s in zip(exercises, spec))
    return blocks, rpe, None, max(5, round(rounds * work / 60))


def _build_inner(session_type: str, week_num: int, *, wk0: bool = False,
                 location: str | None = None, location_key: str = "office",
                 on=None, gate: dict | None = None):
    if session_type == "yoga_strength":
        return _yoga_strength(location or LOCATION)
    if session_type in ("core", "mobility"):
        return _extra(session_type, location or LOCATION)
    if session_type == "bodyweight_circuit":
        # BW-CIRCUIT. Without this the type fell through to the rest/mobility
        # branch and a placed circuit rendered as "Rest / Mobility" -- a row
        # that says one thing and does another.
        from artemis import bw_circuit
        return bw_circuit.build(gate=gate, location=location or LOCATION)
    if session_type == "recovery_flow":
        return _recovery_flow(location or LOCATION)
    if session_type.startswith("strength"):
        return _strength(session_type, week_num, wk0=wk0,
                         location=location or LOCATION, location_key=location_key)
    if session_type == "cardio_z2":
        return _z2(week_num, location or LOCATION, location_key=location_key, on=on)
    if session_type == "cardio_intervals":
        return _intervals(week_num, location or LOCATION, location_key=location_key,
                          on=on, gate=gate)
    if session_type == "rest":
        return _rest_day()
    return _rest(week_num)


def _apply_prep(blocks: dict, location_key: str, location: str,
                *, warmup: bool = True, add_equipment: bool = False) -> None:
    """Put this LOCATION's warmup and cooldown on a row, or the explicit unknown
    state when it has none (LOCATION-1, 2026-09-26).

    Never falls back to the office. A missing key is the signal every consumer
    reads: gym-display renders "not configured" the way it does for a row with no
    `load_config`, rather than telling Ryan to use an elliptical that is not in
    the room. `prep_unknown` makes it positive rather than merely absent, so a
    validator and a screen can both see it without inferring from a missing key.
    """
    if not _prep.is_known(location_key):
        blocks["prep_unknown"] = True
        note = _prep.unknown_note(location_key, location)
        blocks.setdefault("setup_notes", []).append(note)
        return
    if warmup and _prep.warmup_for(location_key):
        blocks["warmup"] = _prep.warmup_for(location_key)
    cool = _prep.cooldown_for(location_key)
    if cool:
        blocks["cooldown"] = cool
        # Only Z2 lists the cooldown's equipment, which is what it did before this
        # refactor: SESSION_EQUIPMENT already fixes a strength row's list, and
        # appending here would change every existing office lift and show up as a
        # reseed diff for no reason.
        if add_equipment:
            eq = _prep.cooldown_equipment(location_key)
            if eq and eq not in blocks.get("equipment", []):
                blocks.setdefault("equipment", []).append(eq)


def _rest_day():
    """EVENING-1: a planned rest morning. A REAL row — "no plan" and "rest
    today" are different facts, and only one of them is a data problem."""
    return ({"type": "rest", "display_name": _DISPLAY["rest"], "equipment": [],
             "notes": "Rest. Nothing planned this morning."}, None, None, 0)


# ============================================================================
# Schedule
# ============================================================================

# ---------------------------------------------------------------------------
# REPEAT-WEEK (Ryan, 2026-09-27): a program week with more than one session not
# done is repeated, and the block's END MOVES OUT a week rather than losing its
# last week. A repeat is recorded as the Sunday the repeat week begins; the
# program week number for any date is the calendar count minus the repeats
# that have begun by then. Applied only on Ryan's `repeat week` (propose-then-
# confirm) — see artemis/program_repeat.py.
# ---------------------------------------------------------------------------

REPEATS_KEY = "program_repeats"


def repeat_starts() -> list[date]:
    """The recorded repeat weeks. FAIL-CLOSED: an unreadable or malformed value
    raises — a silent [] would renumber every week and rebuild the plan wrong.
    The one permitted swallow is RealDbInTestError ("no table here")."""
    from knowledge.dbguard import RealDbInTestError
    from artemis.quiet_hours import get_system_value
    try:
        raw = get_system_value(REPEATS_KEY)
    except RealDbInTestError:
        return []
    if not raw:
        return []
    got = json.loads(raw)
    if not isinstance(got, list):
        raise ValueError(f"{REPEATS_KEY} is not a list: {raw[:80]!r}")
    return sorted(date.fromisoformat(str(x)[:10]) for x in got)


def program_end(repeats: list[date] | None = None) -> date:
    """The block's last day: OFFICE_END plus one week per repeat."""
    reps = repeat_starts() if repeats is None else repeats
    return OFFICE_END + timedelta(days=7 * len(reps))


def calendar_week_start(d: date) -> date:
    return WEEK2_START + timedelta(days=7 * ((d - WEEK2_START).days // 7))


def week_num_for(d: date, repeats: list[date] | None = None) -> int:
    """Program week. Week 1 is the 9/16..9/19 stub; weeks 2+ run Sun..Sat from
    WEEK2_START, matching the CYCLE-1 pay period (SCHEDULE-2). Each repeat that
    has begun by `d` holds the number back one (REPEAT-WEEK)."""
    if d < WEEK2_START:
        return 1
    reps = repeat_starts() if repeats is None else repeats
    return 2 + (d - WEEK2_START).days // 7 - sum(1 for r in reps if r <= d)


def build_schedule(repeats: list[date] | None = None) -> list[dict]:
    """Ordered specs {plan_date, session_type, week_num, location, wk0} from
    WEEK2_START to program_end() — OFFICE_END plus a week per repeat.

    The 9/16..9/19 week-1 stub is NOT regenerated — it is logged history and
    stays as seeded. Weeks 2..7 are Sun..Sat and the session for each day comes
    from SCHEDULE-2's office-day rule over the CYCLE-1 day types.
    """
    reps = repeat_starts() if repeats is None else repeats
    end = program_end(reps)
    specs: list[dict] = []
    d = WEEK2_START
    while d <= end:
        specs.append({"plan_date": d, "slot": "morning", "session_type": session_for(d),
                      "week_num": week_num_for(d, reps), "location": day_location(d),
                      "location_key": day_location_key(d), "day_type": day_type(d),
                      "pos": cycle_pos(d), "wk0": False})
        # EVENING-1: four evenings a week, and never one in a transit segment.
        if cycle_pos(d) in EVENING_POS and evening_is_possible(d):
            specs.append({"plan_date": d, "slot": "evening", "session_type": EVENING_SESSION,
                          "week_num": week_num_for(d, reps),
                          "location": day_location(d, "evening"),
                          "location_key": day_location_key(d, "evening"),
                          "day_type": day_type(d), "wk0": False})
        d += timedelta(days=1)
    return specs


def build_row(spec: dict) -> dict:
    wk0 = spec["wk0"]
    week_num = spec["week_num"]
    session_type = spec["session_type"]
    location = spec.get("location") or LOCATION
    # PROGRAM-2: the row's OWN DATE reaches the builder, so a dated fact -- the
    # rower moving on 10/04 -- resolves to where the equipment will be on that
    # day rather than where it is while the seeder happens to run.
    blocks, rpe, zone, est = _build(session_type, week_num, wk0=wk0, location=location,
                                    location_key=spec.get("location_key", "office"),
                                    on=spec.get("plan_date"), gate=spec.get("gate"))
    blocks = copy.deepcopy(blocks)
    extra = suggested_extra(session_type, spec.get("pos"))
    if extra:
        # Display only: a NAME the card and the wake post show as one line. It
        # seeds no row and changes nothing about what "extras" means.
        blocks["suggested_extra"] = extra
    # CYCLE-1: every row carries where it happens and the day type it came from.
    blocks["location"] = location
    # LOCATION-1: …and what a load means there. Two consumers must agree —
    # gym-display's stepper and the box's lighter_load() — so it travels on
    # the row rather than being a table shipped to the client.
    key = spec.get("location_key", "office")
    blocks["location_key"] = key                       # _build sets it too; belt and braces
    cfg = _load_config.for_location(key)
    if cfg:
        blocks["load_config"] = copy.deepcopy(cfg)
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


def build_rows(repeats: list[date] | None = None) -> list[dict]:
    return [build_row(s) for s in build_schedule(repeats)]


# ============================================================================
# Validation
# ============================================================================

# ── TIME-CAP (Ryan, 2026-09-19): 45 min is a target, not a limit ────────────
# 45-59 min is fine and gets a note (reseed diff + log); never auto-cut. 60+
# is rejected — except the program slots in CALIBRATION_PENDING, whose old
# estimate (10 + sets x exercises x 2.5 min) is 60+ while those weeks stay as
# planned.
#
# LIFT-RECOMP (2026-09-29) EMPTIED MOST OF THIS SET RATHER THAN RAISING THE CAP.
# Supersets are what brought Strength B from 62 to 50 and Strength C from 67 to
# 54, so weeks 5 and 6 now fit the office window on their own and the exemption
# they needed is gone — the hard reject applies to them again. What remains is
# weeks 3 and 4, which deliberately keep the OLD prescription (they are already
# in front of him) and so keep the old 62-minute estimate.
#
# An exemption that outlives its cause is a cap that has quietly stopped
# applying, so these come out as soon as the rows they cover do.
TARGET_MIN = 45
HARD_MAX_MIN = 60
CALIBRATION_PENDING = {("strength_b", 3), ("strength_b", 4)}


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


def validate_rows(rows: list[dict], end: date | None = None) -> list[str]:
    """Structural asserts so a bad edit fails loudly. Returns the TIME-CAP
    notes (45-59 min, and the CALIBRATION_PENDING 60+ rows); a 60+ row
    outside CALIBRATION_PENDING fails the assert.

    `end` is the block's last day — program_end() unless the caller is
    validating a proposed repeat. Weekly counts are per CALENDAR week: a
    repeated week has the same week_num twice (REPEAT-WEEK)."""
    end = program_end() if end is None else end
    keys = [(r["plan_date"], r.get("slot", "morning")) for r in rows]
    assert len(keys) == len(set(keys)), "duplicate (plan_date, slot)"
    mornings = sorted(r["plan_date"] for r in rows if r.get("slot", "morning") == "morning")
    expected = [WEEK2_START + timedelta(days=i) for i in range((end - WEEK2_START).days + 1)]
    assert mornings == expected, f"every day {WEEK2_START}..{end} needs a MORNING row"
    for r in rows:
        b = r["blocks"]
        assert r["session_type"] in LEGAL_SESSION_TYPES, r["session_type"]
        assert 1 <= r["week_num"] <= 7, r["week_num"]
        assert b.get("display_name"), "blocks must carry a display_name"
        # PROGRAM-2 added "intervals". A block type the validator does not know
        # is a hard failure on purpose, which is how this caught the new one.
        assert b["type"] in ("circuit", "steady", "mobility", "recovery_flow", "rest",
                             "intervals"), b["type"]
        assert not forbidden_hits(b), f"{r['plan_date']}: retired equipment {forbidden_hits(b)}"
        # SCHEDULE-2: a strength session only ever lands on an office day.
        #
        # POSITIONAL FRAME (2026-09-26). `session_for()` picks the session from
        # the positional `DAY_TYPES` table, so WHICH session a date gets is fixed
        # program shape; an override relocates the day, it does not re-choose the
        # session. Asserting against the override-resolved `day_type` compared
        # two different frames and rejected the leave week outright — 9/29 is a
        # `wi` day carrying the positionally-correct strength_a.
        if r["session_type"].startswith("strength"):
            base_type = _cycle.DAY_TYPES[cycle_pos(r["plan_date"])]
            assert base_type == "msp_work", \
                f"{r['plan_date']}: {r['session_type']} on a positional {base_type} day"
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
    lifts = Counter(calendar_week_start(r["plan_date"]) for r in rows
                    if r["session_type"].startswith("strength"))
    for wk in sorted({calendar_week_start(r["plan_date"]) for r in rows}):
        assert lifts[wk] == 3, f"week of {wk} has {lifts[wk]} lifts, expected 3"
    # EVENING-1: evenings are yoga, four a week, and never on the road.
    evenings = [r for r in rows if r.get("slot") == "evening"]
    for r in evenings:
        assert r["session_type"] == EVENING_SESSION, \
            f"{r['plan_date']}: evening is {r['session_type']}, not {EVENING_SESSION}"
        assert evening_is_possible(r["plan_date"]), \
            f"{r['plan_date']}: an evening session cannot be placed in a transit segment"
        assert r["blocks"].get("location") == day_location(r["plan_date"], "evening"), \
            f"{r['plan_date']}: evening location {r['blocks'].get('location')!r}"
    ev_per_week = Counter(calendar_week_start(r["plan_date"]) for r in evenings)
    first_wk, last_wk = calendar_week_start(WEEK2_START), calendar_week_start(end)
    full_weeks = {calendar_week_start(r["plan_date"]) for r in rows
                  if first_wk < calendar_week_start(r["plan_date"]) < last_wk}
    for wk in sorted(full_weeks):
        assert ev_per_week[wk] == 4, f"week of {wk} has {ev_per_week[wk]} evenings, expected 4"

    for r in rows:
        assert r["blocks"].get("day_type") == day_type(r["plan_date"])
        assert r["session_type"] != "walk", \
            f"{r['plan_date']}: walks are activity, never planned sessions"
        # CYCLE-1: no session is ever placed in a transit segment. Resolved WITH
        # overrides, because nothing constrains an override's `location` to a
        # non-transit one — the table's CHECK covers `day_type` only. On the base
        # pattern this can still only fire if DAY_LOCATIONS gains a transit entry.
        assert not _cycle.is_transit(_cycle.anchor_location(r["plan_date"])), \
            f"{r['plan_date']}: a session cannot be placed on the road"
        want = day_location(r["plan_date"], r.get("slot", "morning"))
        got = r["blocks"].get("location")
        assert got == want, f"{r['plan_date']}: location {got!r}, cycle says {want!r}"
    rejects, notes = duration_findings(rows)
    assert not rejects, "est_duration_min >= 60: " + "; ".join(rejects)
    for n in notes:
        logger.warning("TIME-CAP: %s", n)

    # LOCATION-1 findings. These are REPORTED, not asserted: a strength session
    # at a location with no substitution table is exactly the state Ryan chose
    # to keep during the leave ("knowingly wrong beats silently rebuilt"), and a
    # validator that crashed on it would make a reseed impossible — which is how
    # the seeder came to ignore overrides in the first place. Silence is the one
    # thing that is not allowed.
    for r in rows:
        b, key = r["blocks"], r["blocks"].get("location_key", "office")
        if r["session_type"].startswith("strength") and not can_hold(key, r["session_type"]):
            n = (f"{r['plan_date']}: {r['session_type']} is at {key}, which has no "
                 f"substitution table — the row is knowingly wrong")
            notes.append(n)
            logger.warning("LOCATION-1: %s", n)
        if b.get("prep_unknown"):
            n = (f"{r['plan_date']}: {r['session_type']} at {key} has NO configured "
                 f"warmup or cooldown — the row says so rather than guessing")
            notes.append(n)
            logger.warning("LOCATION-1: %s", n)
        # Belt and braces: after 2026-09-26 the office strings can only reach a
        # non-office row through a hand-edit or a stale row, so if one shows up it
        # is a real defect and not an unconfigured location.
        if key != "office" and (b.get("warmup") == WARMUP or b.get("cooldown") == COOLDOWN):
            n = (f"{r['plan_date']}: {r['session_type']} at {key} carries the OFFICE "
                 f"warmup/cooldown ({WARMUP!r} / {COOLDOWN!r}) — neither exists there")
            notes.append(n)
            logger.warning("LOCATION-1: %s", n)
    return notes


# ============================================================================
# Writer — caller owns the transaction
# ============================================================================

def upsert_params(row: dict) -> tuple:
    """The params for `_UPSERT_SQL`, in its column order.

    This used to be written out at each call site -- here, in the reseed script,
    and a third time when the interval gate started rewriting a resolved day.
    The SQL and its params are one fact, and three copies of a positional
    11-tuple is three chances to put `target_rpe` where `target_hr_zone` goes.

    `json.dumps` is deliberately called WITHOUT `default=str`: every block this
    builder produces is plain-JSON-serialisable (asserted by
    tests/test_interval_gate.py), so a value that is not is a bug, and a date
    silently becoming "2026-10-04" in a card field is a wrong value that writes
    cleanly. Better to raise.
    """
    return (row["plan_date"], row.get("slot", "morning"), row["phase"], row["week_num"],
            row["session_type"], json.dumps(row["blocks"]), row["target_rpe"],
            row["target_hr_zone"], row["est_duration_min"], row["generated_by"],
            row["notes"])


_UPSERT_SQL = """
INSERT INTO health.plan
    (plan_date, slot, phase, week_num, session_type, blocks,
     target_rpe, target_hr_zone, est_duration_min, generated_by, notes)
VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s)
-- (plan_date, slot) since migration 042. It was ON CONFLICT (plan_date), which
-- matches no constraint after 042 — every full reseed would have raised.
ON CONFLICT (plan_date, slot) DO UPDATE SET
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


def program_state(repeats: list[date] | None = None) -> dict:
    reps = repeat_starts() if repeats is None else repeats
    weeks = max(RAMP)
    return {"name": "Foundation", "phase": PHASE, "anchor": WEEK1_START.isoformat(),
            "weeks_total": weeks, "deload_week": DELOAD_WEEK,
            "end": program_end(reps).isoformat(),
            "repeated_weeks": [r.isoformat() for r in reps]}


def write_program_state(cur, repeats: list[date] | None = None) -> None:
    cur.execute(
        "INSERT INTO acos.system_state (key, value, updated_at) VALUES (%s, %s, now()) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
        (PROGRAM_STATE_KEY, json.dumps(program_state(repeats))))


def write_rows(cur, rows: list[dict], validate: list[dict] | None = None) -> int:
    """UPSERT every office row and audit it through the same cursor. Does NOT
    commit.

    `validate` is the row set to check — pass the WHOLE program when `rows` is
    a targeted subset (reseed --only), since validate_rows asserts full-window
    coverage and the per-week lift counts.
    """
    duration_notes = validate_rows(validate if validate is not None else rows)
    for r in rows:
        cur.execute(_UPSERT_SQL, upsert_params(r))
    cur.execute(
        "INSERT INTO acos.audit_log (agent, persona, action, domain, confidence, "
        "outcome, token_count, api_cost_usd, metadata) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)",
        ("health_office", None, "office_plan_reseed", "health", None, "executed", 0, 0.0,
         json.dumps({"from": OFFICE_START.isoformat(), "to": program_end().isoformat(),
                     "rows": len(rows), "duration_notes": duration_notes})),
    )
    write_program_state(cur)
    return len(rows)
