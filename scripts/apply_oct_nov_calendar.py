#!/usr/bin/env python3
"""Ryan's calendar 10/23 -> 11/30, as overrides (Ryan, 2026-10-21).

Dry run by default; `--commit` writes. Two phases, like apply_oct24_msp_home:

  1. OVERRIDES, committed on their own. The builder reads overrides on its own
     connection, so a rebuild in the same transaction would not see them (the
     2026-09-26 leave-week incident).
  2. REBUILD the block-1 mornings and evenings from tomorrow to 10/31 that the
     new overrides touch, with the md5 guard over every other row AND a
     per-row re-read. November has no plan rows yet: block 2 seeds from these
     overrides when it is seeded.

What it writes (newest override wins for a date, so every date gets exactly one
of ours and the old ones that would compete are revoked first):

  10/23 Fri         home (day off; rows)
  10/24-25 Sat-Sun  vacation, no gym, at home -> nothing planned
  10/26 Mon         travel                     -> rest
  10/27-28 Tue-Wed  work trip, hotel gym       -> Strength C, then Z2 (hotel)
  10/29 Thu         travel                     -> rest
  10/30 Fri         home (day off; rows)
  11/06-08 Fri-Sun  Brown Deer (Sun: drive back to MSP)
  11/09 Mon         home (day off)
  11/20-22          hunting, Richfield, no gym
  11/23-26          Richfield: Strength A, B, C, then Z2 on the bike (11/26)
  11/27-29          hunting, Richfield, no gym
  11/30 Mon         travel to MSP              -> rest

Revoked (not deleted): the 9/29 November placement set, the 9/29 10/24-10/26
msp_home override, and the 9/29 hunting stays -- all replaced above. 12/1-12/4
go back to the normal cycle. Anything else overlapping the window is LISTED and
left alone.

    PYTHONPATH="$PWD" /usr/bin/python3.11 scripts/apply_oct_nov_calendar.py
    PYTHONPATH="$PWD" /usr/bin/python3.11 scripts/apply_oct_nov_calendar.py --commit
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import date, timedelta

sys.path.insert(0, ".")

D = date
REASON = "Ryan 2026-10-21 calendar"
SET_BY = "ryan"
WINDOW = (D(2026, 10, 23), D(2026, 12, 4))

#: Reasons of the overrides this replaces.
REPLACES = ("Ryan 2026-09-29: approved Nov placement%",
            "Ryan 2026-09-29: msp_home 10/24-10/26%")

#: kind, start, end, location, purpose, lodging, session, note, day_type
#: day_type is WORK STATUS (business hours, nudges): off days are msp_home or
#: wi, the drives are travel, and an away stay is 'away'.
#:   kind "loc"   -> a plain location override
#:   kind "stay"  -> an away stay (day_type 'away'); location rides on the row
#:   kind "place" -> a placement (attrs.session) with its location
OVERRIDES = [
    ("loc",   D(2026, 10, 23), D(2026, 10, 23), "msp_home", None, None, None, "day off, rows", "msp_home"),
    ("stay",  D(2026, 10, 24), D(2026, 10, 25), "msp_home", "vacation", "no_gym", None, "vacation at home", None),
    ("place", D(2026, 10, 26), D(2026, 10, 26), "msp_home", None, None, "rest", "travel day", "travel"),
    ("stay",  D(2026, 10, 27), D(2026, 10, 27), "outside", "work", "hotel_gym", "strength_c", "hotel", None),
    ("stay",  D(2026, 10, 28), D(2026, 10, 28), "outside", "work", "hotel_gym", "cardio_z2", "hotel", None),
    ("place", D(2026, 10, 29), D(2026, 10, 29), "msp_home", None, None, "rest", "travel day", "travel"),
    ("loc",   D(2026, 10, 30), D(2026, 10, 30), "msp_home", None, None, None, "day off, rows", "msp_home"),
    ("loc",   D(2026, 11, 6), D(2026, 11, 8), "brown_deer", None, None, None, "Brown Deer; Sun drive to MSP", "wi"),
    ("loc",   D(2026, 11, 9), D(2026, 11, 9), "msp_home", None, None, None, "day off", "msp_home"),
    ("stay",  D(2026, 11, 20), D(2026, 11, 22), "richfield", "hunting", "no_gym", None, "hunting", None),
    ("place", D(2026, 11, 23), D(2026, 11, 23), "richfield", None, None, "strength_a", "", "wi"),
    ("place", D(2026, 11, 24), D(2026, 11, 24), "richfield", None, None, "strength_b", "", "wi"),
    ("place", D(2026, 11, 25), D(2026, 11, 25), "richfield", None, None, "strength_c", "", "wi"),
    ("place", D(2026, 11, 26), D(2026, 11, 26), "richfield", None, None, "cardio_z2", "Thanksgiving, the bike", "wi"),
    ("stay",  D(2026, 11, 27), D(2026, 11, 29), "richfield", "hunting", "no_gym", None, "hunting", None),
    ("place", D(2026, 11, 30), D(2026, 11, 30), "msp_home", None, None, "rest", "travel to MSP", "travel"),
]

BLOCK1_END = D(2026, 10, 31)


def _g(r, k, i):
    return r[k] if isinstance(r, dict) else r[i]


def _md5_outside(cur, keys: set) -> str:
    cur.execute("SELECT plan_date, slot, session_type, blocks::text FROM health.plan "
                "ORDER BY plan_date, slot")
    h = hashlib.md5()
    for r in cur.fetchall():
        vals = tuple(r.values()) if isinstance(r, dict) else tuple(r)
        if (vals[0], vals[1]) in keys:
            continue
        h.update(str(vals).encode())
    return h.hexdigest()


def _span(a, b):
    return f"{a}" if a == b else f"{a}..{b}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()

    from artemis import health_office as office
    from artemis import quiet_hours
    from knowledge import cognition
    from knowledge.db import get_connection

    tomorrow = quiet_hours.local_today() + timedelta(days=1)

    # ── PHASE 1: overrides ────────────────────────────────────────────────
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT override_id, start_date, end_date, day_type, location, purpose, attrs, reason "
            "FROM acos.cycle_day_overrides WHERE revoked_at IS NULL "
            "  AND start_date <= %s AND end_date >= %s ORDER BY start_date",
            (WINDOW[1], WINDOW[0]))
        live = cur.fetchall()
        ours = [r for r in live if (_g(r, "reason", 7) or "").startswith(REASON)]
        replace = [r for r in live if any(
            (_g(r, "reason", 7) or "").startswith(p.rstrip("%")) for p in REPLACES)]
        other = [r for r in live if r not in ours and r not in replace]

        print(f"already written by this script: {len(ours)}")
        print(f"to REVOKE (replaced): {len(replace)}")
        for r in replace:
            print(f"  #{_g(r, 'override_id', 0)} {_span(_g(r, 'start_date', 1), _g(r, 'end_date', 2))} "
                  f"{_g(r, 'reason', 7)}")
        if other:
            print(f"LEFT ALONE (overlap the window, not ours, not replaced): {len(other)}")
            for r in other:
                print(f"  #{_g(r, 'override_id', 0)} {_span(_g(r, 'start_date', 1), _g(r, 'end_date', 2))} "
                      f"loc={_g(r, 'location', 4)} type={_g(r, 'day_type', 3)} — {_g(r, 'reason', 7)}")
        print(f"\nto WRITE: {0 if ours else len(OVERRIDES)}")
        if not ours:
            for kind, a, b, loc, purpose, lodging, session, note, dtype in OVERRIDES:
                what = (f"{purpose}/{lodging}" if kind == "stay" else
                        f"{session}" if kind == "place" else "location")
                if kind == "stay" and session:
                    what += f" + {session}"
                print(f"  {_span(a, b):24s} {loc:10s} {what:28s} {note}")

        if not args.commit:
            print("\nDRY RUN — nothing written.")
        elif ours:
            print("\noverrides already present — phase 1 is a no-op")
        else:
            for r in replace:
                cur.execute("UPDATE acos.cycle_day_overrides SET revoked_at = now() "
                            "WHERE override_id = %s", (_g(r, "override_id", 0),))
            for kind, a, b, loc, purpose, lodging, session, note, dtype in OVERRIDES:
                attrs = {"session": session} if session else {}
                if note:
                    attrs["note"] = note
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
            # Read back: every date in every span resolves to OUR row.
            wrong = []
            for kind, a, b, loc, *_ in OVERRIDES:
                x = a
                while x <= b:
                    cur.execute("SELECT location, reason FROM acos.cycle_day_overrides "
                                "WHERE revoked_at IS NULL AND start_date <= %s AND end_date >= %s "
                                "ORDER BY created_at DESC LIMIT 1", (x, x))
                    got = cur.fetchone()
                    if not got or not (_g(got, "reason", 1) or "").startswith(REASON) \
                            or _g(got, "location", 0) != loc:
                        wrong.append(f"{x}: resolves to {got}")
                    x += timedelta(days=1)
            if wrong:
                conn.rollback()
                print("\nVERIFY FAILED — overrides rolled back:")
                for w in wrong:
                    print("  " + w)
                return 1
            conn.commit()
            print("\noverrides written and committed")

    # ── PHASE 2: rebuild block-1 rows the overrides touch ─────────────────
    days = set()
    for _k, a, b, *_ in OVERRIDES:
        x = a
        while x <= b:
            if tomorrow <= x <= BLOCK1_END:
                days.add(x)
            x += timedelta(days=1)
    if not days:
        print("no block-1 days left to rebuild")
        return 0

    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT DISTINCT p.plan_date FROM health.plan p "
            "JOIN health.session_log sl ON sl.plan_id = p.plan_id "
            "WHERE p.plan_date = ANY(%s) AND sl.logged_via <> 'inferred'", (sorted(days),))
        logged = {_g(r, "plan_date", 0) for r in cur.fetchall()}
        rows = office.build_rows()           # reads the overrides committed above
        if args.commit:
            office.validate_rows(rows)
        built = {(r["plan_date"], r["slot"]): r for r in rows
                 if r["plan_date"] in days and r["plan_date"] not in logged}
        # A day that loses its evening (an away day) must have it removed.
        cur.execute("SELECT plan_date, slot FROM health.plan WHERE plan_date = ANY(%s)",
                    (sorted(days),))
        existing = {(_g(r, "plan_date", 0), _g(r, "slot", 1)) for r in cur.fetchall()}
        drop = sorted(k for k in existing if k not in built and k[0] not in logged)

        print(f"\nblock-1 rows to rebuild ({len(built)}):")
        for (d, slot), r in sorted(built.items()):
            b = r["blocks"]
            print(f"  {d} {d.strftime('%a')} {slot:7s} {r['session_type']:16s} "
                  f"{b.get('display_name', ''):24s} {b.get('location_key')}")
        for d, slot in drop:
            print(f"  {d} {d.strftime('%a')} {slot:7s} REMOVE (no session that day)")
        if logged:
            print(f"left alone, already logged: {sorted(logged)}")

        if not args.commit:
            print("\nDRY RUN — nothing written. (The rebuild above is computed WITHOUT the "
                  "new overrides when they are not written yet.)")
            return 0

        keys = set(built) | set(drop)
        before = _md5_outside(cur, keys)
        for d, slot in drop:
            cur.execute("DELETE FROM health.plan WHERE plan_date = %s AND slot = %s "
                        "AND NOT EXISTS (SELECT 1 FROM health.session_log sl "
                        "  WHERE sl.plan_id = health.plan.plan_id)", (d, slot))
        for r in built.values():
            cur.execute(office._UPSERT_SQL, office.upsert_params(r))
        after = _md5_outside(cur, keys)
        wrong = [] if after == before else ["a row OUTSIDE the rebuilt set changed"]
        for (d, slot), r in built.items():
            cur.execute("SELECT session_type, blocks FROM health.plan "
                        "WHERE plan_date = %s AND slot = %s", (d, slot))
            got = cur.fetchone()
            if got is None:
                wrong.append(f"{d} {slot}: missing")
                continue
            blocks = _g(got, "blocks", 1)
            if isinstance(blocks, str):
                blocks = json.loads(blocks)
            if _g(got, "session_type", 0) != r["session_type"]:
                wrong.append(f"{d} {slot}: {_g(got, 'session_type', 0)} != {r['session_type']}")
            if blocks.get("location_key") != r["blocks"].get("location_key"):
                wrong.append(f"{d} {slot}: room {blocks.get('location_key')}")
        for d, slot in drop:
            cur.execute("SELECT 1 FROM health.plan WHERE plan_date = %s AND slot = %s", (d, slot))
            if cur.fetchone() is not None:
                wrong.append(f"{d} {slot}: still present")
        if wrong:
            conn.rollback()
            print("\nVERIFY FAILED — rows rolled back (overrides stay):")
            for w in wrong:
                print("  " + w)
            return 1
        cognition.log_decision(
            cur, agent="health_office", action="oct_nov_calendar", domain="health",
            outcome="executed", manual_gap=True,
            metadata={"overrides": len(OVERRIDES), "rows": len(built), "removed": len(drop)},
            assumptions={"source": "Ryan 2026-10-21 calendar",
                         "replaced": list(REPLACES), "logged_left_alone": sorted(map(str, logged))})
        print(f"\nVERIFIED and committed. {len(built)} rows rebuilt, {len(drop)} removed.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
