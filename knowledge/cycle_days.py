"""The 14-day pay-period PATTERN, shared by the box and the Lambda.

`artemis.cycle` owns everything the schedule does with it (segments, wake,
business hours, the per-day override lookup); this module owns only the
facts both sides need, so there is ONE copy of the pattern
(SAME-FACT-MANY-PLACES). STATUS-3 phase 2 moved it here: the strength quota's
feasibility runs in the Lambda too and must know which days are travel days.

`resolve_days` is the RANGE resolver over `acos.cycle_day_overrides`. It keeps
`artemis.cycle.override_for`'s rule -- the NEWEST live override covering a
date decides that date's day_type (when it names a pattern type) and its
location (when it names one) -- and `away.load_stays`'s rule that ANY live
`away` override covering a date makes it an away day (`away` never resolves
through the pattern; see artemis/away.py).

FAIL-CLOSED-RESOLVERS: `read_pg` / `read_sa` raise `CycleUnreadable` when the
overrides or the anchor cannot be read. Nothing answers "no overrides" because
the read failed.
"""

from __future__ import annotations

from datetime import date, timedelta

CYCLE_LEN = 14
DEFAULT_ANCHOR = date(2026, 9, 20)          # Sun — week 1, day 1 (confirmed)
ANCHOR_KEY = "cycle_anchor"
AWAY = "away"

# Position 0..13 from the anchor: 8 msp_work, 3 msp_home, 2 wi, 1 travel.
# SUNDAY-TRAVEL (Ryan, 2026-11-10): the drive back to MSP is the wk-2 SUNDAY
# (pos 7) -- "Monday doesn't leave enough time to prepare for work Tuesday" --
# and the wk-2 Monday (pos 8) is a day at home: off work, a training day.
DAY_TYPES: tuple[str, ...] = (
    "msp_home", "msp_work", "msp_work", "msp_work", "msp_work", "wi", "wi",          # wk 1
    "travel", "msp_home", "msp_work", "msp_work", "msp_work", "msp_work", "msp_home",  # wk 2
)

DAY_LOCATIONS: dict[int, str] = {
    0: "msp_home", 1: "office", 2: "office", 3: "office", 4: "office",
    # wk-1 Friday: the DEFAULT is a train week -- he wakes at MSP home, trains
    # there, and takes the Amtrak after ~08:00. A car week (drove Thursday
    # night) is an override or a placement that moves the day to Richfield.
    5: "msp_home",
    6: "brown_deer",
    7: "brown_deer",         # travel Sunday: wakes at Brown Deer, leaves 11:00
    8: "msp_home",           # wk-2 Monday: home all day
    9: "office", 10: "office", 11: "office", 12: "office", 13: "msp_home",
}


class CycleUnreadable(RuntimeError):
    """The overrides or the anchor could not be read. Never answered with the
    bare pattern."""


def cycle_pos(d: date, anchor_date: date = DEFAULT_ANCHOR) -> int:
    return (d - anchor_date).days % CYCLE_LEN


def parse_anchor(value) -> date:
    """acos.system_state[cycle_anchor], or DEFAULT_ANCHOR when it is unset.
    Unset is a real answer (the built-in anchor is the confirmed one); a value
    that is SET but unparseable raises."""
    if value is None or not str(value).strip():
        return DEFAULT_ANCHOR
    try:
        return date.fromisoformat(str(value).strip().strip('"')[:10])
    except ValueError as exc:
        raise CycleUnreadable(f"{ANCHOR_KEY} is not a date: {value!r}") from exc


def _g(r, k, i):
    return r[k] if isinstance(r, dict) or hasattr(r, "keys") else r[i]


def resolve_days(start: date, end: date, overrides, anchor_date: date = DEFAULT_ANCHOR) -> dict:
    """{date: {"pos", "day_type", "location", "away", "override_id"}} for
    start..end. Pure. `overrides`: live rows (override_id, start_date,
    end_date, day_type, location) ordered NEWEST FIRST."""
    rows = list(overrides or [])
    out = {}
    d = start
    while d <= end:
        p = cycle_pos(d, anchor_date)
        cover = [r for r in rows if _g(r, "start_date", 1) <= d <= _g(r, "end_date", 2)]
        newest = cover[0] if cover else None
        dt, loc = DAY_TYPES[p], DAY_LOCATIONS[p]
        if newest is not None:
            if _g(newest, "day_type", 3) in DAY_TYPES:
                dt = _g(newest, "day_type", 3)
            if _g(newest, "location", 4):
                loc = _g(newest, "location", 4)
        out[d] = {"pos": p, "day_type": dt, "location": loc,
                  "away": any(_g(r, "day_type", 3) == AWAY for r in cover),
                  "override_id": _g(newest, "override_id", 0) if newest is not None else None}
        d += timedelta(days=1)
    return out


_OVERRIDES = ("SELECT override_id, start_date, end_date, day_type, location "
              "FROM acos.cycle_day_overrides "
              "WHERE revoked_at IS NULL AND start_date <= {end} AND end_date >= {start} "
              "ORDER BY created_at DESC, override_id DESC")
_ANCHOR = "SELECT value FROM acos.system_state WHERE key = {k}"
OVERRIDES_PG = _OVERRIDES.format(start="%(start)s", end="%(end)s")
OVERRIDES_SA = _OVERRIDES.format(start=":start", end=":end")
ANCHOR_PG = _ANCHOR.format(k="%(k)s")
ANCHOR_SA = _ANCHOR.format(k=":k")


def _first_value(rows):
    rows = list(rows or [])
    return _g(rows[0], "value", 0) if rows else None


def read_pg(start: date, end: date, execute) -> dict:
    """The box: `execute(sql, params) -> rows` (psycopg2)."""
    try:
        anchor_date = parse_anchor(_first_value(execute(ANCHOR_PG, {"k": ANCHOR_KEY})))
        rows = execute(OVERRIDES_PG, {"start": start, "end": end})
        return resolve_days(start, end, rows, anchor_date)
    except CycleUnreadable:
        raise
    except Exception as exc:                                    # noqa: BLE001
        raise CycleUnreadable(f"could not read the cycle overrides: {exc}") from exc


def read_sa(db, start: date, end: date) -> dict:
    """The Lambda: a SQLAlchemy Session."""
    try:
        from sqlalchemy import text
        anchor_date = parse_anchor(_first_value(
            db.execute(text(ANCHOR_SA), {"k": ANCHOR_KEY}).mappings().all()))
        rows = db.execute(text(OVERRIDES_SA), {"start": start, "end": end}).mappings().all()
        return resolve_days(start, end, rows, anchor_date)
    except CycleUnreadable:
        raise
    except Exception as exc:                                    # noqa: BLE001
        raise CycleUnreadable(f"could not read the cycle overrides: {exc}") from exc
