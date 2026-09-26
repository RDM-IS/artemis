"""LOCATION-1 — the warmup and cooldown a session gets, PER LOCATION.

Same discipline as `knowledge/load_config.py`, and for the same reason. Until
2026-09-26 `health_office._strength()` wrote the office warmup and cooldown into
every strength row whatever the location, so the 10/02 session at Richfield told
Ryan to use an elliptical and a Stretch Trainer that are not in that room. A
guessed warmup is the same class of error as a guessed equipment class: it reads
as fact on the iPad.

A location with NO ENTRY here resolves to the EXPLICIT UNKNOWN STATE — the row
carries no `warmup`/`cooldown` at all and sets `prep_unknown`, so every consumer
renders "not configured" rather than another gym's equipment. Absent is never a
silent fall through to the office.

Shape, per location::

    {"warmup": "5 min elliptical, easy",
     "cooldown": "5 min Stretch Trainer",
     "cooldown_min": 5,
     "cooldown_equipment": "Stretch Trainer"}   # optional

`cooldown_min` feeds `est_duration_min`, so a location with no cooldown does not
get five minutes added for one it cannot do. `cooldown_equipment`, when present,
is appended to the row's equipment list.
"""

from __future__ import annotations

#: The office (all Precor). These were `health_office.WARMUP` / `COOLDOWN` /
#: `COOLDOWN_MIN`, which now read from here so there is ONE definition.
OFFICE: dict = {
    "warmup": "5 min elliptical, easy",
    "cooldown": "5 min Stretch Trainer",
    "cooldown_min": 5,
    "cooldown_equipment": "Stretch Trainer",
}

#: EDIT THIS when an inventory lands, not the code.
#:
#: TODO(inventory 2026-09-26) — richfield, brown_deer and msp_home have NO entry
#: on purpose. Ryan is walking those three rooms today. Until a warmup and a
#: cooldown are confirmed for each, every non-office row carries the explicit
#: unknown state. DO NOT invent one: "5 min easy on the rower" is a guess about
#: what is in the room and how much of it he wants before a lift.
BY_LOCATION: dict[str, dict] = {
    "office": OFFICE,
}


def for_location(key: str | None) -> dict | None:
    """The warmup/cooldown config for a location, or None when unknown.

    None is the EXPLICIT unknown state, never a reason to fall back to the
    office — that fallback is the bug this module exists to remove.
    """
    return BY_LOCATION.get(key or "")


def warmup_for(key: str | None) -> str | None:
    return (for_location(key) or {}).get("warmup")


def cooldown_for(key: str | None) -> str | None:
    return (for_location(key) or {}).get("cooldown")


def cooldown_min(key: str | None) -> int:
    """Minutes to add to the duration estimate. 0 when there is no cooldown —
    a location does not get credit for one it cannot do."""
    return int((for_location(key) or {}).get("cooldown_min") or 0)


def cooldown_equipment(key: str | None) -> str | None:
    return (for_location(key) or {}).get("cooldown_equipment")


def is_known(key: str | None) -> bool:
    return for_location(key) is not None


def unknown_note(key: str | None, display: str | None = None) -> str:
    """The line a row carries when this location has no configured prep."""
    return (f"No warmup or cooldown configured for {display or key or 'this location'} "
            f"— warm up as you see fit and log it; the plan is not guessing at "
            f"equipment that may not be here")
