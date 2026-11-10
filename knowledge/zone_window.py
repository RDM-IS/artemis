"""ZONE-WINDOW — the one rule for WHEN a session ran, for its zone breakdown.

Shared by the Lambda's Finish POST and knowledge.hr_zones (the backfill
script, and the recompute every watch ingest runs). Until 2026-11-10 the Lambda
and the box each carried their own copy of the rule.
Pure: it takes rows, not a cursor, so the rule is tested without a database and
both callers do their own reads.

WHY THIS EXISTS (2026-11-10). Every recent session_hr_zones row was
`no_samples`, though watch_heart_rate held hundreds of samples inside each
matching watch workout. Two faults stacked:

  1. The old rule preferred the LONGEST `cardio_block` and anchored it on
     `logged_at`. Since the row ladders, a block is one 240 s ROUND, and every
     round is POSTed together at Finish -- so the "window" was the four minutes
     AFTER the last round, not the session.
  2. The breakdown was computed only inside that POST, before the watch had
     synced -- samples land later, often an hour or more -- and nothing ever
     recomputed. knowledge.hr_zones.recompute_recent, run by every watch
     ingest, fixes that half.

THE ORDER, most trustworthy first:

  1. A WATCH WORKOUT overlapping the logged span. The watch's own start/stop is
     an observation of the session from the device that measured the heart
     rate. Several overlapping workouts (cycle then run) are joined into
     one window. Walks never count (PB-009: daily life, not a session) -- a
     walk-down after a row would otherwise stretch the window.
  2. The iPad's own span: min(started_at)..max(ended_at) over the session's
     rows (ROW-CIRCUIT, migration 055). The summary row carries the whole
     session; the blocks carry their rounds.
  3. Legacy rows with no started_at: a `session_summary` duration ending at its
     logged_at (the summary is POSTed at Finish, so that anchor is right), then
     a `cardio_block` duration the same way (pre-ladder, one block per session),
     then first..last logged_at.

A single row with no duration is still no window: one row says when something
happened, not for how long. The plan's intended duration is never a fallback.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Iterable, Optional

#: The log types that FINISH a session, and so may trigger a zone breakdown.
#: A strength_set's 20-40 s is one exercise, not a session.
FINISHERS = ("cardio_block", "session_summary")

WATCH_SOURCE = "watch workout"
SPAN_SOURCE = "logged started_at..ended_at"


def _g(r, key):
    return r.get(key) if isinstance(r, dict) else getattr(r, key, None)


def is_walk(kind: Optional[str]) -> bool:
    return "walk" in (kind or "").lower()


def logged_span(logs: Iterable) -> tuple:
    """(start, end, source) from the session's own log rows, or (None, None,
    reason). `logs`: dicts with log_type, duration_sec, logged_at, started_at,
    ended_at -- inferred rows already excluded by the caller."""
    rows = [r for r in logs if _g(r, "logged_at") is not None]
    if not rows:
        return None, None, "no logs"

    timed = [r for r in rows if _g(r, "started_at") and _g(r, "ended_at")]
    if timed:
        start = min(_g(r, "started_at") for r in timed)
        end = max(_g(r, "ended_at") for r in timed)
        if end > start:
            return start, end, SPAN_SOURCE

    for log_type in ("session_summary", "cardio_block"):
        cands = [r for r in rows
                 if _g(r, "log_type") == log_type and _g(r, "duration_sec")]
        if cands:
            best = max(cands, key=lambda r: int(_g(r, "duration_sec")))
            end = _g(best, "logged_at")
            return (end - timedelta(seconds=int(_g(best, "duration_sec"))), end,
                    f"{log_type} duration")

    ordered = sorted(_g(r, "logged_at") for r in rows)
    if ordered[0] == ordered[-1]:
        return None, None, "one log row — no span"
    return ordered[0], ordered[-1], "first..last logged_at"


def overlapping_workouts(workouts: Iterable, start: datetime, end: datetime) -> list:
    """The non-walk watch workouts that overlap [start, end]."""
    out = []
    for w in workouts:
        ws, we = _g(w, "started_at"), _g(w, "ended_at")
        if ws is None:
            continue
        if we is None and _g(w, "duration_sec"):
            we = ws + timedelta(seconds=int(_g(w, "duration_sec")))
        if we is None or is_walk(_g(w, "kind")):
            continue
        if ws < end and we > start:
            out.append((ws, we, w))
    return out


def session_window(logs: Iterable, workouts: Iterable) -> tuple:
    """(start, end, source) for a session's zone breakdown.

    `workouts`: watch_workout rows (kind, started_at, ended_at, duration_sec)
    near the session -- the caller reads a generous range; overlap decides.
    """
    start, end, source = logged_span(logs)
    if start is None:
        return start, end, source
    hits = overlapping_workouts(workouts, start, end)
    if hits:
        kinds = sorted({_g(w, "kind") or "?" for _, _, w in hits})
        return (min(s for s, _, _ in hits), max(e for _, e, _ in hits),
                f"{WATCH_SOURCE} ({', '.join(kinds)})")
    return start, end, source


#: How far either side of the logged span to read watch workouts. Generous on
#: purpose: overlap is the test, this only bounds the read.
WORKOUT_READ_PAD = timedelta(hours=3)
