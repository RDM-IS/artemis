"""Reseed health.plan in place (idempotent) — PB-009 program v2.

Updates EVERY existing row in health.plan via INSERT ... ON CONFLICT (plan_date)
DO UPDATE (plan_date is UNIQUE). It does NOT create or delete rows — it rewrites
session_type / blocks / targets / notes for whatever date range already exists,
and PRESERVES each row's phase and week_num (read first, never recomputed).

Day-of-week (America/Chicago) → repeating week:
    Mon -> strength_full_a (push/quad)  + core finisher
    Tue -> rest_mobility
    Wed -> cardio_intervals_bike  (RPE 8.5, HR z4, 35 min)
    Thu -> strength_full_b (pull/hinge) + core finisher
    Fri -> rest_mobility
    Sat -> cardio_long_z2_bike    (RPE 4.5, HR z2, 55 min) + core finisher
    Sun -> cardio_run_walk        (RPE 5.5, HR z2, 35 min)

CONSTRAINT NOTES (migration 013_health_schema.sql — verified by grep as the only
migration touching these columns):
  * health.plan.session_type CHECK allows ONLY:
      strength_a, strength_b, strength_c, cardio_intervals, cardio_z2,
      walk, rest_mobility
    The richer program names (strength_full_a, cardio_run_walk, ...) would
    violate it, so each program is MAPPED to a constraint-legal session_type
    (see _DAY_MAP). The full program name is preserved in health.plan.notes
    and the blocks payload, neither of which changes the documented JSON shape.
  * health.plan.generated_by CHECK allows ONLY:
      baseline, autoreg_morning, autoreg_evening, manual
    A human-run reseed is a MANUAL change, so generated_by is written as 'manual'
    (the correct semantic, not a workaround). The CHECK is NOT expanded. Provenance
    is preserved by appending a 'reseed_v2 <ISO-date>' tag to each row's notes
    (existing notes kept; see _compose_notes).

blocks JSON shape is held EXACTLY to the live contract (read by the scheduler,
the conversational logger, and api/app/routers/health.py):
    Strength: {type:"circuit", rounds, warmup, cooldown, equipment[],
               exercises:[{name, format:"reps"|"duration",
                           target_reps|duration_sec, rest_after_sec,
                           target_load_lbs?, notes?}],
               rest_between_rounds_sec, setup_notes?, finisher?}
    Cardio:   {type:"intervals"|"steady", ..., finisher?:{type:"core_circuit",
               rounds, exercises[]}}
    Rest:     {type:"mobility", notes, intensity, duration_min}

Run (no fragile shell sourcing — the script loads .env itself):
    python scripts/reseed_health_plan_v2.py               # DRY-RUN (default), NO write
    python scripts/reseed_health_plan_v2.py --commit      # read+compute+WRITE
    python scripts/reseed_health_plan_v2.py --self-test   # no DB; synthetic preview

Ramp mode (feat/health-ramp) — the weeks 1-7 reintroduction schedule. This mode
REPLACES every row on/after 2026-07-25 with the 35 explicit dated ramp rows and
hard-stops after 2026-09-11 (no week 8+). The schedule + block builders live in
artemis.health_ramp (shared with the runtime slide/tier engine); this CLI is the
seeder entry point:
    python scripts/reseed_health_plan_v2.py --ramp             # DRY-RUN (default)
    python scripts/reseed_health_plan_v2.py --ramp --commit    # delete-forward + WRITE
    python scripts/reseed_health_plan_v2.py --ramp --self-test # no DB; schedule preview

Connection: uses $DATABASE_URL if set (parsed from .env if present); otherwise
falls back to knowledge.db.get_connection() (RDS_HOST + Secrets Manager, as on
EC2). The live RDS is in a private VPC, so this must be run from inside the VPC
(the EC2 host), not a laptop.
"""

import argparse
import copy
import json
import os
import re
import sys
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

# health.plan.generated_by is CHECK-constrained to
# (baseline | autoreg_morning | autoreg_evening | manual). A human-run reseed is
# a MANUAL change, so 'manual' is the correct semantic value (not a workaround).
# We do NOT expand the CHECK (same policy as session_type). Provenance that we'd
# otherwise have lost from generated_by is preserved as a tag in notes instead
# (see _compose_notes / RESEED_TAG).
GENERATED_BY_DB = "manual"

# Provenance marker appended to each row's notes (since generated_by can't carry it).
RESEED_TAG = "reseed_v2"
_RESEED_TAG_RE = re.compile(r"\s*\|?\s*reseed_v2\b[^|]*", re.IGNORECASE)


def _compose_notes(existing_notes: str | None, program_label: str, run_iso: str) -> str:
    """Append a 'reseed_v2 <ISO-date>' provenance tag to a row's notes.

    Keeps any existing notes content (append, don't overwrite); falls back to the
    program label when the row had no notes, so we still record which program was
    applied. Any prior reseed_v2 tag is stripped first so re-running stays
    idempotent rather than accumulating tags.
    """
    base = (existing_notes or "").strip()
    base = _RESEED_TAG_RE.sub("", base).strip().strip("|").strip()
    if not base:
        base = program_label
    tag = f"{RESEED_TAG} {run_iso}"
    return f"{base} | {tag}" if base else tag

# Canonical equipment inventory (PB-009). NO treadmill, no unprogrammed filler.
_EQ_POWERBLOCKS = "PowerBlocks 25-35lb"
_EQ_ROWER = "water rower"
_EQ_BIKE = "road bike + indoor trainer"
_EQ_TRX = "TRX"
_EQ_BALL = "exercise ball"
_EQ_MAT = "yoga mat"
_EQ_BANDS = "resistance bands"
_EQ_CURLBAR = "curl bar (2x10# + 2x25#)"
_EQ_BENCH = "flat bench"

# weekday (Mon=0 .. Sun=6) -> (program_label, constraint-legal session_type)
# Sunday run-walk maps to 'cardio_z2' (NOT 'walk') so it is a naggable/loggable
# training day. Sat and Sun therefore SHARE session_type='cardio_z2' but differ
# in blocks: Sat is a bike session (equipment includes a bike), Sun is run-walk
# (no bike). Scheduler bike/weather resolution is gated on blocks, not just
# session_type, so it never applies bike handling to Sunday — see
# artemis.health.is_bike_session().
_DAY_MAP: dict[int, tuple[str, str]] = {
    0: ("strength_full_a", "strength_a"),
    1: ("rest_mobility", "rest_mobility"),
    2: ("cardio_intervals_bike", "cardio_intervals"),
    3: ("strength_full_b", "strength_b"),
    4: ("rest_mobility", "rest_mobility"),
    5: ("cardio_long_z2_bike", "cardio_z2"),
    6: ("cardio_run_walk", "cardio_z2"),
}

# Canonical human-facing program name, surfaced via blocks["display_name"].
_DISPLAY_NAME: dict[str, str] = {
    "strength_full_a": "Strength — Push/Quad",
    "strength_full_b": "Strength — Pull/Hinge",
    "cardio_intervals_bike": "Bike Intervals",
    "cardio_long_z2_bike": "Long Z2 Bike",
    "cardio_run_walk": "Run-Walk Progression",
    "rest_mobility": "Rest / Mobility",
}

_PROGRAMS_WITH_FINISHER = {"strength_full_a", "strength_full_b", "cardio_long_z2_bike"}


# ---------------------------------------------------------------------------
# Core finisher — rotate 2 variants by week parity to avoid staleness.
# ---------------------------------------------------------------------------

def core_finisher(week_num: int) -> dict:
    """Return a core_circuit finisher. Odd weeks → variant A, even → variant B."""
    if week_num % 2 == 1:
        exercises = [
            {"name": "Dead bug", "format": "reps", "target_reps": 10, "notes": "10 each side"},
            {"name": "Bird dog", "format": "reps", "target_reps": 10, "notes": "10 each side"},
            {"name": "Side plank", "format": "duration", "duration_sec": 20, "notes": "20s each side"},
            {"name": "Hollow hold", "format": "duration", "duration_sec": 20},
        ]
    else:
        exercises = [
            {"name": "Ball plank", "format": "duration", "duration_sec": 30},
            {"name": "TRX fallout", "format": "reps", "target_reps": 10},
            {"name": "Pallof press", "format": "reps", "target_reps": 12, "notes": "12 each side"},
            {"name": "Plank", "format": "duration", "duration_sec": 30},
        ]
    return {"type": "core_circuit", "rounds": 2, "exercises": exercises}


# ---------------------------------------------------------------------------
# Block builders — one per program. Return (blocks, target_rpe, hr_zone, est).
# ---------------------------------------------------------------------------

def _strength_full_a(week_num: int) -> tuple[dict, float, int, int]:
    blocks = {
        "type": "circuit",
        "rounds": 3,
        "warmup": "5 min easy bike + band pull-aparts",
        "cooldown": "5 min easy spin + stretch",
        "rest_between_rounds_sec": 120,
        "equipment": [_EQ_POWERBLOCKS, _EQ_BENCH, _EQ_TRX, _EQ_BANDS, _EQ_MAT],
        "exercises": [
            {"name": "Goblet squat", "format": "reps", "target_reps": 12, "target_load_lbs": 30, "rest_after_sec": 60},
            {"name": "DB floor press", "format": "reps", "target_reps": 12, "rest_after_sec": 60},
            {"name": "TRX row", "format": "reps", "target_reps": 12, "rest_after_sec": 60},
            {"name": "DB RDL", "format": "reps", "target_reps": 12, "target_load_lbs": 30, "rest_after_sec": 60},
            {"name": "Band chest press", "format": "reps", "target_reps": 15, "rest_after_sec": 60},
            {"name": "Reverse lunge", "format": "reps", "target_reps": 10, "rest_after_sec": 60, "notes": "10 each side"},
        ],
        "setup_notes": ["Push/quad focus"],
        "finisher": core_finisher(week_num),
    }
    return blocks, 7.5, 3, 40


def _strength_full_b(week_num: int) -> tuple[dict, float, int, int]:
    blocks = {
        "type": "circuit",
        "rounds": 3,
        "warmup": "5 min easy row + band pull-aparts",
        "cooldown": "5 min easy spin + stretch",
        "rest_between_rounds_sec": 120,
        "equipment": [_EQ_POWERBLOCKS, _EQ_CURLBAR, _EQ_TRX, _EQ_BANDS, _EQ_BENCH],
        "exercises": [
            {"name": "DB RDL", "format": "reps", "target_reps": 12, "target_load_lbs": 30, "rest_after_sec": 60},
            {"name": "TRX row", "format": "reps", "target_reps": 12, "rest_after_sec": 60},
            {"name": "Goblet squat", "format": "reps", "target_reps": 12, "target_load_lbs": 30, "rest_after_sec": 60},
            {"name": "Bicep curl", "format": "reps", "target_reps": 12, "rest_after_sec": 60, "notes": "curl bar"},
            {"name": "Band pull-apart", "format": "reps", "target_reps": 20, "rest_after_sec": 60},
            {"name": "TRX single-leg DL", "format": "reps", "target_reps": 10, "rest_after_sec": 60, "notes": "10 each side"},
        ],
        "setup_notes": ["Pull/hinge focus"],
        "finisher": core_finisher(week_num),
    }
    return blocks, 7.5, 3, 40


def _cardio_intervals_bike(week_num: int) -> tuple[dict, float, int, int]:
    blocks = {
        "type": "intervals",
        "rounds": 8,
        "warmup_sec": 300,
        "warmup_settings": "easy spin",
        "cooldown_sec": 300,
        "cooldown_settings": "easy spin",
        "intervals_template": {
            "work_sec": 60, "work_settings": "hard effort (Z4)",
            "rest_sec": 90, "rest_settings": "easy spin",
        },
        "equipment": [_EQ_BIKE, _EQ_ROWER],
        "setup_notes": ["Indoor trainer or water rower", "8 rounds: 60s hard / 90s easy"],
    }
    return blocks, 8.5, 4, 35


def _cardio_long_z2_bike(week_num: int) -> tuple[dict, float, int, int]:
    blocks = {
        "type": "steady",
        "duration_min": 55,
        "target_range_min": [45, 55],
        "intensity": "Zone 2",
        "warmup_sec": 300,
        "cooldown_sec": 300,
        "equipment": [_EQ_BIKE],
        "setup_notes": [
            "Steady 45-55 min Zone 2",
            "Road bike outside; indoor trainer if rain or sub-40F",
        ],
        "finisher": core_finisher(week_num),
    }
    return blocks, 4.5, 2, 55


def _cardio_run_walk(week_num: int) -> tuple[dict, float, int, int]:
    blocks = {
        "type": "steady",
        "rounds": 6,
        "intervals_template": {
            "work_sec": 120, "work_settings": "easy jog",
            "rest_sec": 120, "rest_settings": "walk",
        },
        "warmup_sec": 300,
        "warmup_settings": "walk",
        "cooldown_sec": 300,
        "cooldown_settings": "walk",
        "intensity": "moderate",
        "equipment": [],
        "setup_notes": [
            "Run-walk: 6 rounds of 2 min easy jog / 2 min walk",
            "5 min walk warmup + 5 min walk cooldown",
        ],
    }
    return blocks, 5.5, 2, 35


def _rest_mobility(week_num: int) -> tuple[dict, None, None, int]:
    blocks = {
        "type": "mobility",
        "notes": "20 min gentle yoga or full rest",
        "intensity": "gentle",
        "duration_min": 20,
    }
    return blocks, None, None, 20


_BUILDERS = {
    "strength_full_a": _strength_full_a,
    "strength_full_b": _strength_full_b,
    "cardio_intervals_bike": _cardio_intervals_bike,
    "cardio_long_z2_bike": _cardio_long_z2_bike,
    "cardio_run_walk": _cardio_run_walk,
    "rest_mobility": _rest_mobility,
}


def build_row(plan_date: date, phase: int, week_num: int,
              existing_notes: str | None = None, run_date: date | None = None) -> dict:
    """Build the full reseed payload for one existing plan row.

    phase and week_num come from the EXISTING row and are passed through
    unchanged (week_num also drives finisher rotation). existing_notes is the
    row's current notes (preserved + tagged); run_date stamps the provenance tag.
    """
    program_label, session_type = _DAY_MAP[plan_date.weekday()]
    blocks, target_rpe, hr_zone, est = _BUILDERS[program_label](week_num)
    blocks = copy.deepcopy(blocks)
    # Canonical human-facing name lives in blocks — one source of truth that the
    # plan-lookup handler and the gym read API both render (falling back to the
    # legacy pretty label only when absent).
    blocks["display_name"] = _DISPLAY_NAME[program_label]
    run_iso = (run_date or date.today()).isoformat()
    return {
        "plan_date": plan_date,
        "phase": phase,
        "week_num": week_num,
        "program_label": program_label,
        "session_type": session_type,
        "blocks": blocks,
        "target_rpe": target_rpe,
        "target_hr_zone": hr_zone,
        "est_duration_min": est,
        "generated_by": GENERATED_BY_DB,
        "notes": _compose_notes(existing_notes, program_label, run_iso),
    }


# ---------------------------------------------------------------------------
# DB plumbing
# ---------------------------------------------------------------------------

def _load_dotenv() -> None:
    """Load .env into os.environ WITHOUT shell-sourcing (values contain @ ! :)."""
    env_path = _REPO_ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


@contextmanager
def _connect():
    """Yield a psycopg2 connection. Prefer DATABASE_URL; otherwise use the
    knowledge.db pool (EC2 path) via its own context manager.

    The caller controls the transaction explicitly (commit on write, rollback on
    dry-run). For the pool path we delegate to `with get_connection()` rather
    than manually entering the CM, so cleanup/return-to-pool always happens.
    """
    import psycopg2
    url = os.environ.get("DATABASE_URL")
    if url:
        conn = psycopg2.connect(url, connect_timeout=10)
        try:
            yield conn
        finally:
            conn.close()
    else:
        from knowledge.db import get_connection
        with get_connection() as conn:
            yield conn


_UPSERT_SQL = """
INSERT INTO health.plan
    (plan_date, phase, week_num, session_type, blocks,
     target_rpe, target_hr_zone, est_duration_min, generated_by, notes)
VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s)
ON CONFLICT (plan_date) DO UPDATE SET
    session_type     = EXCLUDED.session_type,
    blocks           = EXCLUDED.blocks,
    target_rpe       = EXCLUDED.target_rpe,
    target_hr_zone   = EXCLUDED.target_hr_zone,
    est_duration_min = EXCLUDED.est_duration_min,
    generated_by     = EXCLUDED.generated_by,
    notes            = EXCLUDED.notes
-- phase, week_num, is_skipped, is_override, skip_reason, generated_at: untouched
"""


def _preflight(cur) -> tuple[bool, str]:
    """Check whether the LIVE health.plan generated_by CHECK permits the value we
    write (GENERATED_BY_DB). Returns (ok, message). Never raises — the caller
    decides whether to abort."""
    cur.execute(
        """SELECT pg_get_constraintdef(c.oid)
           FROM pg_constraint c
           JOIN pg_class t ON t.oid = c.conrelid
           JOIN pg_namespace n ON n.oid = t.relnamespace
           WHERE n.nspname='health' AND t.relname='plan' AND c.contype='c'"""
    )
    defs = [r[0] for r in cur.fetchall()]
    gb_checks = [d for d in defs if "generated_by" in d]
    if gb_checks and not any(f"'{GENERATED_BY_DB}'" in d for d in gb_checks):
        return False, (
            f"LIVE health.plan.generated_by CHECK rejects '{GENERATED_BY_DB}': "
            f"{gb_checks[0]}\n"
            "    Per policy the CHECK must NOT be expanded. Set GENERATED_BY_DB to "
            "an allowed value and re-run."
        )
    return True, f"generated_by '{GENERATED_BY_DB}' permitted by live schema."


def _read_existing(cur) -> list[dict]:
    cur.execute(
        "SELECT plan_date, phase, week_num, session_type AS old_session_type, notes "
        "FROM health.plan ORDER BY plan_date"
    )
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _print_summary(existing: list[dict], rows: list[dict], n: int = 14) -> None:
    print(f"\nBefore/after — first {n} days (plan_date | old -> new session_type | program):")
    print("-" * 78)
    by_date = {r["plan_date"]: r for r in rows}
    for ex in existing[:n]:
        d = ex["plan_date"]
        new = by_date[d]
        wd = d.strftime("%a")
        print(
            f"  {d.isoformat()} {wd} | {ex['old_session_type']:>16} -> "
            f"{new['session_type']:<16} | {new['program_label']}"
        )
    print("-" * 78)
    print(f"Total existing rows: {len(existing)} (all will be rewritten in place)\n")


# ---------------------------------------------------------------------------
# Self-test (no DB): synthesize 14 consecutive days + sample blocks JSON.
# ---------------------------------------------------------------------------

def _self_test() -> None:
    print("SELF-TEST (no DB) — synthetic 14-day preview\n")
    start = date(2026, 6, 8)  # a Monday, for a clean Mon..Sun read
    existing = []
    for i in range(14):
        d = start + timedelta(days=i)
        existing.append({
            "plan_date": d, "phase": 1, "week_num": 1 + (i // 7),
            "old_session_type": "strength_c",  # pretend prior label
        })
    rows = [build_row(e["plan_date"], e["phase"], e["week_num"]) for e in existing]
    _print_summary(existing, rows)

    # Show one of each program's blocks JSON, validated against the contract.
    seen = set()
    for r in rows:
        if r["program_label"] in seen:
            continue
        seen.add(r["program_label"])
        print(f"=== {r['program_label']}  (session_type={r['session_type']}, "
              f"target_rpe={r['target_rpe']}, hr_zone={r['target_hr_zone']}, "
              f"est={r['est_duration_min']}, week_num={r['week_num']}) ===")
        print(json.dumps(r["blocks"], indent=2))
        print()
    _validate(rows)
    print("Self-test OK.")


def _validate(rows: list[dict]) -> None:
    """Cheap structural assertions so a bad edit fails loudly."""
    legal_session = {"strength_a", "strength_b", "strength_c",
                     "cardio_intervals", "cardio_z2", "walk", "rest_mobility"}
    for r in rows:
        assert r["session_type"] in legal_session, f"illegal session_type {r['session_type']}"
        b = r["blocks"]
        assert b.get("display_name"), "blocks must carry a display_name"
        assert b["type"] in ("circuit", "intervals", "steady", "mobility"), b["type"]
        if b["type"] == "circuit":
            assert b["exercises"] and all("name" in e and "format" in e for e in b["exercises"])
            assert "finisher" in b, "strength must carry a finisher"
        if r["program_label"] in _PROGRAMS_WITH_FINISHER:
            assert "finisher" in b and b["finisher"]["type"] == "core_circuit"
        else:
            assert "finisher" not in b, f"{r['program_label']} must NOT have a finisher"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def reseed(dry_run: bool) -> int:
    _load_dotenv()
    with _connect() as conn:
        cur = conn.cursor()

        ok, msg = _preflight(cur)
        print(f"[preflight] {msg}")
        if not ok and not dry_run:
            conn.rollback()
            raise SystemExit(f"[ABORT] {msg}")

        existing = _read_existing(cur)
        if not existing:
            print("No existing rows in health.plan — nothing to reseed.")
            conn.rollback()
            return 0
        run_date = date.today()
        rows = [
            build_row(e["plan_date"], e["phase"], e["week_num"],
                      existing_notes=e.get("notes"), run_date=run_date)
            for e in existing
        ]
        _validate(rows)
        _print_summary(existing, rows)

        if dry_run:
            # Read-only: roll back the (read) transaction, write nothing.
            conn.rollback()
            print("[DRY-RUN] (default) No rows written. Re-run with --commit to write.")
            return 0

        try:
            for r in rows:
                cur.execute(_UPSERT_SQL, (
                    r["plan_date"], r["phase"], r["week_num"], r["session_type"],
                    json.dumps(r["blocks"]), r["target_rpe"], r["target_hr_zone"],
                    r["est_duration_min"], r["generated_by"], r["notes"],
                ))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        print(f"[OK] Rewrote {len(rows)} rows in health.plan "
              f"(generated_by='{GENERATED_BY_DB}').")
        return len(rows)


# ---------------------------------------------------------------------------
# Ramp mode (feat/health-ramp) — delegates the schedule + builders to
# artemis.health_ramp; this file owns the connection + dry-run/commit discipline.
# ---------------------------------------------------------------------------

def _ramp_self_test() -> None:
    import artemis.health_ramp as hr
    print("RAMP SELF-TEST (no DB) — weeks 1-7 schedule\n")
    rows = hr.build_rows(hr.build_initial_schedule())
    hr.validate_ramp_rows(rows)
    print(f"{'DATE':<12}{'WD':<5}{'WK':<4}{'SESSION_TYPE':<18}SESSION")
    print("-" * 70)
    for r in rows:
        d = r["plan_date"]
        print(f"{d.isoformat():<12}{d.strftime('%a'):<5}{r['week_num']:<4}"
              f"{r['session_type']:<18}{r['blocks']['display_name']}")
    print("-" * 70)
    print(f"{len(rows)} rows, {hr.RAMP_START.isoformat()} .. "
          f"{max(r['plan_date'] for r in rows).isoformat()} "
          f"(hard stop {hr.RAMP_END.isoformat()})")
    assert len(rows) == 35, f"expected 35 rows, got {len(rows)}"
    assert max(r["plan_date"] for r in rows) == hr.RAMP_END, "rows escaped week 7"
    print("Ramp self-test OK.")


def reseed_ramp(dry_run: bool) -> int:
    import artemis.health_ramp as hr
    _load_dotenv()
    rows = hr.build_rows(hr.build_initial_schedule())
    hr.validate_ramp_rows(rows)
    with _connect() as conn:
        cur = conn.cursor()
        ok, msg = _preflight(cur)
        print(f"[preflight] {msg}")
        if not ok and not dry_run:
            conn.rollback()
            raise SystemExit(f"[ABORT] {msg}")

        cur.execute("SELECT count(*) FROM health.plan WHERE plan_date >= %s", (hr.RAMP_START,))
        to_replace = cur.fetchone()[0]
        print(f"\nRamp reseed: {to_replace} existing row(s) >= {hr.RAMP_START.isoformat()} "
              f"will be REPLACED by {len(rows)} ramp rows "
              f"({hr.RAMP_START.isoformat()} .. {hr.RAMP_END.isoformat()}, weeks 1-7).")

        if dry_run:
            conn.rollback()
            print("[DRY-RUN] (default) No rows written. Re-run with --ramp --commit to write.")
            return 0

        try:
            hr.write_rows(cur, rows, hr.RAMP_START)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        print(f"[OK] Replaced future rows with {len(rows)} ramp rows "
              f"(generated_by='{GENERATED_BY_DB}').")
        return len(rows)


def main() -> None:
    ap = argparse.ArgumentParser(description="Reseed health.plan in place (v2 program).")
    # Dry-run is the DEFAULT (safety). Writing requires an explicit --commit.
    ap.add_argument("--commit", action="store_true",
                    help="Actually write rows. Without this the script is a dry-run.")
    ap.add_argument("--self-test", action="store_true",
                    help="No DB. Synthesize days and print preview + sample blocks.")
    ap.add_argument("--ramp", action="store_true",
                    help="feat/health-ramp: seed the weeks 1-7 ramp schedule "
                         "(delete-forward from 2026-07-25, hard stop 2026-09-11).")
    args = ap.parse_args()

    if args.ramp and args.self_test:
        _ramp_self_test()
        return
    if args.self_test:
        _self_test()
        return
    if args.ramp:
        reseed_ramp(dry_run=not args.commit)
        return
    reseed(dry_run=not args.commit)


if __name__ == "__main__":
    main()
