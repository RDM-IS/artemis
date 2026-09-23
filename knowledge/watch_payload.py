"""WATCH-1 — parse a Health Auto Export payload into rows. Pure, no DB, no deps.

Lives in knowledge/ because the Lambda bundles that directory but not artemis/
(the same reason knowledge/machine_setup.py does).

Metrics and sleep are now OBSERVED (real exports 2026-09-18..21; sleep shape
below at SLEEP_STAGES). WORKOUTS ARE STILL AN ASSUMPTION — none has arrived.
Health Auto Export posts roughly:

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
from datetime import datetime, timedelta, timezone

_UTC = timezone.utc

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
    "heart_rate": "heart_rate",
}

# WATCH-2 (Ryan, 2026-09-20): the endpoint is DEFENSIVE. One wrong toggle in
# the app must not be able to write 300k rows, so only these metrics are
# stored. Everything else is counted and NAMED in the response — nothing is
# silently dropped — but no row is written for it.
#
# Real data (2026-09-18..20) showed ~3,250 samples/day, of which ~2,600 were
# metrics on nobody's list: step_count, walking gait, stair speeds,
# physical_effort, apple_stand_*, respiratory_rate, cardio_recovery.
WANTED = {
    "resting_heart_rate", "hrv", "walking_heart_rate", "weight",
    "active_energy", "basal_energy",
}
SLEEP_PREFIX = "sleep"
# Minute-level HR: high volume (~1,100/day) and only useful inside a session.
# Held out of watch_sample; see migration 037 for the compact store.
HEART_RATE = "heart_rate"

# STEPS-HOURLY (Ryan, 2026-09-21): stored as HOURLY totals in
# health.watch_hourly (migration 041), never in watch_sample. They leave the
# ignore list; everything else on it stays ignored and named.
HOURLY = {"step_count", "apple_exercise_time"}


# The payload's own "source" field, decoded (Ryan, 2026-09-20).
DEVICE_DECODE = {
    "RAW": "watch",          # Ryan's Apple Watch
    "RIP": "iphone",         # Ryan's iPhone
    "RAW|RIP": "watch+iphone",
    "RIP|RAW": "watch+iphone",
}
# Where a metric can come from either device, the WATCH is the better source.
# Weight comes from the scale via either and needs no preference.
PREFER_WATCH = {"resting_heart_rate", "hrv", "heart_rate"}
PREFER_WATCH_PREFIXES = ("sleep",)


def decode_device(raw: str | None) -> str | None:
    """'RAW' -> 'watch', 'RIP' -> 'iphone', 'RAW|RIP' -> 'watch+iphone'.

    An unrecognised string returns None and the raw value is still stored, so
    a new device name shows up as data rather than being guessed at."""
    if not raw:
        return None
    return DEVICE_DECODE.get(raw.strip().upper())


def prefers_watch(metric: str) -> bool:
    """True when a watch reading should win over an iPhone one for this metric
    (resting HR, HRV, sleep). Used when both devices report the same moment."""
    return metric in PREFER_WATCH or metric.startswith(PREFER_WATCH_PREFIXES)


def is_wanted(metric: str) -> bool:
    """True when a metric is stored in watch_sample."""
    return metric in WANTED or metric.startswith(SLEEP_PREFIX)


def is_hourly(metric: str) -> bool:
    """True when a metric is stored as hourly totals in watch_hourly."""
    return metric in HOURLY


def minute_key(when: datetime, device: str | None) -> str:
    """The key a minute's value is stored under inside an hourly row:
    "<UTC minute ISO>|<device string as it arrived>". The device is part of
    the key so two separate samples for one minute (watch and iPhone) are
    both KEPT and counted as an overlap, never silently merged or dropped.
    `when` must be timezone-aware (bucket_hourly makes it so)."""
    minute = when.astimezone(_UTC).replace(second=0, microsecond=0)
    return f"{minute.strftime('%Y-%m-%dT%H:%MZ')}|{device or ''}"


def bucket_hourly(samples: list[dict], tz) -> dict:
    """Group hourly-metric samples into {(metric, hour_start): bucket}.

    hour_start is the start of the LOCAL hour in `tz` (the active timezone),
    as an aware datetime. Each bucket holds `minutes` {minute_key: value},
    plus the unit and local_date. Within one payload a repeated minute key
    keeps the last value seen: a resend, not a second reading. Samples with
    no date or no value are skipped (parse_counts reports undated ones)."""
    out: dict = {}
    for s in samples:
        if not is_hourly(s["metric"]) or s["measured_at"] is None or s["value"] is None:
            continue
        when = s["measured_at"]
        if when.tzinfo is None:
            when = when.replace(tzinfo=tz)
        local = when.astimezone(tz)
        hour_start = local.replace(minute=0, second=0, microsecond=0)
        b = out.setdefault((s["metric"], hour_start), {
            "metric": s["metric"], "hour_start": hour_start,
            "local_date": hour_start.date(), "unit": s["unit"], "minutes": {}})
        b["minutes"][minute_key(when, s.get("device"))] = float(s["value"])
    return out


def merge_minutes(stored: dict | None, incoming: dict) -> dict:
    """The hour's minute map after a push: stored keys, overwritten or extended
    by the incoming ones. NEVER additive: a minute the push resends replaces
    itself, and a push covering part of the hour can't shrink it."""
    merged = dict(stored or {})
    merged.update(incoming)
    return merged


def summarise_minutes(minutes: dict) -> dict:
    """value / sample_count / overlap_minutes / first_at / last_at / devices
    for one hour's minute map.

    THE VALUE TAKES THE MAXIMUM PER MINUTE, NEVER THE SUM ACROSS DEVICE
    MARKERS (Ryan, 2026-09-22). Apple exports the same minute more than once
    when it merges sources: 06:08 on 9/21 arrived as `RAW` = 18.0 and
    `RAW|RIP` = 18.0 — one set of steps described twice, not 36 steps. Adding
    them inflated 9/21 by 109 steps and 9/22 by 27. The larger value wins when
    they differ, because a marker covering two devices can only be a superset
    of the one covering a single device. Every sample is still STORED under its
    own key, and overlap_minutes still reports how many minutes arrived twice
    — nothing is dropped, it just isn't counted twice.

    This applies to every hourly metric keyed `minute|device`, not just steps.
    """
    per_minute: dict = {}
    devices = set()
    for key, value in minutes.items():
        minute, _, device = key.partition("|")
        seen = per_minute.setdefault(minute, [])
        seen.append(value)
        if device:
            devices.add(device)
    stamps = sorted(per_minute)
    parse = lambda m: datetime.strptime(m, "%Y-%m-%dT%H:%MZ").replace(tzinfo=_UTC)  # noqa: E731
    return {
        "value": round(sum(max(vals) for vals in per_minute.values()), 4),
        "sample_count": len(minutes),
        "overlap_minutes": sum(1 for vals in per_minute.values() if len(vals) > 1),
        "first_at": parse(stamps[0]) if stamps else None,
        "last_at": parse(stamps[-1]) + timedelta(minutes=1) if stamps else None,
        "devices": sorted(devices),
    }

# Sleep arrives as ONE aggregated record per night, a field per stage.
# Observed 2026-09-21 (the first real night):
#   {"date": "2026-09-21 00:00:00 -0500",           <- local MIDNIGHT of the wake day
#    "totalSleep": 5.73, "core": 4.07, "deep": 0.77, "rem": 0.88,
#    "awake": 0.64, "asleep": 0, "inBed": 0, "source": "RAW",
#    "sleepStart": "2026-09-20 20:57:29 -0500", "sleepEnd": "2026-09-21 03:19:20 -0500",
#    "inBedStart": ..., "inBedEnd": ...}
# totalSleep = core + deep + rem (+ asleep); awake is excluded. Units: hours.
SLEEP_STAGES = ("asleep", "deep", "rem", "core", "awake", "in_bed", "inBed",
                "total_sleep", "totalSleep")
SLEEP_NUMERIC = set(SLEEP_STAGES)
# FALSE ZEROS. `asleep` is the "unspecified" stage and reads 0 whenever the
# watch recorded real stages; `inBed` reads 0 because the watch doesn't write
# in-bed time (the in-bed start/end strings still span the night). A 0 in
# either is "no data", not "no sleep" — stored, it read as 0 h asleep / in bed.
SLEEP_ZERO_IS_ABSENT = {"asleep", "in_bed", "inBed"}

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
                            "unit": unit, "raw": sample, "value_min": None,
                            "value_max": None, "device": None})
                continue
            if name == "sleep":
                rows = _sleep_rows(sample, when, unit)
                out.extend(rows or [{"metric": "sleep", "measured_at": when,
                                     "value": _number(sample), "unit": unit,
                                     "raw": sample, "value_min": None,
                                     "value_max": None,
                                     "device": sample.get("source")}])
                continue
            lo = hi = None
            if isinstance(sample, dict) and ("Min" in sample or "Max" in sample):
                lo = _number(sample.get("Min"))
                hi = _number(sample.get("Max"))
            out.append({"metric": name, "measured_at": when,
                        "value": _number(sample), "unit": unit, "raw": sample,
                        "value_min": lo, "value_max": hi,
                        "device": (sample.get("source") if isinstance(sample, dict) else None)})
    return out


def _sleep_rows(sample: dict, when: datetime, unit) -> list[dict]:
    """One row per sleep stage present. 'sleep_asleep', 'sleep_deep', …"""
    rows = []
    for stage in SLEEP_STAGES:
        if stage not in sample or stage not in SLEEP_NUMERIC:
            continue
        value = _number(sample.get(stage))
        if value is None or (value == 0 and stage in SLEEP_ZERO_IS_ABSENT):
            continue
        key = re.sub(r"(?<!^)(?=[A-Z])", "_", stage).lower()
        rows.append({"metric": f"sleep_{key}", "measured_at": when,
                     "value": value, "unit": unit, "raw": sample,
                     "value_min": None, "value_max": None,
                     "device": sample.get("source")})
    return rows


def sleep_bounds(raw) -> tuple[datetime | None, datetime | None]:
    """(sleepStart, sleepEnd) of one night's record, offsets kept. The record's
    own `date` is only a label (local midnight of the wake day) — these two are
    when the night actually happened."""
    if not isinstance(raw, dict):
        return None, None
    return (parse_date(raw.get("sleepStart") or raw.get("sleep_start")),
            parse_date(raw.get("sleepEnd") or raw.get("sleep_end")))


def is_later_night(new_raw, old_raw) -> bool:
    """True when `new_raw` should REPLACE the stored record for the same night.

    A push in the middle of the night stores a partial aggregate; the complete
    one arrives later under the same key with a later sleepEnd. Only a strictly
    later end replaces — a re-send of the same night is a duplicate."""
    _, new_end = sleep_bounds(new_raw)
    _, old_end = sleep_bounds(old_raw)
    if new_end is None:
        return False
    if old_end is None:
        return True
    try:
        return new_end > old_end
    except TypeError:          # one naive, one aware — can't order them safely
        return False


def parse_workouts(payload: dict, skipped: list | None = None) -> list[dict]:
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
            # WATCH-2: a workout with no readable start has no natural key, so
            # it can't be stored — but it is REPORTED, never dropped in silence.
            if skipped is not None:
                skipped.append(w)
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
    """What the payload contained — for the response body, the audit row and
    the log. Nothing here writes; it only describes."""
    data = (payload or {}).get("data") or {}
    samples = parse_samples(payload)
    skipped_workouts: list = []
    workouts = parse_workouts(payload, skipped_workouts)
    ignored: dict[str, int] = {}
    wanted = hr = hourly = 0
    for s in samples:
        if is_wanted(s["metric"]):
            wanted += 1
        elif s["metric"] == HEART_RATE:
            hr += 1
        elif is_hourly(s["metric"]):
            hourly += 1
        else:
            ignored[s["metric"]] = ignored.get(s["metric"], 0) + 1
    return {
        "metrics_in": len(data.get("metrics") or []),
        "workouts_in": len(data.get("workouts") or []),
        "samples": len(samples),
        "stored_samples": wanted,
        "undated_samples": sum(1 for s in samples if s["measured_at"] is None),
        "heart_rate_samples": hr,
        "hourly_samples": hourly,
        "ignored_metrics": dict(sorted(ignored.items(), key=lambda kv: -kv[1])),
        "ignored_samples": sum(ignored.values()),
        "workouts_without_a_readable_start": len(skipped_workouts),
    }
