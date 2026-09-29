"""Double progression — the rule Block 2 runs on.

Block 1 suggested loads from the last top set's RPE and a HARD-CODED +5 lb
(`health._suggestion_from_last`). That is wrong in two ways at once: +5 does not
exist at Richfield, whose PowerBlocks move in 10s and cannot make 45, and RPE
alone ignores whether he actually completed the reps.

Double progression is the approved rule (PROGRAM-2 Block 2, Ryan 2026-09-28):

    every set at the TOP of the rep range, at or under the target RPE
        -> next session, one load step up (and back to the bottom of the range)
    otherwise
        -> same load, aim for one more rep

The load step comes from the LOCATION'S `load_config` for that exercise's
equipment class, which is already on every row since LOCATION-1. A suggestion
that names a weight the room cannot make is worse than no suggestion: he has to
notice it is impossible, and the obvious repair (round to something available)
is a different weight from the one he was told to lift.

Pure: no database, no clock. The caller supplies what was logged.
"""

from __future__ import annotations

import re

#: "3×10-12" / "10-12" / "3 x 8-12". The top of the range is what progression
#: is measured against.
_RANGE = re.compile(r"(?:(\d+)\s*[×x]\s*)?(\d+)\s*[-–]\s*(\d+)")


def rep_range(exercise: dict) -> tuple[int, int] | None:
    """(low, high) from the exercise's notes, or None when it has no range.

    None is the explicit unknown: a duration hold and a fixed-rep movement have
    no top of range to reach, so they cannot double-progress and must not be
    guessed into it.
    """
    m = _RANGE.search(str(exercise.get("notes") or ""))
    if m:
        low, high = int(m.group(2)), int(m.group(3))
        return (low, high) if low <= high else (high, low)
    return None


def load_step(load_config: dict | None, equipment_class: str | None) -> float | None:
    """The smallest load increment for this class HERE, or None if unknown.

    None means "this room has not been measured", and the caller must then not
    offer a number -- the LOCATION-1 rule that an absent inventory is absent,
    not empty.
    """
    if not load_config or not equipment_class:
        return None
    cfg = load_config.get(equipment_class)
    if not isinstance(cfg, dict):
        return None
    step = cfg.get("step")
    return float(step) if isinstance(step, (int, float)) and step > 0 else None


def next_load(current: float | None, step: float | None, cfg: dict | None) -> float | None:
    """One step up, clamped to what the room actually has."""
    if current is None or step is None:
        return None
    nxt = float(current) + step
    if isinstance(cfg, dict):
        top = cfg.get("max")
        if isinstance(top, (int, float)) and nxt > top:
            return float(top)
    return nxt


def advance(*, exercise: dict, sets: list[dict], rpe_cap: float | None,
            load_config: dict | None) -> dict:
    """What to do next for one exercise.

    `sets` is what he logged last time: [{"reps_done", "weight_lbs", "rpe_actual"}].

    Returns::

        {"action": "add_load" | "add_rep" | "hold" | "unknown",
         "load": float | None, "reps": int | None, "why": str}

    `unknown` is a real answer and the caller must render it as one. It happens
    when there is no rep range, no logged set, or no load step for this room --
    and in each case the honest output is "no suggestion", not a number derived
    from a missing input.
    """
    rng = rep_range(exercise)
    done = [s for s in sets or [] if s.get("reps_done") is not None]
    if rng is None:
        return {"action": "unknown", "load": None, "reps": None,
                "why": "no rep range on this movement"}
    if not done:
        return {"action": "unknown", "load": None, "reps": None,
                "why": "nothing logged last time"}

    low, high = rng
    weights = [s.get("weight_lbs") for s in done if s.get("weight_lbs") is not None]
    current = max(float(w) for w in weights) if weights else None
    cls = exercise.get("equipment_class")
    cfg = (load_config or {}).get(cls) if isinstance(load_config, dict) else None
    step = load_step(load_config, cls)

    every_set_at_top = all(int(s["reps_done"]) >= high for s in done)
    # The RPE cap is the "at or under" half of the rule. A set taken to the top
    # of the range at RPE 9 when the cap is 7 is not a set that earned more
    # load -- it is a set that was already too hard.
    rpes = [float(s["rpe_actual"]) for s in done if s.get("rpe_actual") is not None]
    within_cap = rpe_cap is None or not rpes or max(rpes) <= float(rpe_cap)

    if every_set_at_top and within_cap:
        if step is None:
            return {"action": "unknown", "load": None, "reps": None,
                    "why": "earned a load step, but this room's step is unknown"}
        return {"action": "add_load", "load": next_load(current, step, cfg),
                "reps": low,
                "why": f"every set hit {high} within the RPE cap — one step up, "
                       f"back to {low} reps"}
    if every_set_at_top and not within_cap:
        return {"action": "hold", "load": current, "reps": high,
                "why": f"hit {high} but over the RPE cap — same load again"}
    best = max(int(s["reps_done"]) for s in done)
    return {"action": "add_rep", "load": current, "reps": min(best + 1, high),
            "why": f"same load, aim for {min(best + 1, high)}"}


def describe(result: dict) -> str:
    """One line for a card or a reply. Says "no suggestion" rather than a number
    when the inputs were not there."""
    if result["action"] == "unknown":
        return f"No suggestion — {result['why']}."
    load = result.get("load")
    load_txt = ("" if load is None else
                f"{int(load) if float(load).is_integer() else load} lb ")
    if result["action"] == "add_load":
        return f"Go up: {load_txt}× {result['reps']}. {result['why'].capitalize()}."
    if result["action"] == "hold":
        return f"Hold {load_txt}× {result['reps']}. {result['why'].capitalize()}."
    return f"{load_txt}× {result['reps']}. {result['why'].capitalize()}."
