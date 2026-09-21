"""WATCH-1 — which watch values belong to a given morning, and writing them.

Shared by the box (the wake job) and the Lambda (every ingest re-runs it for
today), so it lives in knowledge/ and talks to a plain DB-API cursor: psycopg2
on the box, the SQLAlchemy session's raw connection in the Lambda. Tuple and
dict cursors both work.

The rule that matters most (Ryan, 2026-09-21): **no value is presented as
today's unless it was measured today** — or last night, for sleep. There is no
lookback window. A value that hasn't synced is missing and is reported as
missing; yesterday's number is never carried forward.

  * Sleep: the night whose `sleepEnd` falls in (D-1 12:00, D 12:00] local.
    A record with no readable sleepEnd counts only when its own local_date is
    D (the export labels a night with local midnight of the wake day).
  * Resting HR, weight: a sample whose local_date is D.

Manual always wins: a field Ryan typed is `manual` and FINAL for that date. The
pre-fill fills a field only when it is empty or already `watch`.

Watch values are recorded, never acted on — nothing here (or downstream of
daily_state) adjusts the plan. Only a typed check-in does.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from knowledge.watch_payload import sleep_bounds

SLEEP_TOTAL = "sleep_total_sleep"
# Summed only when the record has no total. `asleep` is the unspecified stage
# (a non-staged source puts everything there); it is only stored when > 0.
SLEEP_STAGE_SUM = ("sleep_asleep", "sleep_core", "sleep_deep", "sleep_rem")
# Not sleep: in-bed time and time awake are never counted as hours slept.
SLEEP_NOT_SLEEP = ("sleep_in_bed", "sleep_awake")


def _dicts(cur) -> list[dict]:
    rows = cur.fetchall()
    if rows and isinstance(rows[0], dict):
        return [dict(r) for r in rows]
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in rows]


def night_window(day: date, tz) -> tuple[datetime, datetime]:
    """(exclusive start, inclusive end) of 'last night' for the morning of `day`."""
    end = datetime.combine(day, time(12, 0), tzinfo=tz)
    return end - timedelta(days=1), end


def pick_night(rows: list[dict], day: date, tz) -> list[dict]:
    """The sleep rows of the one night that belongs to the morning of `day`.

    Rows sharing a measured_at are one night's record. Several candidates
    (unusual — a separate nap record) resolve to the latest sleepEnd."""
    lo, hi = night_window(day, tz)
    nights: dict = {}
    for r in rows:
        nights.setdefault(r["measured_at"], []).append(r)
    best, best_end = None, None
    for group in nights.values():
        _, end = sleep_bounds(group[0].get("raw"))
        if end is not None and end.tzinfo is not None:
            if not (lo < end <= hi):
                continue
        elif group[0].get("local_date") != day:
            continue
        if best is None or (end is not None and (best_end is None or end > best_end)):
            best, best_end = group, end
    return best or []


def sleep_hours(rows: list[dict]) -> tuple[float | None, str | None]:
    """(hours to one decimal, which metric(s) it came from) for one night."""
    by_metric = {r["metric"]: float(r["value"]) for r in rows
                 if r.get("value") is not None}
    if by_metric.get(SLEEP_TOTAL):
        return round(by_metric[SLEEP_TOTAL], 1), SLEEP_TOTAL
    stages = {m: by_metric[m] for m in SLEEP_STAGE_SUM if by_metric.get(m)}
    if stages:
        return round(sum(stages.values()), 1), "+".join(sorted(stages))
    return None, None


def read_values(cur, day: date, tz) -> dict:
    """What the watch has for the morning of `day`. Read-only.

    {sleep_hrs, sleep_source_metric, sleep_start, sleep_end, resting_hr,
     weight_lbs, missing} — `missing` names each field with no sample, so the
    posts say so rather than quietly omitting it."""
    out: dict = {"sleep_hrs": None, "sleep_source_metric": None,
                 "sleep_start": None, "sleep_end": None,
                 "resting_hr": None, "weight_lbs": None, "missing": []}

    # Candidates labelled the day before through the day itself; pick_night
    # decides by sleepEnd.
    cur.execute(
        "SELECT metric, value, measured_at, local_date, raw FROM health.watch_sample "
        "WHERE metric LIKE 'sleep%%' AND local_date BETWEEN %s AND %s",
        (day - timedelta(days=1), day))
    night = pick_night(_dicts(cur), day, tz)
    if night:
        out["sleep_hrs"], out["sleep_source_metric"] = sleep_hours(night)
        if out["sleep_hrs"] is not None:
            out["sleep_start"], out["sleep_end"] = sleep_bounds(night[0].get("raw"))

    # Measured TODAY only. The watch is preferred over the phone for resting HR.
    cur.execute(
        "SELECT value FROM health.watch_sample WHERE metric = 'resting_heart_rate' "
        "AND local_date = %s AND value IS NOT NULL "
        "ORDER BY (device = 'watch') DESC NULLS LAST, measured_at DESC LIMIT 1", (day,))
    got = _dicts(cur)
    if got:
        out["resting_hr"] = int(round(float(got[0]["value"])))

    cur.execute(
        "SELECT value FROM health.watch_sample WHERE metric = 'weight' "
        "AND local_date = %s AND value IS NOT NULL "
        "ORDER BY measured_at DESC LIMIT 1", (day,))
    got = _dicts(cur)
    if got:
        out["weight_lbs"] = round(float(got[0]["value"]), 1)

    for field, label in (("sleep_hrs", "sleep"), ("resting_hr", "resting HR"),
                         ("weight_lbs", "weight")):
        if out[field] is None:
            out["missing"].append(label)
    return out


# health.daily_state: fill a field only when it is empty or already `watch`.
# A `manual` field is Ryan's and is never touched, whatever the arrival order.
UPSERT = """
INSERT INTO health.daily_state
    (state_date, sleep_hrs, resting_hr, weight_lbs,
     sleep_source, resting_hr_source, weight_source)
VALUES (%(day)s, %(sleep)s, %(rhr)s, %(weight)s,
        CASE WHEN %(sleep)s  IS NULL THEN NULL ELSE 'watch' END,
        CASE WHEN %(rhr)s    IS NULL THEN NULL ELSE 'watch' END,
        CASE WHEN %(weight)s IS NULL THEN NULL ELSE 'watch' END)
ON CONFLICT (state_date) DO UPDATE SET
    sleep_hrs = CASE WHEN health.daily_state.sleep_source = 'manual'
                     THEN health.daily_state.sleep_hrs
                     ELSE COALESCE(EXCLUDED.sleep_hrs, health.daily_state.sleep_hrs) END,
    sleep_source = CASE WHEN health.daily_state.sleep_source = 'manual' THEN 'manual'
                        WHEN EXCLUDED.sleep_hrs IS NOT NULL THEN 'watch'
                        ELSE health.daily_state.sleep_source END,
    resting_hr = CASE WHEN health.daily_state.resting_hr_source = 'manual'
                      THEN health.daily_state.resting_hr
                      ELSE COALESCE(EXCLUDED.resting_hr, health.daily_state.resting_hr) END,
    resting_hr_source = CASE WHEN health.daily_state.resting_hr_source = 'manual' THEN 'manual'
                             WHEN EXCLUDED.resting_hr IS NOT NULL THEN 'watch'
                             ELSE health.daily_state.resting_hr_source END,
    weight_lbs = CASE WHEN health.daily_state.weight_source = 'manual'
                      THEN health.daily_state.weight_lbs
                      ELSE COALESCE(EXCLUDED.weight_lbs, health.daily_state.weight_lbs) END,
    weight_source = CASE WHEN health.daily_state.weight_source = 'manual' THEN 'manual'
                         WHEN EXCLUDED.weight_lbs IS NOT NULL THEN 'watch'
                         ELSE health.daily_state.weight_source END
"""


def write(cur, day: date, values: dict) -> bool:
    """Write the watch values for `day`. True when anything was set. Manual
    fields are left exactly as they are."""
    if not any(values.get(k) is not None
               for k in ("sleep_hrs", "resting_hr", "weight_lbs")):
        return False
    cur.execute(UPSERT, {"day": day, "sleep": values.get("sleep_hrs"),
                         "rhr": values.get("resting_hr"),
                         "weight": values.get("weight_lbs")})
    return True


def prefill_day(cur, day: date, tz) -> dict:
    """Read and write in one go. Returns the values plus `written`. The caller
    owns the transaction."""
    values = read_values(cur, day, tz)
    values["written"] = write(cur, day, values)
    return values


def audit_view(values: dict) -> dict:
    """The values as an audit_log metadata fragment (sleep_source_metric kept,
    so which metric a number came from is one query away)."""
    return {k: (v.isoformat() if isinstance(v, datetime) else v)
            for k, v in values.items()}


def _clock(dt: datetime | None, tz) -> str | None:
    if dt is None or dt.tzinfo is None:
        return None
    return dt.astimezone(tz).strftime("%H:%M")


def describe(values: dict, tz=None) -> str | None:
    """One line: what came from the watch, and what hasn't synced. None when
    there is nothing to say. States values only — never a judgement."""
    got = []
    if values.get("sleep_hrs") is not None:
        span = ""
        if tz is not None:
            a, b = _clock(values.get("sleep_start"), tz), _clock(values.get("sleep_end"), tz)
            if a and b:
                span = f" ({a}–{b})"
        got.append(f"sleep {values['sleep_hrs']:g}h{span}")
    if values.get("resting_hr") is not None:
        got.append(f"resting HR {values['resting_hr']}")
    if values.get("weight_lbs") is not None:
        got.append(f"weight {values['weight_lbs']:g}")
    missing = values.get("missing") or []
    if not got:
        return f"No watch data yet — {', '.join(missing)} not synced." if missing else None
    line = "From the watch: " + ", ".join(got) + "."
    if missing:
        line += f" Not synced: {', '.join(missing)}."
    return line
