"""SESSION-OPEN step 3a — `strength_bw`, the bodyweight full-body session
(content approved by Ryan 2026-11-09).

Bodyweight is strength (SESSION-OPEN decision 3), so a strength day exists in
every room. Built in FULLBODY-1's shape — one `circuit` blocks object with
`segments`, every exercise tagged with its segment, ONE flat `exercises` list
(the check-in and pain rules edit `blocks.exercises` in place) — and by its
rules: unilateral/skill work first while fresh, stable movements in the
high-heart-rate rounds, progression by DENSITY and never by load.

Two choices depend on the room, and both resolve FAIL-CLOSED (Ryan, 2026-11-09:
"a room whose inventory can't be read raises; it never picks the fallback
silently"):

  * the block-2 ROW — TRX row where the room has TRX, band row where it has
    bands, else prone Y-T-W. Read from `knowledge.load_config` (the room's
    equipment classes). A room with NO recorded inventory (`for_location()` is
    None — e.g. msp_home, which nobody has confirmed) raises: "we don't know
    whether there is a strap" is not "there is no strap".
  * the 3-min block-2 CARDIO and the warm-up — the room's machine via
    `knowledge.cardio.resolve` on the session's date (the rower moves), or high
    knees / jacks where the room explicitly has no machine. A room absent from
    the cardio inventory raises; an explicitly empty one is a real answer.

Every exercise carries an equipment class (bodyweight, trx, bands, cardio — all
no-load, so the screens show no load fields) and pain-ladder regions
(health_regions); `all_names()` lets the tests prove both for every name any
room can produce.
"""
from __future__ import annotations

from datetime import date

SESSION_TYPE = "strength_bw"
DISPLAY_NAME = "Bodyweight"

#: Same shape as FULLBODY-1: block 1 (3 rounds, 60 s between), block 2 (4
#: rounds; the walk between moves is the only pause), block 3 (2 rounds).
SEGMENTS = (
    {"title": "Fresh — unilateral", "rounds": 3, "rest_between_rounds_sec": 60},
    {"title": "Density circuit", "rounds": 4, "rest_between_rounds_sec": 60},
    {"title": "Trunk", "rounds": 2, "rest_between_rounds_sec": 30},
)
TRANSITION_SEC = {0: 15, 1: 15, 2: 10}
INTERVAL_SEC = 180          # 3 min at Z3, as FULLBODY-1
RPE = 7.0
ZONE = 3

# The fixed movements: (name, segment, format, target, per_side, note)
BLOCK1 = (
    ("Reverse lunge", 0, "reps", 8, True, None),
    ("Single-leg glute bridge", 0, "reps", 10, True, None),
    ("Hand-supported single-leg RDL", 0, "reps", 8, True,
     "Hand on a wall or bench back for balance"),
)
SQUAT = ("Tempo squat (3-1-1)", 1, "reps", 15, False, "3 s down, 1 s pause, 1 s up")
PUSH = ("Push-up", 1, "reps", 12, False, "10-15; incline (hands on a bench or wall) is fine")
GOOD_MORNING = ("Good morning", 1, "reps", 15, False, "Hands behind the head, soft knees, hinge")
BLOCK3 = (
    ("Dead bug", 2, "reps", 10, True, None),
    ("Side plank", 2, "duration", 60, False, "30 s each side"),
    ("Bird dog", 2, "reps", 10, True, None),
)

#: The block-2 row, in preference order: (equipment class the room must have, spec).
ROW_CHOICES = (
    ("trx", ("TRX row", 1, "reps", 15, False, None)),
    ("bands", ("Band row", 1, "reps", 15, False, "Anchor at chest height")),
)
ROW_FALLBACK = ("Prone Y-T-W", 1, "reps", 8, False, "8 each of Y, T and W, face down")

#: The block-2 cardio name by resolved device; the warm-up line by the same.
CARDIO_BY_DEVICE = {
    "water": "Row — Z3",
    "indoor trainer": "Bike — Z3",
    "upright": "Upright bike — Z3",
    "recumbent": "Recumbent bike — Z3",
    "treadmill": "Treadmill — Z3",
    "elliptical": "Elliptical — Z3",
}
NO_MACHINE_CARDIO = "High knees / jacks — Z3"

COOLDOWN = "5 min easy + stretch"


class InventoryUnreadable(ValueError):
    """The room's inventory can't be read, so the session can't be resolved for
    it. Never answered with the fallback."""


def resolve_row(location_key: str) -> tuple:
    """The block-2 row spec for this room. Raises when the room's equipment
    inventory is unknown."""
    from knowledge import load_config
    cfg = load_config.for_location(location_key)
    if cfg is None:
        raise InventoryUnreadable(
            f"strength_bw: no recorded equipment inventory for {location_key!r} "
            "(knowledge/load_config.py) — can't tell TRX / bands / neither")
    for cls, spec in ROW_CHOICES:
        if cls in cfg:
            return spec
    return ROW_FALLBACK


def resolve_cardio(location_key: str, on: date | None) -> tuple[str, str]:
    """(block-2 cardio name, warm-up machine words) for this room on this date.
    Raises when the room is not in the cardio inventory at all."""
    from knowledge import cardio
    if location_key not in cardio.INVENTORY:
        raise InventoryUnreadable(
            f"strength_bw: {location_key!r} is not in the cardio inventory "
            "(knowledge/cardio.py) — can't tell machine from no machine")
    got = cardio.resolve(location_key, on)
    if not got.get("modality"):
        return NO_MACHINE_CARDIO, "marching / jacks"
    device = got.get("device")
    if device not in CARDIO_BY_DEVICE:
        raise InventoryUnreadable(f"strength_bw: no block-2 name for device {device!r}")
    return CARDIO_BY_DEVICE[device], f"easy on the {cardio.label_for(device)}"


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


def estimate_min(rounds2: int = SEGMENTS[1]["rounds"]) -> int:
    """Warm-up 8 + block 1 14 + block 2 6.5/round + trunk 7 + cool-down 5 = 60."""
    return 8 + 14 + round(6.5 * rounds2) + 7 + 5


def build(location_key: str, location: str, *, on: date | None = None, class_for=None):
    """(blocks, rpe, zone, est_min) for the room. Raises InventoryUnreadable where
    the room's inventory can't be read — the caller decides; this never builds
    another room's version or a guessed fallback."""
    if class_for is None:
        from artemis.health_office import class_for as _cf
        class_for = _cf
    row = resolve_row(location_key)
    cardio_name, warm_machine = resolve_cardio(location_key, on)
    interval = (cardio_name, 1, "duration", INTERVAL_SEC, False, "Z3 — heart rate from the watch")
    table = BLOCK1 + (SQUAT, PUSH, row, GOOD_MORNING, interval) + BLOCK3
    segments = [dict(s) for s in SEGMENTS]
    exercises = [_exercise(*spec, segments[spec[1]]["rounds"], class_for) for spec in table]
    equipment = ["mat"]
    if row[0] == "TRX row":
        equipment.append("TRX")
    elif row[0] == "Band row":
        equipment.append("bands")
    if cardio_name != NO_MACHINE_CARDIO:
        equipment.append(cardio_name.split(" — ")[0].lower())
    blocks = {
        "type": "circuit",
        "display_name": DISPLAY_NAME,
        "location": location,
        "location_key": location_key,
        "rounds": max(s["rounds"] for s in segments),
        "rest_between_rounds_sec": segments[1]["rest_between_rounds_sec"],
        "segments": segments,
        "exercises": exercises,
        "warmup": f"8 min: 5 min {warm_machine}, then hip / T-spine mobility",
        "cooldown": COOLDOWN,
        "equipment": equipment,
        "setup_notes": [
            "Bodyweight — skill first, the hard rounds last. No load, ever.",
            "Block 2: 4 rounds, no rest between moves.",
            "Progression, in order: +1 block-2 round (4 → 5) → shorter rests → "
            "machine target up → harder variation (incline → floor → decline push-up; "
            "reverse lunge → rear-foot-elevated split squat).",
        ],
    }
    return blocks, RPE, ZONE, estimate_min()


def all_names() -> set[str]:
    """Every exercise name any room can produce — for the class / regions tests."""
    names = {s[0] for s in BLOCK1 + (SQUAT, PUSH, GOOD_MORNING) + BLOCK3}
    names |= {spec[0] for _, spec in ROW_CHOICES} | {ROW_FALLBACK[0]}
    names |= set(CARDIO_BY_DEVICE.values()) | {NO_MACHINE_CARDIO}
    return names
