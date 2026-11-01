#!/usr/bin/env python3
"""Ryan's November, as told 2026-11-01 09:21. Overrides only; dry run by default.

  11/01-11/04  MSP, working Mon-Thu        -> unchanged (rows)
  11/05 Thu    fly MSP -> MKE              -> travel day: morning row, no evening
  11/06-11/07  Wisconsin (Richfield + BD)  -> unchanged (Brown Deer override)
  11/08 Sun    fly to MSP                  -> unchanged (travel, yoga + mobility evening)
  11/09-11/18  MSP                         -> unchanged (rows)
  11/19 Thu    drive to Richfield          -> travel day: morning row, no evening
  11/20 Fri    Richfield workout           -> Strength A at Richfield
  11/21-11/30  nothing planned             -> planned rest, no evening; he picks
                                              from the Start buttons

Revokes (never deletes) the 2026-10-21 calendar's seven overrides that start
11/20..11/30: hunting 11/20-22, Strength A/B/C 11/23-25, bike 11/26, hunting
11/27-29, travel-rest 11/30. A stay wins over a placement in block 2, so the
11/20 hunting stay has to go or the Strength A placement would be ignored.

Fail-closed: if the revoke set is not exactly those seven, nothing is written.
After --commit, rebuild the rows with seed_block2.py (dry run, then --commit);
it rewrites every unlogged block-2 day from the live overrides.

    PYTHONPATH="$PWD" /usr/bin/python3.11 scripts/apply_nov_update.py
    PYTHONPATH="$PWD" /usr/bin/python3.11 scripts/apply_nov_update.py --commit
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta

sys.path.insert(0, ".")

D = date
REASON = "Ryan 2026-11-01 calendar"
SET_BY = "ryan"
OLD_REASON = "Ryan 2026-10-21 calendar"
REVOKE_FROM, REVOKE_TO = D(2026, 11, 20), D(2026, 11, 30)
EXPECTED_REVOKES = 7

#: kind, start, end, location, purpose, lodging, session, note, day_type
OVERRIDES = [
    ("loc",   D(2026, 11, 5),  D(2026, 11, 5),  "msp_home", None, None, None,
     "travel day: row, then fly to MKE", "travel"),
    ("loc",   D(2026, 11, 19), D(2026, 11, 19), "msp_home", None, None, None,
     "travel day: row, then drive to Richfield", "travel"),
    ("place", D(2026, 11, 20), D(2026, 11, 20), "richfield", None, None, "strength_a",
     "Richfield workout", "wi"),
    # "No workout planned — I'll pick from the list": planned rest, no evening.
    # Two rows, both needed: the placement makes the morning `rest` (a vacation
    # stay alone keeps the cycle's session as a bodyweight sub), and the stay
    # suppresses the evening flow (block 2 plans no evening on an away day).
    ("place", D(2026, 11, 21), D(2026, 11, 30), "richfield", None, None, "rest",
     "nothing planned: pick from Start", "wi"),
    ("stay",  D(2026, 11, 21), D(2026, 11, 30), "richfield", "vacation", "no_gym", None,
     "nothing planned: pick from Start", None),
]


def _g(r, k, i):
    return r[k] if isinstance(r, dict) else r[i]


def _span(a, b):
    return f"{a}" if a == b else f"{a}..{b}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()

    from knowledge.db import get_connection

    lo = min(o[1] for o in OVERRIDES)
    hi = max(o[2] for o in OVERRIDES)

    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT override_id, start_date, end_date, day_type, location, attrs, reason "
            "FROM acos.cycle_day_overrides WHERE revoked_at IS NULL "
            "  AND start_date <= %s AND end_date >= %s ORDER BY start_date",
            (hi, lo))
        live = cur.fetchall()
        reason = lambda r: _g(r, "reason", 6) or ""
        ours = [r for r in live if reason(r).startswith(REASON)]
        revoke = [r for r in live if reason(r).startswith(OLD_REASON)
                  and REVOKE_FROM <= _g(r, "start_date", 1) <= REVOKE_TO]
        other = [r for r in live if r not in ours and r not in revoke]

        if ours:
            print(f"already written by this script: {len(ours)} — nothing to do")
            return 0

        print(f"to REVOKE ({len(revoke)}, expected {EXPECTED_REVOKES}):")
        for r in revoke:
            print(f"  #{_g(r, 'override_id', 0)} {_span(_g(r, 'start_date', 1), _g(r, 'end_date', 2)):24s} "
                  f"{_g(r, 'location', 4)} {_g(r, 'attrs', 5)}")
        if other:
            print(f"LEFT ALONE (overlap, not replaced): {len(other)}")
            for r in other:
                print(f"  #{_g(r, 'override_id', 0)} {_span(_g(r, 'start_date', 1), _g(r, 'end_date', 2)):24s} "
                      f"loc={_g(r, 'location', 4)} type={_g(r, 'day_type', 3)} — {reason(r)}")
        print(f"\nto WRITE ({len(OVERRIDES)}):")
        for kind, a, b, loc, purpose, lodging, session, note, dtype in OVERRIDES:
            what = (f"{purpose}/{lodging}" if kind == "stay" else
                    session if kind == "place" else f"location ({dtype})")
            print(f"  {_span(a, b):24s} {loc:10s} {what:24s} {note}")

        if len(revoke) != EXPECTED_REVOKES:
            print(f"\nSTOP — revoke set is {len(revoke)}, expected {EXPECTED_REVOKES}. Nothing written.")
            return 1
        if not args.commit:
            print("\nDRY RUN — nothing written.")
            return 0

        for r in revoke:
            cur.execute("UPDATE acos.cycle_day_overrides SET revoked_at = now() "
                        "WHERE override_id = %s AND revoked_at IS NULL",
                        (_g(r, "override_id", 0),))
            if cur.rowcount != 1:
                conn.rollback()
                print(f"\nVERIFY FAILED — #{_g(r, 'override_id', 0)} not revoked; rolled back")
                return 1
        for kind, a, b, loc, purpose, lodging, session, note, dtype in OVERRIDES:
            attrs = {"note": note}
            if session:
                attrs["session"] = session
            if kind == "stay":
                cur.execute(
                    "INSERT INTO acos.cycle_day_overrides "
                    "  (start_date, end_date, day_type, purpose, lodging, location, attrs, reason, set_by) "
                    "VALUES (%s, %s, 'away', %s, %s, %s, %s::jsonb, %s, %s)",
                    (a, b, purpose, lodging, loc, json.dumps(attrs), REASON, SET_BY))
            else:
                cur.execute(
                    "INSERT INTO acos.cycle_day_overrides "
                    "  (start_date, end_date, day_type, location, attrs, reason, set_by) "
                    "VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s)",
                    (a, b, dtype, loc, json.dumps(attrs), REASON, SET_BY))

        # Read back: every date resolves to OUR row, and no old-calendar row
        # survives on 11/20..11/30.
        wrong = []
        for kind, a, b, loc, *_ in OVERRIDES:
            x = a
            while x <= b:
                cur.execute("SELECT location, reason FROM acos.cycle_day_overrides "
                            "WHERE revoked_at IS NULL AND start_date <= %s AND end_date >= %s "
                            "ORDER BY created_at DESC LIMIT 1", (x, x))
                got = cur.fetchone()
                if (not got or not (_g(got, "reason", 1) or "").startswith(REASON)
                        or _g(got, "location", 0) != loc):
                    wrong.append(f"{x}: resolves to {got}")
                x += timedelta(days=1)
        cur.execute("SELECT count(*) AS n FROM acos.cycle_day_overrides WHERE revoked_at IS NULL "
                    "AND reason LIKE %s AND start_date BETWEEN %s AND %s",
                    (OLD_REASON + "%", REVOKE_FROM, REVOKE_TO))
        if _g(cur.fetchone(), "n", 0) != 0:
            wrong.append("a 2026-10-21 override on 11/20..11/30 is still live")
        if wrong:
            conn.rollback()
            print("\nVERIFY FAILED — rolled back:")
            for w in wrong:
                print("  " + w)
            return 1
        conn.commit()
        print(f"\noverrides committed: {len(revoke)} revoked, {len(OVERRIDES)} written.")
        print("Next: scripts/seed_block2.py (dry run, then --commit).")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
