"""REPEAT-WEEK — a program week with more than one session not done repeats
(Ryan, 2026-09-27; MAKEUP-2 is the one-session case).

    Sunday 08:35  the weekly job looks at the week that just ended. More than
                  one training session not done (missed or skipped) → it
                  PROPOSES repeating it and stores the proposal. Nothing moves.
    `repeat week` Ryan confirms. The repeat is recorded as the Sunday the repeat
                  week begins (health_office.REPEATS_KEY); every plan row from
                  that Sunday on is rebuilt with the program week held back one,
                  and the block's END MOVES OUT a week (Ryan: "#1 go").
    `no repeat`   the missed stay missed and the program carries on.

Why propose-then-confirm when Ryan's rule is unconditional: applying it
rewrites every future plan row, and CLAUDE.md gates any write to the system of
record on a human yes. The rule decides WHAT is proposed; the yes decides WHEN.

The write follows the migration discipline: target keys pinned first; rows
with real logs are never rewritten (reported instead); an md5 over every row
NOT being rebuilt is compared before and after; and every rebuilt row is re-read
and asserted equal to what the builder produced — the hash alone proves nothing
about the rows it doesn't look at (CLAUDE.md, 2026-09-26). Any failure rolls
back everything, the repeat record included.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import date, datetime, timedelta, timezone

logger = logging.getLogger(__name__)

PENDING_KEY = "repeat_week_pending"


# ---------------------------------------------------------------------------
# Proposal
# ---------------------------------------------------------------------------

def not_done_in(week_start: date) -> list[dict]:
    """Training sessions not done in the program week starting `week_start`."""
    from artemis import session_library as sl
    week_end = week_start + timedelta(days=6)
    rows = sl._load_week_rows(week_end, week_start)
    return sl.makeup_state(rows, week_end + timedelta(days=1), week_start)["not_done"]


def propose(week_start: date, now: datetime | None = None) -> str | None:
    """Store a proposal when the week had more than one not done; return the
    message to post, or None when there's nothing to propose."""
    from artemis import health_office as office
    from artemis.quiet_hours import get_system_value, set_system_value
    nd = not_done_in(week_start)
    if len(nd) <= 1:
        return None
    repeat_start = week_start + timedelta(days=7)
    if repeat_start in office.repeat_starts():
        return None
    wk = office.week_num_for(week_start)
    pending = {"week_start": week_start.isoformat(), "repeat_start": repeat_start.isoformat(),
               "week_num": wk, "not_done": nd,
               "proposed_at": (now or datetime.now(timezone.utc)).isoformat()}
    if get_system_value(PENDING_KEY) == json.dumps(pending, sort_keys=True):
        return None
    set_system_value(PENDING_KEY, json.dumps(pending, sort_keys=True))
    items = ", ".join(f"{date.fromisoformat(m['plan_date']):%a} {m['display_name']}"
                      + (" (skipped)" if m.get("skipped") else "") for m in nd)
    new_end = office.program_end() + timedelta(days=7)
    return (f"🔁 **Week {wk} had {len(nd)} sessions not done** — {items}.\n"
            f"Your rule: the week repeats. Reply `repeat week` to run week {wk} again from "
            f"{repeat_start:%a %-m/%-d} (the block then ends {new_end:%a %-m/%-d}), "
            f"or `no repeat` to carry on as planned.")


def pending() -> dict | None:
    from artemis.quiet_hours import get_system_value
    raw = get_system_value(PENDING_KEY)
    if not raw:
        return None
    p = json.loads(raw)
    # A proposal is live until its repeat week would have ended.
    if date.fromisoformat(p["repeat_start"]) + timedelta(days=6) < _today():
        return None
    return p


def _today() -> date:
    from artemis.quiet_hours import local_today
    return local_today()


def decline() -> str:
    from artemis.quiet_hours import set_system_value
    p = pending()
    set_system_value(PENDING_KEY, "")
    if not p:
        return "Nothing to repeat right now."
    return (f"Not repeating week {p['week_num']} — the missed sessions stay missed and the "
            f"program carries on.")


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------

def _blocks(v) -> dict:
    return v if isinstance(v, dict) else json.loads(v or "{}")


def _md5(rows) -> str:
    h = hashlib.md5()
    for r in rows:
        h.update(json.dumps([str(r[0]), r[1], r[2], r[3], _blocks(r[4])], sort_keys=True,
                            default=str).encode())
    return h.hexdigest()


def plan_rewrite(existing: list[dict], built: list[dict], repeat_start: date) -> dict:
    """Pure. Which (date, slot) to write, which to leave because they carry real
    logs. `existing`: rows on or after repeat_start {plan_date, slot, logged}."""
    logged = {(r["plan_date"], r["slot"]) for r in existing if r["logged"]}
    target = [b for b in built if b["plan_date"] >= repeat_start
              and (b["plan_date"], b.get("slot", "morning")) not in logged]
    return {"target": target, "kept_logged": sorted(logged)}


def apply(repeat_start: date) -> str:
    from artemis import health_office as office
    from artemis.quiet_hours import set_system_value
    from knowledge.db import get_connection

    reps = office.repeat_starts()
    if repeat_start in reps:
        return f"Week of {repeat_start:%-m/%-d} is already a repeat — nothing to do."
    new_reps = sorted(reps + [repeat_start])
    built = office.build_rows(new_reps)
    notes = office.validate_rows(built, end=office.program_end(new_reps))

    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT p.plan_date, p.slot, EXISTS (SELECT 1 FROM health.session_log sl "
            "WHERE sl.plan_id = p.plan_id AND sl.logged_via <> 'inferred') "
            "FROM health.plan p WHERE p.plan_date >= %s", (repeat_start,))
        existing = [{"plan_date": d, "slot": s, "logged": bool(l)} for d, s, l in cur.fetchall()]
        plan = plan_rewrite(existing, built, repeat_start)
        keys = [(r["plan_date"], r.get("slot", "morning")) for r in plan["target"]]  # PINNED

        def untouched():
            cur.execute("SELECT plan_date, slot, week_num, session_type, blocks FROM health.plan "
                        "ORDER BY plan_date, slot")
            return [r for r in cur.fetchall() if (r[0], r[1]) not in set(keys)]
        before = _md5(untouched())

        for r in plan["target"]:
            cur.execute(office._UPSERT_SQL, (
                r["plan_date"], r.get("slot", "morning"), r["phase"], r["week_num"],
                r["session_type"], json.dumps(r["blocks"]), r["target_rpe"],
                r["target_hr_zone"], r["est_duration_min"], r["generated_by"], r["notes"]))
        cur.execute(
            "INSERT INTO acos.system_state (key, value, updated_at) VALUES (%s, %s, now()) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
            (office.REPEATS_KEY, json.dumps([d.isoformat() for d in new_reps])))
        office.write_program_state(cur, new_reps)

        # Guard 1: nothing outside the pinned set moved.
        after = _md5(untouched())
        # Guard 2: every pinned row now IS what the builder produced.
        wrong = []
        for r in plan["target"]:
            cur.execute("SELECT week_num, session_type, blocks FROM health.plan "
                        "WHERE plan_date = %s AND slot = %s",
                        (r["plan_date"], r.get("slot", "morning")))
            got = cur.fetchone()
            if (not got or got[0] != r["week_num"] or got[1] != r["session_type"]
                    or _blocks(got[2]) != json.loads(json.dumps(r["blocks"], default=str))):
                wrong.append(f"{r['plan_date']} {r.get('slot', 'morning')}")
        if after != before or wrong:
            conn.rollback()
            logger.error("repeat week %s aborted: untouched changed=%s wrong=%s",
                         repeat_start, after != before, wrong)
            return ("⚠️ The repeat didn't verify, so nothing changed "
                    f"({'rows outside the rebuild moved; ' if after != before else ''}"
                    f"{len(wrong)} rebuilt row(s) didn't match). Check the logs.")
        cur.execute(
            "INSERT INTO acos.audit_log (agent, persona, action, domain, confidence, "
            "outcome, token_count, api_cost_usd, metadata) "
            "VALUES ('artemis', NULL, 'repeat_week', 'health', NULL, 'executed', 0, 0, %s::jsonb)",
            (json.dumps({"repeat_start": repeat_start.isoformat(),
                         "repeats": [d.isoformat() for d in new_reps],
                         "rows_rebuilt": len(keys),
                         "kept_logged": [f"{d} {s}" for d, s in plan["kept_logged"]],
                         "new_end": office.program_end(new_reps).isoformat(),
                         "md5_untouched": before, "time_cap_notes": len(notes)}),))
    set_system_value(PENDING_KEY, "")
    wk = office.week_num_for(repeat_start, new_reps)
    kept = (f" {len(plan['kept_logged'])} already-logged row(s) were left as they were."
            if plan["kept_logged"] else "")
    return (f"Done — week {wk} runs again from {repeat_start:%a %-m/%-d}. "
            f"{len(keys)} plan rows rebuilt; the block now ends "
            f"{office.program_end(new_reps):%a %-m/%-d}.{kept}")
