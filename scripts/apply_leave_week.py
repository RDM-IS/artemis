"""CYCLE-2 — the leave-week overrides, and a rebuild of what can be rebuilt.

Wake locations as Ryan gave them (2026-09-26, superseding the earlier
"continuous at the farm" pair, which was wrong and is revoked by this script):

    09-26 brown_deer wi     09-30 brown_deer wi     10-04 richfield travel
    09-27 brown_deer wi     10-01 brown_deer wi
    09-28 richfield  wi     10-02 richfield  wi
    09-29 richfield  wi     10-03 richfield  wi

LOCATION IS THE WAKE LOCATION and an override applies to the whole day.
9/26 and 9/27 already resolve to brown_deer/wi from the base pattern, so they
get NO row — an override that merely restates the base is noise that has to be
revoked later. The remaining seven days collapse into four spans.

Migration discipline, because this rewrites rows the iPad reads:

  * target ids are PINNED before anything is written;
  * a row with ANY logged set is skipped — history is never rebuilt;
  * the md5 of every untouched row in health.plan is captured BEFORE the write
    and re-checked after, so "untouched" is proved rather than asserted;
  * one `acos.audit_log` row records exactly what moved;
  * --commit is required; without it nothing is written at all.

Rows deliberately NOT rebuilt:

  every strength row    9/29 strength_a, 10/01 strength_b, 10/02 strength_c.
                        Held pending the Richfield substitution tables (Ryan,
                        2026-09-26): report them as knowingly-wrong rather than
                        converting them. 10/02 was already converted to
                        Richfield in the earlier write under the prior
                        instruction — it is left as it stands, not re-reverted,
                        and reported.
  2026-10-04 evening    WAKE-SLEEP: the model stores only the wake anchor and
                        an override's location wins for the whole day, so
                        rebuilding this row would move it to Richfield. He
                        sleeps in Minneapolis. Its current `home` is the
                        CORRECT sleep location, so touching it would break it.

Reseeding, as of OVERRIDE-DURABILITY (2026-09-26): the seeder resolves WITH
overrides now, so a full reseed no longer reverts this window — all eleven
rebuilt rows come back byte-identical (proved against a TEMP-table reseed).
A reseed is still not a no-op for the three HELD rows: it would move 9/29 to
Richfield and 10/01 to Brown Deer (flagged knowingly-wrong, no substitution
table) and would break 10/04's evening by moving it to Richfield, which is the
open WAKE-SLEEP defect. Reseed the window only if you are prepared for those.

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

#: The wake location and day type for every day of the leave, as given.
WANT: dict[date, tuple[str, str]] = {
    date(2026, 9, 26): ("brown_deer", "wi"),
    date(2026, 9, 27): ("brown_deer", "wi"),
    date(2026, 9, 28): ("richfield", "wi"),
    date(2026, 9, 29): ("richfield", "wi"),
    date(2026, 9, 30): ("brown_deer", "wi"),
    date(2026, 10, 1): ("brown_deer", "wi"),
    date(2026, 10, 2): ("richfield", "wi"),
    date(2026, 10, 3): ("richfield", "wi"),
    date(2026, 10, 4): ("richfield", "travel"),
}

#: The minimal spans: contiguous runs of days that (a) differ from the base
#: pattern and (b) want the same values. Derived from WANT by _spans() below and
#: asserted against it, so the two cannot drift.
REASON = "leave week"

#: (date, slot) → why this row is left exactly as it is.
HOLD_SLOTS = {
    (date(2026, 10, 4), "evening"): "WAKE-SLEEP: sleeps in Minneapolis; `home` is already correct",
}
HOLD_SESSIONS = {
    "strength_a": "held pending the Richfield A table",
    "strength_b": "held pending the Richfield B table",
    "strength_c": "held per 2026-09-26; already converted to Richfield in the earlier write",
}


def _spans() -> list[tuple[date, date, str, str]]:
    """Fewest override rows that make WANT true, skipping days the base pattern
    already satisfies. Merges only adjacent days wanting identical values."""
    out: list[list] = []
    for d in sorted(WANT):
        loc, dt = WANT[d]
        if (cycle.anchor_location(d, use_overrides=False) == loc
                and cycle.day_type(d, use_overrides=False) == dt):
            continue                       # base pattern already says this
        if out and out[-1][1] == d - _ONE and out[-1][2:] == [dt, loc]:
            out[-1][1] = d                 # extend the run
        else:
            out.append([d, d, dt, loc])
    return [(a, b, dt, loc) for a, b, dt, loc in out]


from datetime import timedelta as _td                  # noqa: E402
_ONE = _td(days=1)


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

    spans = _spans()
    skipped = [d for d in sorted(WANT)
               if not any(a <= d <= b for a, b, _, _ in spans)]
    print("minimal override set — %d span(s); %d day(s) need none"
          % (len(spans), len(skipped)))
    for a, b, dt, loc in spans:
        print("    %s → %s  %-7s @ %s" % (a, b, dt, loc))
    for d in skipped:
        loc, dt = WANT[d]
        print("    %s  no row: the base pattern already gives %s / %s" % (d, loc, dt))

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
            elif (r["plan_date"], r["slot"]) in HOLD_SLOTS:
                held.append((r, HOLD_SLOTS[(r["plan_date"], r["slot"])]))
            elif r["session_type"] in HOLD_SESSIONS:
                held.append((r, HOLD_SESSIONS[r["session_type"]]))
            else:
                targets.append(r)

        target_ids = [r["plan_id"] for r in targets]          # PINNED
        before_pairs = _untouched(cur, target_ids)
        before = _md5(before_pairs)

        print(f"\nwindow {start} .. {end}: {len(rows)} rows")
        print(f"  to rebuild : {len(targets)}")
        print(f"  held back  : {len(held)}")
        for r, why in held:
            print(f"      {r['plan_date']} {r['slot']:<8} {r['session_type']:<13} — {why}")
        print(f"  logged     : {len(logged)} (never rebuilt)")
        print(f"  untouched rows in health.plan : {len(before_pairs)}")
        print(f"  md5(untouched) BEFORE : {before}")

        if not args.commit:
            print("\ndry run — nothing revoked, nothing written (pass --commit)")
            return 0

        # 1. Revoke any live override touching the window that is not in the
        #    minimal set. Soft delete: the ledger keeps the wrong ones visible.
        keep = {(a, b, dt, loc) for a, b, dt, loc in spans}
        cur.execute("""
            SELECT override_id, start_date, end_date, day_type, location
            FROM acos.cycle_day_overrides
            WHERE revoked_at IS NULL AND start_date <= %s AND end_date >= %s
        """, (end, start))
        for oid, s, e, dt, loc in cur.fetchall():
            if (s, e, dt, loc) in keep:
                continue
            cur.execute("UPDATE acos.cycle_day_overrides SET revoked_at = now() "
                        "WHERE override_id = %s", (oid,))
            print(f"  revoked override {oid}: {s} → {e}  {dt} @ {loc}")

        # 2. Insert the minimal set, idempotently.
        override_ids = []
        for a, b, dt, loc in spans:
            cur.execute("""
                SELECT override_id FROM acos.cycle_day_overrides
                WHERE revoked_at IS NULL AND start_date = %s AND end_date = %s
                  AND day_type = %s AND location = %s
            """, (a, b, dt, loc))
            existing = cur.fetchone()
            if existing:
                override_ids.append(existing[0])
                print(f"  override {existing[0]}: {a} → {b}  {dt} @ {loc}  (already present)")
                continue
            cur.execute("""
                INSERT INTO acos.cycle_day_overrides
                    (start_date, end_date, day_type, location, reason, set_by)
                VALUES (%s, %s, %s, %s, %s, 'ryan')
                RETURNING override_id
            """, (a, b, dt, loc, f"{REASON}: wake {loc}"))
            oid = cur.fetchone()[0]
            override_ids.append(oid)
            print(f"  override {oid}: {a} → {b}  {dt} @ {loc}")

        # cycle.override_for() reads through execute_one(), which opens its OWN
        # connection — it cannot see this transaction's uncommitted rows, and it
        # is fail-open, so it returns "no override" SILENTLY rather than raising.
        # Run 1 on 2026-09-26 rebuilt 12 rows against an empty table for exactly
        # that reason. Commit first, then prove the resolver agrees with WANT
        # for every single day before touching a plan row.
        conn.commit()
        for d, (loc, dt) in sorted(WANT.items()):
            got = (cycle.anchor_location(d), cycle.day_type(d))
            if got != (loc, dt):
                print(f"ABORT — {d} resolves to {got}, wanted {(loc, dt)}. Nothing rebuilt.")
                return 1
        print("  resolver agrees with the requested table on all "
              f"{len(WANT)} days")

        # 3. Rebuild the pinned rows.
        changed = []
        for r in targets:
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
                assert "cardio" in b, "cardio_z2 must carry a resolved modality or the no-equipment state"
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
                "from": old.get("location"), "to": b["location"], "location_key": key,
                "cardio": (b.get("cardio") or {}).get("modality"),
                "device": (b.get("cardio") or {}).get("device"),
                "no_equipment": b.get("no_equipment"),
                "has_load_config": "load_config" in b,
            })

        after = _md5(_untouched(cur, target_ids))
        print(f"  md5(untouched) AFTER  : {after}")
        if after != before:
            conn.rollback()
            print("MISMATCH — a row outside the pinned set changed. "
                  "Rolled back; the overrides stand, no plan row was rebuilt.")
            return 1

        # The md5 proves NO COLLATERAL DAMAGE and nothing else: it hashes the rows
        # OUTSIDE the pinned set, so it cannot see whether the rebuilt rows are
        # right. The first leave-week run reported an identical hash either side
        # and eleven wrong rows in the same breath. So re-read the pinned rows and
        # assert each one against what was ASKED FOR, not against what was built.
        cur.execute("""
            SELECT plan_id, plan_date, coalesce(slot,'morning'), blocks->>'location_key'
            FROM health.plan WHERE plan_id = ANY(%s)
        """, (target_ids,))
        bad = []
        for pid, d, slot, got in cur.fetchall():
            want_key = WANT[d][0] if slot == "morning" else (
                cycle.location_at(d, office.EVENING_AT))
            if got != want_key:
                bad.append(f"{d} {slot}: location_key {got!r}, asked for {want_key!r}")
        if bad:
            conn.rollback()
            print("REBUILD WRONG — rolled back; the overrides stand, no row rebuilt:")
            for b in bad:
                print("    " + b)
            return 1
        print(f"  verified: all {len(target_ids)} rebuilt rows carry the "
              "location that was asked for")

        cur.execute("""
            INSERT INTO acos.audit_log (agent, persona, action, domain, confidence,
                                        outcome, token_count, api_cost_usd, metadata)
            VALUES ('cycle2', NULL, 'leave_week_overrides', 'health', NULL, 'executed',
                    0, 0, CAST(%s AS jsonb))
        """, (json.dumps({
            "window": [str(start), str(end)],
            "requested": {str(d): list(v) for d, v in WANT.items()},
            "spans_written": [{"start": str(a), "end": str(b), "day_type": dt, "location": loc}
                              for a, b, dt, loc in spans],
            "days_needing_no_row": [str(d) for d in skipped],
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
            if c["no_equipment"]:
                extra = "  [NO CARDIO EQUIPMENT]"
            cfg = "" if c["has_load_config"] else "  (no load_config)"
            print(f"    {c['date']} {c['slot']:<8} {c['session_type']:<14} "
                  f"{c['from']} → {c['to']}{extra}{cfg}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
