"""ZONE-0 — minutes per heart-rate zone for a cardio session, from the watch.

Round #17 found that **no logged cardio session carries an `hr_avg`**, so the
Status page could only ever say "minutes logged only". But WATCH-1 has been
recording heart rate continuously since 2026-09-19, and those samples cover the
sessions whether or not the session itself recorded a number. This reads them.

Deliberately NOT waiting for watch *workouts*: WATCH-1's workout matching is
still at zero, and a feature that needs a second feature to start working is a
feature that does not work.

THE WINDOW IS THE HARD PART, and it is where this fails closed. A zone
breakdown is only as honest as the window it was computed over, so the window
comes from what was actually recorded — never from the plan's intended
duration, which is a prediction, not an observation.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from knowledge import zones

logger = logging.getLogger(__name__)

#: Cardio session types whose logs are worth a zone breakdown.
CARDIO_TYPES = ("cardio_z2", "cardio_intervals")


def window_for(cur, plan_id: int) -> tuple:
    """(start, end, source) for one session's logs, or (None, None, reason).

    Two ways, in order of trustworthiness:

    1. A `cardio_block` log with a real `duration_sec` -- its `logged_at` is when
       the block was finished, so the window is [logged_at - duration, logged_at].
    2. Otherwise the span of the session's own log rows, first to last.

    A single log row gives a zero-length span, which is `bad_window`, not a
    guess: one row says when something happened, not for how long.
    """
    cur.execute(
        "SELECT log_type, duration_sec, logged_at FROM health.session_log "
        "WHERE plan_id = %s AND logged_via <> 'inferred' ORDER BY logged_at",
        (plan_id,))
    rows = cur.fetchall()
    if not rows:
        return None, None, "no logs"

    def _get(r, key, idx):
        return r[key] if isinstance(r, dict) else r[idx]

    for r in rows:
        if _get(r, "log_type", 0) == "cardio_block" and _get(r, "duration_sec", 1):
            end = _get(r, "logged_at", 2)
            return end - timedelta(seconds=int(_get(r, "duration_sec", 1))), end, "cardio_block duration"

    first = _get(rows[0], "logged_at", 2)
    last = _get(rows[-1], "logged_at", 2)
    if first == last:
        return None, None, "one log row — no span"
    return first, last, "first..last logged_at"


def samples_between(cur, start, end) -> list:
    """WATCH-1 heart-rate samples inside the window."""
    cur.execute(
        "SELECT measured_at, bpm FROM health.watch_heart_rate "
        "WHERE measured_at BETWEEN %s AND %s AND bpm IS NOT NULL "
        "ORDER BY measured_at", (start, end))
    return [(r["measured_at"], r["bpm"]) if isinstance(r, dict) else (r[0], r[1])
            for r in cur.fetchall()]


def compute_for_plan(cur, plan_id: int) -> dict:
    """The zone breakdown for one session. Never raises on absent data.

    Returns the row that would be stored, including a non-'ok' status with NULL
    minutes when the window or the samples do not support a number.
    """
    start, end, source = window_for(cur, plan_id)
    if start is None:
        return {"plan_id": plan_id, "window_start": None, "window_end": None,
                "window_source": source, "status": "bad_window", "zones": None,
                "sample_count": 0, "counted_sec": 0, "unaccounted_sec": 0}
    result = zones.minutes_by_zone(samples_between(cur, start, end), start, end)
    return {"plan_id": plan_id, "window_start": start, "window_end": end,
            "window_source": source, **result}


_UPSERT = """
INSERT INTO health.session_hr_zones
    (plan_id, window_start, window_end, window_source, status, sample_count,
     counted_sec, unaccounted_sec, z1_min, z2_min, z3_min, z4_min, z5_min,
     hr_max_used, zones_source, computed_at)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
ON CONFLICT (plan_id) DO UPDATE SET
    window_start = EXCLUDED.window_start, window_end = EXCLUDED.window_end,
    window_source = EXCLUDED.window_source, status = EXCLUDED.status,
    sample_count = EXCLUDED.sample_count, counted_sec = EXCLUDED.counted_sec,
    unaccounted_sec = EXCLUDED.unaccounted_sec,
    z1_min = EXCLUDED.z1_min, z2_min = EXCLUDED.z2_min, z3_min = EXCLUDED.z3_min,
    z4_min = EXCLUDED.z4_min, z5_min = EXCLUDED.z5_min,
    hr_max_used = EXCLUDED.hr_max_used, zones_source = EXCLUDED.zones_source,
    computed_at = now()
"""


def upsert_params(row: dict) -> tuple:
    """The params for `_UPSERT`, in its column order. One place, as with
    health_office.upsert_params -- an 16-tuple written twice is two chances to
    put z4 where z5 goes."""
    z = row.get("zones") or {}
    # A window that could not be resolved still records WHY, so the next reader
    # sees "one log row -- no span" instead of an absent row they must explain.
    return (row["plan_id"], row["window_start"], row["window_end"],
            row["window_source"], row["status"], row.get("sample_count", 0),
            row.get("counted_sec", 0), row.get("unaccounted_sec", 0),
            z.get("Z1"), z.get("Z2"), z.get("Z3"), z.get("Z4"), z.get("Z5"),
            zones.HR_MAX, zones.SOURCE)


def store(cur, row: dict) -> None:
    if row["window_start"] is None:
        # The table requires a window; a session with no usable one is simply
        # not stored, and the caller reports it. Writing a fake window so the
        # row exists would be the opposite of the point.
        return
    cur.execute(_UPSERT, upsert_params(row))


def describe(row: dict) -> str:
    """One line for a card or a Finish-cardio reply."""
    if row.get("status") != "ok" or not row.get("zones"):
        return {"insufficient_hr_data": "not enough heart-rate data",
                "no_samples": "no heart-rate data for this session",
                "bad_window": "couldn't tell when this session ran"}.get(
                    row.get("status"), "no heart-rate data")
    z = row["zones"]
    parts = [f"{name} {z[name]} min" for name in ("Z1", "Z2", "Z3", "Z4", "Z5")
             if z.get(name)]
    return " · ".join(parts) if parts else "below Z1 throughout"
