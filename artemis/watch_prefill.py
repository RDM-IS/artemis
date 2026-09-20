"""WATCH-1 — pre-fill the morning check-in from last night's watch samples.

Three values only: sleep hours, resting heart rate and weight. Ryan still
types energy and soreness; nothing here touches them.

The rules, in order of how much they matter:

  1. **Manual always wins.** `health.daily_state` carries a per-field source
     marker (migration 036). A field written by Ryan is `manual` and is FINAL
     for that date — a watch sample arriving later, in any order, never
     overwrites it. Only a `watch` field or an empty one is filled.
  2. **Nothing is invented.** A value that hasn't synced is simply absent, and
     the reply says which. No interpolation, no carrying yesterday's number
     forward, no estimate from a neighbouring metric.
  3. **"Last night" is local.** The window comes from the ACTIVE timezone
     (CYCLE-1 decides when the day starts), never from UTC.

SLEEP IS PROVISIONAL. As of 2026-09-20 no sleep sample has ever arrived —
sleep tracking was off on the watch until tonight (9/20 → 9/21). SLEEP_METRICS
below is the ASSUMED shape, in priority order. It must be checked against the
first real night and corrected; `sleep_source_metric` in the result says which
metric a number actually came from, so the check is one query.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta

logger = logging.getLogger(__name__)

# Which stored metric to believe for "hours slept", best first. ASSUMED — see
# the module docstring. A total beats a sum of stages; in-bed is NOT sleep.
SLEEP_METRICS = ("sleep_total_sleep", "sleep_asleep")
# Summed only when no single total exists.
SLEEP_STAGE_SUM = ("sleep_deep", "sleep_rem", "sleep_core")
SLEEP_NOT_SLEEP = ("sleep_in_bed", "sleep_awake")

# How stale a sample may be and still count as "last night".
RHR_WINDOW_HOURS = 36
WEIGHT_WINDOW_HOURS = 36        # Ryan weighs sporadically; an older reading is
                                # NOT last night's and is left out rather than
                                # presented as today's.


def _window(day: date, hours: int, tz) -> tuple[datetime, datetime]:
    """(start, end) around the local morning of `day`."""
    end = datetime.combine(day, time(12, 0), tzinfo=tz)
    return end - timedelta(hours=hours), end


def sleep_hours(rows: list[dict]) -> tuple[float | None, str | None]:
    """(hours, which metric it came from) from one night's sleep samples."""
    by_metric = {r["metric"]: float(r["value"]) for r in rows
                 if r.get("value") is not None}
    for metric in SLEEP_METRICS:
        if metric in by_metric:
            return round(by_metric[metric], 2), metric
    stages = {m: by_metric[m] for m in SLEEP_STAGE_SUM if m in by_metric}
    if stages:
        return round(sum(stages.values()), 2), "+".join(sorted(stages))
    return None, None


def read_watch_values(day: date, *, tz=None, query=None) -> dict:
    """What the watch has for the morning of `day`. Read-only.

    Returns {sleep_hrs, sleep_source_metric, resting_hr, weight_lbs, missing}
    where `missing` names the fields with no sample — the reply says these out
    loud rather than quietly omitting them.
    """
    if query is None:
        from knowledge.db import execute_query as query
    if tz is None:
        from artemis.quiet_hours import local_tz
        tz = local_tz()

    out: dict = {"sleep_hrs": None, "sleep_source_metric": None,
                 "resting_hr": None, "weight_lbs": None, "missing": []}

    # Sleep: samples stamped on the morning of `day` (assumed), else the
    # evening before. Both are inside a 36 h window ending at local noon.
    lo, hi = _window(day, 36, tz)
    sleep_rows = query(
        "SELECT metric, value, measured_at FROM health.watch_sample "
        "WHERE metric LIKE 'sleep%%' AND measured_at BETWEEN %s AND %s "
        "ORDER BY measured_at DESC", (lo, hi)) or []
    # keep only the most recent sample per metric
    newest: dict[str, dict] = {}
    for r in sleep_rows:
        newest.setdefault(r["metric"], dict(r))
    hours, which = sleep_hours(list(newest.values()))
    out["sleep_hrs"], out["sleep_source_metric"] = hours, which

    lo, hi = _window(day, RHR_WINDOW_HOURS, tz)
    rhr = query(
        "SELECT value FROM health.watch_sample WHERE metric = 'resting_heart_rate' "
        "AND measured_at BETWEEN %s AND %s AND value IS NOT NULL "
        "ORDER BY measured_at DESC LIMIT 1", (lo, hi))
    if rhr:
        out["resting_hr"] = int(round(float(rhr[0]["value"])))

    lo, hi = _window(day, WEIGHT_WINDOW_HOURS, tz)
    wt = query(
        "SELECT value FROM health.watch_sample WHERE metric = 'weight' "
        "AND measured_at BETWEEN %s AND %s AND value IS NOT NULL "
        "ORDER BY measured_at DESC LIMIT 1", (lo, hi))
    if wt:
        out["weight_lbs"] = round(float(wt[0]["value"]), 1)

    for field, label in (("sleep_hrs", "sleep"), ("resting_hr", "resting HR"),
                         ("weight_lbs", "weight")):
        if out[field] is None:
            out["missing"].append(label)
    return out


# health.daily_state: fill a field only when it is empty or already `watch`.
# A `manual` field is Ryan's and is never touched, whatever the arrival order.
_UPSERT = """
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


def write_prefill(cur, day: date, values: dict) -> bool:
    """Write the watch values for `day`. Returns True when anything was set.
    Manual fields are left exactly as they are."""
    if not any(values.get(k) is not None
               for k in ("sleep_hrs", "resting_hr", "weight_lbs")):
        return False
    cur.execute(_UPSERT, {"day": day, "sleep": values.get("sleep_hrs"),
                          "rhr": values.get("resting_hr"),
                          "weight": values.get("weight_lbs")})
    return True


def describe(values: dict) -> str | None:
    """One line for the check-in reply: what came from the watch, and what
    hasn't synced. None when the watch supplied nothing at all."""
    got = []
    if values.get("sleep_hrs") is not None:
        got.append(f"sleep {values['sleep_hrs']:g}h")
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


def run(day: date | None = None) -> dict:
    """Read last night's samples and write them. Returns the values, with
    `written` saying whether anything landed. Safe to run repeatedly."""
    from knowledge.db import get_connection
    from artemis.quiet_hours import local_today

    day = day or local_today()
    values = read_watch_values(day)
    written = False
    try:
        with get_connection() as conn:
            cur = conn.cursor()
            written = write_prefill(cur, day, values)
            conn.commit()
    except Exception:
        logger.exception("watch pre-fill failed for %s — the check-in still works typed", day)
    values["written"] = written
    logger.info("Watch pre-fill %s: %s", day, describe(values) or "nothing available")
    return values
