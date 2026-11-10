"""REPEAT-WEEK — a program week below the strength quota repeats
(Ryan, 2026-09-27; re-keyed by SESSION-OPEN, 2026-11-09).

    Monday 08:35  the weekly job looks at the Mon..Sun GOAL week that just
                  ended (STATUS-3 phase 2, 2026-11-10). Fewer than
                  3 strength days (knowledge.strength_quota — plan rows are
                  suggestions, so "sessions not done" no longer means anything)
                  → it PROPOSES repeating it and stores the proposal. Nothing
                  moves. Weeks before acos.system_state['strength_quota_counts_from'] never propose.
    `repeat week` Ryan confirms. The repeat is recorded as the Sunday the repeat
                  week begins (health_office.REPEATS_KEY) -- the Sunday that
                  closed the judged goal week, so the program week that began
                  the day before the proposal. Both Sundays are rest days in
                  the template, so starting the repeat there costs no session; every plan row from
                  that Sunday on is rebuilt with the program week held back one,
                  and the block's END MOVES OUT a week (Ryan: "#1 go").
    `no repeat`   the program carries on as planned.

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

def quota_for(week_start: date) -> dict:
    """The strength quota for the Mon..Sun goal week starting `week_start`.
    Raises strength_quota.QuotaUnreadable — never 'no strength days'."""
    from artemis.quiet_hours import get_active_timezone, local_today
    from knowledge import strength_quota as sq
    return sq.read_pg(week_start, tz=get_active_timezone(), today=local_today())


def propose(week_start: date, now: datetime | None = None) -> str | None:
    """Store a proposal when the finished week was below the quota; return the
    message to post, or None when there's nothing to propose. An unreadable
    quota raises (the caller logs it) — it does not propose and it does not
    stay silent as if the week were fine."""
    from artemis import health_office as office
    from artemis.quiet_hours import get_system_value, set_system_value
    if week_start.weekday() != 0:
        raise ValueError(f"a goal week starts on a Monday, got {week_start:%a} {week_start}")
    q = quota_for(week_start)
    if not q["repeat_due"]:
        return None
    # The program week (Sun..Sat) that starts on the goal week's closing Sunday.
    repeat_start = week_start + timedelta(days=6)
    if repeat_start in office.repeat_starts():
        return None
    wk = office.week_num_for(week_start)
    pending = {"week_start": week_start.isoformat(), "repeat_start": repeat_start.isoformat(),
               "week_num": wk, "strength_days": q["strength_days"], "count": q["count"],
               "required": q["required"],
               "proposed_at": (now or datetime.now(timezone.utc)).isoformat()}
    if get_system_value(PENDING_KEY) == json.dumps(pending, sort_keys=True):
        return None
    set_system_value(PENDING_KEY, json.dumps(pending, sort_keys=True))
    days = ", ".join(f"{date.fromisoformat(d):%a %-m/%-d}" for d in q["strength_days"]) or "none"
    new_end = office.program_end() + timedelta(days=7)
    return (f"🔁 **Week {wk} had {q['count']} of {q['required']} strength days** ({days}).\n"
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
    return f"Not repeating week {p['week_num']} — the program carries on as planned."


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


def _assumptions(repeat_start: date, reps: list, new_reps: list, plan: dict,
                 keys: list, md5_before: str, office) -> dict:
    """What the repeat believed when it rewrote the plan (COGNITION-1 bronze).

    Deliberately NOT a copy of `metadata`: metadata records what the write did,
    assumptions records what it took to be true beforehand. The one that matters
    is the quota — the rule fires on "fewer than 3 strength days", and the
    audit row must say WHICH days counted, so a repeat that turns out to have
    been wrong can be argued with.

    The proposal is read for that, and an unreadable proposal is recorded as
    UNKNOWN rather than as an empty list. Absence and silence are different
    answers (FAIL-CLOSED-RESOLVERS); an empty `strength_days` would claim the
    rule fired on a week with none. This never raises: a decision record must not be able to
    break the decision it describes.
    """
    try:
        p = pending()
    except Exception as exc:                                        # noqa: BLE001
        logger.warning("repeat assumptions: proposal unreadable — %s", exc)
        p = None
        trigger = {"strength_days": None, "count": None, "required": None,
                   "source": "unreadable: the proposal could not be read"}
    else:
        if p is None:
            trigger = {"strength_days": None, "count": None, "required": None,
                       "source": "absent: applied with no live proposal"}
        else:
            # A proposal written before SESSION-OPEN carries `not_done`, not
            # the quota; recorded as unknown rather than as zero days.
            trigger = {"strength_days": p.get("strength_days"), "count": p.get("count"),
                       "required": p.get("required"),
                       "source": "proposal",
                       "proposed_at": p.get("proposed_at"),
                       "proposal_week_num": p.get("week_num")}
    return {
        "rule": "a program week with fewer than 3 strength days repeats (SESSION-OPEN)",
        "trigger": trigger,
        "week_num": office.week_num_for(repeat_start, new_reps),
        "repeat_start": repeat_start.isoformat(),
        "repeats_before": [d.isoformat() for d in reps],
        "rows_to_rewrite": len(keys),
        "rows_kept_because_logged": [f"{d} {s}" for d, s in plan["kept_logged"]],
        "md5_untouched_before": md5_before,
        "program_end_before": office.program_end(reps).isoformat(),
        "program_end_after": office.program_end(new_reps).isoformat(),
    }


def apply(repeat_start: date) -> str:
    from artemis import health_office as office
    from knowledge import cognition
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
            cur.execute(office._UPSERT_SQL, office.upsert_params(r))
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
        cognition.log_decision(
            cur, agent="artemis", action="repeat_week", domain="health",
            outcome="executed", manual_gap=False,
            metadata={"repeat_start": repeat_start.isoformat(),
                      "repeats": [d.isoformat() for d in new_reps],
                      "rows_rebuilt": len(keys),
                      "kept_logged": [f"{d} {s}" for d, s in plan["kept_logged"]],
                      "new_end": office.program_end(new_reps).isoformat(),
                      "md5_untouched": before, "time_cap_notes": len(notes)},
            assumptions=_assumptions(repeat_start, reps, new_reps, plan, keys, before,
                                     office))
    set_system_value(PENDING_KEY, "")
    wk = office.week_num_for(repeat_start, new_reps)
    kept = (f" {len(plan['kept_logged'])} already-logged row(s) were left as they were."
            if plan["kept_logged"] else "")
    return (f"Done — week {wk} runs again from {repeat_start:%a %-m/%-d}. "
            f"{len(keys)} plan rows rebuilt; the block now ends "
            f"{office.program_end(new_reps):%a %-m/%-d}.{kept}")
