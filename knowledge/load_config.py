"""LOCATION-1 — what a load looks like at each location.

The config TRAVELS ON THE PLAN ROW (`blocks.load_config`), because it has two
consumers that must agree: gym-display's stepper and the box's own
`health_regions.lighter_load()`, which the pain-2 rule uses to take 80 % of the
last load and round DOWN to something the gym can actually make. A config that
lived only on the client would leave the box computing an office-shaped load
for a PowerBlock.

Shape, per equipment class:
    {"mode": "numeric", "step": 5, "min": 5, "max": 45}
    {"mode": "numeric", "step": 10, "min": 0, "max": 250, "bar": 0,
     "plates": [45, 35, 25, 10, 5]}          # plate-loaded: reachable totals
    {"mode": "none"}                          # bodyweight / bands / trx / cardio

A class absent from a location's config is not available there.
"""

from __future__ import annotations

#: Classes that never carry a numeric load.
NO_LOAD_CLASSES = ("bodyweight", "bands", "trx", "cardio")

_NONE = {"mode": "none"}

#: The office (all Precor). These are today's client-side constants, moved.
OFFICE: dict[str, dict] = {
    "dumbbell": {"mode": "numeric", "step": 5, "min": 5, "max": 45},
    # TODO(office): confirm each Precor pin-stack increment in person.
    "machine": {"mode": "numeric", "step": 10, "min": 0, "max": 300},
    "cable": {"mode": "numeric", "step": 10, "min": 0, "max": 200},
    # TODO(office): the Icarian Smith may be counterbalanced; until measured
    # the bar counts as 0 and loads are plates only.
    "smith": {"mode": "numeric", "step": 10, "min": 0, "max": 240, "bar": 0,
              "plates": [45, 35, 25, 10, 5]},
    "barbell": {"mode": "numeric", "step": 10, "min": 45, "max": 285, "bar": 45,
                "plates": [45, 35, 25, 10, 5]},
    "bodyweight": _NONE,
    "cardio": _NONE,
}

#: Richfield — the farm. PowerBlocks, a curl bar with 70 lb of plates, a flat
#: bench, TRX, bands on a wall mount, a stability ball, a rower and a bike on a
#: trainer. 6.5 ft ceiling: no STANDING overhead work.
#:
#: TODO(richfield) — Ryan is there Friday 2026-09-25 and will confirm:
#:   * the PowerBlock increment (5 lb assumed; they may go in 2.5s), and
#:   * whether a SEATED overhead press clears the 6.5 ft ceiling.
#: Until then the dumbbell step is the conservative 5 and the seated press
#: stays in the session as written.
RICHFIELD: dict[str, dict] = {
    "dumbbell": {"mode": "numeric", "step": 5, "min": 5, "max": 80,
                 "note": "PowerBlocks — TODO(richfield) confirm the increment"},
    "barbell": {"mode": "numeric", "step": 10, "min": 0, "max": 70, "bar": 0,
                "plates": [10, 10, 5, 5, 2.5],
                "note": "curl bar + 70 lb of plates — TODO(richfield) confirm the set"},
    "bands": _NONE,
    "trx": _NONE,
    "bodyweight": _NONE,
    "cardio": _NONE,
}

BY_LOCATION: dict[str, dict] = {
    "office": OFFICE,
    "richfield": RICHFIELD,
    # brown_deer and msp_home have NO recorded inventory (Ryan owes both), so
    # they get no config: nothing may be seeded there until they do.
}


def for_location(key: str | None) -> dict | None:
    """The load config for a location key, or None when its inventory is
    unknown. A row seeded with no config falls back to the office's, which is
    what every row written before LOCATION-1 means."""
    return BY_LOCATION.get(key or "")


def classes_at(key: str | None) -> tuple[str, ...]:
    """Which equipment classes exist at a location."""
    return tuple((for_location(key) or {}).keys())


def is_no_load(cls: str | None) -> bool:
    return (cls or "") in NO_LOAD_CLASSES
