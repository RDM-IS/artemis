"""SESSION-OPEN — the weekly strength quota. ONE read, ONE status.

The question it answers: in the Sun–Sat week that contains a local date, on how
many distinct days did Ryan do strength work, and can he still reach three?

    strength day  a local date with at least one REAL log (logged_via is not
                  'inferred', and not a skipped set) of a STRENGTH_DAY_TYPES
                  type. Planned or ad-hoc (plan_id may be NULL); a DNF round
                  counts — it was trained, it just was not finished.
    required      3.
    days_left     days still available in the week from `today`, today
                  included unless today already counts.
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

from knowledge.session_types import (PLANNED_CIRCUIT_ROW_KIND, PLANNED_CIRCUIT_TYPE,
                                     STRENGTH_DAY_TYPES)

#: Ryan's weekly target (SESSION-OPEN).
REQUIRED = 3

#: The first Sun..Sat week the quota COUNTS (Ryan, 2026-11-09: "only weeks from
#: the deploy week on") lives in acos.system_state under this key, as an ISO
#: date that must be a Sunday. It is SET AT DEPLOY to the deploy week's Sunday —
#: not a constant, because the deploy date is not known when the code is written.
#: Earlier weeks report `not_counted` and never nag or propose a repeat.
#: FAIL-CLOSED: absent, unparseable or not a Sunday → QuotaUnreadable. An unset
#: key must not quietly count every week back to the start of history.
COUNTS_FROM_KEY = "strength_quota_counts_from"

STATUSES: tuple[str, ...] = ("met", "on_track", "tight", "short", "not_counted")


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
    """The Sun..Sat week containing `day`."""
    sunday = day - timedelta(days=(day.weekday() + 1) % 7)
    return sunday, sunday + timedelta(days=6)


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
    if d.weekday() != 6:
        raise QuotaUnreadable(f"{COUNTS_FROM_KEY} must be a Sunday, got {d:%a} {d}")
    return d


_COUNTS_FROM_PG = "SELECT value FROM acos.system_state WHERE key = %(k)s"
_COUNTS_FROM_SA = "SELECT value FROM acos.system_state WHERE key = :k"


def _value_of(rows):
    rows = list(rows or [])
    if not rows:
        return None
    r = rows[0]
    return r["value"] if isinstance(r, dict) or hasattr(r, "keys") else r[0]


def compute(day: date, strength_days, today: date, *, counts_from: date,
            required: int = REQUIRED) -> dict:
    """Pure. `strength_days`: the distinct local dates the read returned.

    status
      met       already at the target.
      on_track  more days left than strength days needed.
      tight     exactly as many days left as needed — every remaining day must
                be a strength day. The nag's condition (needed >= days_left)
                is `tight` or `short` with days left.
      short     cannot be reached any more (or the week ended below it).
      not_counted  a week before `counts_from`: the days are reported, nothing
                is owed, and nothing nags or proposes a repeat.
    """
    ws, we = week_of(day)
    days = sorted({d for d in strength_days if ws <= d <= we})
    needed = max(0, required - len(days))
    finished = today > we
    if finished:
        days_left = 0
    elif today < ws:
        days_left = 7
    else:
        days_left = (we - today).days + 1 - (1 if today in days else 0)
    counted = ws >= counts_from
    if not counted:
        status = "not_counted"
    elif needed == 0:
        status = "met"
    elif needed > days_left:
        status = "short"
    elif needed == days_left:
        status = "tight"
    else:
        status = "on_track"
    return {"week_start": ws.isoformat(), "week_end": we.isoformat(),
            "strength_days": [d.isoformat() for d in days], "count": len(days),
            "required": required, "needed": needed, "days_left": days_left,
            "finished": finished, "counted": counted, "status": status,
            # The nag's condition, stated once so the box and any later reader
            # cannot each write their own version of it.
            "nag_due": counted and needed > 0 and days_left > 0 and needed >= days_left,
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
        return compute(day, _days_from(rows), today, counts_from=counts_from)
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
        return compute(day, _days_from(rows), today, counts_from=counts_from)
    except QuotaUnreadable:
        raise
    except Exception as exc:                                    # noqa: BLE001
        raise QuotaUnreadable(f"could not read the strength quota: {exc}") from exc
