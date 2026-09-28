"""PROGRAM-2 — rebuild the Richfield rows whose CONTENT changed with the
2026-09-28 approvals (Strength B's table, and the warmup/cooldown).

Rebuilds with the builder, never by hand, and only rows that actually differ:
a row the approvals do not change is left alone rather than rewritten to itself.

Guards, all of them:
  * keys PINNED before anything is written;
  * md5 over every row OUTSIDE the pinned set, before and after;
  * every rebuilt row re-read and asserted equal to what the builder produced --
    the hash says nothing about the rows it excludes;
  * a row with a REAL log is skipped and reported, never rebuilt. 9/29 is the one
    that matters: if Ryan has already trained it, it stays as he did it.

    python3.11 scripts/rebuild_richfield_window.py            # dry run
    python3.11 scripts/rebuild_richfield_window.py --commit
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import date, timedelta

FROM, TO = date(2026, 9, 29), date(2026, 10, 4)


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
    """What we compare: the content the approvals can change."""
    return {"display_name": b.get("display_name"),
            "exercises": [e["name"] for e in b.get("exercises", [])],
            "warmup": b.get("warmup"), "cooldown": b.get("cooldown"),
            "prep_unknown": b.get("prep_unknown"),
            "equipment": b.get("equipment")}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()

    from artemis import health_office as office
    from knowledge import cognition
    from knowledge.db import get_connection

    with get_connection() as conn:
        cur = conn.cursor()
        d = FROM
        todo, skipped, same = [], [], []
        while d <= TO:
            cur.execute(
                "SELECT p.slot, p.session_type, p.blocks, p.week_num, "
                "  EXISTS (SELECT 1 FROM health.session_log sl WHERE sl.plan_id = p.plan_id "
                "          AND sl.logged_via <> 'inferred') "
                "FROM health.plan p WHERE p.plan_date = %s ORDER BY p.slot", (d,))
            for slot, st, blocks, wk, logged in cur.fetchall():
                b = blocks if isinstance(blocks, dict) else json.loads(blocks or "{}")
                if b.get("location_key") != "richfield":
                    continue
                if not st.startswith("strength"):
                    continue
                built, rpe, zone, est = office._build(
                    st, wk, location="Richfield", location_key="richfield")
                if _sig(b) == _sig(built):
                    same.append((d, slot, st))
                    continue
                if logged:
                    skipped.append((d, slot, st))
                    continue
                todo.append((d, slot, st, wk, b, built, rpe, zone, est))
            d += timedelta(days=1)

        for dd, slot, st in skipped:
            print(f"  SKIP  {dd} {slot:<7} {st} — carries a real log, left as he did it")
        for dd, slot, st in same:
            print(f"  same  {dd} {slot:<7} {st} — the approvals change nothing here")
        for dd, slot, st, _wk, b, built, *_ in todo:
            old_s, new_s = _sig(b), _sig(built)
            what = [k for k in new_s if old_s.get(k) != new_s.get(k)]
            print(f"  REBUILD {dd} {slot:<7} {st}  changes: {', '.join(what)}")
            if old_s["exercises"] != new_s["exercises"]:
                print(f"            {old_s['exercises']}")
                print(f"         -> {new_s['exercises']}")

        if not todo:
            print("\nNothing to rebuild.")
            return 0
        keys = {(dd, slot) for dd, slot, *_ in todo}
        before = _md5_outside(cur, keys)
        print(f"\n{len(todo)} row(s); md5 outside the set (before): {before}")
        if not args.commit:
            print("DRY RUN — nothing written.")
            return 0

        for dd, slot, st, wk, _b, built, rpe, zone, est in todo:
            cur.execute(office._UPSERT_SQL, (
                dd, slot, built.get("phase", 1), wk, st, json.dumps(built, default=str),
                rpe, zone, est, "manual", "PROGRAM-2: Richfield approvals"))

        after = _md5_outside(cur, keys)
        wrong = []
        if after != before:
            wrong.append("a row outside the pinned set changed")
        for dd, slot, st, _wk, _b, built, *_ in todo:
            cur.execute("SELECT blocks FROM health.plan WHERE plan_date = %s AND slot = %s",
                        (dd, slot))
            got = cur.fetchone()[0]
            got = got if isinstance(got, dict) else json.loads(got)
            if _sig(got) != _sig(built):
                wrong.append(f"{dd} {slot}: does not match the builder")
        if wrong:
            conn.rollback()
            print("VERIFY FAILED, rolled back: " + "; ".join(wrong))
            return 3

        cognition.log_decision(
            cur, agent="health_office", action="richfield_approvals", domain="health",
            outcome="executed", manual_gap=True,
            metadata={"rows": len(todo), "md5_outside": before,
                      "skipped_logged": [f"{d} {s}" for d, s, _ in skipped]},
            assumptions={
                "rule": "PROGRAM-2: apply the approved Richfield Strength B table "
                        "and the approved warmup/cooldown",
                "approved_by": "Ryan 2026-09-28 17:47",
                "rebuilt": [f"{d} {s} {t}" for d, s, t, *_ in todo],
                "unchanged": [f"{d} {s} {t}" for d, s, t in same],
                "skipped_because_logged": [f"{d} {s} {t}" for d, s, t in skipped],
            })
        cur.execute("UPDATE acos.audit_log SET source = 'script' WHERE id = ("
                    "SELECT id FROM acos.audit_log ORDER BY created_at DESC LIMIT 1)")
        print(f"md5 outside the set (after) : {after}  UNCHANGED")
        print("VERIFIED and committed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
