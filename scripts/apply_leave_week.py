"""CYCLE-2 — write the leave-week overrides and rebuild the rows that can be.

Approved by Ryan 2026-09-26:

    2026-09-26 → 2026-10-03   wi       richfield   leave at the farm, continuous
    2026-10-04 → 2026-10-04   travel   richfield   wake Richfield, drive to MSP

Migration discipline, because this rewrites rows the iPad reads:

  * target ids are PINNED before anything is written;
  * a row with ANY logged set is skipped — history is never rebuilt;
  * the md5 of every untouched row in health.plan is captured BEFORE the write
    and re-checked after, so "untouched" is proved rather than asserted;
  * one `acos.audit_log` row records exactly what moved;
  * --commit is required; without it nothing is written at all.

Rows deliberately NOT rebuilt, which is the point rather than an omission:

  2026-09-29 strength_a   Richfield has no approved substitution table for A
  2026-10-01 strength_b   or B. They stay as office sessions: KNOWINGLY WRONG
                          beats silently rebuilt into something nobody approved.
  2026-10-04 evening      He drives to Minneapolis that day. An override's
                          location wins for the WHOLE day (cycle.location_at),
                          so the resolver cannot say "Richfield in the morning,
                          Minneapolis by evening" — it would move this row to
                          Richfield, where he will not be. Left at home.

Note for whoever reseeds next: health_office.day_location_key() resolves with
`use_overrides=False` — the seeder builds the BASE pattern. A full reseed of
this window will therefore revert these rows. That is the CYCLE-OVERRIDE-SOURCE
question, still open; until it is settled, do not reseed 9/26..10/04.

Usage:
    python3.11 scripts/apply_leave_week.py [--commit]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from artemis import cycle                              # noqa: E402
from artemis import health_office as office            # noqa: E402
from knowledge.db import get_connection                # noqa: E402

WINDOW = (date(2026, 9, 26), date(2026, 10, 4))

OVERRIDES = [
    ("2026-09-26", "2026-10-03", "wi", "richfield", "leave at the farm, continuous"),
    ("2026-10-04", "2026-10-04", "travel", "richfield", "wake Richfield, drive to Minneapolis"),
]

#: (date, slot) → why this row is left exactly as it is.
HOLD = {
    (date(2026, 9, 29), "morning"): "strength_a: no approved Richfield table",
    (date(2026, 10, 1), "morning"): "strength_b: no approved Richfield table",
    (date(2026, 10, 4), "evening"): "in Minneapolis by evening; an override is day-wide",
}


def _as_dict(b) -> dict:
    return b if isinstance(b, dict) else json.loads(b or "{}")


def _md5(pairs) -> str:
    h = hashlib.md5()
    for pid, blocks in sorted(pairs, key=lambda p: p[0]):
        h.update(str(pid).encode())
        h.update(json.dumps(blocks, sort_keys=True, default=str).encode())
    return h.hexdigest()


def _untouched(cur, target_ids: list[int]) -> list[tuple]:
    cur.execute("SELECT plan_id, blocks FROM health.plan WHERE NOT (plan_id = ANY(%s))",
                (target_ids or [-1],))
    return [(pid, _as_dict(b)) for pid, b in cur.fetchall()]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()
    start, end = WINDOW

    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT plan_id, plan_date, slot, session_type, week_num, blocks,
                   (SELECT count(*) FROM health.session_log l
                     WHERE l.plan_id = p.plan_id) AS logs
            FROM health.plan p
            WHERE plan_date BETWEEN %s AND %s
            ORDER BY plan_date, CASE WHEN slot = 'evening' THEN 1 ELSE 0 END
        """, (start, end))
        cols = [c.name for c in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]

        targets, held, logged = [], [], []
        for r in rows:
            r["slot"] = r["slot"] or "morning"
            if r["logs"]:
                logged.append(r)                              # never rebuilt
            elif (r["plan_date"], r["slot"]) in HOLD:
                held.append((r, HOLD[(r["plan_date"], r["slot"])]))
            else:
                targets.append(r)

        target_ids = [r["plan_id"] for r in targets]          # PINNED
        before_pairs = _untouched(cur, target_ids)
        before = _md5(before_pairs)

        print(f"window {start} .. {end}: {len(rows)} rows")
        print(f"  to rebuild : {len(targets)}")
        print(f"  held back  : {len(held)}")
        for r, why in held:
            print(f"      {r['plan_date']} {r['slot']:<8} {r['session_type']:<13} — {why}")
        print(f"  logged     : {len(logged)} (never rebuilt)")
        print(f"  untouched rows in health.plan : {len(before_pairs)}")
        print(f"  md5(untouched) BEFORE : {before}")

        if not args.commit:
            print("\ndry run — no overrides written, no rows rebuilt (pass --commit)")
            return 0

        # IDEMPOTENT. These rows must be COMMITTED before the rebuild resolves
        # anything: cycle.override_for() goes through execute_one(), which opens
        # its OWN connection, so it cannot see this transaction's uncommitted
        # inserts — and it is fail-open, so it returns "no override" silently
        # rather than raising. Run 1 (2026-09-26) rebuilt 12 rows against an
        # empty override table for exactly that reason.
        override_ids = []
        for s, e, day_type, loc, reason in OVERRIDES:
            cur.execute("""
                SELECT override_id FROM acos.cycle_day_overrides
                WHERE revoked_at IS NULL AND start_date = %s AND end_date = %s
                  AND day_type = %s AND location = %s
            """, (s, e, day_type, loc))
            existing = cur.fetchone()
            if existing:
                override_ids.append(existing[0])
                print(f"  override {existing[0]}: {s} → {e}  {day_type} @ {loc}  (already present)")
                continue
            cur.execute("""
                INSERT INTO acos.cycle_day_overrides
                    (start_date, end_date, day_type, location, reason, set_by)
                VALUES (%s, %s, %s, %s, %s, 'ryan')
                RETURNING override_id
            """, (s, e, day_type, loc, reason))
            oid = cur.fetchone()[0]
            override_ids.append(oid)
            print(f"  override {oid}: {s} → {e}  {day_type} @ {loc}  ({reason})")
        conn.commit()      # so the resolver below can actually SEE them

        # Prove it, rather than assuming: resolve one leave day and one travel
        # day and refuse to rebuild anything if the override is not visible.
        for probe in (start, date(2026, 10, 4)):
            ov = cycle.override_for(probe)
            if not ov or ov.get("location") != "richfield":
                print(f"ABORT — override not visible to the resolver for {probe}: {ov!r}. "
                      "Nothing rebuilt.")
                return 1

        changed = []
        for r in targets:
            # Resolve WITH the committed overrides. The morning is the day's
            # anchor (where he wakes); the evening asks the segment-aware
            # resolver at 19:00 — an override's location wins for the whole day,
            # so during the leave both answer richfield, but asking the right
            # question keeps this correct if an override is ever absent.
            key = (cycle.location_at(r["plan_date"], office.EVENING_AT)
                   if r["slot"] == "evening" else cycle.anchor_location(r["plan_date"]))
            label = (cycle.locations().get(key) or {}).get("display", key)
            spec = {"plan_date": r["plan_date"], "slot": r["slot"],
                    "session_type": r["session_type"], "week_num": r["week_num"],
                    "location": label, "location_key": key,
                    "day_type": cycle.day_type(r["plan_date"]), "wk0": False}
            new = office.build_row(spec)
            b = new["blocks"]
            assert b["location"] == label and b["location_key"] == key, r["plan_id"]
            if r["session_type"] == "cardio_z2":
                assert b.get("cardio"), "cardio_z2 must carry a resolved modality"
            cur.execute("""
                UPDATE health.plan
                SET blocks = %s::jsonb, target_rpe = %s, target_hr_zone = %s,
                    est_duration_min = %s, notes = %s
                WHERE plan_id = %s
            """, (json.dumps(b, default=str), new["target_rpe"], new["target_hr_zone"],
                  new["est_duration_min"], new["notes"], r["plan_id"]))
            old = _as_dict(r["blocks"])
            changed.append({
                "plan_id": r["plan_id"], "date": str(r["plan_date"]), "slot": r["slot"],
                "session_type": r["session_type"],
                "from": old.get("location"), "to": b["location"],
                "location_key": key,
                "cardio": (b.get("cardio") or {}).get("modality"),
                "device": (b.get("cardio") or {}).get("device"),
            })

        after = _md5(_untouched(cur, target_ids))
        print(f"  md5(untouched) AFTER  : {after}")
        if after != before:
            conn.rollback()
            print("MISMATCH — a row outside the pinned set changed. "
                  "Rolled back; nothing written.")
            return 1

        cur.execute("""
            INSERT INTO acos.audit_log (agent, persona, action, domain, confidence,
                                        outcome, token_count, api_cost_usd, metadata)
            VALUES ('cycle2', NULL, 'leave_week_overrides', 'health', NULL, 'executed',
                    0, 0, CAST(%s AS jsonb))
        """, (json.dumps({
            "window": [str(start), str(end)],
            "overrides": [dict(zip(("start", "end", "day_type", "location", "reason"), o))
                          for o in OVERRIDES],
            "override_ids": override_ids,
            "rebuilt": changed,
            "rebuilt_plan_ids": target_ids,
            "held_back": [{"date": str(r["plan_date"]), "slot": r["slot"],
                           "session_type": r["session_type"], "why": w} for r, w in held],
            "logged_rows_skipped": [r["plan_id"] for r in logged],
            "untouched_row_count": len(before_pairs),
            "md5_untouched_before": before,
            "md5_untouched_after": after,
        }, default=str),))
        conn.commit()

        print(f"\ncommitted: {len(override_ids)} overrides, {len(changed)} rows rebuilt, "
              f"{len(before_pairs)} untouched and verified")
        for c in changed:
            extra = f"  [{c['cardio']}/{c['device']}]" if c["cardio"] else ""
            print(f"    {c['date']} {c['slot']:<8} {c['session_type']:<14} "
                  f"{c['from']} → {c['to']}{extra}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
