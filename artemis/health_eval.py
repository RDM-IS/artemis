"""EVAL-1 — read-only weekly evaluator.

Counts what happened in one office week; it never changes a plan, never
recommends, and holds no pending state or confirm routes. It is the report-only
evaluator RAMP-RETIRE called for (health_ramp.py is not reused).

  Week       the office Wed-Tue window counted from health_office.WEEK1_START,
             in the ACTIVE timezone (quiet_hours.local_today()).
  Inputs     the week's health.plan rows, and health.session_log rows (the week
             plus the 7 days before it, for load change), logged_via <> 'inferred'.
  Output     one dict (evaluate()): sessions done vs planned, missed sessions,
             average session RPE vs the plan's cap (target_rpe), per-exercise
             top-set load change vs the prior week, adjustments applied.

Training days are every plan row except rest_mobility; rest days are neither
done nor missed. A day before the program anchor is pre-program and ignored.

Consumers: the weekly report (scripts/export_report.py) and the Sunday post
(scheduler.job_weekly_eval — the week to date, labelled partial).

    python3.11 -m artemis.health_eval --week 2026-09-16      # print one week
"""

from __future__ import annotations

import json
from datetime import date, timedelta

from artemis import health_office as office
from artemis import health_regions as hr

from knowledge.session_types import REST_TYPES as _REST  # noqa: E402

REST_TYPES = set(_REST)
# WALK-RETIRE (Ryan, 2026-09-22): activity, never a prescribed session. Rows of
# these types are left out of sessions done vs planned entirely — not counted
# as done, missed, planned or rest. The activity totals (steps, active minutes,
# energy, heart rate) are read from the watch tables and still include walks.
ACTIVITY_ONLY_TYPES = {"walk"}

# Names logged before a correction, grouped under the current name so a rename
# never reads as a new exercise. session_log 123/129 (9/18) predate the fix.
EXERCISE_ALIASES = {"45° back extension": "Seated back extension"}


def canon(name: str | None) -> str | None:
    return EXERCISE_ALIASES.get(name, name) if name else name


def _recovery(checkins, start: date, through: date) -> dict | None:
    """WATCH-1: average sleep hours and resting HR for the week. Data only —
    no interpretation, no target, no advice. None when nothing was recorded."""
    sleeps, hrs = [], []
    for c in checkins:
        d = c.get("state_date")
        if d is None or not (start <= d <= through):
            continue
        if c.get("sleep_hrs") is not None:
            sleeps.append(float(c["sleep_hrs"]))
        if c.get("resting_hr") is not None:
            hrs.append(float(c["resting_hr"]))
    if not sleeps and not hrs:
        return None
    return {
        "avg_sleep_hrs": round(sum(sleeps) / len(sleeps), 1) if sleeps else None,
        "sleep_nights": len(sleeps),
        "avg_resting_hr": round(sum(hrs) / len(hrs)) if hrs else None,
        "resting_hr_days": len(hrs),
    }


def week_of(day: date, anchor: date = office.WEEK2_START) -> tuple[date, date]:
    """The program week containing `day` (SCHEDULE-2).

    Weeks run Sun..Sat from WEEK2_START (2026-09-20), matching the CYCLE-1 pay
    period. Week 1 is the 9/16..9/19 stub before the anchor, and reports as
    that partial span rather than a 7-day window."""
    if day < anchor:
        return office.WEEK1_START, anchor - timedelta(days=1)
    start = anchor + timedelta(days=7 * ((day - anchor).days // 7))
    return start, start + timedelta(days=6)


def _num(v):
    return None if v is None else float(v)


def _blocks(v) -> dict:
    if isinstance(v, str):
        try:
            return json.loads(v or "{}")
        except ValueError:
            return {}
    return v or {}


def _top_weights(logs) -> dict[str, float | None]:
    """Top set per (canonical) exercise; None when it was logged with no load."""
    out: dict[str, float | None] = {}
    for l in logs:
        if l["log_type"] != "strength_set" or l.get("is_skipped"):
            continue
        name = canon(l["exercise"])
        if not name:
            continue
        w = _num(l.get("weight_lbs"))
        prev = out.get(name)
        out[name] = w if prev is None else (prev if w is None else max(prev, w))
    return out


def evaluate(plans, logs, prior_logs, *, start: date, end: date, today: date,
             anchor: date = office.WEEK1_START, checkins=None) -> dict:
    """Pure. `plans`: health.plan rows for start..end; `logs`: real session_log
    rows for start..end (with plan_date, plan_id); `prior_logs`: the same for
    the 7 days before `start`; `checkins`: health.daily_state rows for
    start..end (WATCH-1 — sleep and resting HR, data only)."""
    by_plan: dict[int, list] = {}
    for l in logs:
        by_plan.setdefault(l["plan_id"], []).append(l)

    # EVENING-1 (Ryan, 2026-09-23): the two slots get SEPARATE denominators.
    # A blended figure would say nothing useful — a missed evening yoga is not
    # a missed lift, and 4 evenings a week would swamp 3 sessions.
    evening_plans = [p for p in plans if (p.get("slot") or "morning") == "evening"]
    plans = [p for p in plans if (p.get("slot") or "morning") == "morning"]

    sessions, missed, skipped, adjustments = [], [], [], []
    done = due = upcoming = 0
    rpe_pairs = []
    for p in sorted(plans, key=lambda r: r["plan_date"]):
        d = p["plan_date"]
        if d < anchor:
            continue
        b = _blocks(p.get("blocks"))
        adj = b.get("adjustment")
        if isinstance(adj, dict):
            adjustments.append({"date": d.isoformat(), "rules": list(adj.get("rules_fired") or []),
                                "reason": adj.get("reason") or ""})
        st = p["session_type"]
        if st in ACTIVITY_ONLY_TYPES:
            # WALK-RETIRE (Ryan, 2026-09-22): a walk is activity, never a
            # session. It counts in steps / active minutes / energy, never in
            # sessions done vs planned. No current plan row is a walk; this
            # guard keeps a legacy or hand-inserted row out of the counts.
            continue
        if st in REST_TYPES:
            status = "rest"
        elif p.get("is_skipped"):
            # MAKEUP-1: Ryan said so out loud. A deliberate skip is NOT a
            # missed session and never counts against the week.
            status = "skipped"
            skipped.append({"date": d.isoformat(), "label": b.get("display_name") or st,
                            "reason": (p.get("skip_reason") or "").strip() or None})
        else:
            real = [l for l in by_plan.get(p["plan_id"], []) if not l.get("is_skipped")]
            if real:
                status = "done"
            elif d > today:
                status = "upcoming"
            elif d == today:
                status = "today"
            else:
                status = "missed"
        sets = [l for l in by_plan.get(p["plan_id"], []) if l["log_type"] == "strength_set"]
        summ = [_num(l.get("rpe_actual")) for l in by_plan.get(p["plan_id"], [])
                if l["log_type"] == "session_summary" and l.get("rpe_actual") is not None]
        set_rpes = [_num(l["rpe_actual"]) for l in sets if l.get("rpe_actual") is not None]
        rpe = summ[-1] if summ else (round(sum(set_rpes) / len(set_rpes), 1) if set_rpes else None)
        cap = _num(p.get("target_rpe"))
        label = b.get("display_name") or st
        sessions.append({"date": d.isoformat(), "session_type": st, "label": label,
                         "status": status, "sets": len(sets), "rpe": rpe, "cap": cap})
        if status == "done":
            done += 1
            due += 1
            if rpe is not None and cap is not None:
                rpe_pairs.append((rpe, cap, d.isoformat(), label))
        elif status == "missed":
            due += 1
            missed.append({"date": d.isoformat(), "label": label})
        elif status in ("upcoming", "today"):
            upcoming += 1

    # LOCATION-1: the class comes from the ROW, because names no longer imply
    # one. An exercise absent here is reported as unknown, not as bodyweight.
    classes_by_exercise: dict[str, str] = {}
    for p_row in list(plans) + list(evening_plans):
        b = p_row.get("blocks") if isinstance(p_row.get("blocks"), dict) else {}
        for ex in (b.get("exercises") or []):
            if ex.get("name") and ex.get("equipment_class"):
                classes_by_exercise[ex["name"]] = ex["equipment_class"]

    now_w, prev_w = _top_weights(logs), _top_weights(prior_logs)
    loads = []
    for name, cur in now_w.items():
        prev = prev_w.get(name, "absent")
        if cur is None:
            cls = classes_by_exercise.get(name)
            note = ("bodyweight" if cls in hr.NO_LOAD_CLASSES
                    else "no load logged" if cls
                    else "no load logged (class unknown)")
            change = None
        elif prev == "absent":
            note, change = "first week", None
        elif prev is None:
            note, change = "no load last week", None
        else:
            change = round(cur - prev, 1)
            note = "same" if change == 0 else None
        loads.append({"exercise": name, "this_week": cur,
                      "last_week": None if prev == "absent" else prev,
                      "change": change, "note": note})

    rpe = None
    if rpe_pairs:
        rpe = {"avg_session_rpe": round(sum(r for r, *_ in rpe_pairs) / len(rpe_pairs), 1),
               "avg_cap": round(sum(c for _, c, *_ in rpe_pairs) / len(rpe_pairs), 1),
               "over_cap": [{"date": d, "label": lab, "rpe": r, "cap": c}
                            for r, c, d, lab in rpe_pairs if r > c]}
    # SCHEDULE-2: week 1 is the 9/16..9/19 stub, weeks 2+ are Sun..Sat from
    # WEEK2_START. office.week_num_for is the one definition of that.
    program_week = office.week_num_for(start) if start >= office.WEEK1_START else None
    recovery = _recovery(checkins or [], start, min(today, end))
    return {
        "week": {"start": start.isoformat(), "end": end.isoformat(), "program_week": program_week,
                 "through": min(today, end).isoformat(), "partial": end > today},
        "counts": {"planned": sum(1 for s in sessions if s["status"] != "rest"),
                   "done": done, "due": due, "missed": len(missed), "upcoming": upcoming},
        "evening": _evening_counts(evening_plans, by_plan, today),
        "sessions": sessions,
        "recovery": recovery,
        "missed": missed,
        "skipped": skipped,
        "rpe": rpe,
        "loads": loads,
        "adjustments": adjustments,
    }


def _evening_counts(plans, by_plan, today: date) -> dict:
    """EVENING-1: the evening slot's own denominator. Same rules as the
    morning — a skip is not a miss, today is not yet late — reported apart."""
    done = due = upcoming = missed = 0
    for p in plans:
        if p.get("is_skipped"):
            continue
        if any(not l.get("is_skipped") for l in by_plan.get(p["plan_id"], [])):
            done += 1
            due += 1
        elif p["plan_date"] > today:
            upcoming += 1
        elif p["plan_date"] == today:
            pass                      # tonight hasn't happened yet
        else:
            missed += 1
            due += 1
    return {"planned": sum(1 for p in plans if not p.get("is_skipped")),
            "done": done, "due": due, "missed": missed, "upcoming": upcoming}


# ── Loading (read-only) ─────────────────────────────────────────────────────

def load(start: date, end: date, today: date | None = None) -> dict:
    """Read the week from RDS and evaluate it. SELECTs only."""
    from knowledge.db import execute_query
    from artemis.quiet_hours import local_today

    today = today or local_today()
    plans = execute_query(
        "SELECT plan_id, plan_date, slot, session_type, week_num, target_rpe, blocks, "
        "is_skipped, skip_reason "
        "FROM health.plan WHERE plan_date BETWEEN %s AND %s "
        "ORDER BY plan_date, slot DESC", (start, end))
    sql = ("SELECT p.plan_date, sl.plan_id, sl.log_type, sl.exercise, sl.weight_lbs, "
           "sl.reps_done, sl.rpe_actual, sl.is_skipped "
           "FROM health.session_log sl JOIN health.plan p ON p.plan_id = sl.plan_id "
           "WHERE p.plan_date BETWEEN %s AND %s AND sl.logged_via <> 'inferred' "
           "ORDER BY p.plan_date, sl.log_id")
    logs = execute_query(sql, (start, end))
    prior = execute_query(sql, (start - timedelta(days=7), start - timedelta(days=1)))
    checkins = execute_query(
        "SELECT state_date, sleep_hrs, resting_hr FROM health.daily_state "
        "WHERE state_date BETWEEN %s AND %s ORDER BY state_date", (start, end))
    return evaluate([dict(r) for r in plans], [dict(r) for r in logs], [dict(r) for r in prior],
                    start=start, end=end, today=today,
                    checkins=[dict(r) for r in checkins])


# ── Rendering (data only — no advice) ───────────────────────────────────────

def _d(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d:%a} {d.month}/{d.day}"


def _n(x) -> str:
    return "—" if x is None else (f"{x:g}")


def render_lines(ev: dict, through: date | None = None) -> list[str]:
    """The evaluation as short plain lines (the Sunday post, the weekly report).
    `through` overrides the last day named in the span (the Sunday post says
    Wed–Sat: at 08:35 Sunday's own session hasn't happened)."""
    w, c = ev["week"], ev["counts"]
    head = f"Week {w['program_week']}" if w["program_week"] else "Week"
    # A week entirely in the future has nothing "through" yet — name its real end.
    last = through.isoformat() if through else (
        w["through"] if w["partial"] and w["through"] >= w["start"] else w["end"])
    span = f"{_d(w['start'])} – {_d(last)}"
    lines = [f"{head} ({span}{', partial' if w['partial'] else ''}): "
             f"{c['done']} of {c['due']} sessions due done"
             + (f" · {c['upcoming']} still to come" if c["upcoming"] else "")
             + f" · {c['planned']} planned"]
    # EVENING-1: its own line, never folded into the morning's numbers.
    ec = ev.get("evening") or {}
    if ec.get("planned"):
        lines.append(f"Evenings: {ec['done']} of {ec['due']} due done"
                     + (f" · {ec['upcoming']} still to come" if ec.get("upcoming") else "")
                     + f" · {ec['planned']} planned")
    rec = ev.get("recovery")
    if rec:
        bits = []
        if rec["avg_sleep_hrs"] is not None:
            bits.append(f"sleep {rec['avg_sleep_hrs']}h avg over {rec['sleep_nights']} night(s)")
        if rec["avg_resting_hr"] is not None:
            bits.append(f"resting HR {rec['avg_resting_hr']} avg over "
                        f"{rec['resting_hr_days']} day(s)")
        lines.append("Recovery: " + " · ".join(bits))
    else:
        lines.append("Recovery: no sleep or resting HR recorded")
    if ev.get("skipped"):
        lines.append("Skipped: " + ", ".join(
            f"{_d(s['date'])} {s['label']}" + (f" — {s['reason']}" if s["reason"] else "")
            for s in ev["skipped"]))
    lines.append("Missed: " + (", ".join(f"{_d(m['date'])} {m['label']}" for m in ev["missed"])
                               if ev["missed"] else "none"))
    r = ev["rpe"]
    if r:
        over = "; ".join(f"{_d(o['date'])} {_n(o['rpe'])} vs {_n(o['cap'])}" for o in r["over_cap"])
        lines.append(f"Effort: average session RPE {_n(r['avg_session_rpe'])} vs cap "
                     f"{_n(r['avg_cap'])}" + (f" (over cap: {over})" if over else ""))
    else:
        lines.append("Effort: no session RPE logged")
    changed = [l for l in ev["loads"] if l["change"] not in (None, 0)]
    firsts = [l for l in ev["loads"] if l["note"] == "first week"]
    if changed:
        lines.append("Loads vs last week: " + ", ".join(
            f"{l['exercise'].lower()} {l['change']:+g} lb" for l in changed))
    elif ev["loads"]:
        lines.append("Loads vs last week: " + (
            "no change" if not firsts else
            f"{len(firsts)} exercise(s) in their first week, nothing to compare"))
    lines.append("Adjustments: " + ("; ".join(
        f"{_d(a['date'])} {', '.join(a['rules']) or 'adjusted'}" for a in ev["adjustments"])
        if ev["adjustments"] else "none"))
    return lines


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Print one office week's evaluation (read-only).")
    ap.add_argument("--week", type=date.fromisoformat, help="any day in the week (default: today)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    from artemis.quiet_hours import local_today
    s, e = week_of(args.week or local_today())
    result = load(s, e)
    print(json.dumps(result, indent=1, default=str) if args.json else "\n".join(render_lines(result)))
