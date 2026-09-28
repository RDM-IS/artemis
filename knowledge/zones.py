"""PROGRAM-2 — heart-rate zones. ONE definition, shared by every consumer.

These are ESTIMATES and the module says so in the data, not only in a comment:
every row carries `source`, so a screen showing a range can say where it came
from. ZONE-1 will replace the estimate with measured values, and when it does it
replaces THIS module's numbers — not a second copy somewhere else.

The estimate: **Tanaka** (208 − 0.7 × age) at age 49 → HRmax 174.
  * Z2 = 60–70 % of HRmax → 105–122 bpm
  * Z4 = 80–90 % of HRmax → 139–157 bpm

Why Tanaka rather than 220 − age: 220 − age overestimates HRmax for adults over
about 40, and an overestimated max makes every derived zone too high — which for
a Zone 2 session means training above the intensity the session exists for.
"""

from __future__ import annotations

#: The estimate every range below is derived from.
AGE = 49
HR_MAX = 174
SOURCE = "estimate, Tanaka HRmax 174, age 49; ZONE-1 replaces"

#: bpm ranges, inclusive. Keyed by the zone name a session card shows.
ZONES: dict[str, tuple[int, int]] = {
    "Z2": (105, 122),
    "Z4": (139, 157),
}


def zone_range(zone: str) -> tuple[int, int] | None:
    """(low, high) bpm, or None for a zone with no recorded range.

    None is the explicit unknown: a caller renders "not set" rather than a
    guessed range, for the same reason an absent warmup renders as unknown.
    """
    return ZONES.get(zone)


def zone_block(zone: str) -> dict | None:
    """What a plan row carries, so the iPad and the box read one answer.

    Shape::

        {"zone": "Z2", "low_bpm": 105, "high_bpm": 122, "source": "estimate, …"}
    """
    rng = zone_range(zone)
    if rng is None:
        return None
    return {"zone": zone, "low_bpm": rng[0], "high_bpm": rng[1], "source": SOURCE}


def describe(zone: str) -> str:
    """One human line for a card or a post."""
    rng = zone_range(zone)
    if rng is None:
        return f"{zone} — no range recorded"
    return f"{zone} {rng[0]}–{rng[1]} bpm"
