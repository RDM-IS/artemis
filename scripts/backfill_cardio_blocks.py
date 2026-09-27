"""CARDIO-REQUIRED — write `blocks.cardio` onto cardio rows that predate CARDIO-LOC.

Why: the iPad logs a cardio session's modality straight off `blocks.cardio`
(the client never picks). On 2026-09-26 only 1 of 36 `cardio_z2` rows carried
it, so even a correctly logged cardio block would store `modality: null` and
the rowing baseline could never advance. This writes the resolution the
seeder would write today — `knowledge.cardio.resolve(location_key)` — and
nothing else.

Migration discipline (CLAUDE.md), because this edits rows the iPad reads:

  * the target ids are PINNED before anything is written;
  * only steady (`cardio_z2`) rows dated today or later are candidates, and a
    row with ANY log is skipped — history is not rewritten;
  * a row with no `location_key` is reported and skipped, never defaulted
    (FAIL-CLOSED-RESOLVERS);
  * md5 over every untouched row BEFORE and AFTER proves no collateral damage;
  * each written row is RE-READ and asserted to equal the resolution — the
    hash alone says nothing about the pinned rows (2026-09-26 lesson);
  * one `acos.audit_log` row; --commit required, dry run otherwise.

Usage (on the box):
    python3.11 scripts/backfill_cardio_blocks.py            # dry run
    python3.11 scripts/backfill_cardio_blocks.py --commit
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from knowledge import cardio as cardio_cfg             # noqa: E402

STEADY_SESSION_TYPES = ("cardio_z2",)


def _blocks(raw) -> dict:
    return raw if isinstance(raw, dict) else json.loads(raw or "{}")


def md5_of(rows) -> str:
    """A stable fingerprint of (plan_id, blocks) for a set of rows."""
    h = hashlib.md5()
    for pid, blocks in rows:
        h.update(str(pid).encode())
        h.update(json.dumps(_blocks(blocks), sort_keys=True, default=str).encode())
    return h.hexdigest()


@dataclass
class Plan:
    targets: list = field(default_factory=list)   # (plan_id, plan_date, resolved)
    already: list = field(default_factory=list)
    logged: list = field(default_factory=list)
    no_location: list = field(default_factory=list)
    not_steady: list = field(default_factory=list)


def plan_backfill(rows, resolve=cardio_cfg.resolve) -> Plan:
    """rows: (plan_id, plan_date, session_type, blocks, n_logs). Pure."""
    out = Plan()
    for pid, pdate, stype, raw, n_logs in rows:
        b = _blocks(raw)
        if stype not in STEADY_SESSION_TYPES or b.get("type") != "steady":
            out.not_steady.append(pid)
            continue
        if "cardio" in b:
            out.already.append(pid)
            continue
        if n_logs:
            out.logged.append(pid)
            continue
        key = b.get("location_key")
        if not key:
            out.no_location.append(pid)
            continue
        out.targets.append((pid, pdate, resolve(key)))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true", help="actually write")
    args = ap.parse_args()

    from artemis.quiet_hours import get_active_timezone  # noqa: E402
    from knowledge.db import get_connection             # noqa: E402

    tz = get_active_timezone()
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT p.plan_id, p.plan_date, p.session_type, p.blocks,
                   (SELECT count(*) FROM health.session_log l WHERE l.plan_id = p.plan_id)
            FROM health.plan p
            WHERE p.plan_date >= (now() AT TIME ZONE %s)::date
            ORDER BY p.plan_id
        """, (tz,))
        rows = cur.fetchall()
        p = plan_backfill(rows)

        target_ids = [t[0] for t in p.targets]            # PINNED
        pinned = set(target_ids)
        untouched = [(r[0], r[3]) for r in rows if r[0] not in pinned]
        before = md5_of(untouched)

        print(f"future plan rows            : {len(rows)}")
        print(f"  not a steady cardio row   : {len(p.not_steady)}")
        print(f"  already carry cardio      : {len(p.already)}")
        print(f"  skipped, has logs         : {len(p.logged)} {p.logged or ''}")
        print(f"  skipped, no location_key  : {len(p.no_location)} {p.no_location or ''}")
        print(f"  TO WRITE                  : {len(p.targets)}")
        for pid, pdate, res in p.targets:
            print(f"    {pdate}  plan {pid}: {res.get('modality')}/{res.get('device')}")
        print(f"  md5(untouched) BEFORE     : {before}")
        if not args.commit:
            print("\ndry run — nothing written (pass --commit)")
            return 0

        for pid, _, res in p.targets:
            cur.execute("""
                UPDATE health.plan
                SET blocks = jsonb_set(blocks::jsonb, '{cardio}', %s::jsonb, true)
                WHERE plan_id = %s
            """, (json.dumps(res), pid))

        # Guard 1: nothing outside the pinned set moved.
        cur.execute("""
            SELECT plan_id, blocks FROM health.plan
            WHERE plan_date >= (now() AT TIME ZONE %s)::date AND NOT (plan_id = ANY(%s))
            ORDER BY plan_id
        """, (tz, target_ids or [0]))
        after = md5_of(cur.fetchall())
        # Guard 2: every pinned row now carries exactly the resolution.
        cur.execute("SELECT plan_id, blocks FROM health.plan WHERE plan_id = ANY(%s)",
                    (target_ids or [0],))
        got = {pid: _blocks(b).get("cardio") for pid, b in cur.fetchall()}
        wrong = [pid for pid, _, res in p.targets if got.get(pid) != res]
        print(f"  md5(untouched) AFTER      : {after}")
        if after != before or wrong:
            conn.rollback()
            print(f"MISMATCH — untouched changed: {after != before}; wrong rows: {wrong}. "
                  "Rolled back, nothing written.")
            return 1

        cur.execute("""
            INSERT INTO acos.audit_log (agent, persona, action, domain, confidence,
                                        outcome, token_count, api_cost_usd, metadata)
            VALUES ('backfill', NULL, 'cardio_blocks_backfill', 'health', NULL, 'executed', 0, 0,
                    CAST(%s AS jsonb))
        """, (json.dumps({
            "written": len(p.targets), "plan_ids": target_ids,
            "skipped_logged": p.logged, "skipped_no_location": p.no_location,
            "already": len(p.already), "untouched_rows": len(untouched),
            "md5_untouched_before": before, "md5_untouched_after": after,
        }, default=str),))
        conn.commit()
        print(f"\ncommitted: {len(p.targets)} rows written and verified, "
              f"{len(untouched)} untouched and verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
