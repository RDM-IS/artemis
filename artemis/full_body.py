"""FULLBODY-1 — the one full-body strength session (approved 2026-11-01 10:42).

The 2026-10-02 redesign collapsed A/B/C into one session; at two lifts a week a
split would leave each lift 7-10 days stale. The rules it is built from:

  * multi-joint work at moderate load with heart rate held;
  * SKILL AND UNILATERAL WORK FIRST, while fresh; simpler, stable movements in the
    high-heart-rate rounds — a single-leg move in round four at 150 bpm is the
    injury;
  * cardio alternates with the lifting IN THE SAME ROOM;
  * progression is DENSITY — rounds, then rest, then the machine target — and
    only then load (`setup_notes` says so on the card).

Two expressions of the same patterns, tied to the room because the equipment
decides: **X** at the office (Wednesday, every week), **Y** at Richfield. TRX
exists only at Richfield. The office's Pulldown/Seated Row combo is broken
(Ryan, 2026-11-01), so X depends on no machine station: its block-2 pull is a
chest-supported DB row, and the single-arm cable row is the swap when the
functional trainer is free.

The session is ONE `circuit` blocks object with `segments` (gym-display runs
each segment as its own circuit body, in order) and every exercise tagged with
its `segment`. The exercises stay in ONE flat `exercises` list on purpose: the
check-in and pain rules edit `blocks.exercises` in place, and a second copy of
the list inside each segment would let the two disagree.
"""
from __future__ import annotations

from datetime import date

#: Rooms that hold this session. Anything else falls back (week_template).
ROOMS = ("office", "richfield")

#: The first planned full-body session runs block 2 at three rounds, RPE 6
#: (Ryan, 2026-11-01); every later one at four. A date, not "the first one in
#: the build", so a reseed after the first is logged cannot hand the next
#: session the easy version again.
FIRST_SESSION_ON_OR_BEFORE = date(2026, 11, 4)

SEGMENTS = (
    {"title": "Fresh — unilateral", "rounds": 3, "rest_between_rounds_sec": 60},
    {"title": "Density circuit", "rounds": 4, "rest_between_rounds_sec": 60},
    {"title": "Trunk", "rounds": 2, "rest_between_rounds_sec": 30},
)

#: Rest after each move INSIDE a segment: a walk to the next station, not a rest.
TRANSITION_SEC = {0: 15, 1: 15, 2: 10}

#: The machine interval in block 2 (approved: 3 min — heart rate needs 60-90 s
#: to reach Z3, and time in zone is the progression).
INTERVAL_SEC = 180

# (name, segment, format, target, per_side, note)
#   format "reps": target = reps; "duration": target = seconds
X_OFFICE = (
    ("DB split squat", 0, "reps", 8, True, None),
    ("1-arm DB row", 0, "reps", 10, True, None),
    ("DB goblet squat", 1, "reps", 12, False, None),
    ("DB bench press", 1, "reps", 10, False, "Movable bench — set it by the bike"),
    ("Chest-supported DB row", 1, "reps", 12, False,
     "Incline bench. Swap: single-arm cable row if the functional trainer is free"),
    ("DB Romanian deadlift", 1, "reps", 10, False, None),
    ("Upright bike — Z3", 1, "duration", INTERVAL_SEC, False,
     "Z3 — heart rate from the watch"),
    ("Cable Pallof press", 2, "reps", 10, True, "Floor full? Do this, then an incline plank on a bench"),
    ("Dead bug", 2, "reps", 10, True, None),
    ("Side plank", 2, "duration", 60, False, "30 s each side"),
)

Y_RICHFIELD = (
    ("DB step-up", 0, "reps", 8, True, "Flat bench"),
    ("TRX single-arm row", 0, "reps", 10, True, None),
    ("DB front squat", 1, "reps", 12, False, None),
    ("Push-up", 1, "reps", 12, False, "10-15"),
    ("Band seated row", 1, "reps", 15, False, None),
    ("DB Romanian deadlift", 1, "reps", 10, False, None),
    ("Bike — Z3", 1, "duration", INTERVAL_SEC, False, "Z3 on the trainer — heart rate from the watch"),
    ("Band Pallof press", 2, "reps", 10, True, None),
    ("Bird dog", 2, "reps", 10, True, None),
    ("Plank", 2, "duration", 40, False, None),
)

VARIANTS = {"office": ("X", X_OFFICE), "richfield": ("Y", Y_RICHFIELD)}

WARMUP = {
    "office": "8 min easy on the upright bike + hip / T-spine mobility",
    "richfield": "8 min easy on the bike + hip / T-spine mobility",
}
COOLDOWN = "5 min easy + stretch"

EQUIPMENT = {
    "office": ["DBs", "flat bench", "adjustable bench", "functional trainer",
               "upright bike", "mat"],
    "richfield": ["PowerBlocks", "flat bench", "TRX", "bands", "bike on trainer", "mat"],
}


def block2_rounds(on: date | None) -> int:
    return 3 if on is not None and on <= FIRST_SESSION_ON_OR_BEFORE else SEGMENTS[1]["rounds"]


def estimate_min(rounds2: int) -> int:
    """Warm-up 8 + block 1 14 + block 2 6.5/round + trunk 7 + cool-down 5."""
    return 8 + 14 + round(6.5 * rounds2) + 7 + 5


def _exercise(name, seg, fmt, target, per_side, note, rounds, class_for) -> dict:
    ex = {"name": name, "format": fmt, "segment": seg,
          "rest_after_sec": TRANSITION_SEC[seg], "equipment_class": class_for(name)}
    words = []
    if fmt == "reps":
        ex["target_reps"] = target
        words.append(f"{rounds}×{target}" + (" each side" if per_side else ""))
    else:
        ex["duration_sec"] = target
        words.append(f"{rounds}×{target // 60} min" if target >= 60 and target % 60 == 0
                     else f"{rounds}×{target}s")
    if note:
        words.append(note)
    ex["notes"] = "; ".join(words)
    return ex


def build(location_key: str, location: str, *, on: date | None = None, class_for=None):
    """(blocks, rpe, zone, est_min) for the room. Raises where the session can't
    run — FAIL-CLOSED: the caller (week_template) decides the fallback, this
    never quietly builds another room's version."""
    if location_key not in VARIANTS:
        raise ValueError(f"strength_full has no version for {location_key!r}")
    if class_for is None:
        from artemis.health_office import class_for as _cf
        class_for = _cf
    letter, table = VARIANTS[location_key]
    rounds2 = block2_rounds(on)
    segments = [dict(s) for s in SEGMENTS]
    segments[1]["rounds"] = rounds2
    exercises = [_exercise(*spec, segments[spec[1]]["rounds"], class_for) for spec in table]
    first = rounds2 < SEGMENTS[1]["rounds"]
    setup = [
        f"Full Body {letter} — skill first, the hard rounds last.",
        ("First session: block 2 at 3 rounds, RPE 6 — learn the movements."
         if first else "Block 2: 4 rounds."),
        "Progression, in order: +1 block-2 round → shorter rests → bike into Z3/Z4 → "
        "only then more load.",
    ]
    blocks = {
        "type": "circuit",
        "display_name": "Full Body",
        "variant": letter,
        "location": location,
        "rounds": max(s["rounds"] for s in segments),
        "rest_between_rounds_sec": segments[1]["rest_between_rounds_sec"],
        "segments": segments,
        "exercises": exercises,
        "warmup": WARMUP[location_key],
        "cooldown": COOLDOWN,
        "equipment": list(EQUIPMENT[location_key]),
        "setup_notes": setup,
    }
    return blocks, (6.0 if first else 7.0), 3, estimate_min(rounds2)


def all_names() -> set[str]:
    return {spec[0] for _, table in VARIANTS.values() for spec in table}
