"""ROW-SCHEDULE — every Minneapolis morning is a row (Ryan, 2026-10-19 21:54).

"Let's just focus on rowing every MPLS day, some row only, some floor work."

Every MSP morning of the 14-day cycle is a rowing session at MSP HOME, where
the rower is: a ROW LADDER (rower only) or a ROW CIRCUIT (rows with push-ups and
core between). Wisconsin days are untouched, and so are the evenings.

    cycle pos   day          where        session
    0           Sun  wk1     MSP home     ladder
    1           Mon  wk1     work day     circuit
    2           Tue  wk1     work day     ladder
    3           Wed  wk1     work day     circuit
    4           Thu  wk1     work day     ladder      (drive to WI after work)
    5-8         Fri-Mon      Wisconsin    unchanged
    9           Tue  wk2     work day     ladder      (first day back: no floor work)
    10          Wed  wk2     work day     circuit
    11          Thu  wk2     work day     ladder
    12          Fri  wk2     work day     circuit
    13          Sat  wk2     MSP home     ladder

**Ladders on Tue and Thu in both weeks**, so the pattern is easy to keep in
your head, and **no two circuit days in a row** — the floor work's chest and
core get a day between. Four circuits and six ladders a cycle.

**It replaces the A/B/C strength days on MSP mornings.** The program redesign
(2026-10-02) already collapsed A/B/C; this takes Ryan's narrower instruction to
row for now. Strength comes back as its own decision.

**The room is MSP HOME on a work day too.** A work day's morning anchor is the
office gym, but the rower is at home: the session happens at 05:00 before he
leaves, so the row carries `msp_home`, which also stops the wake post adding the
office-gym departure checklist to a morning he trains at home.

**Override-aware.** A day an override moves out of Minneapolis (Wisconsin, a
trip) gets no row from this table: the cycle position says what a NORMAL day
is, and an override says this day is not normal.

The planned rows are `cardio_z2` with `blocks.row_kind` naming the screen, so
no CHECK constraint changes (not an ENUM-EXPAND). The cost is that reports
reading only `session_type` see a circuit day as cardio — which it mostly is.
"""

from __future__ import annotations

import copy
from datetime import date

#: The first morning this applies to. Earlier rows are history or already in
#: front of him; nothing before this date is rebuilt.
ROW_FROM = date(2026, 10, 20)

LOCATION_KEY = "msp_home"
SESSION_TYPE = "cardio_z2"

KINDS = ("ladder", "circuit")

ROW_BY_POS: dict[int, str] = {
    0: "ladder", 1: "circuit", 2: "ladder", 3: "circuit", 4: "ladder",
    9: "ladder", 10: "circuit", 11: "ladder", 12: "circuit", 13: "ladder",
}

#: Locations that mean "in Minneapolis this morning". Anything else -- a
#: Wisconsin override, a trip -- gets no row from the table.
MSP_LOCATIONS = frozenset({"office", "msp_home"})

DISPLAY = {"ladder": "Row ladder", "circuit": "Row circuit"}

#: Planned minutes: what the default rounds come to with the rest minutes and
#: the cool-down (ladder 6 x (4+1) + 3; circuit 4 x ~8 + 3). The real length is
#: chosen on the start screen; this is what the week view and the wake post say.
EST_MIN = {"ladder": 35, "circuit": 40}

NOTES = {
    "ladder": ["Row ladder — rounds and +1 stroke/min or +1:00 are chosen on the start "
               "screen, from your last ladder.",
               "One record card at the end."],
    "circuit": ["Row circuit — row, then push-ups and two core moves, each round. "
                "Rounds and progression are chosen on the start screen.",
                "One record card per round."],
}


def kind_for(pos: int, d: date, location_key: str) -> str | None:
    """'ladder' | 'circuit' for this morning, or None to leave the day alone."""
    if d < ROW_FROM:
        return None
    if location_key not in MSP_LOCATIONS:
        return None
    return ROW_BY_POS.get(pos)


def place(spec: dict, locations: dict) -> dict:
    """A morning spec, turned into its row if the table names this day. Pure.

    ONE place both seeders call (block 1's `build_schedule` and block 2's
    `_specs_for`), so the two cannot disagree about which mornings are rows.
    `locations` is `cycle.DEFAULT_LOCATIONS`, for the display name.
    """
    kind = kind_for(spec["pos"], spec["plan_date"], spec["location_key"])
    if not kind:
        return spec
    return {**spec, "session_type": SESSION_TYPE, "row_kind": kind,
            "location_key": LOCATION_KEY,
            "location": (locations.get(LOCATION_KEY) or {}).get("display", LOCATION_KEY)}


def apply(blocks: dict, kind: str) -> dict:
    """Reshape a built rower Z2 row into the planned ladder or circuit. Pure.

    The Z2 shape underneath stays (zones, cardio, equipment) because ZONE-0 and
    the zone line read it; what changes is the NAME, the notes, and
    `row_kind`, which is what tells gym-display which screen to open.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown row kind {kind!r}")
    out = copy.deepcopy(blocks)
    modality = (out.get("cardio") or {}).get("modality")
    if modality != "row":
        # FAIL-CLOSED: a row day must resolve to the rower. If the inventory
        # says otherwise the row is wrong, and saying so beats a ladder screen
        # opening on a bike.
        raise ValueError(f"row schedule needs the rower at {out.get('location_key')}, "
                         f"inventory resolved {modality!r}")
    out["row_kind"] = kind
    out["display_name"] = DISPLAY[kind]
    out["intensity"] = "Ladder — stroke rate climbs each round"
    out["setup_notes"] = list(NOTES[kind])
    out.pop("duration_min", None)
    out.pop("target_range_min", None)
    out["duration_min"] = EST_MIN[kind]
    return out
