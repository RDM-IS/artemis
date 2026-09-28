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

#: Richfield — the farm. INVENTORY CONFIRMED by Ryan 2026-09-28 15:14: a road
#: bike on an indoor trainer, a yoga mat, TRX, 10–50 lb resistance bands with
#: handles and connectors, wall anchors every 6" from 6" off the floor to the
#: ceiling (two anchor boards side by side, one continuing across the joist, with
#: a mirrored board on the opposite wall), a flat bench, a stability ball, a curl
#: bar and PowerBlocks. 6.5 ft ceiling, so presses stay SEATED.
#:
#: PowerBlocks — CONFIRMED by Ryan 2026-09-28 17:02 from a photo. They are paired
#: to 90 lb and he keeps BOTH adders in and will not take them out, so the only
#: loads that exist here are 10, 20, 30, 40, 50, 60, 70, 80, 90. The old
#: TODO(richfield) guessed a 5 lb step, which would have offered him 15s, 25s and
#: 35s that the room cannot make; `lighter_load()` rounds DOWN inside this set.
RICHFIELD: dict[str, dict] = {
    "dumbbell": {"mode": "numeric", "step": 10, "min": 10, "max": 90,
                 "note": "PowerBlocks, paired to 90 lb with both adders in — "
                         "10 lb steps, nothing between"},
    # The curl bar carries 2×10 and 2×25, i.e. ONE 10 and ONE 25 per side, so the
    # reachable plate totals are exactly 0, 20, 50, 70.
    #
    # TODO(richfield) — the BAR'S OWN WEIGHT is still unmeasured. It is recorded
    # as 0 rather than guessed at 15 or 20, so every total here is PLATES ONLY and
    # the note says so; the stepper labels it "+ bar". A guessed bar weight would
    # be silently wrong on every set at this location, and wrong in the direction
    # of under-loading.
    "barbell": {"mode": "numeric", "step": 20, "min": 0, "max": 70, "bar": 0,
                "plates": [25, 10],
                "note": "curl bar: plates only, + bar (bar weight not yet measured)"},
    "bands": _NONE,
    "trx": _NONE,
    "bodyweight": _NONE,
    "cardio": _NONE,
}

#: Brown Deer — CONFIRMED 2026-09-26. A treadmill, a yoga mat, and nothing else:
#: no dumbbells, no bar, no bench, no machines, no cables, no anchor for bands or
#: a strap. 7 ft ceiling.
#:
#: PRESENT AND EMPTY OF NUMERIC LOAD, which is NOT the same as absent (Ryan,
#: 2026-09-26). An absent key means "we do not know what is there"; this entry
#: means "we know, and there is nothing to load". The loadable classes —
#: `dumbbell`, `barbell`, `machine`, `cable`, `smith` — are deliberately not
#: listed, because a class absent from a location's config is not available
#: there, and here that absence is a measured fact rather than a gap.
BROWN_DEER: dict[str, dict] = {
    "bodyweight": _NONE,
    "cardio": _NONE,          # the treadmill; see knowledge/cardio.py
}

BY_LOCATION: dict[str, dict] = {
    "office": OFFICE,
    "richfield": RICHFIELD,
    "brown_deer": BROWN_DEER,
    # msp_home has NO recorded inventory, so it gets no config and
    # for_location() returns None for it — "unknown", not "empty".
}

#: Room constraints that are not loads, kept OUT of BY_LOCATION so `classes_at()`
#: cannot mistake one for an equipment class.
#:
#: The ceiling decides whether overhead work is possible, and it belongs in the
#: inventory rather than in someone's memory (Ryan, 2026-09-26) — Richfield's
#: 6.5 ft lived only in a comment until now.
#:
#: `standing_overhead` is THREE-STATE on purpose: True, False, or None for "not
#: determined". None is never read as permission.
CONSTRAINTS: dict[str, dict] = {
    "office": {"ceiling_ft": None, "standing_overhead": True,
               "note": "commercial gym; height never measured because it has never mattered"},
    "richfield": {"ceiling_ft": 6.5, "standing_overhead": False,
                  "note": "no STANDING overhead work; a SEATED press clears 6.5 ft "
                          "and presses stay seated here (Ryan, 2026-09-28)"},
    "brown_deer": {"ceiling_ft": 7.0, "standing_overhead": None,
                   "note": "confirmed 2026-09-26. Undetermined rather than assumed: "
                           "7 ft is marginal and depends on reach. Moot in practice — "
                           "there is nothing here to press overhead."},
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


def has_numeric_load(key: str | None) -> bool:
    """Does this location have ANY class that carries a numeric load?

    False for a location we know is bodyweight-only (Brown Deer). Also False for
    one we know nothing about — so never use this alone to decide whether a
    session may be seeded; ask `for_location()` first, where None means unknown.
    """
    cfg = for_location(key) or {}
    return any(v.get("mode") == "numeric" for v in cfg.values())


def ceiling_ft(key: str | None) -> float | None:
    """Recorded ceiling height, or None when it was never measured."""
    return (CONSTRAINTS.get(key or "") or {}).get("ceiling_ft")


def standing_overhead(key: str | None) -> bool | None:
    """True / False / None, where None means NOT DETERMINED — never permission."""
    return (CONSTRAINTS.get(key or "") or {}).get("standing_overhead")
