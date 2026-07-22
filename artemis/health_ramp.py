"""Ramp slide/tier engine (feat/health-ramp) — Workstreams 2 & 3.

The canonical weeks 1-7 ramp schedule, its block builders, the deterministic
seeder (shared by scripts/reseed_health_plan_v2.py --ramp), and the nightly
reconcile → slide → evaluate → propose engine.

Semantics (authoritative — see the SPEC):
  * A "week" is its 5 rows sharing a calendar week. Week window = first planned
    date → the Saturday before the next week's first planned date (week 1:
    7/25–8/1; week 2: 8/2–8/8; … week 7: 9/6–9/12).
  * Session states: planned → completed | slid | missed.
      OPEN  = planned | slid   (still a to-do; can complete, slide again, or miss)
      TERMINAL = completed | missed
  * Slide (AUTOMATIC, audited, no confirm): a session not completed by the end of
    its planned day (CT) slides to the next available makeup morning inside its
    week window. Wed and Sat are the makeup slots. Order preserved; nothing slides
    past the window; a session that can't fit is `missed`.
  * Week evaluation (nightly, after a window closes):
      5/5 → success.
      4/5 → propose repeat-week.
      ≤3/5 OR two consecutive non-successful weeks → propose restart at week 1.
    Travel weeks are PINNED to their calendar dates (8/16 & 9/6 anchors): the
    travel template applies to whatever program week lands on those windows.
  * Ramp complete = two consecutive successful weeks. After the week-2 evaluation,
    whatever the outcome, post a ramp-revisit prompt (do NOT auto-transition).
  * HARD GATE: the engine detects and PROPOSES; it never rewrites plan state
    (repeat/restart) without an explicit confirm. Slides are the only auto-action,
    and every slide/evaluation/proposal/confirm/reseed writes acos.audit_log.
    Artemis-does-statistics / Ryan-does-semantics.

This module writes ONLY health.plan (+ health.ramp_state) and reads
health.session_log. CT-anchored throughout (RDS is UTC).
"""

import copy
import json
import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

CT = ZoneInfo("America/Chicago")

# The initial ramp is phase 1 (reintroduction). generated_by is CHECK-constrained
# to (baseline|autoreg_morning|autoreg_evening|manual); a tool-run reseed is a
# MANUAL change — the correct semantic, and it does not expand the CHECK.
RAMP_PHASE = 1
GENERATED_BY = "manual"

# Inclusive date bounds of the INITIAL seed. The reseed replaces everything from
# RAMP_START forward and never writes past RAMP_END (weeks 1-7 only; week 8+ is
# location-unknown by design, and the plan ending is the correct signal).
RAMP_START = date(2026, 7, 25)
RAMP_END = date(2026, 9, 11)

# Calendar weeks whose Sunday anchor gets the travel overlay on Tue/Thu/Fri.
# Pinned by DATE (not program-week number): the travel template applies to
# whatever program week lands on 8/18–8/21 and 9/8–9/11.
TRAVEL_ANCHORS = {date(2026, 8, 16), date(2026, 9, 6)}

# The one-time Sat-for-Sun swap: week 1's long Z2 lands on Sat 7/25, not Sun 7/26.
_WEEK1_SUN_OVERRIDE = date(2026, 7, 25)

# The durable pending-proposal payload lives in health.ramp_state.pending_proposal
# (jsonb) — the SAME row and transaction as pending_proposal_id, so the gate flag
# and the data needed to apply it can never diverge (no cross-store wedge). The
# proposal carries the ops channel_id so the confirm handler stays channel-scoped.


# ============================================================================
# Block builders — one per program label. Return (blocks, target_rpe, hr_zone,
# est_duration_min). Blocks are held to the live render contract (health.py
# _render_circuit / _render_cardio dispatch on blocks["type"]).
# ============================================================================

# Canonical equipment tokens (PB-009 inventory; validator allow-list friendly).
_EQ_POWERBLOCKS = "PowerBlocks 20-35lb"
_EQ_ROWER = "water rower"
_EQ_BIKE = "road bike + indoor trainer"
_EQ_TRX = "TRX"
_EQ_BALL = "exercise ball"
_EQ_MAT = "yoga mat"
_EQ_BANDS = "resistance bands"
_EQ_BENCH = "flat bench"


def _load_note(week_num: int) -> str:
    if week_num <= 2:
        return ("Weeks 1-2: PowerBlocks 20-35 lb, RPE ≤6, 2-3s eccentrics, "
                "stop shy of any shoulder pinch.")
    return ("Progression: +2.5-5 lb or +1 rep once all sets hit the top of the "
            "rep range.")


def _strength_rpe(week_num: int) -> float:
    return 6.0 if week_num <= 2 else 7.0


def _b_strength_a(week_num: int):
    """Strength A — lower + core (~40 min)."""
    blocks = {
        "type": "circuit",
        "display_name": "Strength A — Lower + Core",
        "rounds": 3,
        "warmup": "5 min rower + hip/shoulder mobility",
        "cooldown": "10 min easy rower finisher",
        "rest_between_rounds_sec": 90,
        "equipment": [_EQ_POWERBLOCKS, _EQ_TRX, _EQ_BALL, _EQ_ROWER, _EQ_MAT],
        "exercises": [
            {"name": "PowerBlock goblet squat", "format": "reps", "target_reps": 12, "notes": "3×10-12"},
            {"name": "DB RDL", "format": "reps", "target_reps": 12, "notes": "3×10-12"},
            {"name": "TRX hamstring curl", "format": "reps", "target_reps": 10, "notes": "3×10"},
            {"name": "Exercise-ball plank", "format": "duration", "duration_sec": 45, "notes": "3×45s"},
        ],
        "setup_notes": ["Lower + core focus", _load_note(week_num)],
    }
    return blocks, _strength_rpe(week_num), 3, 40


def _b_strength_b(week_num: int):
    """Strength B — upper, shoulder-safe (~40 min). Ceiling 6'6\": overhead work
    seated/kneeling; face pulls are non-negotiable (clavicle/shoulder stability)."""
    blocks = {
        "type": "circuit",
        "display_name": "Strength B — Upper (shoulder-safe)",
        "rounds": 3,
        "warmup": "5 min bike + band pull-aparts",
        "cooldown": "5 min easy spin + stretch",
        "rest_between_rounds_sec": 90,
        "equipment": [_EQ_POWERBLOCKS, _EQ_TRX, _EQ_BANDS, _EQ_BENCH],
        "exercises": [
            {"name": "Neutral-grip DB flat bench", "format": "reps", "target_reps": 12, "notes": "3×10-12"},
            {"name": "TRX low row", "format": "reps", "target_reps": 12, "notes": "3×10-12"},
            {"name": "Seated neutral-grip DB shoulder press", "format": "reps", "target_reps": 10,
             "notes": "3×8-10, seated — ceiling 6'6\"; pain-free ROM only"},
            {"name": "Band face pulls", "format": "reps", "target_reps": 15,
             "notes": "3×15 — non-negotiable (clavicle/shoulder stability)"},
            {"name": "Band curls + pressdowns", "format": "reps", "target_reps": 15, "notes": "2×12-15"},
        ],
        "setup_notes": ["Upper, shoulder-safe", _load_note(week_num),
                        "All overhead work seated or kneeling (ceiling 6'6\")"],
    }
    return blocks, _strength_rpe(week_num), 3, 40


def _b_z2_bike(week_num: int):
    """Z2 Bike (Tue): 30-40 min trainer, conversational, HR ~110-125."""
    blocks = {
        "type": "steady",
        "display_name": "Z2 Bike",
        "duration_min": 35,
        "target_range_min": [30, 40],
        "intensity": "Zone 2",
        "warmup_sec": 300,
        "cooldown_sec": 300,
        "equipment": [_EQ_BIKE],
        "setup_notes": ["30-40 min trainer, conversational pace, HR ~110-125"],
    }
    return blocks, 4.0, 2, 35


def _b_intervals(week_num: int):
    """Intervals (Fri): bike 30s hard / 30s easy × 12-15 (or rower 1min/1min × 10)."""
    blocks = {
        "type": "intervals",
        "display_name": "Intervals",
        "rounds": 13,
        "warmup_sec": 300,
        "warmup_settings": "easy spin",
        "cooldown_sec": 300,
        "cooldown_settings": "easy spin",
        "intervals_template": {
            "work_sec": 30, "work_settings": "hard effort (Z4)",
            "rest_sec": 30, "rest_settings": "easy spin",
        },
        "equipment": [_EQ_BIKE, _EQ_ROWER],
        "setup_notes": [
            "Bike 30s hard / 30s easy × 12-15",
            "OR rower 1 min mod-hard / 1 min easy × 10",
            "5 min warm-up / cool-down either side",
        ],
    }
    return blocks, 8.5, 4, 30


def _b_long_z2(week_num: int):
    """Long Z2 (Sun; Sat wk1): 45-60 min outdoor bike or brisk walk, conversational."""
    blocks = {
        "type": "steady",
        "display_name": "Long Z2",
        "duration_min": 55,
        "target_range_min": [45, 60],
        "intensity": "Zone 2",
        "warmup_sec": 300,
        "cooldown_sec": 300,
        "equipment": [_EQ_BIKE],
        "setup_notes": ["45-60 min outdoor bike or brisk walk, conversational pace"],
    }
    return blocks, 4.5, 2, 55


def _b_travel_circuit_a(week_num: int):
    """Travel Circuit A (Tue): 4 rounds, 45s work / 15s rest + 10 min brisk walk."""
    blocks = {
        "type": "circuit",
        "display_name": "Travel Circuit A",
        "rounds": 4,
        "warmup": "3-5 min easy movement",
        "cooldown": "+10 min brisk walk",
        "rest_between_rounds_sec": 15,
        "equipment": [_EQ_BANDS],
        "exercises": [
            {"name": "Air squats", "format": "duration", "duration_sec": 45, "rest_after_sec": 15},
            {"name": "Push-ups (incline as needed)", "format": "duration", "duration_sec": 45, "rest_after_sec": 15},
            {"name": "Reverse lunges", "format": "duration", "duration_sec": 45, "rest_after_sec": 15},
            {"name": "Plank", "format": "duration", "duration_sec": 45, "rest_after_sec": 15},
        ],
        "setup_notes": [
            "Travel session — 4 rounds, 45s work / 15s rest",
            "Then 10 min brisk walk",
            "If band + door anchor packed: add band rows + face pulls",
        ],
    }
    return blocks, 7.0, 3, 35


def _b_travel_walk(week_num: int):
    """Travel Z2 Walk (Thu): 40-45 min brisk outdoor walk."""
    blocks = {
        "type": "steady",
        "display_name": "Travel Z2 Walk",
        "duration_min": 42,
        "target_range_min": [40, 45],
        "intensity": "Zone 2",
        "equipment": ["walking shoes"],
        "setup_notes": ["Travel session — 40-45 min brisk outdoor walk"],
    }
    return blocks, 4.0, 2, 42


def _b_travel_circuit_b(week_num: int):
    """Travel Circuit B (Fri): 3 rounds squats/push-ups/step-ups, then 15 min walk
    intervals 1 min fast / 2 min easy."""
    blocks = {
        "type": "circuit",
        "display_name": "Travel Circuit B",
        "rounds": 3,
        "warmup": "3-5 min easy movement",
        "cooldown": "15 min walk intervals: 1 min fast / 2 min easy",
        "rest_between_rounds_sec": 30,
        "equipment": [_EQ_BANDS],
        "exercises": [
            {"name": "Squats", "format": "reps", "target_reps": 15, "rest_after_sec": 20},
            {"name": "Push-ups", "format": "reps", "target_reps": 12, "rest_after_sec": 20},
            {"name": "Step-ups", "format": "reps", "target_reps": 12, "rest_after_sec": 20, "notes": "each leg"},
        ],
        "setup_notes": [
            "Travel session — 3 rounds squats / push-ups / step-ups",
            "Then 15 min walk intervals: 1 min fast / 2 min easy",
        ],
    }
    return blocks, 7.0, 3, 35


# program_label -> (session_type, display_name, builder). session_type values are
# all CHECK-legal (strength_a/b/c, cardio_z2, cardio_intervals). Travel circuits
# map to strength_c (bodyweight circuit); the travel Z2 walk maps to cardio_z2.
PROGRAM: dict[str, tuple[str, str, object]] = {
    "long_z2":          ("cardio_z2",        "Long Z2",                       _b_long_z2),
    "strength_a":       ("strength_a",       "Strength A — Lower + Core",     _b_strength_a),
    "z2_bike":          ("cardio_z2",        "Z2 Bike",                       _b_z2_bike),
    "strength_b":       ("strength_b",       "Strength B — Upper (shoulder-safe)", _b_strength_b),
    "intervals":        ("cardio_intervals", "Intervals",                     _b_intervals),
    "travel_circuit_a": ("strength_c",       "Travel Circuit A",              _b_travel_circuit_a),
    "travel_walk":      ("cardio_z2",        "Travel Z2 Walk",                _b_travel_walk),
    "travel_circuit_b": ("strength_c",       "Travel Circuit B",              _b_travel_circuit_b),
}

# Weekday role → base program label. The 5 session weekdays are Sun/Mon/Tue/Thu/Fri.
BASE_ROLES = {"SUN": "long_z2", "MON": "strength_a", "TUE": "z2_bike",
              "THU": "strength_b", "FRI": "intervals"}
# Travel overlay replaces Tue/Thu/Fri on the pinned calendar weeks.
TRAVEL_ROLES = {"TUE": "travel_circuit_a", "THU": "travel_walk", "FRI": "travel_circuit_b"}
_ROLE_ORDER = ("SUN", "MON", "TUE", "THU", "FRI")
_ROLE_OFFSET = {"SUN": 0, "MON": 1, "TUE": 2, "THU": 4, "FRI": 5}


# ============================================================================
# Schedule construction
# ============================================================================

def _week_dates(sunday_anchor: date) -> dict[str, date]:
    return {role: sunday_anchor + timedelta(days=off) for role, off in _ROLE_OFFSET.items()}


def _roles_for_week(sunday_anchor: date) -> dict[str, str]:
    roles = dict(BASE_ROLES)
    if sunday_anchor in TRAVEL_ANCHORS:
        roles.update(TRAVEL_ROLES)
    return roles


def build_initial_schedule() -> list[dict]:
    """The canonical weeks 1-7 schedule as ordered {week_num, plan_date,
    program_label, role} specs. Week 1's long Z2 is the one-time Sat 7/25 slot."""
    specs: list[dict] = []
    for w in range(1, 8):
        anchor = date(2026, 7, 26) + timedelta(days=7 * (w - 1))
        dates = _week_dates(anchor)
        roles = _roles_for_week(anchor)
        if w == 1:
            dates["SUN"] = _WEEK1_SUN_OVERRIDE  # one-time Sat-for-Sun swap
        for role in _ROLE_ORDER:
            specs.append({"week_num": w, "plan_date": dates[role],
                          "program_label": roles[role], "role": role})
    return specs


def build_regenerated_schedule(program_week_seq: list[int], start_sunday: date) -> list[dict]:
    """Lay an ordered sequence of program-week numbers onto consecutive calendar
    weeks starting at `start_sunday`. Travel overlay is applied by DATE (pinned to
    the 8/16 & 9/6 anchors), never by program-week number — so the travel template
    lands on whatever week occupies those windows, and only there."""
    specs: list[dict] = []
    for i, w in enumerate(program_week_seq):
        anchor = start_sunday + timedelta(days=7 * i)
        dates = _week_dates(anchor)
        roles = _roles_for_week(anchor)
        for role in _ROLE_ORDER:
            specs.append({"week_num": w, "plan_date": dates[role],
                          "program_label": roles[role], "role": role})
    return specs


def build_ramp_row(spec: dict) -> dict:
    """Expand a schedule spec into a full health.plan row payload."""
    label = spec["program_label"]
    week_num = spec["week_num"]
    session_type, display, builder = PROGRAM[label]
    blocks, target_rpe, hr_zone, est = builder(week_num)
    blocks = copy.deepcopy(blocks)
    blocks["display_name"] = display
    return {
        "plan_date": spec["plan_date"],
        "phase": RAMP_PHASE,
        "week_num": week_num,
        "program_label": label,
        "session_type": session_type,
        "blocks": blocks,
        "target_rpe": target_rpe,
        "target_hr_zone": hr_zone,
        "est_duration_min": est,
        "generated_by": GENERATED_BY,
        "notes": f"{display} | ramp wk{week_num}",
    }


def build_rows(specs: list[dict]) -> list[dict]:
    return [build_ramp_row(s) for s in specs]


def validate_ramp_rows(rows: list[dict]) -> None:
    """Cheap structural asserts so a bad edit fails loudly (mirrors the reseed
    script's _validate)."""
    legal = {"strength_a", "strength_b", "strength_c",
             "cardio_intervals", "cardio_z2", "walk", "rest_mobility"}
    dates = [r["plan_date"] for r in rows]
    assert len(dates) == len(set(dates)), "duplicate plan_date in ramp rows"
    for r in rows:
        assert r["session_type"] in legal, f"illegal session_type {r['session_type']}"
        assert 1 <= r["week_num"] <= 19, f"week_num out of CHECK range: {r['week_num']}"
        b = r["blocks"]
        assert b.get("display_name"), "blocks must carry a display_name"
        assert b["type"] in ("circuit", "intervals", "steady"), b["type"]
        if b["type"] == "circuit":
            assert b.get("exercises") and all(
                "name" in e and "format" in e for e in b["exercises"]), r["program_label"]


# ============================================================================
# Deterministic writer — DELETE forward + INSERT. Shared by the CLI reseed and
# the confirm-commit path. Takes a live cursor; the CALLER owns the transaction.
# ============================================================================

_INSERT_SQL = """
INSERT INTO health.plan
    (plan_date, phase, week_num, session_type, blocks,
     target_rpe, target_hr_zone, est_duration_min, generated_by, notes)
VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s)
"""


def write_rows(cur, rows: list[dict], delete_from: date) -> int:
    """DELETE every health.plan row on/after `delete_from`, then INSERT `rows`.

    status/original_date are left to their column defaults ('planned'/NULL) so a
    fresh seed or a regenerate resets the lifecycle. Does NOT commit — the caller
    (reseed CLI or commit_ramp_proposal) controls the transaction."""
    validate_ramp_rows(rows)
    cur.execute("DELETE FROM health.plan WHERE plan_date >= %s", (delete_from,))
    for r in rows:
        cur.execute(_INSERT_SQL, (
            r["plan_date"], r["phase"], r["week_num"], r["session_type"],
            json.dumps(r["blocks"]), r["target_rpe"], r["target_hr_zone"],
            r["est_duration_min"], r["generated_by"], r["notes"],
        ))
    return len(rows)


def seed_initial(cur) -> list[dict]:
    """Build + write the canonical weeks 1-7 schedule (delete-forward from
    RAMP_START). Returns the row payloads. Caller owns commit/rollback."""
    rows = build_rows(build_initial_schedule())
    assert all(RAMP_START <= r["plan_date"] <= RAMP_END for r in rows), \
        "initial schedule escaped [RAMP_START, RAMP_END]"
    assert len(rows) == 35, f"expected 35 rows (7×5), got {len(rows)}"
    write_rows(cur, rows, RAMP_START)
    return rows


# ============================================================================
# Week windows (CT calendar). Grouping is date-based, so it is robust across a
# restart (where week_num repeats).
# ============================================================================

def _week_anchor(d: date) -> date:
    """The Sunday that anchors d's ramp week. Week 1's leading Sat 7/25 is folded
    into the 7/26 anchor (its one-time swap)."""
    if d == _WEEK1_SUN_OVERRIDE:
        return date(2026, 7, 26)
    return d - timedelta(days=(d.weekday() + 1) % 7)  # Sunday on/before d


def compute_windows(dates: list[date]) -> dict[date, tuple[date, date]]:
    """Map each distinct week anchor → (window_start, window_end). window_start is
    the earliest planned date in the week; window_end is the day before the next
    week's anchor, or anchor+6 (that week's Saturday) for the final week."""
    by_anchor: dict[date, list[date]] = {}
    for d in dates:
        by_anchor.setdefault(_week_anchor(d), []).append(d)
    anchors = sorted(by_anchor)
    windows: dict[date, tuple[date, date]] = {}
    for i, a in enumerate(anchors):
        start = min(by_anchor[a])
        if i + 1 < len(anchors):
            end = anchors[i + 1] - timedelta(days=1)
        else:
            end = a + timedelta(days=6)  # trailing Saturday
        windows[a] = (start, end)
    return windows


def _makeup_slots(window: tuple[date, date]) -> list[date]:
    """The Wed and Sat makeup dates inside a window (Sat = the trailing Saturday,
    never a week-1 leading Sat since that is the window start)."""
    start, end = window
    wed = [start + timedelta(days=i) for i in range((end - start).days + 1)
           if (start + timedelta(days=i)).weekday() == 2]
    sat = [start + timedelta(days=i) for i in range((end - start).days + 1)
           if (start + timedelta(days=i)).weekday() == 5]
    slots = []
    if wed:
        slots.append(min(wed))
    if sat:
        slots.append(max(sat))  # trailing Saturday
    return sorted(slots)


def _next_makeup_slot(cur_date: date, window: tuple[date, date],
                      occupied: set[date], min_date: date | None = None) -> date | None:
    """Earliest makeup slot strictly after cur_date, on/after min_date (a makeup is
    never scheduled in the past), inside the window, and not already taken by
    another session in the week."""
    for slot in _makeup_slots(window):
        if slot <= cur_date or (min_date is not None and slot < min_date):
            continue
        if window[0] <= slot <= window[1] and slot not in occupied:
            return slot
    return None


# ============================================================================
# Engine — reconcile / slide / evaluate. Pure-ish helpers take an explicit
# `today` so tests can drive any calendar date; run_nightly() supplies CT today.
# ============================================================================

def _ct_today() -> date:
    return datetime.now(CT).date()


def _load_ramp_rows(cur) -> list[dict]:
    """All ramp rows (plan_date >= RAMP_START), ordered. Plain tuple cursor."""
    cur.execute(
        "SELECT plan_id, plan_date, week_num, session_type, status, original_date, "
        "blocks FROM health.plan WHERE plan_date >= %s ORDER BY plan_date",
        (RAMP_START,),
    )
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _is_completed(cur, plan_id: int, plan_date: date) -> bool:
    """A real (non-inferred) session_log row tied to this plan, logged on/after
    the plan date in CT. plan_id is set at capture time from today's CT plan row,
    so this is the strong signal; the CT-date guard honors 'on/after its date'
    (a 10pm-CT log = 3am-UTC-next-day still resolves to the CT plan date)."""
    cur.execute(
        "SELECT 1 FROM health.session_log "
        "WHERE plan_id = %s AND logged_via <> 'inferred' "
        "AND (logged_at AT TIME ZONE 'America/Chicago')::date >= %s LIMIT 1",
        (plan_id, plan_date),
    )
    return cur.fetchone() is not None


def _set_status(cur, plan_id: int, status: str, original_date=None,
                new_date: date | None = None) -> None:
    if new_date is not None:
        cur.execute(
            "UPDATE health.plan SET status = %s, plan_date = %s, "
            "original_date = COALESCE(original_date, %s) WHERE plan_id = %s",
            (status, new_date, original_date, plan_id),
        )
    else:
        cur.execute(
            "UPDATE health.plan SET status = %s WHERE plan_id = %s",
            (status, plan_id),
        )


def mark_completions(cur, rows: list[dict], today: date) -> list[dict]:
    """Flip OPEN rows (planned|slid) to completed when a real session_log exists.
    Mutates `rows` in place and writes DB. Returns the rows newly completed."""
    newly: list[dict] = []
    for r in rows:
        if r["status"] in ("planned", "slid") and _is_completed(cur, r["plan_id"], r["plan_date"]):
            _set_status(cur, r["plan_id"], "completed")
            r["status"] = "completed"
            newly.append(r)
    return newly


def apply_slides(cur, rows: list[dict], today: date) -> list[dict]:
    """For each OPEN session whose planned day has passed (plan_date < today, CT)
    and wasn't completed, slide it to the next makeup slot (Wed/Sat) on/after today
    inside its week window; if none remains, mark it missed. Processing past-due
    rows in date order keeps the makeup order stable and is robust to a skipped
    nightly run (a session two days stale still slides, not silently forfeited).
    Returns a list of {row, action, from, to} for notices/audit."""
    windows = compute_windows([r["plan_date"] for r in rows])
    actions: list[dict] = []
    due = sorted(
        [r for r in rows if r["status"] in ("planned", "slid") and r["plan_date"] < today],
        key=lambda r: r["plan_date"],
    )
    for r in due:
        anchor = _week_anchor(r["plan_date"])
        window = windows[anchor]
        occupied = {o["plan_date"] for o in rows
                    if _week_anchor(o["plan_date"]) == anchor and o is not r}
        slot = _next_makeup_slot(r["plan_date"], window, occupied, min_date=today)
        if slot is None:
            _set_status(cur, r["plan_id"], "missed")
            actions.append({"row": r, "action": "missed", "from": r["plan_date"], "to": None})
            r["status"] = "missed"
        else:
            orig = r["original_date"] or r["plan_date"]
            _set_status(cur, r["plan_id"], "slid", original_date=orig, new_date=slot)
            actions.append({"row": r, "action": "slid", "from": r["plan_date"], "to": slot})
            r["original_date"] = orig
            r["plan_date"] = slot
            r["status"] = "slid"
    return actions


# ---- Week evaluation ------------------------------------------------------

def _session_label(row: dict) -> str:
    b = row.get("blocks")
    if isinstance(b, str):
        try:
            b = json.loads(b)
        except (ValueError, TypeError):
            b = {}
    if isinstance(b, dict) and b.get("display_name"):
        return b["display_name"]
    return str(row.get("session_type", "?"))


def classify_week(completed: int, prev_nonsuccess: int) -> str:
    """5/5 → success; ≤3 → restart; ==4 → repeat unless the previous week was also
    non-successful (two-in-a-row → restart)."""
    if completed >= 5:
        return "success"
    if completed <= 3:
        return "restart"
    return "restart" if prev_nonsuccess >= 1 else "repeat"


def _regen_sequence(outcome: str, week_num: int) -> list[int]:
    """Program-week sequence to lay down from the next calendar week.
    repeat: [w, w+1, …, 7] (re-run w, then downstream shifts behind it).
    restart: [1, 2, …, 7]."""
    if outcome == "repeat":
        return list(range(week_num, 8))
    return list(range(1, 8))  # restart


# ============================================================================
# Durable pending proposal (system_state KV) + audit
# ============================================================================

def _audit(action: str, outcome: str, metadata: dict) -> str:
    try:
        from knowledge.db import log_audit
        return log_audit(agent="health_ramp", action=action, domain="health",
                         outcome=outcome, metadata=metadata)
    except Exception:
        logger.exception("ramp audit write failed (%s/%s)", action, outcome)
        return ""


def load_ramp_pending(channel_id: str | None = None) -> dict | None:
    """Read the open proposal payload from health.ramp_state (singleton). The
    channel_id arg is advisory (the confirm handler passes the reply channel); the
    payload itself carries the ops channel_id the handler matches against.

    Defensive: this runs on EVERY inbound message (the confirm handler gates on it),
    so a DB read failure must never propagate — it degrades to 'no pending' and the
    handler falls through rather than crashing the router."""
    from knowledge.db import execute_one
    try:
        row = execute_one("SELECT pending_proposal FROM health.ramp_state WHERE id = 1")
    except Exception:
        logger.debug("ramp: could not read pending proposal", exc_info=True)
        return None
    if not row:
        return None
    payload = row.get("pending_proposal")
    if not payload:
        return None
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (ValueError, TypeError):
            return None
    return payload


def _serialize_rows(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        d = dict(r)
        d["plan_date"] = r["plan_date"].isoformat()
        out.append(d)
    return out


def _deserialize_rows(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        d = dict(r)
        d["plan_date"] = date.fromisoformat(r["plan_date"])
        out.append(d)
    return out


def _proposal_id(outcome: str, week_num: int, start_sunday: date) -> str:
    return f"ramp_{outcome}_wk{week_num}_{start_sunday.isoformat()}"


def build_proposal(outcome: str, week_num: int, closed_end: date,
                   current_future_rows: list[dict], channel_id: str | None = None) -> dict:
    """Build a repeat/restart proposal: the regenerated rows, the delete cutoff,
    and a human diff table (old → new by date). Writes NOTHING. channel_id (the ops
    channel) is stored in the payload so the confirm handler stays channel-scoped."""
    start_sunday = closed_end + timedelta(days=1)  # the following calendar week
    seq = _regen_sequence(outcome, week_num)
    new_specs = build_regenerated_schedule(seq, start_sunday)
    new_rows = build_rows(new_specs)
    pid = _proposal_id(outcome, week_num, start_sunday)

    old_by_date = {}
    for r in current_future_rows:
        if r["plan_date"] >= start_sunday:
            old_by_date[r["plan_date"]] = _session_label(r)
    new_by_date = {r["plan_date"]: r["blocks"]["display_name"] for r in new_rows}

    all_dates = sorted(set(old_by_date) | set(new_by_date))
    diff_lines = []
    for d in all_dates:
        old = old_by_date.get(d, "—")
        new = new_by_date.get(d, "—")
        mark = "" if old == new else "  ←"
        diff_lines.append(f"  {d.isoformat()} {d.strftime('%a')}  {old:>28}  →  {new}{mark}")

    verb = "repeat this week" if outcome == "repeat" else "restart at week 1, workout #1"
    header = (f"📋 **Ramp {outcome} proposal** — week {week_num} closed. Proposing to "
              f"**{verb}** from {start_sunday.isoformat()}.\n"
              f"Travel weeks stay pinned (8/18-21, 9/8-11).\n\n"
              f"```\n{'date':<15}{'current':>28}     new\n" + "\n".join(diff_lines) + "\n```")
    footer = "\nReply `yes` to apply, `no` to keep the current plan. Nothing changes until you confirm."

    return {
        "proposal_id": pid,
        "outcome": outcome,
        "week_num": week_num,
        "start_sunday": start_sunday.isoformat(),
        "delete_from": start_sunday.isoformat(),
        "rows": _serialize_rows(new_rows),
        "text": header + footer,
        "channel_id": channel_id,
        "created_at": datetime.now(CT).isoformat(),
    }


def commit_ramp_proposal(channel_id: str) -> str:
    """Confirm leg: write the regenerated rows in ONE transaction, clear the
    ramp_state pending marker, audit, and render the confirmation FROM the written
    rows (re-read), never from the proposal."""
    payload = load_ramp_pending(channel_id)
    if payload is None:
        return "Nothing pending to apply (it may have expired). The nightly job will re-propose."

    rows = _deserialize_rows(payload.get("rows", []))
    delete_from = date.fromisoformat(payload["delete_from"])
    outcome = payload.get("outcome")
    # Applying a proposal is a clean slate: reset the tier counters (so a fresh
    # imperfect week after a restart/repeat can't immediately re-restart). A
    # RESTART re-arms the one-time week-2 revisit prompt (a restart is a new ramp).
    from knowledge.db import get_connection
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                write_rows(cur, rows, delete_from)
                if outcome == "restart":
                    cur.execute(
                        "UPDATE health.ramp_state SET pending_proposal_id = NULL, "
                        "pending_proposal = NULL, consecutive_success_count = 0, "
                        "consecutive_nonsuccess_count = 0, revisit_prompted = false, "
                        "updated_at = now() WHERE id = 1")
                else:
                    cur.execute(
                        "UPDATE health.ramp_state SET pending_proposal_id = NULL, "
                        "pending_proposal = NULL, consecutive_success_count = 0, "
                        "consecutive_nonsuccess_count = 0, updated_at = now() WHERE id = 1")
                # Re-read what was actually written for the confirmation.
                cur.execute(
                    "SELECT plan_date, week_num, blocks FROM health.plan "
                    "WHERE plan_date >= %s ORDER BY plan_date", (delete_from,))
                written = cur.fetchall()
    except Exception:
        logger.exception("ramp proposal commit failed")
        return "⚠️ Couldn't apply the ramp change — nothing was written. Check DB."

    _audit("ramp_proposal_commit", "executed", {
        "proposal_id": payload.get("proposal_id"),
        "outcome": payload.get("outcome"),
        "delete_from": payload["delete_from"],
        "rows_written": len(written),
    })

    n = len(written)
    first = written[0][0].isoformat() if written else "?"
    last = written[-1][0].isoformat() if written else "?"
    return (f"✅ Applied. Wrote {n} plan row{'s' if n != 1 else ''} "
            f"({first} → {last}). The nightly job now tracks the new schedule.")


def cancel_ramp_proposal(channel_id: str) -> str:
    payload = load_ramp_pending(channel_id)
    try:
        from knowledge.db import execute_write
        execute_write("UPDATE health.ramp_state SET pending_proposal_id = NULL, "
                      "pending_proposal = NULL, updated_at = now() WHERE id = 1")
    except Exception:
        logger.exception("ramp cancel: failed to clear pending marker")
        return "⚠️ Couldn't clear the pending proposal — check DB."
    _audit("ramp_proposal_cancel", "cancelled",
           {"proposal_id": (payload or {}).get("proposal_id")})
    return "Kept the current plan — nothing changed."


# ============================================================================
# Nightly entrypoint — reconcile → slide → evaluate → notice/propose.
# ============================================================================

def _read_ramp_state(cur) -> dict:
    cur.execute(
        "SELECT consecutive_success_count, consecutive_nonsuccess_count, "
        "last_evaluated_end_date, pending_proposal_id, revisit_prompted, ramp_complete "
        "FROM health.ramp_state WHERE id = 1")
    row = cur.fetchone()
    if row is None:
        cur.execute("INSERT INTO health.ramp_state (id) VALUES (1) ON CONFLICT DO NOTHING")
        return {"consecutive_success_count": 0, "consecutive_nonsuccess_count": 0,
                "last_evaluated_end_date": None, "pending_proposal_id": None,
                "revisit_prompted": False, "ramp_complete": False}
    cols = [c[0] for c in cur.description]
    return dict(zip(cols, row))


class _DryRunRollback(Exception):
    """Sentinel to force the main transaction to roll back on a dry run."""


def run_nightly(mm=None, *, today: date | None = None, dry_run: bool = False) -> dict:
    """Reconcile yesterday's ramp rows against session_log, apply slides, and — if
    a week window closed — evaluate it and emit a notice or a propose-then-confirm
    proposal to #artemis-ryan. Slides auto-apply (audited); repeat/restart only
    ever PROPOSE. Returns a summary dict (also the test surface).

    All health.plan / ramp_state writes land in ONE transaction. Side-effects
    (audit rows, the durable pending KV, Mattermost posts) are flushed AFTER that
    transaction commits, so nothing is audited or staged for a change that didn't
    persist — and no second pool connection is taken while the first is held."""
    from knowledge.db import get_connection
    import artemis.config as config

    today = today or _ct_today()
    summary: dict = {"today": today.isoformat(), "completed": [], "slides": [],
                     "evaluated": None, "outcome": None, "notices": [], "proposal": None}
    audit_events: list[tuple[str, str, dict]] = []
    pending_payload: dict | None = None

    # Resolve the ops channel up front so the proposal payload can carry it (the
    # confirm handler scopes on it). Best-effort — a proposal can still be staged
    # and applied without it (it just isn't channel-scoped).
    channel_id = None
    if mm is not None:
        try:
            channel_id = mm.get_channel_id(config.CHANNEL_OPS)
        except Exception:
            logger.exception("ramp nightly: could not resolve #artemis-ryan channel id")

    try:
        with get_connection() as conn:
            cur = conn.cursor()
            rows = _load_ramp_rows(cur)
            if not rows:
                logger.info("ramp nightly: no ramp rows (plan_date >= %s)", RAMP_START)
                raise _DryRunRollback()  # nothing to do — release without writing

            state = _read_ramp_state(cur)

            # 1) completions, 2) slides
            newly_completed = mark_completions(cur, rows, today)
            summary["completed"] = [r["plan_date"].isoformat() for r in newly_completed]
            slide_actions = apply_slides(cur, rows, today)
            summary["slides"] = [
                {"session": _session_label(a["row"]), "action": a["action"],
                 "from": a["from"].isoformat(), "to": a["to"].isoformat() if a["to"] else None}
                for a in slide_actions
            ]
            for a in slide_actions:
                label = _session_label(a["row"])
                if a["action"] == "slid":
                    note = f"↔ Slid **{label}** {a['from'].isoformat()} → {a['to'].isoformat()} (makeup slot)."
                else:
                    note = f"✗ **{label}** ({a['from'].isoformat()}) missed — no makeup slot left this week."
                summary["notices"].append(note)
                audit_events.append(("ramp_slide", a["action"],
                                     {"session": label, "from": a["from"].isoformat(),
                                      "to": a["to"].isoformat() if a["to"] else None}))

            # 3) evaluate the earliest closed, not-yet-evaluated week (unless a
            #    proposal is already open — never stack two).
            windows = compute_windows([r["plan_date"] for r in rows])
            last_eval = state.get("last_evaluated_end_date")
            pending_open = bool(state.get("pending_proposal_id"))

            eval_anchor = None
            for a in sorted(windows):
                start, end = windows[a]
                if end < today and (last_eval is None or end > last_eval):
                    eval_anchor = a
                    break

            if eval_anchor is not None and not pending_open:
                start, end = windows[eval_anchor]
                week_rows = [r for r in rows if _week_anchor(r["plan_date"]) == eval_anchor]
                # Finalize: any OPEN row in a closed week is terminal → missed.
                for r in week_rows:
                    if r["status"] in ("planned", "slid"):
                        _set_status(cur, r["plan_id"], "missed")
                        r["status"] = "missed"
                completed = sum(1 for r in week_rows if r["status"] == "completed")
                week_num = min(r["week_num"] for r in week_rows)
                outcome = classify_week(completed, state["consecutive_nonsuccess_count"])
                summary["evaluated"] = {"week_num": week_num, "completed": completed,
                                        "window": [start.isoformat(), end.isoformat()]}
                summary["outcome"] = outcome

                # Update tier counters.
                if completed >= 5:
                    sc = state["consecutive_success_count"] + 1
                    nsc = 0
                else:
                    sc = 0
                    nsc = state["consecutive_nonsuccess_count"] + 1
                ramp_complete = state["ramp_complete"] or sc >= 2

                pending_id = None
                if outcome == "success":
                    summary["notices"].append(
                        f"✅ Week {week_num}: 5/5 completed. Nice — that's a successful week.")
                else:
                    proposal = build_proposal(outcome, week_num, end, rows, channel_id=channel_id)
                    pending_id = proposal["proposal_id"]
                    pending_payload = proposal
                    summary["proposal"] = proposal["text"]
                    summary["notices"].append(proposal["text"])
                    audit_events.append(("ramp_propose", outcome,
                                         {"proposal_id": pending_id, "week_num": week_num,
                                          "completed": completed}))

                audit_events.append(("ramp_evaluate", outcome,
                                     {"week_num": week_num, "completed": completed,
                                      "consecutive_success": sc, "consecutive_nonsuccess": nsc}))

                # Fire the one-time week-2 revisit prompt at most once per ramp.
                revisit_now = (week_num == 2 and not state.get("revisit_prompted"))
                revisit_flag = bool(state.get("revisit_prompted")) or revisit_now

                # Atomic: the gate flag AND the payload it needs land together — no
                # cross-store wedge. pending_proposal is NULL when there is no proposal.
                cur.execute(
                    "UPDATE health.ramp_state SET consecutive_success_count = %s, "
                    "consecutive_nonsuccess_count = %s, last_evaluated_end_date = %s, "
                    "pending_proposal_id = %s, pending_proposal = %s::jsonb, "
                    "revisit_prompted = %s, ramp_complete = %s, updated_at = now() "
                    "WHERE id = 1",
                    (sc, nsc, end, pending_id,
                     json.dumps(pending_payload) if pending_payload is not None else None,
                     revisit_flag, ramp_complete))

                # Ramp-revisit prompt: after the WEEK-2 evaluation, whatever the outcome.
                if revisit_now:
                    summary["notices"].append(
                        "🔁 **Ramp revisit** — two weeks in. Completions so far are logged. "
                        "When you're ready, confirm the phase-2 shape (3 strength / 2 cardio) — "
                        "I won't auto-transition.")
                if ramp_complete and not state["ramp_complete"]:
                    summary["notices"].append(
                        "🎯 **Ramp complete** — two consecutive successful weeks. "
                        "Ready to lock phase 2 when you are.")
            elif eval_anchor is not None and pending_open:
                logger.info("ramp nightly: week closed but a proposal is open — holding evaluation")

            if dry_run:
                raise _DryRunRollback()
            # clean exit → get_connection() commits the whole transaction
    except _DryRunRollback:
        pass  # get_connection() rolled the transaction back on the raised exception

    if dry_run:
        return summary  # no side-effects on a dry run

    # ── Side-effects AFTER the atomic write: audit ledger + Mattermost. The
    #    pending proposal was already persisted atomically inside the transaction
    #    (health.ramp_state.pending_proposal), so there is nothing to stage here. ──
    for action, outcome, meta in audit_events:
        _audit(action, outcome, meta)

    if mm is not None:
        for note in summary["notices"]:
            try:
                mm.post_message(config.CHANNEL_OPS, note)
            except Exception:
                logger.exception("ramp nightly: failed to post notice")

    return summary
