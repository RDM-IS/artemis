#!/usr/bin/env python3
"""Seed block 2 ("Build · Phase 2") into health.plan. Dry run by default.

Block 2 was built and dry-run on 2026-09-29 and never seeded, so from its first
day (the Sunday after block 1 ends: 11/01 unless a week repeated) there were NO
plan rows -- Today said "No workout today" and the recommendation had no plan
to read. This writes them, from the same builder the dry runs use, honouring
every live override: Ryan's 2026-10-21 calendar (trips, hunting, Richfield
lifts) arrives as stays and placements, and the rowing schedule fills every
other Minneapolis morning.

Guards (CLAUDE.md, both questions):
  * md5 over every plan row OUTSIDE block 2's dates, unchanged;
  * every block-2 date has exactly one morning row after the write, and each
    written row re-reads with the session type and room it was built with;
  * a date that already carries a REAL log is never rewritten.

    PYTHONPATH="$PWD" /usr/bin/python3.11 scripts/seed_block2.py
    PYTHONPATH="$PWD" /usr/bin/python3.11 scripts/seed_block2.py --commit
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import timedelta

sys.path.insert(0, ".")


def _g(r, k, i):
    return r[k] if isinstance(r, dict) else r[i]


def _md5_outside(cur, start, end) -> str:
    cur.execute("SELECT plan_date, slot, session_type, blocks::text FROM health.plan "
                "WHERE plan_date < %s OR plan_date > %s ORDER BY plan_date, slot", (start, end))
    h = hashlib.md5()
    for r in cur.fetchall():
        h.update(str(tuple(r.values()) if isinstance(r, dict) else tuple(r)).encode())
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()

    from artemis import away, block2
    from artemis import health_office as office
    from knowledge import cognition
    from knowledge.db import get_connection

    reps = office.repeat_starts()                 # fail-closed
    start, end = block2.start_date(reps), block2.end_date(reps)

    with get_connection() as conn:
        cur = conn.cursor()
        stays = away.load_stays(cur, start, end)          # fail-closed
        placed = away.placements(cur, start, end)         # fail-closed
        rows = block2.build_rows(reps, placements=placed, away_stays=stays)

        mornings = sorted(r["plan_date"] for r in rows if r["slot"] == "morning")
        expected = [start + timedelta(days=i) for i in range((end - start).days + 1)]
        assert mornings == expected, "block 2 must have exactly one morning per day"
        keys = [(r["plan_date"], r["slot"]) for r in rows]
        assert len(keys) == len(set(keys)), "duplicate (plan_date, slot)"

        cur.execute(
            "SELECT DISTINCT p.plan_date FROM health.plan p "
            "JOIN health.session_log sl ON sl.plan_id = p.plan_id "
            "WHERE p.plan_date BETWEEN %s AND %s AND sl.logged_via <> 'inferred'", (start, end))
        logged = {_g(r, "plan_date", 0) for r in cur.fetchall()}
        cur.execute("SELECT plan_date, slot FROM health.plan WHERE plan_date BETWEEN %s AND %s",
                    (start, end))
        existing = {(_g(r, "plan_date", 0), _g(r, "slot", 1)) for r in cur.fetchall()}
        write = [r for r in rows if r["plan_date"] not in logged]
        drop = sorted(k for k in existing if k not in set(keys) and k[0] not in logged)

        print(f"block 2: {start} .. {end}   ({len(rows)} rows built, "
              f"{len(existing)} already in health.plan)")
        print(f"stays: {len(stays)}   placements: {len(placed)}   repeats: {len(reps)}\n")
        for r in rows:
            if r["slot"] != "morning":
                continue
            b = r["blocks"]
            tag = " (logged — left alone)" if r["plan_date"] in logged else ""
            print(f"  {r['plan_date']} {r['plan_date'].strftime('%a')}  wk{r['week_num']} "
                  f"{r['session_type']:16s} {b.get('display_name', ''):24s} "
                  f"{b.get('location_key')}{tag}")
        ev = sum(1 for r in rows if r["slot"] == "evening")
        print(f"\n  + {ev} evening rows")
        for d, slot in drop:
            print(f"  REMOVE {d} {slot} (no longer planned)")

        if not args.commit:
            print("\nDRY RUN — nothing written.")
            return 0

        before = _md5_outside(cur, start, end)
        for d, slot in drop:
            cur.execute("DELETE FROM health.plan WHERE plan_date = %s AND slot = %s "
                        "AND NOT EXISTS (SELECT 1 FROM health.session_log sl "
                        "  WHERE sl.plan_id = health.plan.plan_id)", (d, slot))
        for r in write:
            cur.execute(office._UPSERT_SQL, office.upsert_params(r))

        wrong = []
        if _md5_outside(cur, start, end) != before:
            wrong.append("a row OUTSIDE block 2 changed")
        for r in write:
            cur.execute("SELECT session_type, blocks FROM health.plan "
                        "WHERE plan_date = %s AND slot = %s", (r["plan_date"], r["slot"]))
            got = cur.fetchone()
            if got is None:
                wrong.append(f"{r['plan_date']} {r['slot']}: missing")
                continue
            blocks = _g(got, "blocks", 1)
            if isinstance(blocks, str):
                blocks = json.loads(blocks)
            if _g(got, "session_type", 0) != r["session_type"]:
                wrong.append(f"{r['plan_date']} {r['slot']}: {_g(got, 'session_type', 0)}")
            if blocks.get("location_key") != r["blocks"].get("location_key"):
                wrong.append(f"{r['plan_date']} {r['slot']}: room {blocks.get('location_key')}")
        cur.execute("SELECT count(*) AS n FROM health.plan WHERE plan_date BETWEEN %s AND %s "
                    "AND slot = 'morning'", (start, end))
        n = _g(cur.fetchone(), "n", 0)
        if n != len(expected):
            wrong.append(f"{n} morning rows in block 2, expected {len(expected)}")
        if wrong:
            conn.rollback()
            print("\nVERIFY FAILED — rolled back:")
            for w in wrong:
                print("  " + w)
            return 1
        cognition.log_decision(
            cur, agent="health_office", action="seed_block2", domain="health",
            outcome="executed", manual_gap=True,
            metadata={"start": str(start), "end": str(end), "rows": len(write),
                      "removed": len(drop), "stays": len(stays), "placements": len(placed)},
            assumptions={"builder": "artemis.block2.build_rows with live stays + placements",
                         "logged_left_alone": sorted(map(str, logged))})
        print(f"\nVERIFIED and committed. {len(write)} rows written, {len(drop)} removed.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
