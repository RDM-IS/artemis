"""PROGRAM-2 Step 5 — the NAME-ONLY update for rows that are not otherwise rebuilt.

A session name says WHAT, never WHERE (Ryan, 2026-09-28). Rows already seeded
carry the old name in `blocks.display_name`; this rewrites that ONE FIELD and
nothing else.

Deliberately narrow, because a name change must not become a reseed:
  * only rows in [--from, --to], default 2026-09-29 .. 2026-10-04;
  * only `blocks.display_name`, recomputed from `display_name_for()` -- every
    other key in `blocks`, and every other column, is written back byte-identical;
  * a row carrying a REAL log is skipped, never renamed (past work keeps its
    name);
  * an md5 over every row OUTSIDE the touched set, before and after;
  * each touched row is re-read and asserted to differ in the name ALONE.

    python3.11 scripts/rename_future_sessions.py            # dry run
    python3.11 scripts/rename_future_sessions.py --commit
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import date

DEFAULT_FROM = date(2026, 9, 29)
DEFAULT_TO = date(2026, 10, 4)


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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--from", dest="d_from", default=DEFAULT_FROM.isoformat())
    ap.add_argument("--to", dest="d_to", default=DEFAULT_TO.isoformat())
    args = ap.parse_args()
    d_from, d_to = date.fromisoformat(args.d_from), date.fromisoformat(args.d_to)

    from artemis import health_office as office
    from knowledge import cognition
    from knowledge.db import get_connection

    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT p.plan_date, p.slot, p.session_type, p.blocks, "
            "  EXISTS (SELECT 1 FROM health.session_log sl WHERE sl.plan_id = p.plan_id "
            "          AND sl.logged_via <> 'inferred') "
            "FROM health.plan p WHERE p.plan_date BETWEEN %s AND %s "
            "ORDER BY p.plan_date, p.slot", (d_from, d_to))
        rows = cur.fetchall()

        plan = []
        for d, slot, st, blocks, logged in rows:
            b = blocks if isinstance(blocks, dict) else json.loads(blocks or "{}")
            old = b.get("display_name")
            modality = (b.get("cardio") or {}).get("modality")
            new = office.display_name_for(st, modality=modality)
            if logged:
                print(f"  SKIP  {d} {slot:<7} {old!r} — carries a real log")
                continue
            if old == new:
                continue
            plan.append((d, slot, b, old, new))
            print(f"  {d} {slot:<7} {old!r} -> {new!r}")

        if not plan:
            print("Nothing to rename.")
            return 0
        keys = {(d, slot) for d, slot, *_ in plan}
        before = _md5_outside(cur, keys)
        print(f"\n{len(plan)} row(s); md5 outside the set (before): {before}")
        if not args.commit:
            print("DRY RUN — nothing written.")
            return 0

        for d, slot, b, _old, new in plan:
            nb = dict(b)
            nb["display_name"] = new
            cur.execute("UPDATE health.plan SET blocks = %s::jsonb "
                        "WHERE plan_date = %s AND slot = %s",
                        (json.dumps(nb, default=str), d, slot))

        after = _md5_outside(cur, keys)
        wrong = []
        if after != before:
            wrong.append("a row outside the set changed")
        for d, slot, b, _old, new in plan:
            cur.execute("SELECT blocks FROM health.plan WHERE plan_date = %s AND slot = %s",
                        (d, slot))
            got = cur.fetchone()[0]
            got = got if isinstance(got, dict) else json.loads(got)
            if got.get("display_name") != new:
                wrong.append(f"{d} {slot}: name is {got.get('display_name')!r}")
            # THE name-only assertion: everything else must be identical.
            if {k: v for k, v in got.items() if k != "display_name"} != \
                    {k: v for k, v in b.items() if k != "display_name"}:
                wrong.append(f"{d} {slot}: something other than the name changed")
        if wrong:
            conn.rollback()
            print("VERIFY FAILED, rolled back: " + "; ".join(wrong))
            return 3

        cognition.log_decision(
            cur, agent="health_office", action="session_rename", domain="health",
            outcome="executed", manual_gap=True,
            metadata={"rows": len(plan), "from": d_from.isoformat(),
                      "to": d_to.isoformat(), "md5_outside": before},
            assumptions={
                "rule": "a session name says WHAT, never WHERE",
                "approved_by": "Ryan 2026-09-28 17:17",
                "renamed": [f"{d} {slot}: {old} -> {new}" for d, slot, _b, old, new in plan],
                "skipped_logged_rows": True,
                "fields_changed": ["blocks.display_name"],
            })
        cur.execute("UPDATE acos.audit_log SET source = 'script' WHERE id = ("
                    "SELECT id FROM acos.audit_log ORDER BY created_at DESC LIMIT 1)")
        print(f"md5 outside the set (after) : {after}  UNCHANGED")
        print("VERIFIED and committed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
