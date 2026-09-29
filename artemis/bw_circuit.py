"""BW-CIRCUIT — "swap today for a bodyweight circuit". CONTENT IS A DRAFT
pending Ryan's approval (round #20 report).

One session type, reused by AWAY's no-gym and vacation modes so there is ONE
content source rather than three.

**It is a FLOW, not a circuit screen** (Ryan, 11:29). Same shape as the Recovery
Flow, so gym-display's Flow screen runs it: auto-advance, voice cues, one tap to
start, automatic logging by elapsed time. No reps, no rounds, no per-set
logging — nothing in the design waits on input.

**There is no static rest anywhere.** Every recovery segment is MARCHING IN
PLACE (or jogging with `impact on`). A circuit whose "rest" is standing still
is a circuit that stops, and the point of this session is that it does not.

**Intensity follows the interval gate, fail-closed.** Gate passed → 40 s work /
20 s march, Z4 bursts. Gate not passed, unreadable, or never evaluated →
moderate: 30 s / 30 s, Z3, with the failing condition named on the card. Same
rule as a `cardio_intervals` row, and for the same reason: Zone 4 is the
sharpest thing in the program and "I could not check" must not look like "go".
"""

from __future__ import annotations

#: DRAFT (2026-09-29) — awaiting Ryan's approval.
CONTENT_IS_A_DRAFT = True

WARMUP_SEC = 300          # 5 min, moving throughout
COOLDOWN_SEC = 300        # 5 min, march down into easy mobility
LAPS = 3                  # 3 laps x 5 stations x 60 s = 15 min main block

#: (work_sec, recover_sec, target_zone) by intensity.
FULL = (40, 20, 4)
MODERATE = (30, 30, 3)

#: The five stations, in order. `low` is the low-impact default; `impact` is the
#: jumping variant `impact on` swaps in. `needs` names the kit that unlocks the
#: better version of that station.
STATIONS: tuple[dict, ...] = (
    {"slot": "push", "low": "Push-up", "impact": "Push-up",
     "regression": "Incline push-up (hands on bench or counter)",
     "cue": "Chest to fist height. Elbows about 45 degrees, not flared."},
    # THE PULL IS THE WEAK LINK WITHOUT KIT, and the draft says so rather than
    # pretending otherwise: nothing bodyweight pulls the way a row does.
    {"slot": "pull", "low": "TRX row", "impact": "TRX row",
     "needs": "trx", "alt_band": "Band row",
     "regression": "Prone Y-T-W", "no_kit": "Table row or towel row",
     "cue": "Shoulder blades first, then elbows. Chest tall."},
    {"slot": "legs", "low": "Reverse lunge", "impact": "Jumping lunge",
     "regression": "Tempo squat", "cue": "Step back, knee down slow, drive up."},
    {"slot": "core", "low": "Slow mountain climber", "impact": "Mountain climber",
     "regression": "Dead bug", "cue": "Hips quiet. One knee at a time."},
    {"slot": "burst", "low": "Fast step-ups", "impact": "Skater steps",
     "regression": "March with high knees",
     "cue": "This is the one that lifts the heart rate. Drive the knee."},
)

#: The recovery segment. Never "rest".
RECOVER_LOW = "March in place"
RECOVER_IMPACT = "Jog in place"


def station_movement(station: dict, *, impact: bool, kit: set | None) -> str:
    """Which movement this station resolves to here, with what is packed.

    Equipment-aware: the pull station becomes a TRX row when a TRX is present, a
    band row with bands, and otherwise the honest fallback. Nothing invents kit
    that is not there -- the same rule as a location's inventory.
    """
    kit = kit or set()
    if station["slot"] == "pull":
        if "trx" in kit:
            return station["low"]
        if "bands" in kit:
            return station["alt_band"]
        return station["no_kit"]
    return station["impact"] if impact else station["low"]


def build(*, gate: dict | None = None, impact: bool = False,
          kit: set | None = None, location: str = "home") -> tuple[dict, float, int, int]:
    """The flow blocks, RPE, target zone and estimated minutes.

    `gate` is the interval gate's answer. Its ABSENCE is a fail, exactly as for
    `cardio_intervals`: a circuit built before the gate was evaluated runs
    moderate, and says so.
    """
    passed = bool(gate and gate.get("ok"))
    work_sec, recover_sec, zone = FULL if passed else MODERATE
    reason = None
    if not passed:
        reason = ((gate or {}).get("reason")
                  or "the interval gate has not been evaluated for this day yet")

    steps: list[dict] = []
    n = 1

    def add(name, dur, *, cue=None, kind="work"):
        nonlocal n
        steps.append({"step": str(n), "name": name, "side": None, "side_label": None,
                      "duration_sec": dur, "transition_sec": 0, "mirror_group": None,
                      "cue": cue, "kind": kind, "posture": "standing"})
        n += 1

    recover = RECOVER_IMPACT if impact else RECOVER_LOW
    # Warm-up MOVES: marching into easy mobility, never standing.
    add(recover, 120, cue="Easy pace. Get the shoulders and hips moving.", kind="warmup")
    add("Arm circles and hip openers", 120, cue="Big, slow circles.", kind="warmup")
    add(recover, 60, cue="Pick the pace up a little. The first station is next.",
        kind="warmup")

    for _lap in range(LAPS):
        for st in STATIONS:
            add(station_movement(st, impact=impact, kit=kit), work_sec, cue=st["cue"])
            add(recover, recover_sec, cue="Keep moving. Shake it out.", kind="recover")

    add(recover, 120, cue="Slow it down.", kind="cooldown")
    add("Easy mobility — hips, chest, shoulders", 180,
        cue="Nothing forced. Breathe out on each stretch.", kind="cooldown")

    total = sum(s["duration_sec"] for s in steps)
    blocks = {
        "type": "recovery_flow",            # the RENDERER: the Flow screen
        "session_type": "bodyweight_circuit",
        "display_name": "Bodyweight circuit",
        "location": location,
        "rounds": 1,                        # one continuous pass, not rounds
        "hold_sec": work_sec,
        "leadin_sec": 7,
        "start_posture": "standing",
        "steps": steps,
        "intensity": "full" if passed else "moderate",
        "work_sec": work_sec,
        "recover_sec": recover_sec,
        "impact": impact,
        "kit": sorted(kit or []),
        "content_is_draft": CONTENT_IS_A_DRAFT,
        "setup_notes": [
            "Start a workout on your watch (HIIT or Other) so heart rate is "
            "recorded densely — the zone split comes from that afterwards.",
            f"{work_sec}s work / {recover_sec}s easy, continuous. "
            f"The easy part is {recover.lower()}, not standing.",
        ],
    }
    if not passed:
        blocks["ran_as"] = "moderate"
        blocks["moderate_reason"] = reason
        blocks["setup_notes"].append(f"Moderate today: {reason}.")
    else:
        blocks["ran_as"] = "full"
    rpe = 7.0 if passed else 5.5
    return blocks, rpe, zone, max(1, round(total / 60))


# ── The swap ────────────────────────────────────────────────────────────────

PENDING_KEY = "bw_circuit_pending"


def can_swap(cur, day) -> tuple[bool, str]:
    """(ok, why-not). Refuses a day that already carries a real log.

    Swapping a day he has already trained would replace what happened with what
    he now intends -- the same reason ZONE-0 never rewrites a logged session.
    """
    cur.execute(
        "SELECT 1 FROM health.session_log sl JOIN health.plan p ON p.plan_id = sl.plan_id "
        "WHERE p.plan_date = %s AND sl.logged_via <> 'inferred' LIMIT 1", (day,))
    if cur.fetchone() is not None:
        return False, "that day already has a logged session"
    cur.execute("SELECT plan_id FROM health.plan WHERE plan_date = %s AND slot = 'morning'",
                (day,))
    if cur.fetchone() is None:
        return False, "there is no planned session that day"
    return True, ""


def swap_row(cur, day, *, gate: dict | None, impact: bool = False,
             kit: set | None = None) -> dict:
    """Replace the day's morning row with the circuit, keeping the original.

    The original content goes in `blocks.original`, the same place every other
    adjustment keeps it, so `undo circuit` is a restore rather than a rebuild --
    a rebuild would silently pick up any template change since.
    """
    import json

    cur.execute(
        "SELECT plan_id, session_type, blocks, target_rpe, target_hr_zone, "
        "       est_duration_min FROM health.plan "
        "WHERE plan_date = %s AND slot = 'morning'", (day,))
    row = cur.fetchone()
    if row is None:
        raise ValueError("no morning row to swap")
    get = (lambda k, i: row[k] if isinstance(row, dict) else row[i])
    old_blocks = get("blocks", 2)
    if isinstance(old_blocks, str):
        old_blocks = json.loads(old_blocks)

    location = (old_blocks or {}).get("location") or "home"
    blocks, rpe, zone, est = build(gate=gate, impact=impact, kit=kit, location=location)
    blocks["original"] = {
        "session_type": get("session_type", 1), "blocks": old_blocks,
        "target_rpe": get("target_rpe", 3), "target_hr_zone": get("target_hr_zone", 4),
        "est_duration_min": get("est_duration_min", 5),
    }
    blocks["location_key"] = (old_blocks or {}).get("location_key")
    cur.execute(
        "UPDATE health.plan SET session_type = 'bodyweight_circuit', blocks = %s::jsonb, "
        "  target_rpe = %s, target_hr_zone = %s, est_duration_min = %s "
        "WHERE plan_id = %s",
        (json.dumps(blocks, default=str), rpe, zone, est, get("plan_id", 0)))
    return {"plan_id": get("plan_id", 0), "replaced": get("session_type", 1),
            "intensity": blocks["intensity"], "minutes": est}


def undo(cur, day) -> dict | None:
    """Put the original session back. None when the day is not a swapped circuit."""
    import json

    cur.execute(
        "SELECT plan_id, session_type, blocks FROM health.plan "
        "WHERE plan_date = %s AND slot = 'morning'", (day,))
    row = cur.fetchone()
    if row is None:
        return None
    get = (lambda k, i: row[k] if isinstance(row, dict) else row[i])
    if get("session_type", 1) != "bodyweight_circuit":
        return None
    blocks = get("blocks", 2)
    if isinstance(blocks, str):
        blocks = json.loads(blocks)
    original = (blocks or {}).get("original")
    if not original:
        return None
    cur.execute(
        "UPDATE health.plan SET session_type = %s, blocks = %s::jsonb, target_rpe = %s, "
        "  target_hr_zone = %s, est_duration_min = %s WHERE plan_id = %s",
        (original["session_type"], json.dumps(original["blocks"], default=str),
         original.get("target_rpe"), original.get("target_hr_zone"),
         original.get("est_duration_min"), get("plan_id", 0)))
    return {"restored": original["session_type"]}
