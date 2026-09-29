#!/usr/bin/env python3
"""Ryan's 11/19 -> 12/4 as OVERRIDES. Writes overrides only — no plan rows.

The plan rows follow in the block-2 seeding round (~10/25). Writing the
overrides now is what makes that round's build reproduce this placement instead
of re-deriving it from a table in a report.

Idempotent: every row is keyed on (start_date, end_date, day_type/session) and
skipped when already present, so a re-run is a no-op rather than a duplicate.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date

sys.path.insert(0, ".")

D = date

#: (start, end, purpose) — hunting stays.
HUNTING = ((D(2026, 11, 21), D(2026, 11, 22)), (D(2026, 11, 27), D(2026, 11, 29)))

#: (date, session, location_key, note)
PLACEMENTS = [
    (D(2026, 11, 19), "rest", "richfield", "travel: drive to Richfield, whole day"),
    (D(2026, 11, 20), "strength_a", "richfield", ""),
    (D(2026, 11, 23), "strength_b", "richfield", ""),
    (D(2026, 11, 24), "strength_c", "richfield", ""),
    (D(2026, 11, 25), "cardio_z2", "brown_deer", "Zone 2 – Treadmill 45"),
    (D(2026, 11, 26), "bodyweight_circuit", "brown_deer", ""),
    (D(2026, 11, 30), "rest", "msp_home", "travel: to MSP"),
    (D(2026, 12, 1), "strength_a", "office", ""),
    (D(2026, 12, 2), "strength_b", "office", ""),
    (D(2026, 12, 3), "strength_c", "office", "evening drive to Richfield, normal"),
    (D(2026, 12, 4), "cardio_z2", "richfield", "Zone 2 – Bike 60, then the normal cycle"),
]

SET_BY = "ryan"
REASON = "Ryan 2026-09-29: approved Nov placement"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()

    from artemis import away
    from knowledge import cognition
    from knowledge.db import get_connection

    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT start_date, end_date, day_type, purpose, attrs "
            "FROM acos.cycle_day_overrides WHERE revoked_at IS NULL "
            "  AND start_date BETWEEN %s AND %s", (D(2026, 11, 1), D(2026, 12, 31)))
        existing = set()
        for r in cur.fetchall():
            g = (lambda k, i: r[k] if isinstance(r, dict) else r[i])
            attrs = g("attrs", 4)
            if isinstance(attrs, str):
                attrs = json.loads(attrs or "{}")
            existing.add((g("start_date", 0), g("end_date", 1),
                          g("day_type", 2), (attrs or {}).get("session")))

        planned, skipped = [], []
        for a, b in HUNTING:
            key = (a, b, away.DAY_TYPE, None)
            (skipped if key in existing else planned).append(
                ("hunting", a, b, None, None))
        for d, session, loc, note in PLACEMENTS:
            key = (d, d, None, session)
            (skipped if key in existing else planned).append(
                ("placement", d, d, session, loc))

        print(f"to write: {len(planned)}   already present: {len(skipped)}\n")
        for kind, a, b, session, loc in planned:
            span = f"{a}" if a == b else f"{a}..{b}"
            print(f"  {kind:10s} {span:24s} {session or 'rest (hunting)':19s} {loc or ''}")

        if not args.commit:
            print("\nDRY RUN — nothing written.")
            return 0

        for kind, a, b, session, loc in planned:
            if kind == "hunting":
                away.insert(cur, away.Stay(a, b, away.HUNTING, away.NO_GYM,
                                           reason=f"{REASON} — hunting"))
            else:
                cur.execute(
                    "INSERT INTO acos.cycle_day_overrides "
                    "  (start_date, end_date, location, attrs, reason, set_by) "
                    "VALUES (%s, %s, %s, %s::jsonb, %s, %s)",
                    (a, b, loc, json.dumps({"session": session}), REASON, SET_BY))

        # Read back and assert, rather than trusting the writes.
        cur.execute(
            "SELECT count(*) n FROM acos.cycle_day_overrides "
            "WHERE revoked_at IS NULL AND reason LIKE %s", (f"{REASON}%",))
        got = cur.fetchone()
        n = got["n"] if isinstance(got, dict) else got[0]
        if n != len(planned) + len([s for s in skipped]):
            pass  # a re-run legitimately sees more; the per-row check below is the guard
        missing = []
        for kind, a, b, session, loc in planned:
            if kind == "hunting":
                cur.execute("SELECT 1 FROM acos.cycle_day_overrides WHERE day_type = %s "
                            "AND start_date = %s AND end_date = %s AND revoked_at IS NULL",
                            (away.DAY_TYPE, a, b))
            else:
                cur.execute("SELECT 1 FROM acos.cycle_day_overrides "
                            "WHERE start_date = %s AND attrs->>'session' = %s "
                            "AND revoked_at IS NULL", (a, session))
            if cur.fetchone() is None:
                missing.append(f"{kind} {a} {session or ''}")
        if missing:
            conn.rollback()
            print("\nVERIFY FAILED — rolled back:")
            for mrow in missing:
                print("  " + mrow)
            return 1

        cognition.log_decision(
            cur, agent="health_office", action="nov_placement_overrides",
            domain="health", outcome="executed", manual_gap=True,
            metadata={"hunting": [[str(a), str(b)] for a, b in HUNTING],
                      "placements": len(PLACEMENTS), "written": len(planned)},
            assumptions={"rule": "overrides only — no plan rows; the block-2 seeding "
                                 "round builds from these",
                         "source": "Ryan 2026-09-29 approved placement"})
        print(f"\nVERIFIED and committed. {len(planned)} override(s).")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
