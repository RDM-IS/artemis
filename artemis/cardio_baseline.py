"""CARDIO-LOC — the rowing baseline, and why a substitute can't move it.

Rowing carries the progression. A session on the bike or the treadmill runs the
SAME target — the minutes the plan asked for — but it is a substitute, and a
substitute must not advance the rowing baseline: three weeks of treadmill would
otherwise read as three weeks of rowing progress.

The baseline is a QUERY over `health.session_log`, not a second store (one
system of record). What keeps substitutes out is one predicate — `modality =
'row'` — which is why modality had to become a column instead of free text in
`exercise`: telling a row from a bike by matching a display name is the same
failure the exercise-name rules were deleted for.

Duration is the currency, never distance: distance does not compare across
modalities, and it does not compare across rowers either.
"""

from __future__ import annotations

import logging
from datetime import date

logger = logging.getLogger(__name__)

ROW = "row"


def rowing_baseline(cur, *, before: date | None = None, lookback_days: int = 56) -> dict | None:
    """The most recent rowing session, as the baseline to progress from.

    `None` means there is no rowing history in the window — an explicit
    "no baseline yet", never a zero and never a substitute's numbers.
    """
    cur.execute(
        """
        SELECT l.logged_at::date AS on_date, l.duration_sec, l.rpe_actual, l.hr_avg, l.device
        FROM health.session_log l
        WHERE l.modality = %s
          AND l.duration_sec IS NOT NULL
          AND (%s::date IS NULL OR l.logged_at::date < %s::date)
          AND l.logged_at >= now() - make_interval(days => %s)
        ORDER BY l.logged_at DESC
        LIMIT 1
        """,
        (ROW, before, before, lookback_days),
    )
    row = cur.fetchone()
    if not row:
        return None
    on_date, duration_sec, rpe, hr_avg, device = row
    return {"date": on_date, "duration_sec": int(duration_sec),
            "minutes": round(int(duration_sec) / 60, 1),
            "effort": float(rpe) if rpe is not None else None,
            "hr_avg": int(hr_avg) if hr_avg is not None else None,
            "device": device}


def sessions_by_modality(cur, *, since: date, until: date) -> dict[str, int]:
    """How many cardio sessions of each modality in a window — the figure that
    shows how much of a period was actually rowing."""
    cur.execute(
        """
        SELECT coalesce(l.modality, 'unrecorded') AS modality, count(*)
        FROM health.session_log l
        WHERE l.log_type = 'cardio_block'
          AND l.logged_at::date BETWEEN %s AND %s
        GROUP BY 1 ORDER BY 2 DESC
        """,
        (since, until),
    )
    return {m: int(n) for m, n in cur.fetchall()}


def advances_baseline(modality: str | None) -> bool:
    """True only for rowing. The one rule, in one place, so a caller cannot
    quietly decide that a bike session counts this once."""
    return modality == ROW
