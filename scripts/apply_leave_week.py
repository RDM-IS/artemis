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

WHAT THIS SCRIPT OWNS
---------------------
THE WHOLE WINDOW — every row from 9/26 to 10/04, with no exceptions. An assert
enforces it: a row may only escape the rebuild by carrying logged sets, which is
history and is never rewritten. So "reseed, then re-run" is deterministic for
every date, not for most of them.

  the four override spans     revoked and rewritten idempotently
  every row                   rebuilt from the resolver, so locations, load
                              configs, cardio modality and warmup/cooldown all
                              come from the location the cycle says the row is at
  SESSION_MOVES               TWO PLAN EDITS the seeder cannot produce (below)
  SLOT_LOCATIONS              2026-10-04 evening and 2026-09-29 morning (below)

2026-09-29 was the last carve-out and is now owned (Ryan, 2026-09-26). It is
strength_a at RICHFIELD with `prep_unknown`, and its SESSION AND EXERCISE LIST
ARE UNCHANGED: `subs_for("richfield", "strength_a")` is empty, so the builder
substitutes nothing, and the row keeps the six office movements. That row is
still knowingly wrong — Richfield cannot run a leg press — but it is now wrong
in the RIGHT PLACE, listing the equipment that room actually has instead of
office machines it does not, and a reseed can no longer move it while this file
says nothing. The A table is still outstanding; when it lands, the substitutions
appear here automatically and nothing in this script changes.

SESSION_MOVES — a PLAN EDIT, not a resolver change
--------------------------------------------------
`session_for()` picks the session POSITIONALLY from `cycle.DAY_TYPES` and
deliberately ignores overrides, which is what stops an override from silently
reshuffling which lift falls where. So moving a lift between dates is an
explicit edit that the seeder does not know about and will undo:

    2026-10-01 morning   strength_b -> rest         (Brown Deer: a treadmill, a
                                                     mat and bodyweight; it can
                                                     hold no lift at all)
    2026-10-03 morning   rest       -> strength_b   (Richfield)

10/03 is NOT a fix, and the report says so: `can_hold("richfield",
"strength_b")` is still False and the Richfield B table is still empty. It moves
the session from a room where 0 of 7 movements are possible to one where 1 is
native, 5 are substitutable once a table is approved, and 1 (Incline DB press)
needs an adjustable bench that room does not have — see CLASS-ATTRIBUTES.

RESEED, THEN RE-RUN THIS SCRIPT
------------------------------
This replaces the old "do not reseed" note. A reseed is now safe and expected:
the seeder resolves locations WITH overrides, so the location rows come back
byte-identical. What a reseed undoes is exactly what this script owns — the two
session moves and 10/04's evening location — and re-running restores both. The
script is idempotent: it asserts the target state rather than diffing against
what happens to be there, so running it twice changes nothing the second time.

  reseed the window  ->  python3.11 scripts/apply_leave_week.py --commit

SLOT_LOCATIONS — 2026-10-04 evening
-----------------------------------
WAKE-SLEEP: the model stores only the wake anchor and an override's location
wins for the whole day, so the resolver puts this row at Richfield. He wakes at
Richfield and sleeps in Minneapolis. The row is FORCED to msp_home rather than
merely skipped, because skipping it meant a reseed silently broke it.

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

#: PLAN EDITS this script owns: (date, slot) → the session that date must carry.
#: `session_for()` is positional and will undo these on a reseed, which is why
#: they live here and why re-running restores them. Keyed by date, not by what is
#: currently in the row, so the script asserts a target rather than diffing.
SESSION_MOVES: dict[tuple, str] = {
    (date(2026, 10, 1), "morning"): "rest",         # Brown Deer holds no lift
    (date(2026, 10, 3), "morning"): "strength_b",   # Richfield: still no B table
}

#: (date, slot) → the location this row MUST carry, stated rather than resolved.
#:
#:   10/04 evening  the resolver gets it WRONG. WAKE-SLEEP: an override's
#:                  location wins for the whole day, so it answers Richfield
#:                  when he sleeps in Minneapolis.
#:   09/29 morning  the resolver gets it RIGHT today (the 9/28-9/29 override
#:                  says richfield), and it is listed anyway — belt and braces,
#:                  so the row stays deterministic even if that override is ever
#:                  revoked, and so the window has NO date this file is silent
#:                  about (Ryan, 2026-09-26).
SLOT_LOCATIONS: dict[tuple, str] = {
    (date(2026, 10, 4), "evening"): "msp_home",
    (date(2026, 9, 29), "morning"): "richfield",
}

#: EMPTY as of 2026-09-26. 9/29 morning was the last entry: strength_a held at
#: the office while the Richfield A table is outstanding. Holding it there meant
#: the row claimed office machines in a room that has none, AND that a reseed
#: would move it while this script sat silent — the worst of both. It is owned
#: now: Richfield, prep_unknown, and the SESSION AND ITS EXERCISES UNTOUCHED,
#: because `subs_for("richfield", "strength_a")` is empty and a row nobody
#: approved is not an improvement on a row that is honestly wrong.
HOLD_SLOTS: dict[tuple, str] = {}


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
                   est_duration_min AS est,
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
            key = (r["plan_date"], r["slot"])
            #: what this row MUST end up as — a move target ignores what is
            #: there, which is what makes a re-run after a reseed restorative.
            r["want_session"] = SESSION_MOVES.get(key, r["session_type"])
            r["want_key"] = SLOT_LOCATIONS.get(key)      # None = ask the resolver
            if r["logs"]:
                logged.append(r)                              # never rebuilt
            elif key in HOLD_SLOTS:
                held.append((r, HOLD_SLOTS[key]))
            else:
                targets.append(r)

        # EVERY row in the window is owned — no carve-outs (Ryan, 2026-09-26).
        # A row may only escape the rebuild by carrying logged sets, which is
        # history and is never rewritten.
        unowned = [(r["plan_date"], r["slot"]) for r in rows
                   if r not in targets and not r["logs"]]
        assert not unowned, f"unowned rows in the window: {unowned}"

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
            # A forced location wins; otherwise the resolver answers — the anchor
            # for a morning, the 19:00 segment for an evening.
            key = r["want_key"] or (
                cycle.location_at(r["plan_date"], office.EVENING_AT)
                if r["slot"] == "evening" else cycle.anchor_location(r["plan_date"]))
            label = (cycle.locations().get(key) or {}).get("display", key)
            session = r["want_session"]
            spec = {"plan_date": r["plan_date"], "slot": r["slot"],
                    "session_type": session, "week_num": r["week_num"],
                    "location": label, "location_key": key,
                    "day_type": cycle.day_type(r["plan_date"]), "wk0": False}
            new = office.build_row(spec)
            b = new["blocks"]
            assert b["location"] == label and b["location_key"] == key, r["plan_id"]
            if session == "cardio_z2":
                assert "cardio" in b, "cardio_z2 must carry a resolved modality or the no-equipment state"
            # session_type is in the UPDATE because SESSION_MOVES changes it; the
            # notes line carries the display name, so it moves with it.
            cur.execute("""
                UPDATE health.plan
                SET session_type = %s, blocks = %s::jsonb, target_rpe = %s,
                    target_hr_zone = %s, est_duration_min = %s, notes = %s
                WHERE plan_id = %s
            """, (session, json.dumps(b, default=str), new["target_rpe"],
                  new["target_hr_zone"], new["est_duration_min"], new["notes"],
                  r["plan_id"]))
            old = _as_dict(r["blocks"])
            changed.append({
                "plan_id": r["plan_id"], "date": str(r["plan_date"]), "slot": r["slot"],
                "session_from": r["session_type"], "session_to": session,
                "moved": session != r["session_type"],
                "from": old.get("location"), "to": b["location"], "location_key": key,
                "forced_location": bool(r["want_key"]),
                "est_from": r["est"], "est_to": new["est_duration_min"],
                "cardio": (b.get("cardio") or {}).get("modality"),
                "device": (b.get("cardio") or {}).get("device"),
                "no_equipment": b.get("no_equipment"),
                "prep_unknown": bool(b.get("prep_unknown")),
                "has_load_config": "load_config" in b,
                "gained_load_config": "load_config" in b and "load_config" not in old,
                "can_hold": office.can_hold(key, session) if session.startswith("strength") else None,
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
            SELECT plan_id, plan_date, coalesce(slot,'morning'), blocks->>'location_key',
                   session_type
            FROM health.plan WHERE plan_id = ANY(%s)
        """, (target_ids,))
        by_id = {c["plan_id"]: c for c in changed}
        bad = []
        for pid, d, slot, got, got_session in cur.fetchall():
            want_key = SLOT_LOCATIONS.get((d, slot)) or (
                WANT[d][0] if slot == "morning" else cycle.location_at(d, office.EVENING_AT))
            if got != want_key:
                bad.append(f"{d} {slot}: location_key {got!r}, asked for {want_key!r}")
            # the plan edits are the whole point of the re-run, so assert them too
            want_session = SESSION_MOVES.get((d, slot), (by_id.get(pid) or {}).get("session_to"))
            if want_session and got_session != want_session:
                bad.append(f"{d} {slot}: session_type {got_session!r}, asked for {want_session!r}")
        if bad:
            conn.rollback()
            print("REBUILD WRONG — rolled back; the overrides stand, no row rebuilt:")
            for b in bad:
                print("    " + b)
            return 1
        print(f"  verified: all {len(target_ids)} rebuilt rows carry the session "
              "AND the location that were asked for")

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
            "session_moves": {f"{d} {slot}": sess for (d, slot), sess in SESSION_MOVES.items()},
            "forced_locations": {f"{d} {slot}": k for (d, slot), k in SLOT_LOCATIONS.items()},
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
            if c["prep_unknown"]:
                extra += "  [PREP-UNKNOWN]"
            cfg = "" if c["has_load_config"] else "  (no load_config)"
            if c["gained_load_config"]:
                cfg = "  (+load_config)"
            sess = (f"{c['session_from']} → {c['session_to']}" if c["moved"]
                    else c["session_to"])
            est = f"  est {c['est_from']}→{c['est_to']}" if c["est_from"] != c["est_to"] else ""
            loc = (f"{c['from']} → {c['to']}" + ("  [FORCED]" if c["forced_location"] else "")
                   if c["from"] != c["to"] or c["forced_location"] else c["to"])
            print(f"    {c['date']} {c['slot']:<8} {sess:<26} {loc}{est}{extra}{cfg}")

        moves = [c for c in changed if c["moved"]]
        if moves:
            print("\n  PLAN EDITS — the seeder will undo these; re-run after a reseed:")
            for c in moves:
                hold = ("" if c["can_hold"] is None
                        else f"   can_hold({c['location_key']}, {c['session_to']})={c['can_hold']}")
                print(f"    {c['date']} {c['slot']}: {c['session_from']} → "
                      f"{c['session_to']} @ {c['to']}{hold}")
        ests = [c for c in changed if c["est_from"] != c["est_to"]]
        if ests:
            print("\n  DURATION CHANGES (a real number moved):")
            for c in ests:
                print(f"    {c['date']} {c['slot']} {c['session_to']}: "
                      f"est_duration_min {c['est_from']} → {c['est_to']}")
        gained = [c for c in changed if c["gained_load_config"]]
        if gained:
            print(f"\n  GAINED load_config ({len(gained)} rows — their location had no "
                  "entry until its inventory was confirmed):")
            for c in gained:
                print(f"    {c['date']} {c['slot']} {c['session_to']} @ {c['to']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
