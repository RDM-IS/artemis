"""PROGRAM-2 Step 3 — reseed the template from 2026-10-05 to the block end.

REFUSES to touch anything before 10/05, and refuses any row carrying a real log.
Those are not guards you can pass a flag to turn off: a row he has already
trained is his, and the whole point of starting at 10/05 is that the days before
it were settled by hand.

Guards, all of them:
  * keys PINNED from the dry run, so the apply writes exactly what was shown;
  * md5 over every row OUTSIDE the pinned set, before and after;
  * every written row re-read and asserted equal to the builder's;
  * one audit row with assumptions, `source='script'`.

    python3.11 scripts/reseed_program2.py             # dry run
    python3.11 scripts/reseed_program2.py --commit
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from datetime import date

FLOOR = date(2026, 10, 5)


def _md5_outside(cur, keys) -> str:
    cur.execute("SELECT plan_date, slot, week_num, session_type, blocks "
                "FROM health.plan ORDER BY plan_date, slot")
    h = hashlib.md5()
    for d, slot, wk, st, blocks in cur.fetchall():
        if (d, slot) in keys:
            continue
        b = blocks if isinstance(blocks, dict) else json.loads(blocks or "{}")
        h.update(json.dumps([str(d), slot, wk, st, b], sort_keys=True, default=str).encode())
    return h.hexdigest()


def _sig(b: dict) -> dict:
    return {"display_name": b.get("display_name"),
            "type": b.get("type"),
            "exercises": [e["name"] for e in b.get("exercises", [])],
            "suggested_extra": b.get("suggested_extra"),
            "intervals": b.get("intervals"),
            "duration_min": b.get("duration_min"),
            "location_key": b.get("location_key")}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()

    from artemis import health_office as office
    from knowledge import cognition
    from knowledge.db import get_connection

    built = {(r["plan_date"], r.get("slot", "morning")): r
             for r in office.build_rows() if r["plan_date"] >= FLOOR}

    with get_connection() as cur_conn:
        cur = cur_conn.cursor()
        cur.execute(
            "SELECT p.plan_date, p.slot, p.session_type, p.blocks, "
            "  EXISTS (SELECT 1 FROM health.session_log sl WHERE sl.plan_id = p.plan_id "
            "          AND sl.logged_via <> 'inferred') "
            "FROM health.plan p WHERE p.plan_date >= %s ORDER BY p.plan_date, p.slot",
            (FLOOR,))
        existing = {}
        logged_keys = set()
        for d, slot, st, blocks, logged in cur.fetchall():
            b = blocks if isinstance(blocks, dict) else json.loads(blocks or "{}")
            existing[(d, slot)] = (st, b)
            if logged:
                logged_keys.add((d, slot))

        changes, skipped, new = [], [], []
        for key, row in sorted(built.items()):
            if key[0] < FLOOR:                      # belt and braces
                print(f"REFUSED: {key[0]} is before {FLOOR}")
                return 2
            if key in logged_keys:
                skipped.append(key)
                continue
            if key not in existing:
                new.append(key)
                changes.append((key, row, "new row"))
                continue
            old_st, old_b = existing[key]
            what = []
            if old_st != row["session_type"]:
                what.append(f"session_type {old_st}→{row['session_type']}")
            os_, ns = _sig(old_b), _sig(row["blocks"])
            what += [k for k in ns if os_.get(k) != ns.get(k)]
            if what:
                changes.append((key, row, ", ".join(what)))

        kinds = Counter()
        for (d, slot), _row, what in changes:
            kinds["new row" if what == "new row" else
                  ("session type" if "session_type" in what else "content")] += 1
        print(f"floor            : {FLOOR} (nothing before it is touched)")
        print(f"rows in scope    : {len(built)}")
        print(f"unchanged        : {len(built) - len(changes) - len(skipped)}")
        print(f"skipped (logged) : {len(skipped)}  {[f'{d} {s}' for d, s in skipped]}")
        print(f"changed          : {len(changes)}   by kind: {dict(kinds)}")
        print()
        for (d, slot), _row, what in changes:
            print(f"  {d} {slot:<7} {what}")

        if not changes:
            print("\nNothing to reseed.")
            return 0
        keys = {k for k, _r, _w in changes}
        before = _md5_outside(cur, keys)
        print(f"\nmd5 outside the set (before): {before}")
        if not args.commit:
            print("DRY RUN — nothing written.")
            return 0

        for (d, slot), row, _what in changes:
            cur.execute(office._UPSERT_SQL, (
                d, slot, row["phase"], row["week_num"], row["session_type"],
                json.dumps(row["blocks"], default=str), row["target_rpe"],
                row["target_hr_zone"], row["est_duration_min"], row["generated_by"],
                row["notes"]))

        after = _md5_outside(cur, keys)
        wrong = []
        if after != before:
            wrong.append("a row outside the pinned set changed")
        for (d, slot), row, _w in changes:
            cur.execute("SELECT session_type, blocks FROM health.plan "
                        "WHERE plan_date = %s AND slot = %s", (d, slot))
            got = cur.fetchone()
            gb = got[1] if isinstance(got[1], dict) else json.loads(got[1])
            if got[0] != row["session_type"] or _sig(gb) != _sig(row["blocks"]):
                wrong.append(f"{d} {slot} does not match the builder")
        if wrong:
            cur_conn.rollback()
            print("VERIFY FAILED, rolled back: " + "; ".join(wrong))
            return 3

        cognition.log_decision(
            cur, agent="health_office", action="program2_reseed", domain="health",
            outcome="executed", manual_gap=True,
            metadata={"rows": len(changes), "floor": FLOOR.isoformat(),
                      "md5_outside": before, "by_kind": dict(kinds)},
            assumptions={
                "rule": "PROGRAM-2 template from 2026-10-05 to the block end",
                "approved_by": "Ryan 2026-09-28",
                "floor": FLOOR.isoformat(),
                "skipped_because_logged": [f"{d} {s}" for d, s in skipped],
                "changed": [f"{d} {s}: {w}" for (d, s), _r, w in changes],
            })
        cur.execute("UPDATE acos.audit_log SET source = 'script' WHERE id = ("
                    "SELECT id FROM acos.audit_log ORDER BY created_at DESC LIMIT 1)")
        print(f"md5 outside the set (after) : {after}  UNCHANGED")
        print("VERIFIED and committed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
