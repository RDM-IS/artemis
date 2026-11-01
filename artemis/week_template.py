"""WEEK-TEMPLATE — one 14-day cycle: the plan names the session, the room names
the machine (approved by Ryan 2026-11-01 10:42; supersedes ROW-SCHEDULE's
"every MSP morning is a row" from TEMPLATE_FROM).

    pos  wk 1                         pos  wk 2
    0    Sun  rest (optional ladder)   7    Sun  rest
    1    Mon  circuit   MSP · work     8    Mon  STRENGTH  Richfield, before the drive
    2    Tue  ladder    MSP · work     9    Tue  ladder    MSP · work
    3    Wed  STRENGTH  office         10   Wed  STRENGTH  office
    4    Thu  ladder    MSP · work     11   Thu  ladder    MSP · work
    5    Fri  STRENGTH  Richfield      12   Fri  circuit   MSP · work
    6    Sat  ladder    Richfield/BD   13   Sat  ladder    MSP home

Rules the table encodes: strength twice a week, 48 h+ apart (except the wk2-Wed
→ wk1-Wed gap); NO FLOOR WORK THE DAY AFTER STRENGTH; never two circuits in a
row; every Sunday rest — week 1's is an optional ladder from Start (taken is a
bonus, skipped is not a miss).

**Resolution by room.** Where he is comes from the day's spec (the cycle plus
overrides), and the room decides the machine:

    ladder / circuit  in Minneapolis (office or home) -> the rower at HOME
                      (ROW-SCHEDULE's ladder/circuit screens);
                      Richfield -> Zone 2 on the bike (gym-display opens the
                      RIDE ladder); Brown Deer -> the treadmill (RUN ladder).
                      A circuit outside Minneapolis runs as that room's ladder:
                      ride + floor and run + floor are not built.
    strength          strength_full where full_body can run it; a Minneapolis
                      morning resolves to the OFFICE (same city, and it holds
                      it — Ryan confirmed access on off days); anywhere else
                      (Brown Deer, home with no office) falls back to that
                      room's ladder, and RECOMMEND's existing "no lift in 48 h"
                      rule highlights strength at the next capable room.

**Overrides still win**, exactly as they won over ROW-SCHEDULE: a placement or
an away stay is a decision about that day, so `block2._specs_for` only asks
this module when neither applies.
"""
from __future__ import annotations

from datetime import date

#: The first morning this governs. Earlier rows are history or already lived;
#: the reseed never rewrites a logged day either way.
TEMPLATE_FROM = date(2026, 11, 2)

REST, LADDER, CIRCUIT, STRENGTH = "rest", "ladder", "circuit", "strength"

TABLE: dict[int, str] = {
    0: REST, 1: CIRCUIT, 2: LADDER, 3: STRENGTH, 4: LADDER, 5: STRENGTH, 6: LADDER,
    7: REST, 8: STRENGTH, 9: LADDER, 10: STRENGTH, 11: LADDER, 12: CIRCUIT, 13: LADDER,
}

#: "In Minneapolis this morning" — the rower is at home, the lifts at the office.
MSP = frozenset({"office", "msp_home"})
ROWER_ROOM = "msp_home"
STRENGTH_ROOM_IN_MSP = "office"

#: Rooms whose Zone 2 is a ladder gym-display can run (bike -> ride,
#: treadmill -> run). Anything else gets planned rest: nothing missable in a
#: room the template doesn't know.
LADDER_ROOMS = frozenset({"richfield", "brown_deer"})

STRENGTH_TYPE = "strength_full"


def applies(d: date) -> bool:
    return d >= TEMPLATE_FROM


def kind_for(pos: int) -> str:
    return TABLE[pos]


def _display(key: str, locations: dict) -> str:
    return (locations.get(key) or {}).get("display", key)


def place(spec: dict, locations: dict) -> dict:
    """The morning spec for this day under the template. Pure.

    `spec` is block 2's base morning spec (plan_date, pos, location_key, …);
    `locations` is `cycle.DEFAULT_LOCATIONS`, for display names. Returns a new
    dict; never mutates the input.
    """
    from artemis import full_body

    kind = kind_for(spec["pos"])
    key = spec["location_key"]
    out = {**spec}
    out.pop("row_kind", None)

    if kind == STRENGTH:
        room = STRENGTH_ROOM_IN_MSP if key in MSP else key
        if room in full_body.ROOMS:
            return {**out, "session_type": STRENGTH_TYPE, "location_key": room,
                    "location": _display(room, locations)}
        kind = LADDER                     # can't lift here: that room's ladder

    if kind == REST:
        return {**out, "session_type": "rest"}

    # ladder / circuit
    if key in MSP:
        return {**out, "session_type": "cardio_z2", "row_kind": kind,
                "location_key": ROWER_ROOM, "location": _display(ROWER_ROOM, locations)}
    if key in LADDER_ROOMS:
        # No row_kind: a bike / treadmill Zone 2 row opens the ride / run ladder.
        return {**out, "session_type": "cardio_z2"}
    return {**out, "session_type": "rest"}
