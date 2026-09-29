#!/usr/bin/env python3
"""Ryan's 10/24-10/26: msp_home all three days (no office gym).

Only the LOCATION is overridden. Setting day_type would change which sessions
`session_for()` plans, and Ryan's instruction keeps 10/24 and 10/26 as rest and
10/25 as the Z2 -- what changes is the room, so what the override says is the
room.

Guarded both ways (the round #15 lesson): the md5 over every row OUTSIDE the
three proves nothing else moved, AND each rebuilt row is re-read and matched
against what the builder produced. An unchanged hash alone would say nothing
about whether the three were rebuilt correctly.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import date

sys.path.insert(0, ".")

DAYS = (date(2026, 10, 24), date(2026, 10, 25), date(2026, 10, 26))
LOCATION_KEY = "msp_home"
REASON = "Ryan 2026-09-29: msp_home 10/24-10/26, no office gym"


def _md5_outside(cur, days) -> str:
    cur.execute(
        "SELECT plan_date, slot, session_type, blocks::text FROM health.plan "
        "WHERE plan_date <> ALL(%s) ORDER BY plan_date, slot", (list(days),))
    h = hashlib.md5()
    for r in cur.fetchall():
        h.update(str(tuple(r.values()) if isinstance(r, dict) else r).encode())
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()

    from artemis import health_office as office
    from knowledge import cognition
    from knowledge.db import get_connection

    # PHASE 1 — the override, COMMITTED ON ITS OWN.
    #
    # `office.build_rows()` resolves locations through `cycle.override_for()`,
    # which opens its OWN connection. An override inserted in the same
    # transaction as the rebuild is invisible to it, so the rebuild would
    # produce brown_deer rows and every guard would pass -- the hash only covers
    # OTHER days, and the row assertions compare against the same wrong build.
    #
    # That is exactly the 2026-09-26 leave-week incident, and the dry run showed
    # it happening here: the builder still said brown_deer / richfield because
    # the override did not exist yet.
    if args.commit:
        with get_connection() as pre:
            pcur = pre.cursor()
            pcur.execute(
                "SELECT 1 FROM acos.cycle_day_overrides WHERE start_date = %s "
                "AND end_date = %s AND revoked_at IS NULL", (DAYS[0], DAYS[-1]))
            if pcur.fetchone() is None:
                pcur.execute(
                    "INSERT INTO acos.cycle_day_overrides "
                    "  (start_date, end_date, location, reason, set_by) "
                    "VALUES (%s, %s, %s, %s, 'ryan')",
                    (DAYS[0], DAYS[-1], LOCATION_KEY, REASON))
                print("override inserted and committed")

    # PHASE 2 — rebuild, now that the override is visible to the builder.
    with get_connection() as conn:
        cur = conn.cursor()
        before = _md5_outside(cur, DAYS)
        print(f"md5 outside the three days (before): {before}")

        cur.execute(
            "SELECT 1 FROM acos.cycle_day_overrides WHERE start_date = %s "
            "AND end_date = %s AND revoked_at IS NULL", (DAYS[0], DAYS[-1]))
        have_override = cur.fetchone() is not None
        print(f"location override already present: {have_override}")

        # A logged day is never rebuilt.
        cur.execute(
            "SELECT DISTINCT p.plan_date FROM health.plan p "
            "JOIN health.session_log sl ON sl.plan_id = p.plan_id "
            "WHERE p.plan_date = ANY(%s) AND sl.logged_via <> 'inferred'", (list(DAYS),))
        logged = {r["plan_date"] if isinstance(r, dict) else r[0] for r in cur.fetchall()}
        if logged:
            print(f"SKIPPING logged days: {sorted(logged)}")

        built = {(r["plan_date"], r.get("slot", "morning")): r
                 for r in office.build_rows()
                 if r["plan_date"] in DAYS and r["plan_date"] not in logged}
        # The rebuild is only meaningful if the builder can SEE the override.
        # Without this a dry run reads as if nothing needs changing, and a real
        # run writes the unchanged rows while reporting success.
        wrong_loc = [f"{d} {slot}" for (d, slot), r in sorted(built.items())
                     if r["blocks"].get("location_key") != LOCATION_KEY]
        if wrong_loc and args.commit:
            conn.rollback()
            print(f"\nABORTED — the builder does not see the override for: "
                  f"{', '.join(wrong_loc)}")
            return 1
        print(f"\nrows the builder produces for those days: {len(built)}")
        for (d, slot), row in sorted(built.items()):
            b = row["blocks"]
            print(f"  {d} {slot:7s} {row['session_type']:15s} {b['display_name']:22s} "
                  f"{b.get('location_key')}")

        if not args.commit:
            print("\nDRY RUN — nothing written.")
            return 0

        for (d, slot), row in built.items():
            cur.execute(office._UPSERT_SQL, office.upsert_params(row))

        after = _md5_outside(cur, DAYS)
        wrong = []
        if after != before:
            wrong.append("a row OUTSIDE the three days changed")
        for (d, slot), row in built.items():
            cur.execute("SELECT session_type, blocks FROM health.plan "
                        "WHERE plan_date = %s AND slot = %s", (d, slot))
            got = cur.fetchone()
            if got is None:
                wrong.append(f"{d} {slot} missing after the write")
                continue
            g = (lambda k, i: got[k] if isinstance(got, dict) else got[i])
            blocks = g("blocks", 1)
            if isinstance(blocks, str):
                blocks = json.loads(blocks)
            if g("session_type", 0) != row["session_type"]:
                wrong.append(f"{d} {slot}: session_type {g('session_type', 0)}")
            if blocks.get("location_key") != row["blocks"].get("location_key"):
                wrong.append(f"{d} {slot}: location {blocks.get('location_key')}")
            if blocks.get("display_name") != row["blocks"].get("display_name"):
                wrong.append(f"{d} {slot}: name {blocks.get('display_name')}")
        print(f"md5 outside the three days (after) : {after}"
              f"{'  UNCHANGED' if after == before else '  CHANGED'}")
        if wrong:
            conn.rollback()
            print("\nVERIFY FAILED — rolled back:")
            for w in wrong:
                print("  " + w)
            return 1

        cognition.log_decision(
            cur, agent="health_office", action="oct24_msp_home", domain="health",
            outcome="executed", manual_gap=True,
            metadata={"days": [d.isoformat() for d in DAYS], "rows": len(built)},
            assumptions={"rule": "location-only override; day_type untouched so the "
                                 "planned sessions keep their shape",
                         "reason": REASON})
        print(f"\nVERIFIED and committed. {len(built)} rows.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
