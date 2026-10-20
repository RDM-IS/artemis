#!/usr/bin/env python3
"""ROW-SCHEDULE — rebuild the MSP mornings as rows (Ryan, 2026-10-19).

Dry run by default; `--commit` writes. From tomorrow (or `--from`, never before
row_schedule.ROW_FROM) to the end of the seeded block, every MORNING the rowing
table names is rebuilt as its row ladder or row circuit. Nothing else is
touched: Wisconsin mornings, every evening, and any day with a real log.

Guarded both ways (CLAUDE.md): the md5 over every row OUTSIDE the rebuilt set
proves nothing else moved, AND every rebuilt row is re-read and checked for
its session type, row kind, room and modality. The rows come from the same
builder the seeder uses (`health_office.build_rows`), which resolves
overrides, so a day an override has moved out of Minneapolis is not a target.

    PYTHONPATH="$PWD" /usr/bin/python3.11 scripts/apply_row_schedule.py
    PYTHONPATH="$PWD" /usr/bin/python3.11 scripts/apply_row_schedule.py --commit
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import date, timedelta

sys.path.insert(0, ".")


def _md5_outside(cur, targets: set) -> str:
    cur.execute("SELECT plan_date, slot, session_type, blocks::text FROM health.plan "
                "ORDER BY plan_date, slot")
    h = hashlib.md5()
    for r in cur.fetchall():
        vals = tuple(r.values()) if isinstance(r, dict) else tuple(r)
        if (vals[0], vals[1]) in targets:
            continue
        h.update(str(vals).encode())
    return h.hexdigest()


def _g(row, key, idx):
    return row[key] if isinstance(row, dict) else row[idx]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--from", dest="start", type=date.fromisoformat, default=None,
                    help="first date to rebuild (default: tomorrow, local)")
    args = ap.parse_args()

    from artemis import health_office as office
    from artemis import quiet_hours
    from artemis import row_schedule as rs
    from knowledge import cognition
    from knowledge.db import get_connection

    start = args.start or (quiet_hours.local_today() + timedelta(days=1))
    start = max(start, rs.ROW_FROM)

    with get_connection() as conn:
        cur = conn.cursor()

        # A logged day is never rebuilt: replacing what he did with what is now
        # planned would rewrite history.
        cur.execute(
            "SELECT DISTINCT p.plan_date FROM health.plan p "
            "JOIN health.session_log sl ON sl.plan_id = p.plan_id "
            "WHERE p.plan_date >= %s AND sl.logged_via <> 'inferred'", (start,))
        logged = {_g(r, "plan_date", 0) for r in cur.fetchall()}

        # The rows that exist, so a date with no seeded row (past the block) is
        # reported rather than silently created.
        cur.execute("SELECT plan_date FROM health.plan WHERE slot = 'morning' "
                    "AND plan_date >= %s", (start,))
        seeded = {_g(r, "plan_date", 0) for r in cur.fetchall()}

        rows = office.build_rows()            # override-aware; raises on a failed read
        office.validate_rows(rows)            # the whole program, not the subset
        built = {r["plan_date"]: r for r in rows
                 if r["slot"] == "morning" and r["plan_date"] >= start
                 and r["blocks"].get("row_kind")}
        skipped_logged = sorted(d for d in built if d in logged)
        not_seeded = sorted(d for d in built if d not in seeded)
        targets = {d: r for d, r in built.items() if d not in logged and d in seeded}

        print(f"from {start}: {len(targets)} morning(s) to rebuild")
        for d, r in sorted(targets.items()):
            cur.execute("SELECT session_type, blocks->>'display_name' FROM health.plan "
                        "WHERE plan_date = %s AND slot = 'morning'", (d,))
            was = cur.fetchone()
            was_s = f"{_g(was, 'session_type', 0)} / {_g(was, 'display_name', 1)}" if was else "—"
            print(f"  {d} {d.strftime('%a')}  {was_s:40s} -> {r['blocks']['display_name']}")
        if skipped_logged:
            print(f"left alone, already logged: {', '.join(map(str, skipped_logged))}")
        if not_seeded:
            print(f"not seeded yet (block 2 seeds these with the rows already): "
                  f"{not_seeded[0]} .. {not_seeded[-1]}")

        if not args.commit:
            print("\nDRY RUN — nothing written.")
            return 0
        if not targets:
            print("nothing to do.")
            return 0

        keys = {(d, "morning") for d in targets}
        before = _md5_outside(cur, keys)
        for r in targets.values():
            cur.execute(office._UPSERT_SQL, office.upsert_params(r))

        wrong: list[str] = []
        after = _md5_outside(cur, keys)
        if after != before:
            wrong.append("a row OUTSIDE the rebuilt mornings changed")
        for d, r in targets.items():
            cur.execute("SELECT session_type, blocks FROM health.plan "
                        "WHERE plan_date = %s AND slot = 'morning'", (d,))
            got = cur.fetchone()
            if got is None:
                wrong.append(f"{d}: missing after the write")
                continue
            blocks = _g(got, "blocks", 1)
            if isinstance(blocks, str):
                blocks = json.loads(blocks)
            want = r["blocks"]["row_kind"]
            if _g(got, "session_type", 0) != rs.SESSION_TYPE:
                wrong.append(f"{d}: session_type {_g(got, 'session_type', 0)}")
            if blocks.get("row_kind") != want:
                wrong.append(f"{d}: row_kind {blocks.get('row_kind')!r}, want {want!r}")
            if blocks.get("location_key") != rs.LOCATION_KEY:
                wrong.append(f"{d}: room {blocks.get('location_key')!r}")
            if (blocks.get("cardio") or {}).get("modality") != "row":
                wrong.append(f"{d}: modality {(blocks.get('cardio') or {}).get('modality')!r}")
        print(f"md5 outside (before) {before}\nmd5 outside (after)  {after}")
        if wrong:
            conn.rollback()
            print("\nVERIFY FAILED — rolled back:")
            for w in wrong:
                print("  " + w)
            return 1

        cognition.log_decision(
            cur, agent="health_office", action="row_schedule", domain="health",
            outcome="executed", manual_gap=True,
            metadata={"from": start.isoformat(), "rows": len(targets),
                      "days": [d.isoformat() for d in sorted(targets)]},
            assumptions={"rule": "every MSP morning is a row at msp_home; ladder/circuit by "
                                 "cycle position (artemis/row_schedule.py)",
                         "skipped_logged": [d.isoformat() for d in skipped_logged]})
        print(f"\nVERIFIED and committed. {len(targets)} mornings.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
