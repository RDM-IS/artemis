#!/usr/bin/env python3
"""Ryan's 2026-10-04: Zone 2 on the rower at msp_home, not the bike.

He drove to Minneapolis a day early, so Saturday's row (Richfield) was missed
and Sunday's row -- planned for Richfield, where only the bike is left once the
rower moves -- resolved to the bike in a room he is not in.

Only the LOCATION is overridden, for the one day. The session stays the Z2 the
plan already had; `knowledge.cardio` resolves msp_home to the rower from
2026-10-04 (ROWER-MOVE), so the modality follows from the room rather than
being written by hand. Saturday is left alone: unlogged, it reads as missed.

Guarded both ways: the md5 over every row OUTSIDE the day proves nothing else
moved, AND the rebuilt row is re-read and matched -- including that it is a
cardio on `row`, which is the whole point of the change.

The day already carried a richfield override (the original travel plan). The
newer msp_home one wins -- `cycle.override_for()` reads newest first -- and the
older row is left in place as history rather than revoked.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import date

sys.path.insert(0, ".")

DAYS = (date(2026, 10, 4),)
LOCATION_KEY = "msp_home"
REASON = "Ryan 2026-10-04: in MSP a day early; Z2 on the rower at msp_home, not the bike"
CARDIO_TYPES = ("cardio_z2", "cardio_intervals")
EXPECT_MODALITY = "row"


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
                "AND end_date = %s AND location = %s AND revoked_at IS NULL",
                (DAYS[0], DAYS[-1], LOCATION_KEY))
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
        print(f"md5 outside the day (before): {before}")

        cur.execute(
            "SELECT 1 FROM acos.cycle_day_overrides WHERE start_date = %s "
            "AND end_date = %s AND location = %s AND revoked_at IS NULL",
                (DAYS[0], DAYS[-1], LOCATION_KEY))
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
        # The point of the change: a Z2 on the rower. Anything else the builder
        # produces for the day is reported and never written.
        not_row = [f"{d} {slot}: {r['session_type']} on "
                   f"{(r['blocks'].get('cardio') or {}).get('modality')}"
                   for (d, slot), r in sorted(built.items())
                   if r["session_type"] in CARDIO_TYPES
                   and (r["blocks"].get("cardio") or {}).get("modality") != EXPECT_MODALITY]
        if not any(r["session_type"] in CARDIO_TYPES for r in built.values()):
            not_row.append(f"no cardio row is built for {DAYS[0]}")
        if not_row and args.commit:
            conn.rollback()
            print("\nABORTED — not cardio on the rower: " + "; ".join(not_row))
            return 1
        if not_row:
            print("WOULD ABORT on commit — " + "; ".join(not_row))
        if wrong_loc and args.commit:
            conn.rollback()
            print(f"\nABORTED — the builder does not see the override for: "
                  f"{', '.join(wrong_loc)}")
            return 1
        print(f"\nrows the builder produces for those days: {len(built)}")
        for (d, slot), row in sorted(built.items()):
            b = row["blocks"]
            print(f"  {d} {slot:7s} {row['session_type']:15s} {b['display_name']:22s} "
                  f"{b.get('location_key')}  cardio={(b.get('cardio') or {}).get('modality')}")

        if not args.commit:
            print("\nDRY RUN — nothing written.")
            return 0

        for (d, slot), row in built.items():
            cur.execute(office._UPSERT_SQL, office.upsert_params(row))

        after = _md5_outside(cur, DAYS)
        wrong = []
        if after != before:
            wrong.append("a row OUTSIDE the day changed")
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
            if (blocks.get("cardio") or {}).get("modality") != (row["blocks"].get("cardio") or {}).get("modality"):
                wrong.append(f"{d} {slot}: cardio {(blocks.get('cardio') or {}).get('modality')}")
            if blocks.get("display_name") != row["blocks"].get("display_name"):
                wrong.append(f"{d} {slot}: name {blocks.get('display_name')}")
        print(f"md5 outside the day (after) : {after}"
              f"{'  UNCHANGED' if after == before else '  CHANGED'}")
        if wrong:
            conn.rollback()
            print("\nVERIFY FAILED — rolled back:")
            for w in wrong:
                print("  " + w)
            return 1

        cognition.log_decision(
            cur, agent="health_office", action="oct04_msp_row", domain="health",
            outcome="executed", manual_gap=True,
            metadata={"days": [d.isoformat() for d in DAYS], "rows": len(built)},
            assumptions={"rule": "location-only override; day_type untouched so the "
                                 "planned sessions keep their shape",
                         "reason": REASON})
        print(f"\nVERIFIED and committed. {len(built)} rows.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
