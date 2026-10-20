"""ROW-CIRCUIT — rower intervals with floor work between them (Ryan, 2026-10-19).

The circuit day of the 2026-10 program redesign, on the rower: a round is a row
at a set STROKE RATE, one easy minute, then push-ups and two core moves, then a
short record card. Rounds are chosen at the start of the session.

**Progression (Ryan, 17:26).** The last session he logged is the base. This time
the app offers two ways forward and he taps one:

    rate   every round one stroke/min faster, same lengths     20/21/22/23 -> 21/22/23/24
    time   every round the same rate, one minute longer         4:00 each   -> 5:00 each

The default is the lever that did NOT move last time, so tapping the default
alternates the two, as his own rowing ladder does. "Same as last time" is always
offered too: a bad-sleep morning must not be a choice between progressing and
not training.

**The base is what he DID, not what he was told.** Each round logs the stroke
rate he actually held (the record card is prefilled with the target and changed
only when he missed it). Progressing from the achieved rate means a missed round
is repeated rather than stacked on: target 23, held 22, next time 23 again.

Everything here is pure. The Lambda reads the history and calls it, so the
prescription is computed in one place, in Python, and the iPad only renders it.

**Heart rate is not targeted here.** Each logged block carries its own start and
end time; heart rate per round and per exercise is computed afterwards from the
watch's samples over those windows. Nothing in the prescription depends on it.
"""

from __future__ import annotations

from dataclasses import dataclass

SESSION_TYPE = "row_circuit"
DISPLAY_NAME = "Row circuit"
ROW_EXERCISE = "Row"

#: ROW-LADDER (Ryan, 2026-10-19 21:23): the same climb with the floor work
#: removed -- row, rest minute, row -- and ONE record card at the end, because a
#: card every round would stop the rowing it exists to measure. It replaces
#: ROW-BUILD (#269, closed) on the planned rower cardio days, and runs on demand.
#:
#: Each kind keeps its OWN history, keyed by the exercise name on its Row
#: blocks: 22 spm between push-up rounds is not 22 spm rowed straight, so one
#: must never progress the other. `stroke_rate` is written only by these two
#: screens, so exercise + stroke_rate is the identity whether a session was
#: launched on demand (adhoc) or as the day's planned cardio (plan_id).
KINDS: dict[str, dict] = {
    "circuit": {"session_type": "row_circuit", "display_name": "Row circuit",
                "exercise": "Row", "default_rounds": 4, "floor": True},
    "ladder": {"session_type": "row_ladder", "display_name": "Row ladder",
               "exercise": "Row ladder", "default_rounds": 6, "floor": False},
}

#: First session ever, round i (0-based) rows at START_RATE + i for START_WORK_SEC.
#: 20/21/22/23 at 4:00 is the session he did on 2026-10-19, minus the rate he
#: overshot in round 3.
START_RATE = 20
START_WORK_SEC = 240

MIN_ROUNDS = 1
MAX_ROUNDS = 8
DEFAULT_ROUNDS = 4

#: A water rower above ~32 strokes/min is sprinting, not this session. Neither
#: cap is a target; both stop a progression that has been tapped for months from
#: walking somewhere absurd without anyone deciding it should.
MAX_RATE = 32
MIN_RATE = 14
MAX_WORK_SEC = 600

RATE_STEP = 1
TIME_STEP_SEC = 60

#: Ryan's rule: always a one-minute rest after each work interval, stopped or
#: easy at 18.
REST_SEC = 60
REST_RATE = 18

STATION_SEC = 40
#: Seconds to get from one thing to the next. Counted as transition, never as
#: part of an exercise's window.
OFF_ROWER_SEC = 15
BETWEEN_MOVES_SEC = 10
BACK_ON_ROWER_SEC = 15
COOLDOWN_SEC = 180

LEVERS = ("rate", "time")

#: Push-ups every round. The two core moves rotate by round, skill and
#: anti-rotation work early, simpler holds late, because the later rounds are
#: done at a higher heart rate (program redesign, 2026-10-02). `reps` moves get
#: a count on the record card; `hold` moves are timed and record nothing.
PUSH = ("Push-up", "reps", "Chest to fist height. Pick the variant on the day.")
CORE_ROTATION: tuple[tuple[tuple[str, str, str], tuple[str, str, str]], ...] = (
    (("Dead bug", "reps", "Low back stays down. Slow."),
     ("Plank shoulder taps", "reps", "Hips quiet. Feet wide if you need it.")),
    (("Russian twist", "reps", "Rotate the ribs, not just the arms."),
     ("Side plank", "hold", "Half each side. Hips high.")),
    (("Bird dog", "reps", "Reach long. Don't let the hips rock."),
     ("Forearm plank", "hold", "Squeeze glutes. Breathe.")),
    (("Flutter kicks", "reps", "Small kicks, low back pressed down."),
     ("Glute bridge", "reps", "Drive through the heels. Pause at the top.")),
)

#: Never. Ryan, 2026-09-29. Enumerated against what this module BUILDS.
FORBIDDEN_MOVEMENTS = ("burpee",)


@dataclass(frozen=True)
class RowRound:
    round: int          # 1-based
    work_sec: int
    spm: int

    def as_dict(self) -> dict:
        return {"round": self.round, "work_sec": self.work_sec, "spm": self.spm}


def kind_spec(kind: str) -> dict:
    """Raises KeyError on an unknown kind: a typo must not become a circuit."""
    return KINDS[kind]


def clamp_rounds(n: int | None, default: int = DEFAULT_ROUNDS) -> int:
    if n is None:
        return default
    return max(MIN_ROUNDS, min(MAX_ROUNDS, int(n)))


def default_base(rounds: int) -> list[RowRound]:
    """The first session ever: 20, 21, 22 … at 4:00."""
    return [RowRound(i + 1, START_WORK_SEC, START_RATE + i) for i in range(rounds)]


def resize(last: list[RowRound], rounds: int) -> list[RowRound]:
    """Last session's rounds, fitted to today's count.

    Fewer rounds: the first ones (the ladder climbs, so dropping the top keeps
    the shape). More rounds: each extra round one stroke/min above the round
    before it, at that round's length -- the same climb continued.
    """
    if not last:
        return default_base(rounds)
    out = [RowRound(i + 1, r.work_sec, r.spm) for i, r in enumerate(last[:rounds])]
    while len(out) < rounds:
        prev = out[-1]
        out.append(RowRound(len(out) + 1, prev.work_sec, min(MAX_RATE, prev.spm + RATE_STEP)))
    return out


def moved_lever(last: list[RowRound], prev: list[RowRound]) -> str | None:
    """Which lever the last session moved relative to the one before, over the
    rounds they share. None when neither moved, or there is nothing to compare.
    Derived from what was logged -- nothing stores the choice."""
    n = min(len(last), len(prev))
    if n == 0:
        return None
    d_rate = sum(last[i].spm - prev[i].spm for i in range(n))
    d_time = sum(last[i].work_sec - prev[i].work_sec for i in range(n))
    if d_rate > 0 and d_time <= 0:
        return "rate"
    if d_time > 0 and d_rate <= 0:
        return "time"
    # Both moved (a round-count change can do that) or neither: no single lever.
    return None


def default_lever(last: list[RowRound], prev: list[RowRound]) -> str:
    """The lever that did not move last time. Rate when there is no history,
    matching Ryan's example (20/21/22/23 -> 21/22/23/24)."""
    moved = moved_lever(last, prev)
    return "rate" if moved in (None, "time") else "time"


def option(base: list[RowRound], lever: str | None) -> dict:
    """One prescription. `lever` None = same as last time.

    `ok` False with a `reason` when a cap would be crossed; the option is then
    shown but cannot be picked, so the screen says why instead of silently
    offering something else.
    """
    rounds: list[RowRound] = []
    for r in base:
        if lever == "rate":
            rounds.append(RowRound(r.round, r.work_sec, r.spm + RATE_STEP))
        elif lever == "time":
            rounds.append(RowRound(r.round, r.work_sec + TIME_STEP_SEC, r.spm))
        else:
            rounds.append(r)
    reason = None
    if any(r.spm > MAX_RATE for r in rounds):
        reason = f"a round would go above {MAX_RATE} strokes/min"
    elif any(r.work_sec > MAX_WORK_SEC for r in rounds):
        reason = f"a round would go past {MAX_WORK_SEC // 60} min"
    return {
        "lever": lever or "repeat",
        "ok": reason is None,
        "reason": reason,
        "rounds": [r.as_dict() for r in rounds],
        "row_sec": sum(r.work_sec for r in rounds),
        "label": label_for(lever, rounds),
    }


def label_for(lever: str | None, rounds: list[RowRound]) -> str:
    rates = "/".join(str(r.spm) for r in rounds)
    lengths = sorted({r.work_sec for r in rounds})
    length = (_clock(lengths[0]) if len(lengths) == 1
              else "/".join(_clock(r.work_sec) for r in rounds))
    head = {"rate": "+1 stroke/min", "time": "+1:00 each round"}.get(lever or "", "Same as last time")
    return f"{head} — {rates} spm · {length}"


def _clock(sec: int) -> str:
    return f"{sec // 60}:{sec % 60:02d}"


def stations_for_round(round_num: int) -> list[dict]:
    """Push-up, then that round's two core moves. 1-based round."""
    a, b = CORE_ROTATION[(round_num - 1) % len(CORE_ROTATION)]
    out = []
    for name, fmt, cue in (PUSH, a, b):
        out.append({"name": name, "format": fmt, "duration_sec": STATION_SEC, "cue": cue})
    return out


def prescription(history: list[list[RowRound]], rounds: int | None,
                 kind: str = "circuit") -> dict:
    """Everything the start screen needs. `history` is newest first: one list
    of rounds per logged session of THIS kind, each in round order."""
    spec = kind_spec(kind)
    n = clamp_rounds(rounds, spec["default_rounds"])
    last = history[0] if history else []
    prev = history[1] if len(history) > 1 else []
    base = resize(last, n)
    default = default_lever(last, prev)
    options = {lv: option(base, lv) for lv in LEVERS}
    if not options[default]["ok"]:
        other = "time" if default == "rate" else "rate"
        default = other if options[other]["ok"] else "repeat"
    moved = moved_lever(last, prev)
    if history:
        why = {"rate": "last time added a stroke/min, so a minute is the default today",
               "time": "last time added a minute, so a stroke/min is the default today",
               }.get(moved or "", "one session logged so far — the rate goes up first"
                     if len(history) == 1 else
                     "last time didn't move one lever alone — the rate goes up first")
    else:
        why = f"no logged {spec['display_name'].lower()} yet — starting the ladder"
    return {
        "kind": kind,
        "session_type": spec["session_type"],
        "display_name": spec["display_name"],
        "row_exercise": spec["exercise"],
        "rounds": n,
        "min_rounds": MIN_ROUNDS,
        "max_rounds": MAX_ROUNDS,
        "base": [r.as_dict() for r in base],
        "has_history": bool(history),
        "last_rounds": len(last),
        "options": {**options, "repeat": option(base, None)},
        "default": default,
        "why": why,
        "rest": {"sec": REST_SEC, "rate": REST_RATE},
        "timing": {
            "station_sec": STATION_SEC, "off_rower_sec": OFF_ROWER_SEC,
            "between_moves_sec": BETWEEN_MOVES_SEC, "back_on_rower_sec": BACK_ON_ROWER_SEC,
            "cooldown_sec": COOLDOWN_SEC,
        },
        # The ladder has no floor work: an empty list per round, not a missing
        # key, so the screen's per-round indexing has one shape for both kinds.
        "stations": [stations_for_round(i + 1) if spec["floor"] else [] for i in range(n)],
    }


def group_history(rows: list[dict], *, max_sessions: int = 2) -> list[list[RowRound]]:
    """Logged Row blocks -> sessions, newest first.

    `rows` carry `day` (the ACTIVE-timezone date the block started), `set_num`
    (the round), `duration_sec`, `stroke_rate` and `logged_at`, in any order.
    One session per day; a round logged twice that day keeps its LATEST log,
    because the record card can be re-saved and the last save is the one he
    meant. A row with no stroke rate or no duration is skipped -- it cannot
    describe a round.
    """
    by_day: dict = {}
    for r in rows:
        if r.get("stroke_rate") is None or not r.get("duration_sec") or not r.get("set_num"):
            continue
        day = by_day.setdefault(r["day"], {})
        cur = day.get(int(r["set_num"]))
        if cur is None or r["logged_at"] >= cur["logged_at"]:
            day[int(r["set_num"])] = r
    sessions = []
    for day in sorted(by_day, reverse=True)[:max_sessions]:
        rounds = by_day[day]
        sessions.append([RowRound(i + 1, int(rounds[k]["duration_sec"]), int(rounds[k]["stroke_rate"]))
                         for i, k in enumerate(sorted(rounds))])
    return sessions


def last_reps(rows: list[dict]) -> dict[str, dict[str, int]]:
    """{round: {move: reps}} from the most recent day's strength sets, for
    prefilling the record card. Keys are strings so the JSON shape is plain."""
    if not rows:
        return {}
    day = max(r["day"] for r in rows)
    out: dict[str, dict[str, int]] = {}
    latest: dict = {}
    for r in rows:
        if r["day"] != day or r.get("reps_done") is None or not r.get("set_num"):
            continue
        key = (int(r["set_num"]), r["exercise"])
        if key in latest and latest[key] > r["logged_at"]:
            continue
        latest[key] = r["logged_at"]
        out.setdefault(str(key[0]), {})[r["exercise"]] = int(r["reps_done"])
    return out
