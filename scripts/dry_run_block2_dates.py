#!/usr/bin/env python3
"""Block 2 dry run WITH Ryan's 11/19 -> 12/4 placement. Writes nothing, ever.

The placements and hunting stays are declared here rather than read from the
database because they are not recorded yet: this round writes the OVERRIDES for
the dates, and the plan rows follow in the block-2 seeding round around 10/25.
Printing them from the same constants the seeder will use is what makes this a
preview rather than a description.
"""
from __future__ import annotations

import sys
from collections import Counter
from datetime import date

sys.path.insert(0, ".")

D = date

#: Ryan's approved placement (2026-09-29). location_key + session per date.
PLACEMENTS = {
    D(2026, 11, 19): {"session": "rest", "location_key": "richfield"},          # travel
    D(2026, 11, 20): {"session": "strength_a", "location_key": "richfield"},
    D(2026, 11, 23): {"session": "strength_b", "location_key": "richfield"},
    D(2026, 11, 24): {"session": "strength_c", "location_key": "richfield"},
    D(2026, 11, 25): {"session": "cardio_z2", "location_key": "brown_deer"},
    D(2026, 11, 26): {"session": "bodyweight_circuit", "location_key": "brown_deer"},
    D(2026, 11, 30): {"session": "rest", "location_key": "msp_home"},           # travel
    D(2026, 12, 1): {"session": "strength_a", "location_key": "office"},
    D(2026, 12, 2): {"session": "strength_b", "location_key": "office"},
    D(2026, 12, 3): {"session": "strength_c", "location_key": "office"},
    D(2026, 12, 4): {"session": "cardio_z2", "location_key": "richfield"},
}

HUNTING = ((D(2026, 11, 21), D(2026, 11, 22)), (D(2026, 11, 27), D(2026, 11, 29)))


def main() -> int:
    from artemis import away, block2

    stays = [away.Stay(a, b, away.HUNTING, away.NO_GYM, reason="hunting")
             for a, b in HUNTING]
    rows = block2.build_rows(placements=PLACEMENTS, away_stays=stays)

    print(f"{block2.NAME}: {block2.start_date()} .. {block2.end_date()}  "
          f"({len(rows)} rows)\n")

    window = (D(2026, 11, 15), D(2026, 12, 12))
    print(f"the fortnight {window[0]} .. {window[1]}:")
    for r in sorted(rows, key=lambda r: (r["plan_date"], r.get("slot", "morning"))):
        d = r["plan_date"]
        if not (window[0] <= d <= window[1]):
            continue
        b = r["blocks"]
        stay = away.stay_on(stays, d)
        tag = "  [hunting]" if stay else ("  [placed]" if d in PLACEMENTS else "")
        print(f"  {d} {d:%a} {r.get('slot','morning'):7s} wk{block2.week_num_for(d)} "
              f"{r['session_type']:19s} {b['display_name']:22s} "
              f"{b.get('location_key'):11s}{tag}")

    print("\nby session type in the window:")
    for k, n in sorted(Counter(
            r["session_type"] for r in rows
            if window[0] <= r["plan_date"] <= window[1]).items()):
        print(f"  {k:19s} {n}")

    # The deload must still be week 6, and the block must still end 12/12.
    deload_start = block2.start_date() + __import__("datetime").timedelta(
        days=7 * (block2.DELOAD_WEEK - 1))
    print(f"\ndeload: week {block2.DELOAD_WEEK} = {deload_start} .. {block2.end_date()}")

    # Nothing between 11/19 and 11/30 may be counted as missed.
    print("\nnothing in 11/19-11/30 is missable:")
    for r in sorted(rows, key=lambda r: r["plan_date"]):
        d = r["plan_date"]
        if not (D(2026, 11, 19) <= d <= D(2026, 11, 30)):
            continue
        stay = away.stay_on(stays, d)
        why = ("away — never missed" if stay else
               "rest — nothing to miss" if r["session_type"] == "rest" else
               "planned session (missable, as intended)")
        print(f"  {d} {r.get('slot','morning'):7s} {r['session_type']:19s} {why}")

    print("\nDRY RUN — nothing written. There is no --commit here by design.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
