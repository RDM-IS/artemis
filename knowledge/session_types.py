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


#: Planned TRAINING — what can be missed, and so what can be made up
#: (MAKEUP-2, Ryan 2026-09-27). Flows and rest are not training.
def is_training(session_type: str | None) -> bool:
    t = session_type or ""
    return t.startswith("strength") or t == "cardio_z2"


#: What the Sessions tab offers every day, rest days included — low-impact
#: work that sits on top of planned training without affecting it (Ryan,
#: 2026-09-27). Core and standalone mobility join when they have definitions.
EXTRA_TYPES: tuple[str, ...] = ("recovery_flow", "core", "mobility",
                                "yoga_strength")


# ── Strength types — ONE list (SAME-FACT-MANY-PLACES, 2026-11-01) ────────────
# The Lambda's rotation, RECOMMEND, the check-in and the session library each
# carried their own ("strength_a", "strength_b", "strength_c"), so adding
# FULLBODY-1 would have been four edits with one forgotten. They read these now;
# tests/test_session_types.py greps for a fresh literal copy.

#: The current program's only lift (WEEK-TEMPLATE / FULLBODY-1, migration 058).
STRENGTH_CURRENT = "strength_full"

#: The retired A/B/C split. Still real in history, so still a strength type.
STRENGTH_LEGACY: tuple[str, ...] = ("strength_a", "strength_b", "strength_c")

#: Every strength type a plan row or a logged session can carry.
STRENGTH_TYPES: tuple[str, ...] = STRENGTH_LEGACY + (STRENGTH_CURRENT,)

#: SESSION-OPEN 3a (approved 2026-11-09): the bodyweight full-body session.
#: Deliberately NOT in STRENGTH_TYPES — that tuple is the lift ROTATION
#: (RECOMMEND's "next strength", /strength-next's history, the check-in's
#: lettered labels), and "bodyweight" is a room's answer, not a step in it. It
#: IS a lift for "did he lift in the last 48 h" (LIFT_TYPES) and a strength day
#: for the weekly quota (STRENGTH_DAY_TYPES).
STRENGTH_BW = "strength_bw"

#: Everything that is a lift, for "when did he last lift": the rotation plus
#: bodyweight. RECOMMEND's 48 h rule reads this, never STRENGTH_TYPES.
LIFT_TYPES: tuple[str, ...] = STRENGTH_TYPES + (STRENGTH_BW,)

#: Short label where a letter used to be shown.
STRENGTH_LABEL: dict[str, str] = {"strength_a": "A", "strength_b": "B",
                                  "strength_c": "C", "strength_full": "Full Body",
                                  STRENGTH_BW: "Bodyweight"}


def is_strength(session_type: str | None) -> bool:
    return (session_type or "") in STRENGTH_TYPES


# ── Strength DAYS — the weekly quota (SESSION-OPEN, 2026-11-09) ──────────────
# A strength day is a lift or a circuit with floor work. Checked against the
# live tables on 2026-11-09 (MATCHES-NOTHING):
#   * a PLANNED circuit is never its own session_type. ROW-SCHEDULE writes it as
#     `cardio_z2` with `blocks.row_kind = 'circuit'` (12 live rows, 10/21-12/11),
#     so a list of session_type values alone matches zero planned circuits. The
#     quota reader maps that row_kind onto CIRCUIT_TYPES before it compares.
#   * an AD-HOC circuit (Start button) logs `adhoc_session_type = 'row_circuit'`
#     (knowledge.row_circuit.KINDS["circuit"]). Zero live rows today.
#   * ride + floor / run + floor were never built (WEEK-TEMPLATE, "as built"):
#     `ride_ladder` / `run_ladder` have no floor work and are NOT strength days.
#     A name for a floor ride or run would match nothing anyone writes, so none
#     is listed; tests/test_strength_quota.py fails when a floor kind is added
#     to row_circuit.KINDS without being added here.
#   * `yoga_strength` is excluded on purpose (an extra, not a strength day).

#: Circuit formats that count as a strength day — every row_circuit kind with
#: floor work, by its ad-hoc slug.
CIRCUIT_TYPES: tuple[str, ...] = ("row_circuit",)

#: The `blocks.row_kind` that makes a planned cardio_z2 row a circuit, and the
#: circuit type it counts as.
PLANNED_CIRCUIT_ROW_KIND = "circuit"
PLANNED_CIRCUIT_TYPE = "row_circuit"

#: AWAY's no-gym substitute (BW-CIRCUIT): a planned row typed
#: `bodyweight_circuit` on a work or vacation day without a gym. Counts toward
#: the quota (Ryan, 2026-11-09). A hotel-gym day is already a strength type.
AWAY_STRENGTH_TYPES: tuple[str, ...] = ("bodyweight_circuit",)

#: Everything that makes a day a strength day. ONE list — readers import it.
STRENGTH_DAY_TYPES: tuple[str, ...] = (STRENGTH_TYPES + (STRENGTH_BW,) + CIRCUIT_TYPES
                                       + AWAY_STRENGTH_TYPES)


def day_type(session_type: str | None, row_kind: str | None = None) -> str | None:
    """What a logged session counts as for the quota: a planned circuit row is a
    circuit, whatever its session_type says."""
    if row_kind == PLANNED_CIRCUIT_ROW_KIND:
        return PLANNED_CIRCUIT_TYPE
    return session_type


def is_strength_day_type(session_type: str | None, row_kind: str | None = None) -> bool:
    return day_type(session_type, row_kind) in STRENGTH_DAY_TYPES
