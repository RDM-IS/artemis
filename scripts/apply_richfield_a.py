"""PROGRAM-2 Step 1 — rebuild ONLY the 2026-09-29 morning row as Richfield
Strength A, using the builder.

Ryan approved the Richfield Strength A substitutions on 2026-09-28 as part of
PROGRAM-2, and 9/29 is the first day that needs them: the row asks for Office
Strength A at Richfield, which `can_hold` refused, so the week-ahead lookahead has
been flagging it as `session_cant_be_held` since 2026-09-28.

Migration discipline, all of it:
  * the target key is PINNED before anything is read or written;
  * an md5 over EVERY OTHER ROW is taken before and after and must be identical;
  * the rebuilt row is RE-READ and asserted equal to what the builder produced --
    the hash proves no collateral damage and says nothing about whether the one
    row it does not look at was written correctly (CLAUDE.md, 2026-09-26);
  * a real log on the row REFUSES the whole run;
  * one audit row, `source='script'` and `manual_gap=TRUE` (COGNITION-1 rule 2),
    carrying the decision's assumptions.

    python3.11 scripts/apply_richfield_a.py            # dry run
    python3.11 scripts/apply_richfield_a.py --commit
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import date

TARGET = date(2026, 9, 29)
SLOT = "morning"
LOCATION_KEY = "richfield"


def _md5_others(cur) -> str:
    """Hash of every plan row EXCEPT the pinned one."""
    cur.execute("SELECT plan_date, slot, week_num, session_type, blocks "
                "FROM health.plan ORDER BY plan_date, slot")
    h = hashlib.md5()
    for d, slot, wk, st, blocks in cur.fetchall():
        if (d, slot) == (TARGET, SLOT):
            continue
        b = blocks if isinstance(blocks, dict) else json.loads(blocks or "{}")
        h.update(json.dumps([str(d), slot, wk, st, b], sort_keys=True, default=str).encode())
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()

    from artemis import health_office as office
    from knowledge import cognition
    from knowledge.db import get_connection

    week = office.week_num_for(TARGET)
    location = (office._cycle.DEFAULT_LOCATIONS.get(LOCATION_KEY) or {}).get(
        "display", LOCATION_KEY)
    blocks, rpe, zone, est = office._build("strength_a", week, location=location,
                                           location_key=LOCATION_KEY)

    with get_connection() as conn:
        cur = conn.cursor()

        # REFUSE on a real log. `inferred` rows are the nightly backstop.
        cur.execute("SELECT count(*) FROM health.session_log sl JOIN health.plan p "
                    "ON p.plan_id = sl.plan_id WHERE p.plan_date = %s AND p.slot = %s "
                    "AND sl.logged_via <> 'inferred'", (TARGET, SLOT))
        logged = cur.fetchone()[0]
        if logged:
            print(f"REFUSED: {TARGET} {SLOT} carries {logged} real log row(s).")
            return 2

        cur.execute("SELECT session_type, week_num, blocks->>'location_key' "
                    "FROM health.plan WHERE plan_date = %s AND slot = %s", (TARGET, SLOT))
        before_row = cur.fetchone()
        if before_row is None:
            print(f"REFUSED: no {TARGET} {SLOT} row to rebuild.")
            return 2
        print(f"before : {before_row[0]} week {before_row[1]} at {before_row[2]}")
        print(f"after  : strength_a week {week} at {LOCATION_KEY} "
              f"(rpe {rpe}, est {est} min)")
        for e in blocks["exercises"]:
            print(f"           {e['name']:<32} {e['notes']}")

        before = _md5_others(cur)
        print(f"md5 of every other row (before): {before}")
        if not args.commit:
            print("\nDRY RUN — nothing written. Re-run with --commit.")
            return 0

        cur.execute(office._UPSERT_SQL, (
            TARGET, SLOT, blocks.get("phase", 1), week, "strength_a",
            json.dumps(blocks, default=str), rpe, zone, est, "manual",
            "PROGRAM-2: Richfield Strength A"))

        # GUARD 1 — nothing else moved.
        after = _md5_others(cur)
        # GUARD 2 — the row IS what the builder produced. The hash cannot see this.
        cur.execute("SELECT session_type, week_num, target_rpe, est_duration_min, blocks "
                    "FROM health.plan WHERE plan_date = %s AND slot = %s", (TARGET, SLOT))
        got = cur.fetchone()
        got_blocks = got[4] if isinstance(got[4], dict) else json.loads(got[4])
        wrong = []
        if got[0] != "strength_a":
            wrong.append(f"session_type {got[0]}")
        if got[1] != week:
            wrong.append(f"week_num {got[1]}")
        if float(got[2] or 0) != float(rpe):
            wrong.append(f"target_rpe {got[2]}")
        if got_blocks.get("location_key") != LOCATION_KEY:
            wrong.append(f"location_key {got_blocks.get('location_key')}")
        if [e["name"] for e in got_blocks.get("exercises", [])] != \
                [e["name"] for e in blocks["exercises"]]:
            wrong.append("exercise names")
        if after != before:
            wrong.append("a row outside the pinned key changed")
        if wrong:
            conn.rollback()
            print("VERIFY FAILED, rolled back: " + "; ".join(wrong))
            return 3

        cognition.log_decision(
            cur, agent="health_office", action="richfield_strength_a", domain="health",
            outcome="executed", manual_gap=True,
            metadata={"plan_date": TARGET.isoformat(), "slot": SLOT,
                      "md5_untouched": before, "est_duration_min": est},
            assumptions={
                "rule": "PROGRAM-2: Richfield can hold Strength A once its "
                        "substitution table is approved",
                "approved_by": "Ryan 2026-09-28",
                "week_num": week,
                "target_rpe": float(rpe),
                "location_key": LOCATION_KEY,
                "substitutions": blocks.get("substitutions"),
                "exercises": [e["name"] for e in blocks["exercises"]],
                "had_real_log": False,
            })
        # COGNITION-1 rule 2: a script's audit row marks itself.
        cur.execute("UPDATE acos.audit_log SET source = 'script' WHERE id = ("
                    "SELECT id FROM acos.audit_log ORDER BY created_at DESC LIMIT 1)")
        print(f"md5 of every other row (after) : {after}  UNCHANGED")
        print("VERIFIED and committed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
