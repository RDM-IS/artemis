"""RECOMMEND — which Start button to highlight right now (Ryan, 2026-10-20).

Buttons, not automation: this only HIGHLIGHTS. Every button stays tappable,
nothing is scheduled, nothing is written. One rule, evaluated on each read:

    0. today's planned morning is REST          -> Recovery
    1. today's planned session isn't logged yet -> that session's button
    2. it's done (or has no button), the room can hold strength and
       nothing strength-shaped was logged in the last 48 h -> Strength (next)
    3. otherwise                                -> Recovery

Plus the "More" list: highlight every extra in it EXCEPT the one done most
recently, so the two alternate (did Mobility last -> Core is highlighted). If
none of them has ever been done, highlight none.

Pure. The Lambda does the reads and passes facts in, so the rule has one home
and Sessions and Status cannot disagree (ONE-READ-ONE-STATUS).
"""

from __future__ import annotations

from datetime import datetime, timedelta

#: The Start buttons, as gym-display names them.
BUTTONS = ("row", "row_circuit", "ride", "run", "strength", "yoga", "recovery")

#: The extras that live under "More" (not on a Start button).
MORE_TYPES = ("core", "mobility")

STRENGTH_TYPES = ("strength_a", "strength_b", "strength_c")
REST_TYPES = ("rest", "rest_mobility")
LIFT_GAP = timedelta(hours=48)

_LABEL = {"row": "Row", "row_circuit": "Row + floor", "ride": "Ride", "run": "Run",
          "strength": "Strength", "yoga": "Yoga — Intense", "recovery": "Recovery"}
_MODALITY_BUTTON = {"row": "row", "bike": "ride", "treadmill": "run"}


def button_for(session_type: str | None, blocks: dict | None) -> str | None:
    """The Start button a planned session corresponds to, or None."""
    b = blocks or {}
    if b.get("row_kind") == "circuit":
        return "row_circuit"
    if b.get("row_kind") == "ladder":
        return "row"
    if session_type in STRENGTH_TYPES:
        return "strength"
    if session_type == "recovery_flow":
        return "recovery"
    if session_type == "yoga_strength":
        return "yoga"
    modality = (b.get("cardio") or {}).get("modality")
    if session_type in ("cardio_z2", "cardio_intervals") and modality in _MODALITY_BUTTON:
        return _MODALITY_BUTTON[modality]
    return None


def _ago(now: datetime, then: datetime | None) -> str:
    if then is None:
        return "no lift logged yet"
    h = (now - then).total_seconds() / 3600
    if h < 36:
        return "last lift yesterday" if then.date() != now.date() else "lifted today"
    return f"last lift {then.strftime('%a')}"


def recommend(*, now: datetime, planned_type: str | None, planned_blocks: dict | None,
              planned_logged: bool, room_holds_strength: bool | None,
              next_strength: str | None, last_strength_at: datetime | None,
              last_more: dict[str, datetime]) -> dict:
    """The highlight. `room_holds_strength` None = the library could not be read;
    the rule then stops BEFORE step 2 rather than guessing (FAIL-CLOSED)."""
    planned_btn = button_for(planned_type, planned_blocks)
    primary, reason = None, ""
    if planned_type in REST_TYPES:
        primary, reason = "recovery", "planned rest day"
    elif planned_btn and not planned_logged:
        primary = planned_btn
        reason = f"today's plan — {_LABEL[planned_btn]}"
    else:
        done = (f"{_LABEL[planned_btn]} done" if planned_btn and planned_logged
                else "nothing else planned")
        recent_lift = last_strength_at is not None and now - last_strength_at < LIFT_GAP
        if room_holds_strength is None:
            return {"available": False,
                    "reason": "Couldn't read which sessions this room can hold."}
        if room_holds_strength and next_strength and not recent_lift:
            primary = "strength"
            reason = f"{done} · {_ago(now, last_strength_at)}"
        else:
            primary = "recovery"
            why = ("lifted in the last 48 h" if recent_lift
                   else "this room can't hold strength" if not room_holds_strength
                   else "no strength session available")
            reason = f"{done} · {why}"

    more: list[str] = []
    if last_more:
        latest = max(last_more, key=lambda k: last_more[k])
        more = [t for t in MORE_TYPES if t != latest]

    return {"available": True, "primary": primary, "label": _LABEL[primary],
            "strength": next_strength if primary == "strength" else None,
            "reason": reason, "more": more}
