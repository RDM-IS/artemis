"""WATCH-1 — match a watch workout to the day's planned session.

Shared by the Lambda (every ingest re-matches the dates it received) and the
box (a silent job re-matches recent days, because a workout can arrive before
or after its sets are logged). Like knowledge.watch_prefill it talks to a
plain DB-API cursor, tuple or dict.

The rules (design approved 2026-09-21, docs/ARTEMIS_STATE.md WATCH-1):

  * A workout matches a non-skipped plan for its local date when at least one
    of that plan's session_log rows has logged_at in [start, end + 10 min].
    Logging trails the set, so the window runs past the workout's end.
  * Kind gate: strength training -> strength_*, yoga/flexibility ->
    recovery_flow, cycling/elliptical -> cardio_z2. A kind that doesn't fit the
    plan is not matched; it is reported.
  * A WALK NEVER MATCHES (Ryan, 2026-09-22). Walking is daily life, not a
    prescribed session, so counting it against a plan makes adherence
    meaningless in both directions. Walks still count as activity — steps,
    active minutes, energy and heart rate all include them — they just never
    attach to a session. See PB-009.
  * Several workouts on one plan: the one containing the most log rows wins;
    the others are reported. An exact tie matches none of them. Picking one
    would be a guess.
  * A workout with no logged rows in its window stays unmatched and is
    reported. Nothing is ever attached by date alone.

Writes only health.watch_workout.plan_id. Nothing goes into session_log.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta

from knowledge.watch_prefill import _dicts

LOG_GRACE = timedelta(minutes=10)

MATCHED = "matched"
NO_PLAN = "no_plan"
PLAN_SKIPPED = "plan_skipped"
KIND_MISMATCH = "kind_mismatch"
NO_LOGGED_ROWS = "no_logged_rows"
OUTRANKED = "outranked"
TIED = "tied"


def kind_fits(kind: str | None, session_type: str | None) -> bool:
    """Whether a watch workout kind can be this plan's session."""
    k, st = (kind or "").lower(), session_type or ""
    if "strength training" in k:
        return st.startswith("strength")
    if "yoga" in k or "flexibility" in k:
        return st == "recovery_flow"
    if "walk" in k:
        return False                      # activity, never a session
    if "cycling" in k or "elliptical" in k:
        return st == "cardio_z2"
    return False


def window(w: dict) -> tuple[datetime, datetime]:
    """[start, end + grace]. With no end, start + duration; with neither, the
    start alone (plus grace)."""
    start = w["started_at"]
    end = w.get("ended_at")
    if end is None and w.get("duration_sec"):
        end = start + timedelta(seconds=int(w["duration_sec"]))
    return start, (end or start) + LOG_GRACE


def decide(workouts: list[dict], plans: list[dict], logs: list[dict]) -> list[dict]:
    """Pure. One decision per workout, in input order:
    {workout_id, plan_id (None unless matched), outcome, detail}.

    workouts: workout_id, kind, local_date, started_at, ended_at, duration_sec
    plans:    plan_id, plan_date, session_type, is_skipped
    logs:     plan_id, logged_at
    """
    plans_by_date: dict = defaultdict(list)
    for p in plans:
        plans_by_date[p["plan_date"]].append(p)
    logs_by_plan: dict = defaultdict(list)
    for r in logs:
        if r.get("logged_at") is not None:
            logs_by_plan[r["plan_id"]].append(r["logged_at"])

    out: dict = {}
    claims: dict = defaultdict(list)          # plan_id -> [(workout, rows in window)]
    for w in workouts:
        wid = w["workout_id"]
        day_plans = plans_by_date.get(w["local_date"], [])
        live = [p for p in day_plans if not p.get("is_skipped")]
        if not day_plans:
            out[wid] = (NO_PLAN, f"no plan on {w['local_date']}")
            continue
        if not live:
            out[wid] = (PLAN_SKIPPED, f"the {w['local_date']} plan is skipped")
            continue
        fit = [p for p in live if kind_fits(w["kind"], p["session_type"])]
        if not fit:
            types = ", ".join(p["session_type"] for p in live)
            out[wid] = (KIND_MISMATCH, f"{w['kind']} does not fit {types}")
            continue
        lo, hi = window(w)
        scored = [(p, sum(1 for t in logs_by_plan[p["plan_id"]] if lo <= t <= hi))
                  for p in fit]
        scored = [(p, n) for p, n in scored if n]
        if not scored:
            out[wid] = (NO_LOGGED_ROWS,
                        f"no session_log row in [{lo.isoformat()}, {hi.isoformat()}]")
            continue
        best = max(n for _, n in scored)
        top = [p for p, n in scored if n == best]
        if len(top) > 1:                      # two fitting plans on one day, same count
            out[wid] = (TIED, "equal log rows on plans "
                        + ", ".join(str(p["plan_id"]) for p in top))
            continue
        claims[top[0]["plan_id"]].append((w, best))

    matched: dict = {}
    for plan_id, entries in claims.items():
        best = max(n for _, n in entries)
        winners = [w for w, n in entries if n == best]
        if len(winners) > 1:
            for w, n in entries:
                out[w["workout_id"]] = (
                    TIED if n == best else OUTRANKED,
                    f"{n} log row(s); plan {plan_id} has {len(winners)} workouts with {best}")
            continue
        winner = winners[0]
        matched[winner["workout_id"]] = plan_id
        out[winner["workout_id"]] = (MATCHED, f"{best} log row(s) in window")
        for w, n in entries:
            if w is not winner:
                out[w["workout_id"]] = (
                    OUTRANKED, f"{n} log row(s); workout {winner['workout_id']} has {best}")

    return [{"workout_id": w["workout_id"], "plan_id": matched.get(w["workout_id"]),
             "outcome": out[w["workout_id"]][0], "detail": out[w["workout_id"]][1]}
            for w in workouts]


def rematch(cur, days) -> dict:
    """Re-decide every workout on `days` and write plan_id where it changed.
    The caller owns the transaction. Returns an audit-ready summary."""
    days = sorted({d for d in days if isinstance(d, date)})
    if not days:
        return {"days": [], "matched": [], "unmatched": [], "changed": 0}
    cur.execute(
        "SELECT workout_id, kind, local_date, started_at, ended_at, duration_sec, plan_id "
        "FROM health.watch_workout WHERE local_date = ANY(%(days)s) ORDER BY started_at",
        {"days": days})
    workouts = _dicts(cur)
    if not workouts:
        return {"days": [d.isoformat() for d in days], "matched": [], "unmatched": [],
                "changed": 0}
    cur.execute(
        "SELECT plan_id, plan_date, session_type, is_skipped FROM health.plan "
        "WHERE plan_date = ANY(%(days)s)", {"days": days})
    plans = _dicts(cur)
    logs: list[dict] = []
    if plans:
        cur.execute(
            "SELECT plan_id, logged_at FROM health.session_log WHERE plan_id = ANY(%(ids)s)",
            {"ids": [p["plan_id"] for p in plans]})
        logs = _dicts(cur)

    decisions = decide(workouts, plans, logs)
    current = {w["workout_id"]: w for w in workouts}
    changed = 0
    for d in decisions:
        if current[d["workout_id"]]["plan_id"] != d["plan_id"]:
            cur.execute("UPDATE health.watch_workout SET plan_id = %(plan_id)s "
                        "WHERE workout_id = %(id)s",
                        {"plan_id": d["plan_id"], "id": d["workout_id"]})
            changed += 1

    def label(d):
        w = current[d["workout_id"]]
        return {**d, "kind": w["kind"], "local_date": w["local_date"].isoformat(),
                "started_at": w["started_at"].isoformat()}

    return {"days": [d.isoformat() for d in days],
            "matched": [label(d) for d in decisions if d["outcome"] == MATCHED],
            "unmatched": [label(d) for d in decisions if d["outcome"] != MATCHED],
            "changed": changed}
