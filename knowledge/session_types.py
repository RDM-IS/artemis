"""One definition of what a session type MEANS, shared by the box and the Lambda.

EVENING-1 (Ryan, 2026-09-23) adds `rest` beside `rest_mobility`: a rest morning
gets a real row typed `rest`, never an absent row — "no plan" and "planned
rest" are different facts, and only one of them is a data problem.

Everything that used to name `rest_mobility` alone has to name both, or a rest
morning gets nagged at, inferred as missed, and counted against adherence.
"""

from __future__ import annotations

#: A planned rest. Never nagged, never inferred as a missed session, never
#: counted in sessions done vs planned.
REST_TYPES: tuple[str, ...] = ("rest", "rest_mobility")

#: Rest plus the flows: nothing here is a loggable lifting session, so none of
#: them get a debrief nag or an inferred "missed" summary. A Recovery Flow logs
#: itself from gym-display (YOGA-1).
NO_FOLLOWUP_TYPES: tuple[str, ...] = REST_TYPES + ("recovery_flow",)

#: The two slots a plan row can occupy (migration 042).
SLOTS: tuple[str, ...] = ("morning", "evening")


def is_rest(session_type: str | None) -> bool:
    return (session_type or "") in REST_TYPES


def needs_followup(session_type: str | None) -> bool:
    """True when a missing log on this session is worth saying something about."""
    return (session_type or "") not in NO_FOLLOWUP_TYPES
