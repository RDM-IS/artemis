"""ZONE-0 — minutes per heart-rate zone for a cardio session, from the watch.

Round #17 found that **no logged cardio session carries an `hr_avg`**, so the
Status page could only ever say "minutes logged only". But WATCH-1 has been
recording heart rate continuously since 2026-09-19, and those samples cover the
sessions whether or not the session itself recorded a number. This reads them.

Still deliberately NOT waiting for workout MATCHING (watch_workout.plan_id):
a feature that needs a second feature to start working does not work. Since
2026-11-10 the window does use a watch workout's own start/stop, found by TIME
OVERLAP with the logged span (knowledge.zone_window), which needs no plan_id.

THE WINDOW IS THE HARD PART, and it is where this fails closed. A zone
breakdown is only as honest as the window it was computed over, so the window
comes from what was actually recorded — never from the plan's intended
duration, which is a prediction, not an observation.
"""

from __future__ import annotations

import logging

from knowledge import zone_window, zones
from knowledge.watch_prefill import _dicts

logger = logging.getLogger(__name__)

#: Cardio session types whose logs are worth a zone breakdown.
CARDIO_TYPES = ("cardio_z2", "cardio_intervals")


def window_for(cur, plan_id: int) -> tuple:
    """(start, end, source) for one session, or (None, None, reason).

    The rule lives in knowledge.zone_window (shared with the Lambda): a watch
    workout overlapping the logged span first, then the iPad's own
    started_at..ended_at, then the legacy duration anchors. This only reads.
    A failed read RAISES -- it must never look like "no workout"
    (FAIL-CLOSED-RESOLVERS).
    """
    cur.execute(
        "SELECT log_type, duration_sec, logged_at, started_at, ended_at "
        "FROM health.session_log "
        "WHERE plan_id = %s AND logged_via <> 'inferred' ORDER BY logged_at",
        (plan_id,))
    logs = _dicts(cur)
    start, end, _ = zone_window.logged_span(logs)
    if start is None:
        return zone_window.session_window(logs, [])
    cur.execute(
        "SELECT kind, started_at, ended_at, duration_sec FROM health.watch_workout "
        "WHERE started_at < %s AND started_at > %s ORDER BY started_at",
        (end + zone_window.WORKOUT_READ_PAD, start - zone_window.WORKOUT_READ_PAD))
    return zone_window.session_window(logs, _dicts(cur))


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


#: How many local days back the ingest recompute reaches. The watch syncs a
#: session within hours; a push that carries OLDER days (a re-export) must not
#: quietly backfill history -- scripts/backfill_hr_zones.py is the explicit path.
RECOMPUTE_DAYS = 3


def recompute_recent(cur, days, today) -> dict:
    """Recompute the zone breakdown of finished sessions on `days`, limited to
    the last RECOMPUTE_DAYS local days ending `today`.

    WHY (2026-11-10): the Finish POST computes the breakdown before the watch
    has synced, so it stores `no_samples` and, until this, nothing ever looked
    again. The watch ingest calls this after it stores heart rate.

    Only sessions that a Finish could have scored (a cardio_block or
    session_summary with a duration, same trigger as the POST) and whose row is
    not already final -- 'ok' over a watch-workout window -- are recomputed.
    Writes only health.session_hr_zones. The caller owns the transaction; a
    read failure RAISES, so the caller's savepoint rolls it back rather than
    storing a breakdown built on a read that did not happen.
    """
    from datetime import timedelta as _td
    floor = today - _td(days=RECOMPUTE_DAYS - 1)
    days = sorted({d for d in days if floor <= d <= today})
    if not days:
        return {"days": [], "recomputed": []}
    cur.execute(
        "SELECT DISTINCT p.plan_id FROM health.plan p "
        "JOIN health.session_log sl ON sl.plan_id = p.plan_id "
        "WHERE p.plan_date = ANY(%s) AND sl.logged_via <> 'inferred' "
        "  AND sl.log_type = ANY(%s) AND sl.duration_sec IS NOT NULL "
        "ORDER BY p.plan_id",
        (days, list(zone_window.FINISHERS)))
    plan_ids = [r["plan_id"] if isinstance(r, dict) else r[0] for r in cur.fetchall()]
    if not plan_ids:
        return {"days": [d.isoformat() for d in days], "recomputed": []}
    # A separate read, not a join: one statement naming health.plan beside a
    # `status` column trips the deploy's column-drop preflight (039).
    cur.execute(
        "SELECT plan_id FROM health.session_hr_zones "
        "WHERE plan_id = ANY(%s) AND status = 'ok' AND window_source LIKE %s",
        (plan_ids, zone_window.WATCH_SOURCE + "%"))
    final = {r["plan_id"] if isinstance(r, dict) else r[0] for r in cur.fetchall()}
    plan_ids = [p for p in plan_ids if p not in final]
    out = []
    for plan_id in plan_ids:
        row = compute_for_plan(cur, plan_id)
        store(cur, row)
        out.append({"plan_id": plan_id, "status": row["status"],
                    "window_source": row["window_source"],
                    "sample_count": row.get("sample_count", 0)})
    return {"days": [d.isoformat() for d in days], "recomputed": out}
