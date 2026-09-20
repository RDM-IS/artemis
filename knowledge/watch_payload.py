"""WATCH-1 — parse a Health Auto Export payload into rows. Pure, no DB, no deps.

Lives in knowledge/ because the Lambda bundles that directory but not artemis/
(the same reason knowledge/machine_setup.py does).

THE SHAPE HERE IS AN ASSUMPTION until a real payload lands. Health Auto Export
posts roughly:

    {"data": {"metrics":  [{"name": ..., "units": ..., "data": [{...}, ...]}],
              "workouts": [{"name": ..., "start": ..., "end": ..., ...}]}}

so the rules are deliberately forgiving:

  * A sample we can't read is STORED RAW with value NULL, never rejected. A
    payload is only refused if it isn't an object at all.
  * An unknown metric name is stored under its own name, unparsed.
  * Dates keep the payload's own UTC offset. A naive date is left naive for the
    caller to localise — this module never guesses a zone.
  * Numbers arrive as `qty`, or as an Avg/Min/Max object for heart-rate style
    metrics, or per sleep stage. Each recognised stage becomes its own row.
"""

from __future__ import annotations

import re
from datetime import datetime

# Health Auto Export metric name -> our metric key. Everything Ryan asked for
# (2026-09-19): sleep (all stages), resting HR, HRV, weight, active + basal
# energy. Workouts are handled separately.
METRIC_NAMES = {
    "sleep_analysis": "sleep",                      # expands per stage, below
    "resting_heart_rate": "resting_heart_rate",
    "heart_rate_variability": "hrv",
    "walking_heart_rate_average": "walking_heart_rate",
    "weight_body_mass": "weight",
    "body_mass": "weight",
    "active_energy": "active_energy",
    "basal_energy_burned": "basal_energy",
}

# Sleep arrives as one sample with a field per stage.
SLEEP_STAGES = ("asleep", "deep", "rem", "core", "awake", "in_bed", "inBed",
                "total_sleep", "totalSleep", "sleep_start", "sleep_end")
SLEEP_NUMERIC = {"asleep", "deep", "rem", "core", "awake", "in_bed", "inBed",
                 "total_sleep", "totalSleep"}

_DATE_FORMATS = (
    "%Y-%m-%d %H:%M:%S %z",     # 2026-09-25 06:30:00 -0500  (documented form)
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%S.%f%z",
    "%Y-%m-%d",
)


def normalise_metric(name: str) -> str:
    """A payload metric name as we store it. Unknown names pass through,
    lower-cased and underscored, rather than being dropped."""
    key = (name or "").strip()
    if key in METRIC_NAMES:
        return METRIC_NAMES[key]
    slug = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")
    return METRIC_NAMES.get(slug, slug or "unknown")


def parse_date(value) -> datetime | None:
    """A payload timestamp, offset preserved. None when unreadable."""
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    try:                                    # last resort: ISO-8601 with 'Z'
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _number(value):
    """A float from qty / Avg / a bare number. None when there isn't one."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, dict):
        for key in ("qty", "Avg", "avg", "average", "value"):
            got = _number(value.get(key))
            if got is not None:
                return got
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _hr_bounds(value) -> tuple:
    """(avg, max) from a heart-rate style object."""
    if not isinstance(value, dict):
        n = _number(value)
        return n, None
    avg = _number(value.get("Avg") or value.get("avg") or value.get("average"))
    mx = _number(value.get("Max") or value.get("max") or value.get("maximum"))
    return avg, mx


def parse_samples(payload: dict) -> list[dict]:
    """Every metric sample in the payload, as
    {metric, measured_at, value, unit, raw}. Unreadable samples keep
    value=None; nothing is dropped."""
    out: list[dict] = []
    data = (payload or {}).get("data") or {}
    for metric in data.get("metrics") or []:
        if not isinstance(metric, dict):
            continue
        name = normalise_metric(metric.get("name", ""))
        unit = metric.get("units") or metric.get("unit")
        for sample in metric.get("data") or []:
            if not isinstance(sample, dict):
                continue
            when = parse_date(sample.get("date") or sample.get("startDate")
                              or sample.get("start"))
            if when is None:
                # keep it: a sample with no readable date is still evidence
                out.append({"metric": name, "measured_at": None, "value": None,
                            "unit": unit, "raw": sample})
                continue
            if name == "sleep":
                rows = _sleep_rows(sample, when, unit)
                out.extend(rows or [{"metric": "sleep", "measured_at": when,
                                     "value": _number(sample), "unit": unit,
                                     "raw": sample}])
                continue
            out.append({"metric": name, "measured_at": when,
                        "value": _number(sample), "unit": unit, "raw": sample})
    return out


def _sleep_rows(sample: dict, when: datetime, unit) -> list[dict]:
    """One row per sleep stage present. 'sleep_asleep', 'sleep_deep', …"""
    rows = []
    for stage in SLEEP_STAGES:
        if stage not in sample or stage not in SLEEP_NUMERIC:
            continue
        value = _number(sample.get(stage))
        if value is None:
            continue
        key = re.sub(r"(?<!^)(?=[A-Z])", "_", stage).lower()
        rows.append({"metric": f"sleep_{key}", "measured_at": when,
                     "value": value, "unit": unit, "raw": sample})
    return rows


def parse_workouts(payload: dict) -> list[dict]:
    """Every workout, as {kind, started_at, ended_at, duration_sec, hr_avg,
    hr_max, kcal, raw}. A workout with no readable start is skipped — it has
    no natural key — but it is reported by parse_counts as unreadable."""
    out: list[dict] = []
    data = (payload or {}).get("data") or {}
    for w in data.get("workouts") or []:
        if not isinstance(w, dict):
            continue
        start = parse_date(w.get("start") or w.get("startDate"))
        if start is None:
            continue
        end = parse_date(w.get("end") or w.get("endDate"))
        hr_avg, hr_max = _hr_bounds(w.get("heartRateAvg") or w.get("heart_rate_avg")
                                    or w.get("heartRate"))
        _, hr_max2 = _hr_bounds(w.get("heartRateMax") or w.get("heart_rate_max"))
        duration = _number(w.get("duration"))
        if duration is None and end is not None:
            duration = (end - start).total_seconds()
        out.append({
            "kind": (w.get("name") or w.get("workoutActivityType") or "unknown").strip(),
            "started_at": start,
            "ended_at": end,
            "duration_sec": int(duration) if duration is not None else None,
            "hr_avg": int(hr_avg) if hr_avg is not None else None,
            "hr_max": int(hr_max if hr_max is not None else (hr_max2 or 0)) or None,
            "kcal": _number(w.get("activeEnergyBurned") or w.get("activeEnergy")
                            or w.get("totalEnergyBurned")),
            "raw": w,
        })
    return out


def parse_counts(payload: dict) -> dict:
    """What the payload contained — for the response body and the log."""
    data = (payload or {}).get("data") or {}
    samples = parse_samples(payload)
    known = {m for m in METRIC_NAMES.values()}
    return {
        "metrics_in": len(data.get("metrics") or []),
        "workouts_in": len(data.get("workouts") or []),
        "samples": len(samples),
        "undated_samples": sum(1 for s in samples if s["measured_at"] is None),
        "unparsed_metrics": sorted({s["metric"] for s in samples
                                    if s["metric"].split("_")[0] not in
                                    {k.split("_")[0] for k in known} | {"sleep"}}),
    }
