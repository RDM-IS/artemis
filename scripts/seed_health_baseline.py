"""Seed health.plan with the 137-day baseline (2026-05-06 → 2026-09-19).

Reads templates from scripts/data/baseline_plan.yaml and produces one row
per day with phase-appropriate volume.

Phase windows:
    Phase 1 (Foundation): 2026-05-06 → 2026-06-02   (28 days, wks 1-4)
    Phase 2 (Build):       2026-06-03 → 2026-07-14  (42 days, wks 5-10)
    Phase 3 (Peak):        2026-07-15 → 2026-08-25  (42 days, wks 11-16)
    Phase 4 (Polish):      2026-08-26 → 2026-09-19  (25 days, wks 17-19)

Idempotent by default (ON CONFLICT DO NOTHING). --force truncates first.

Usage:
    RDS_SECRET_ARN=arn:... RDS_HOST=... python scripts/seed_health_baseline.py
    RDS_SECRET_ARN=arn:... RDS_HOST=... python scripts/seed_health_baseline.py --force
"""

import argparse
import copy
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

import yaml

# Ensure repo root is on sys.path
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

FIXTURE_PATH = _REPO_ROOT / "scripts" / "data" / "baseline_plan.yaml"

# Phase windows. (phase, start_date, end_date, week_start, week_end)
# Week numbers are 1-indexed and continuous across phases (1..19).
# Phase 4 runs 25 days but only spans weeks 17-19 — the trailing 4 days
# are absorbed into week 19 (the deload week).
#
# Recalibrated 2026-05-05: start shifted from 2026-05-06 → 2026-06-06 (Saturday).
# Day-of-week mapping also changed; see scripts/data/baseline_plan.yaml.
PHASE_WINDOWS = [
    (1, date(2026, 6, 6),  date(2026, 7, 3),   1,  4),    # 28 days, wks 1-4
    (2, date(2026, 7, 4),  date(2026, 8, 14),  5,  10),   # 42 days, wks 5-10
    (3, date(2026, 8, 15), date(2026, 9, 25),  11, 16),   # 42 days, wks 11-16
    (4, date(2026, 9, 26), date(2026, 10, 20), 17, 19),   # 25 days, wks 17-19 (deload tail)
]

START_DATE = date(2026, 6, 6)
END_DATE = date(2026, 10, 20)


def get_connection():
    """Connect to RDS using AWS Secrets Manager creds. Imports deferred so pure
    functions can be tested without boto3/psycopg2 installed."""
    import psycopg2
    from knowledge.secrets import get_rds_credentials

    host = os.environ.get("RDS_HOST")
    db = os.environ.get("RDS_DB", "crm")
    if not host:
        sys.exit("ERROR: RDS_HOST not set")
    creds = get_rds_credentials()
    return psycopg2.connect(
        host=host, port=5432, dbname=db,
        user=creds["username"], password=creds["password"],
        connect_timeout=10,
    )


def load_fixture() -> dict:
    with open(FIXTURE_PATH) as f:
        return yaml.safe_load(f)


def phase_for_date(d: date) -> tuple[int, int]:
    """Return (phase, week_num) for a given date.

    week_num is clamped to the phase's max so trailing days (e.g. last 4 days
    of Phase 4) get absorbed into the final week rather than overflowing.
    """
    for phase, start, end, week_start, week_end in PHASE_WINDOWS:
        if start <= d <= end:
            week_offset = (d - start).days // 7
            week_num = min(week_start + week_offset, week_end)
            return phase, week_num
    raise ValueError(f"Date {d} outside training window")


def build_circuit_blocks(template: dict, phase: int, fixture: dict, week_num: int) -> dict:
    """Apply phase-specific overrides to a strength template."""
    blocks = copy.deepcopy(template)
    meta = fixture["phase_meta"][phase]

    blocks["rounds"] = meta["rounds"]

    # Week 19 mandatory deload — 50% volume
    if week_num == 19:
        # Halve the rounds (min 1) and note it
        blocks["rounds"] = max(1, blocks["rounds"] // 2)
        existing = blocks.get("setup_notes", [])
        existing.append("DELOAD WEEK 19: 50% volume. Easy intentional sets.")
        blocks["setup_notes"] = existing

    # Phase 3 / 4: heavy top set notation on first compound exercise
    if phase in (3, 4) and week_num != 19 and blocks.get("exercises"):
        first = blocks["exercises"][0]
        first["notes"] = "Heavy top set: add 5lb if last week's RPE <= target."

    return blocks


def build_intervals_blocks(template: dict, phase: int, week_num: int) -> dict:
    """Apply phase progression to interval workouts."""
    blocks = copy.deepcopy(template)

    if week_num == 19:
        # Deload: halve rounds
        blocks["rounds"] = max(1, blocks["rounds"] // 2)
        existing = blocks.get("setup_notes", [])
        existing.append("DELOAD WEEK 19: 50% volume.")
        blocks["setup_notes"] = existing
    elif phase >= 2:
        # Add 1 round in Phase 2; 2 rounds in Phase 3+
        blocks["rounds"] = blocks["rounds"] + (1 if phase == 2 else 2)

    return blocks


def build_passthrough(template: dict, phase: int, week_num: int) -> dict:
    """Walk / mobility — no progression, return as-is."""
    return copy.deepcopy(template)


def build_blocks(session_type: str, fixture: dict, phase: int, week_num: int) -> tuple[dict, int, int | None, float]:
    """Build the blocks JSONB + (est_duration_min, target_hr_zone, target_rpe) for a session.

    est_duration_min and target_hr_zone are pulled out of the template and stored as
    top-level columns on health.plan; they are NOT part of the JSONB blocks shape.
    """
    template = fixture[session_type]
    meta = fixture["phase_meta"][phase]
    target_rpe = meta["target_rpe"]

    if template["type"] == "circuit":
        blocks = build_circuit_blocks(template, phase, fixture, week_num)
    elif template["type"] == "intervals":
        blocks = build_intervals_blocks(template, phase, week_num)
    else:
        blocks = build_passthrough(template, phase, week_num)

    est_duration = blocks.pop("est_duration_min", template.get("est_duration_min", 30))
    target_hr_zone = blocks.pop("target_hr_zone", template.get("target_hr_zone"))

    # Rest day → no RPE expected
    if session_type == "rest_mobility":
        target_rpe = None
    elif session_type == "walk":
        target_rpe = None

    return blocks, est_duration, target_hr_zone, target_rpe


def generate_rows(fixture: dict) -> list[tuple]:
    """Generate one tuple per day in (2026-05-06 .. 2026-09-19)."""
    rows = []
    d = START_DATE
    while d <= END_DATE:
        phase, week_num = phase_for_date(d)
        weekday = d.weekday()  # Mon=0
        session_type = fixture["day_to_session"][weekday]
        blocks, est_duration, hr_zone, target_rpe = build_blocks(
            session_type, fixture, phase, week_num
        )

        rows.append((
            d,                          # plan_date
            phase,                      # phase
            week_num,                   # week_num
            session_type,               # session_type
            json.dumps(blocks),         # blocks
            target_rpe,                 # target_rpe
            hr_zone,                    # target_hr_zone
            est_duration,               # est_duration_min
            False,                      # is_override
            False,                      # is_skipped
            None,                       # skip_reason
            "baseline",                 # generated_by
            None,                       # notes
        ))
        d += timedelta(days=1)
    return rows


INSERT_SQL = """
INSERT INTO health.plan (
    plan_date, phase, week_num, session_type, blocks,
    target_rpe, target_hr_zone, est_duration_min,
    is_override, is_skipped, skip_reason, generated_by, notes
) VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (plan_date) DO NOTHING
"""


def seed(force: bool = False) -> int:
    """Seed health.plan. Returns the number of rows inserted."""
    fixture = load_fixture()
    rows = generate_rows(fixture)

    expected_total = (END_DATE - START_DATE).days + 1
    if len(rows) != expected_total:
        sys.exit(f"ERROR: generated {len(rows)} rows, expected {expected_total}")

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            if force:
                print("[FORCE] Truncating health.plan ...")
                cur.execute("TRUNCATE health.plan RESTART IDENTITY CASCADE")

            cur.executemany(INSERT_SQL, rows)
            inserted = cur.rowcount
        conn.commit()
    finally:
        conn.close()

    return inserted


def main():
    parser = argparse.ArgumentParser(description="Seed health.plan baseline")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Truncate health.plan before inserting (destructive).",
    )
    args = parser.parse_args()

    print(f"Seeding {(END_DATE - START_DATE).days + 1} plan rows ...")
    inserted = seed(force=args.force)
    if inserted == 0:
        print("[NO-OP] All rows already present (use --force to reload).")
    else:
        print(f"[OK] Inserted {inserted} rows into health.plan.")


if __name__ == "__main__":
    main()
