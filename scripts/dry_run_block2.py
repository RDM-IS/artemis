#!/usr/bin/env python3
"""Block 2 DRY RUN. Writes nothing, ever — there is no --commit flag.

Seeding block 2 is a separate, approved act. Its first day is five weeks out and
everything between now and then (a repeat week, a trip, a changed schedule)
moves it, so a script that could write it today would be writing a guess.
"""
from __future__ import annotations

import sys
from collections import Counter

sys.path.insert(0, ".")


def main() -> int:
    from artemis import block2, health_office as office
    from knowledge.db import get_connection

    rows = block2.build_rows()
    start, end = block2.start_date(), block2.end_date()
    print(f"{block2.NAME} — phase {block2.PHASE}, {block2.WEEKS} weeks "
          f"(deload wk {block2.DELOAD_WEEK})")
    print(f"block 1 ends {office.program_end()} -> block 2 {start} .. {end}")
    print(f"rows built in memory: {len(rows)}\n")

    by_type = Counter(r["session_type"] for r in rows)
    print("by session type:")
    for k, n in sorted(by_type.items()):
        print(f"  {k:17s} {n}")

    # What would CHANGE if this were applied: compare against what is stored.
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT plan_date, slot, session_type FROM health.plan "
                    "WHERE plan_date BETWEEN %s AND %s", (start, end))
        existing = {(r["plan_date"] if isinstance(r, dict) else r[0],
                     r["slot"] if isinstance(r, dict) else r[1]):
                    (r["session_type"] if isinstance(r, dict) else r[2])
                    for r in cur.fetchall()}
        conn.rollback()

    kinds = Counter()
    print("\nevery date:")
    for r in sorted(rows, key=lambda r: (r["plan_date"], r.get("slot", "morning"))):
        key = (r["plan_date"], r.get("slot", "morning"))
        was = existing.get(key)
        kind = "new" if was is None else ("retype" if was != r["session_type"] else "rebuild")
        kinds[kind] += 1
        wk = block2.week_num_for(r["plan_date"])
        b = r["blocks"]
        extra = ""
        if b.get("intervals"):
            iv = b["intervals"]
            extra = f"  {iv['reps']}x{iv['work_sec']}s/{iv['easy_sec']}s"
        elif r["session_type"] == "cardio_intervals":
            # A seeded interval row builds FAIL-CLOSED as the Z2 variant: the
            # gate is evaluated on the morning, not five weeks ahead. Show what
            # it WOULD be so the prescription is reviewable now.
            spec = block2.INTERVAL_WEEKS.get(wk)
            would = (f"{spec[0]}x{spec[1] // 60}min Z4 / {spec[2] // 60}min easy"
                     if spec else "Z2 (deload)")
            extra = f"  gated -> Z2 today; would be {would}"
        elif b.get("type") == "circuit":
            extra = f"  {b.get('rounds')} sets, RPE cap {r.get('target_rpe')}"
        print(f"  {r['plan_date']} {r.get('slot','morning'):7s} wk{wk} "
              f"{r['session_type']:17s} {kind:8s} {b['display_name']:24s}"
              f"{extra}")

    print("\nby change kind:", dict(kinds))
    print("\nDRY RUN — nothing written. There is no --commit here by design.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
