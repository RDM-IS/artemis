"""PROGRAM-2 Block 2 — "Build · Phase 2". BUILD AND DRY RUN ONLY.

Approved outline (Ryan, 2026-09-28): lifts RPE 7-8 on double progression;
intervals 4x3' -> 4x4' Z4 with 3' easy recoveries (Norwegian 4x4); the Paris week
becomes a travel week. Block 1 ends Sat 10/31 (deload week 7).

NOTHING HERE WRITES TO health.plan. The module builds rows in memory and the
dry-run script prints them. Seeding is a separate, approved act -- and it has to
be, because block 2's first day is five weeks out and everything between now and
then (a repeat week, a trip, a changed schedule) moves it.

**The boundary is computed, never typed.** `program_end()` already accounts for
REPEAT-WEEK, so block 2 starts the Sunday after whatever day block 1 actually
ends on. Writing 2026-11-01 here would silently be wrong the first time a week
repeats, and wrong in the direction that looks right.
"""

from __future__ import annotations

from datetime import date, timedelta

from artemis import health_office as office

NAME = "Build · Phase 2"
PHASE = 2

#: 6 program weeks: 1-5 build, 6 deload.
WEEKS = 6
DELOAD_WEEK = 6

#: Lift RPE by block-2 week. 7-8 across the build, backed off for the deload --
#: the same shape RAMP has in block 1, and like RAMP this is the authority
#: rather than a number retyped in a doc.
RAMP: dict[int, tuple[int, float]] = {
    1: (3, 7.0),
    2: (3, 7.0),
    3: (3, 7.5),
    4: (3, 7.5),
    5: (3, 8.0),
    6: (2, 6.0),          # deload
}

#: Norwegian 4x4 built up: (reps, work_sec, easy_sec).
#: Weeks absent from this map run the Z2 variant, exactly as in block 1 -- one
#: concept, not two.
INTERVAL_WEEKS: dict[int, tuple[int, int, int]] = {
    1: (4, 180, 180),
    2: (4, 180, 180),
    3: (4, 240, 180),
    4: (4, 240, 180),
    5: (4, 240, 180),
}


def start_date(repeats: list[date] | None = None) -> date:
    """The Sunday after block 1 ends.

    `program_end()` is OFFICE_END plus a week per repeat, and it lands on a
    Saturday, so block 2 starts the next day. Asking the function rather than
    naming a date is what makes a repeat week move block 2 with it.
    """
    end = office.program_end(repeats)
    return end + timedelta(days=1)


def end_date(repeats: list[date] | None = None) -> date:
    return start_date(repeats) + timedelta(days=7 * WEEKS - 1)


def week_num_for(d: date, repeats: list[date] | None = None) -> int | None:
    """Block-2 week number for a date, or None when the date is outside it."""
    start = start_date(repeats)
    if d < start or d > end_date(repeats):
        return None
    return (d - start).days // 7 + 1


def build_rows(repeats: list[date] | None = None) -> list[dict]:
    """Every block-2 row, in memory. Reuses block 1's builders entirely.

    The 14-day template, the lift days, the locations and the cardio resolve all
    come from `health_office`, so block 2 is the same program with a different
    ramp and different interval prescriptions -- not a second copy of the
    schedule that can drift from the first.
    """
    start, end = start_date(repeats), end_date(repeats)
    rows: list[dict] = []
    d = start
    while d <= end:
        wk = week_num_for(d, repeats)
        for spec in _specs_for(d, wk):
            rows.append(_build_one(spec))
        d += timedelta(days=1)
    return rows


def _specs_for(d: date, wk: int) -> list[dict]:
    """The day's specs, from block 1's own schedule rules."""
    base = {
        "plan_date": d, "slot": "morning",
        "session_type": office.session_for(d),
        "week_num": wk, "location": office.day_location(d),
        "location_key": office.day_location_key(d),
        "day_type": office.day_type(d), "pos": office.cycle_pos(d), "wk0": False,
    }
    specs = [base]
    if office.cycle_pos(d) in office.EVENING_POS and office.evening_is_possible(d):
        specs.append({**base, "slot": "evening",
                      "session_type": office.EVENING_SESSION})
    return specs


def _build_one(spec: dict) -> dict:
    """One row, with block 2's ramp and intervals swapped in.

    `health_office` reads its own RAMP and INTERVAL_WEEKS at build time, so the
    swap is done around the call rather than by passing a parameter through six
    functions. It is restored in a finally: a builder left pointing at block 2's
    tables would quietly change what a block 1 reseed produces.
    """
    old_ramp, old_intervals = office.RAMP, office.INTERVAL_WEEKS
    try:
        office.RAMP = {w: (sets, rpe, old_ramp[min(w, max(old_ramp))][2])
                       for w, (sets, rpe) in RAMP.items()}
        office.INTERVAL_WEEKS = dict(INTERVAL_WEEKS)
        row = office.build_row(spec)
    finally:
        office.RAMP, office.INTERVAL_WEEKS = old_ramp, old_intervals
    row["phase"] = PHASE
    row["notes"] = f"{row['blocks']['display_name']} | {NAME} wk{spec['week_num']}"
    return row
