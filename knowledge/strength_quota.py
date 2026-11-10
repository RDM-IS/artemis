"""SESSION-OPEN — the weekly strength quota. ONE read, ONE status.

The question it answers: in the Mon–Sun GOAL week that contains a local date,
on how many distinct days did Ryan do strength work, can he still reach three,
and which day is the real last chance?

STATUS-3 phase 2 (Ryan, 2026-11-10): goal weeks run MONDAY to SUNDAY. Program
weeks (health_office.week_num_for, REPEAT-WEEK) still run Sun..Sat; a program
week starting Sunday S is judged by the goal week S+1..S+7 (`for_program_week`).

FEASIBILITY: the remaining sessions are placed AS LATE AS FEASIBLE on days that
are not travel or away days, never on two consecutive days (and never next to
a day already done). The earliest placed day is the real LAST CHANCE (red);
the day before it is orange. A week with any AWAY day is `not_counted` (HQ
weeks, Ryan's decision 4).

    strength day  a local date with at least one REAL log (logged_via is not
                  'inferred', and not a skipped set) of a STRENGTH_DAY_TYPES
                  type. Planned or ad-hoc (plan_id may be NULL); a DNF round
                  counts — it was trained, it just was not finished.
    required      3.
    days_left     non-travel, non-away days still available in the week from
                  `today`, today included unless today already counts.
    capacity      the most strength days still placeable under the spacing rule.
    placements    the `needed` latest feasible days (ascending).
    status        met | on_track | tight | short | not_counted (see `compute`).

The DATE of a log is its plan row's `plan_date` when it has one (a debrief typed
the next morning still belongs to the day it describes), otherwise the local
date of `started_at`, else `logged_at`, in the ACTIVE timezone (passed in as a
parameter — never interpolated).

ONE-READ-ONE-STATUS: every consumer (the Lambda's /status and /overview, the
box's nag, REPEAT-WEEK's proposal) reads THIS statement and THIS pure function,
so a summary and its detail cannot disagree. The box binds with psycopg2
(`%(name)s`), the Lambda with SQLAlchemy (`:name`); both statements are
generated from one template, as knowledge/cognition.py does for its INSERT.

FAIL-CLOSED-RESOLVERS: a read that fails raises QuotaUnreadable. Nothing here
returns "0 strength days" because the database could not be read — that would
be a plausible wrong answer that fires a nag or proposes a repeat week.
"""

from __future__ import annotations

from datetime import date, timedelta

from knowledge import cycle_days
from knowledge.session_types import (PLANNED_CIRCUIT_ROW_KIND, PLANNED_CIRCUIT_TYPE,
                                     STRENGTH_DAY_TYPES)

#: Ryan's weekly target (SESSION-OPEN).
REQUIRED = 3

#: The first week the quota COUNTS (Ryan, 2026-11-09: "only weeks from the
#: deploy week on") lives in acos.system_state under this key, as an ISO date.
#: It was SET AT the SESSION-OPEN deploy to that Sun..Sat week's Sunday. Since
#: goal weeks became Mon..Sun a MONDAY is the native form; a stored Sunday is
#: read as the Monday after it (the goal week that starts inside the deploy
#: week), so the live value needs no rewrite.
#: Earlier weeks report `not_counted` and never nag or propose a repeat.
#: FAIL-CLOSED: absent, unparseable or not a Sunday → QuotaUnreadable. An unset
#: key must not quietly count every week back to the start of history.
COUNTS_FROM_KEY = "strength_quota_counts_from"

STATUSES: tuple[str, ...] = ("met", "on_track", "tight", "short", "not_counted")
URGENCIES: tuple[str, ...] = ("red", "orange")
#: Day types on which no strength session is ever placed.
BLOCKED_DAY_TYPES = frozenset({"travel"})


class QuotaUnreadable(RuntimeError):
    """The quota could not be read. Never answered with a default."""


_TEMPLATE = """
SELECT DISTINCT x.day
FROM (
    SELECT COALESCE(p.plan_date,
                    (COALESCE(sl.started_at, sl.logged_at) AT TIME ZONE {tz})::date) AS day,
           CASE WHEN p.blocks->>'row_kind' = {row_kind} THEN {row_kind_type}
                ELSE COALESCE(p.session_type, sl.adhoc_session_type) END AS day_type
    FROM health.session_log sl
    LEFT JOIN health.plan p ON p.plan_id = sl.plan_id
    WHERE sl.logged_via IS DISTINCT FROM 'inferred'
      AND NOT COALESCE(sl.is_skipped, false)
) x
WHERE x.day_type = ANY({types})
  AND x.day BETWEEN {week_start} AND {week_end}
ORDER BY x.day
"""

_PARAMS = ("tz", "row_kind", "row_kind_type", "types", "week_start", "week_end")

SQL_PG = _TEMPLATE.format(**{p: f"%({p})s" for p in _PARAMS})
SQL_SA = _TEMPLATE.format(**{p: f":{p}" for p in _PARAMS})


def week_of(day: date) -> tuple[date, date]:
    """The Mon..Sun goal week containing `day`."""
    monday = day - timedelta(days=day.weekday())
    return monday, monday + timedelta(days=6)


def for_program_week(program_week_start: date) -> date:
    """The day to pass for the goal week that judges the Sun..Sat program week
    starting `program_week_start`: the Monday after it."""
    if program_week_start.weekday() != 6:
        raise ValueError(f"a program week starts on a Sunday, got {program_week_start:%a}")
    return program_week_start + timedelta(days=1)


def params(day: date, tz: str) -> dict:
    if not tz:
        raise QuotaUnreadable("no active timezone was passed")
    ws, we = week_of(day)
    return {"tz": tz, "row_kind": PLANNED_CIRCUIT_ROW_KIND,
            "row_kind_type": PLANNED_CIRCUIT_TYPE, "types": list(STRENGTH_DAY_TYPES),
            "week_start": ws, "week_end": we}


def parse_counts_from(value) -> date:
    """The stored COUNTS_FROM_KEY value, or QuotaUnreadable. Pure."""
    if value is None or not str(value).strip():
        raise QuotaUnreadable(f"acos.system_state[{COUNTS_FROM_KEY!r}] is not set "
                              "(set it at deploy to the deploy week's Sunday)")
    try:
        d = date.fromisoformat(str(value).strip().strip('"'))
    except ValueError as exc:
        raise QuotaUnreadable(f"{COUNTS_FROM_KEY} is not a date: {value!r}") from exc
    if d.weekday() == 6:                     # the SESSION-OPEN-era Sunday form
        return d + timedelta(days=1)
    if d.weekday() != 0:
        raise QuotaUnreadable(f"{COUNTS_FROM_KEY} must be a Monday (or a legacy Sunday), "
                              f"got {d:%a} {d}")
    return d


_COUNTS_FROM_PG = "SELECT value FROM acos.system_state WHERE key = %(k)s"
_COUNTS_FROM_SA = "SELECT value FROM acos.system_state WHERE key = :k"


def _value_of(rows):
    rows = list(rows or [])
    if not rows:
        return None
    r = rows[0]
    return r["value"] if isinstance(r, dict) or hasattr(r, "keys") else r[0]


def place(candidates, done, needed: int) -> list[date]:
    """Greedy from the LATEST day: the maximal set of non-adjacent days, none
    next to a day in `done`, returned descending. Greedy from one end is
    optimal for this rule, so len(result) is the true capacity. Pure."""
    taken = set(done)
    picked: list[date] = []
    for d in sorted(set(candidates), reverse=True):
        if (d - timedelta(days=1)) in taken or (d + timedelta(days=1)) in taken or d in taken:
            continue
        picked.append(d)
        taken.add(d)
    return picked


def compute(day: date, strength_days, today: date, *, counts_from: date,
            required: int = REQUIRED, days: dict | None = None) -> dict:
    """Pure. `strength_days`: the distinct local dates the read returned.
    `days`: knowledge.cycle_days.resolve_days for the week -- which days are
    travel or away. None means "the pattern alone" and is for tests only;
    the readers always pass the resolved week.

    status
      met       already at the target.
      on_track  more placeable days than strength days needed.
      tight     exactly as many placeable days as needed: every placement is
                a must.
      short     cannot be reached any more (or the week ended below it).
      not_counted  a week before `counts_from`, or a week with an away day:
                the days are reported, nothing is owed, nothing nags, nothing
                proposes a repeat, and nothing is coloured.
    """
    ws, we = week_of(day)
    if days is None:
        days = cycle_days.resolve_days(ws, we, [])
    done = sorted({d for d in strength_days if ws <= d <= we})
    needed = max(0, required - len(done))
    finished = today > we
    blocked = sorted(d for d, info in days.items()
                     if ws <= d <= we and (info["day_type"] in BLOCKED_DAY_TYPES or info["away"]))
    away = any(info["away"] for d, info in days.items() if ws <= d <= we)
    first = max(today, ws)
    open_days = [] if finished else [
        first + timedelta(days=i) for i in range((we - first).days + 1)
        if (first + timedelta(days=i)) not in blocked and (first + timedelta(days=i)) not in done]
    days_left = len(open_days)
    picked = place(open_days, done, needed if needed else 0) if needed else []
    capacity = len(place(open_days, done, len(open_days))) if open_days else 0
    placements = sorted(picked[:needed]) if needed else []
    counted = ws >= counts_from and not away
    if not counted:
        status = "not_counted"
    elif needed == 0:
        status = "met"
    elif needed > capacity:
        status = "short"
    elif needed == capacity:
        status = "tight"
    else:
        status = "on_track"
    urgency: dict[str, str] = {}
    last_chance = placements[0] if placements else None
    if counted and needed and last_chance is not None:
        urgency[last_chance.isoformat()] = "red"
        before = last_chance - timedelta(days=1)
        # Orange only where a strength day could actually go: open, and not
        # next to a day already done (the red day itself is the alternative).
        if (before >= first and before in open_days
                and (before - timedelta(days=1)) not in done):
            urgency[before.isoformat()] = "orange"
    return {"week_start": ws.isoformat(), "week_end": we.isoformat(),
            "strength_days": [d.isoformat() for d in done], "count": len(done),
            "required": required, "needed": needed, "days_left": days_left,
            "capacity": capacity, "finished": finished, "counted": counted, "status": status,
            "away": away, "blocked_days": [d.isoformat() for d in blocked],
            "placements": [d.isoformat() for d in placements],
            "last_chance": last_chance.isoformat() if last_chance else None,
            "urgency": urgency,
            # The nag's condition, stated once so the box and any later reader
            # cannot each write their own version of it: TODAY is the real last
            # chance for the first remaining session.
            "nag_due": counted and needed > 0 and last_chance == today,
            # REPEAT-WEEK's condition: a finished, counted week below target.
            "repeat_due": counted and finished and needed > 0}


def _days_from(rows) -> list[date]:
    out = []
    for r in rows or []:
        v = r["day"] if isinstance(r, dict) or hasattr(r, "keys") else r[0]
        if not isinstance(v, date):
            raise QuotaUnreadable(f"the read returned a non-date day: {v!r}")
        out.append(v)
    return out


def read_pg(day: date, *, tz: str, today: date, execute=None) -> dict:
    """The box: psycopg2 via knowledge.db.execute_query (or `execute`, for tests)."""
    try:
        if execute is None:
            from knowledge.db import execute_query as execute
        counts_from = parse_counts_from(_value_of(execute(_COUNTS_FROM_PG, {"k": COUNTS_FROM_KEY})))
        rows = execute(SQL_PG, params(day, tz))
        ws, we = week_of(day)
        days = cycle_days.read_pg(ws, we, execute)
        return compute(day, _days_from(rows), today, counts_from=counts_from, days=days)
    except QuotaUnreadable:
        raise
    except Exception as exc:                                    # noqa: BLE001
        raise QuotaUnreadable(f"could not read the strength quota: {exc}") from exc


def read_sa(db, day: date, *, tz: str, today: date) -> dict:
    """The Lambda: a SQLAlchemy Session."""
    try:
        from sqlalchemy import text
        counts_from = parse_counts_from(_value_of(
            db.execute(text(_COUNTS_FROM_SA), {"k": COUNTS_FROM_KEY}).mappings().all()))
        rows = db.execute(text(SQL_SA), params(day, tz)).mappings().all()
        ws, we = week_of(day)
        days = cycle_days.read_sa(db, ws, we)
        return compute(day, _days_from(rows), today, counts_from=counts_from, days=days)
    except QuotaUnreadable:
        raise
    except Exception as exc:                                    # noqa: BLE001
        raise QuotaUnreadable(f"could not read the strength quota: {exc}") from exc
