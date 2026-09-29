#!/usr/bin/env python3
"""ZONE-0 backfill: compute zone minutes for every logged cardio session.

Dry run by default. `--commit` writes.

The guard that matters here is not an md5 over other rows -- this table is new
and empty, so nothing can be damaged. It is that a session whose samples do not
support a number is RECORDED AS SUCH rather than skipped: a missing row and a
row saying "insufficient" read very differently to whoever looks next.
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter

sys.path.insert(0, ".")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()

    from artemis import hr_zones
    from knowledge import cognition
    from knowledge.db import get_connection

    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT DISTINCT p.plan_id, p.plan_date, p.session_type "
            "FROM health.plan p JOIN health.session_log sl ON sl.plan_id = p.plan_id "
            # EVENING-1: cardio is a morning session; say so rather than rely on it.
            "WHERE p.slot = 'morning' AND p.session_type IN %s "
            "  AND sl.logged_via <> 'inferred' "
            "ORDER BY p.plan_date", (hr_zones.CARDIO_TYPES,))
        sessions = cur.fetchall()

        print(f"cardio sessions with at least one real log: {len(sessions)}\n")
        counts: Counter = Counter()
        rows = []
        for s in sessions:
            plan_id = s["plan_id"] if isinstance(s, dict) else s[0]
            plan_date = s["plan_date"] if isinstance(s, dict) else s[1]
            stype = s["session_type"] if isinstance(s, dict) else s[2]
            row = hr_zones.compute_for_plan(cur, plan_id)
            counts[row["status"]] += 1
            rows.append((plan_date, stype, row))
            line = hr_zones.describe(row)
            print(f"  {plan_date} {stype:17s} {row['status']:21s} "
                  f"samples={row.get('sample_count', 0):5d}  {line}")

        print("\nby status:", dict(counts))
        usable = counts.get("ok", 0)
        print(f"sufficient HR data: {usable} of {len(sessions)}")

        if not args.commit:
            print("\nDRY RUN — nothing written.")
            return 0

        written = 0
        for _d, _t, row in rows:
            hr_zones.store(cur, row)
            if row["window_start"] is not None:
                written += 1

        # Read back and assert, rather than trusting the writes.
        cur.execute("SELECT plan_id, status, z2_min, z4_min FROM health.session_hr_zones")
        stored = {(r["plan_id"] if isinstance(r, dict) else r[0]): r for r in cur.fetchall()}
        wrong = []
        for _d, _t, row in rows:
            if row["window_start"] is None:
                continue
            got = stored.get(row["plan_id"])
            if got is None:
                wrong.append(f"plan {row['plan_id']} not stored")
                continue
            got_status = got["status"] if isinstance(got, dict) else got[1]
            if got_status != row["status"]:
                wrong.append(f"plan {row['plan_id']}: stored {got_status} != {row['status']}")
        if wrong:
            conn.rollback()
            print("\nVERIFY FAILED — rolled back:")
            for w in wrong:
                print("  " + w)
            return 1

        cognition.log_decision(
            cur, agent="hr_zones", action="zone0_backfill", domain="health",
            outcome="executed", manual_gap=True,
            metadata={"sessions": len(sessions), "written": written,
                      "by_status": dict(counts)},
            assumptions={"rule": "zone minutes from watch_heart_rate samples inside "
                                 "each session's logged window; sparse or absent "
                                 "samples are recorded as such, never interpolated",
                         "min_samples_per_min": 1.0, "max_sample_gap_sec": 120})
        print(f"\nVERIFIED and committed. {written} rows.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
