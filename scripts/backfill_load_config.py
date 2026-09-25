"""LOCATION-1 — write `blocks.load_config` onto plan rows that predate it.

Migration discipline, because this edits rows the iPad reads:

  * the target row ids are PINNED before anything is written, so a row created
    while this runs is never touched;
  * a row with ANY logged set is skipped — history is not rewritten;
  * the md5 of every untouched row is captured BEFORE the write and checked
    after, so "untouched" is proved rather than asserted;
  * one `acos.audit_log` row records what happened;
  * --commit is required. Without it this reports and writes nothing.

Usage:
    python3.11 scripts/backfill_load_config.py --through 2026-10-01 [--commit]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from knowledge import load_config                      # noqa: E402
from knowledge.db import get_connection                # noqa: E402


def _md5_of(rows) -> str:
    """A stable fingerprint of (plan_id, blocks) for a set of rows."""
    h = hashlib.md5()
    for pid, blocks in rows:
        h.update(str(pid).encode())
        h.update(json.dumps(blocks, sort_keys=True, default=str).encode())
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--through", required=True, help="last plan_date to touch (YYYY-MM-DD)")
    ap.add_argument("--commit", action="store_true", help="actually write")
    args = ap.parse_args()

    with get_connection() as conn:
        cur = conn.cursor()

        # 1) Everything in range, with its logged-set count.
        cur.execute("""
            SELECT p.plan_id, p.plan_date, p.slot, p.blocks,
                   (SELECT count(*) FROM health.session_log l WHERE l.plan_id = p.plan_id) AS logs
            FROM health.plan p
            WHERE p.plan_date <= %s
            ORDER BY p.plan_id
        """, (args.through,))
        rows = cur.fetchall()

        targets, skipped_logged, already, no_location = [], [], [], []
        for pid, pdate, slot, blocks, logs in rows:
            b = blocks if isinstance(blocks, dict) else json.loads(blocks or "{}")
            if b.get("load_config"):
                already.append(pid)
                continue
            if b.get("type") in ("rest", "recovery_flow"):
                continue                                   # nothing to load
            if logs:
                skipped_logged.append(pid)                 # never rewrite history
                continue
            key = b.get("location_key") or ("richfield" if (b.get("location") or "").lower().startswith("rich") else "office")
            cfg = load_config.for_location(key)
            if not cfg:
                no_location.append(pid)
                continue
            targets.append((pid, pdate, slot, b, key, cfg))

        target_ids = [t[0] for t in targets]              # PINNED
        untouched = [(pid, (blocks if isinstance(blocks, dict) else json.loads(blocks or "{}")))
                     for pid, _, _, blocks, _ in rows if pid not in set(target_ids)]
        before = _md5_of(untouched)

        print(f"rows through {args.through}: {len(rows)}")
        print(f"  already carry load_config : {len(already)}")
        print(f"  skipped, has logged sets  : {len(skipped_logged)} {skipped_logged or ''}")
        print(f"  skipped, no location cfg  : {len(no_location)} {no_location or ''}")
        print(f"  TO WRITE                  : {len(targets)}")
        print(f"  untouched rows            : {len(untouched)}")
        print(f"  md5(untouched) BEFORE     : {before}")
        if not args.commit:
            print("\ndry run — nothing written (pass --commit)")
            return 0

        for pid, _, _, b, key, cfg in targets:
            cur.execute("""
                UPDATE health.plan
                SET blocks = jsonb_set(blocks::jsonb, '{load_config}', %s::jsonb, true)
                WHERE plan_id = %s
            """, (json.dumps(cfg), pid))

        cur.execute("""
            SELECT plan_id, blocks FROM health.plan
            WHERE plan_date <= %s AND NOT (plan_id = ANY(%s))
            ORDER BY plan_id
        """, (args.through, target_ids or [0]))
        after_rows = [(pid, b if isinstance(b, dict) else json.loads(b or "{}"))
                      for pid, b in cur.fetchall()]
        after = _md5_of(after_rows)
        print(f"  md5(untouched) AFTER      : {after}")
        if after != before:
            conn.rollback()
            print("MISMATCH — untouched rows changed. Rolled back, nothing written.")
            return 1

        cur.execute("""
            INSERT INTO acos.audit_log (agent, persona, action, domain, confidence,
                                        outcome, token_count, api_cost_usd, metadata)
            VALUES ('backfill', NULL, 'load_config_backfill', 'health', NULL, 'executed', 0, 0,
                    CAST(%s AS jsonb))
        """, (json.dumps({
            "through": args.through,
            "written": len(targets),
            "plan_ids": target_ids,
            "skipped_logged": skipped_logged,
            "already_had_config": len(already),
            "untouched_rows": len(untouched),
            "md5_untouched_before": before,
            "md5_untouched_after": after,
        }, default=str),))
        conn.commit()
        print(f"\ncommitted: {len(targets)} rows written, {len(untouched)} untouched and verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
