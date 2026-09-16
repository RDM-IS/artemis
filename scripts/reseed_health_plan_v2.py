#!/usr/bin/env python3.11
"""Reseed health.plan — HEALTH-2 office gym program.

Office mode writes the canonical 9/16-11/08 schedule (5 ramp-up days + weeks
1-7) from artemis.health_office: INSERT ... ON CONFLICT (plan_date) DO UPDATE for
every date in the window, in ONE transaction with an acos.audit_log row. plan_ids
are preserved; phase/week_num/session_type/blocks/targets/notes are rewritten and
is_skipped/is_override/skip_reason are reset. Rows outside the window are never
touched.

Dry-run is the DEFAULT and prints the old → new diff for every date. Nothing is
written without --commit.

    /usr/bin/python3.11 scripts/reseed_health_plan_v2.py --office              # DRY-RUN
    /usr/bin/python3.11 scripts/reseed_health_plan_v2.py --office --commit     # WRITE
    /usr/bin/python3.11 scripts/reseed_health_plan_v2.py --office --self-test   # no DB

Retired (HEALTH-2):
  * the legacy v2 whole-table home-gym reseed (PowerBlocks / TRX / rower / bike),
  * --ramp (feat/health-ramp weeks 1-7, 7/25-9/11). Its delete-forward from 7/25
    would wipe the office plan, so it refuses to run.

CONSTRAINTS (live RDS): session_type CHECK (strength_a/b/c, cardio_intervals,
cardio_z2, walk, rest_mobility); week_num CHECK 1..19; phase CHECK 1..4;
generated_by CHECK (baseline|autoreg_morning|autoreg_evening|manual) — a
human-run reseed writes 'manual' and the CHECK is NOT expanded.

Connection: $DATABASE_URL if set (parsed from .env), else knowledge.db
get_connection() (RDS_HOST + Secrets Manager). RDS is in a private VPC — run on
the EC2 host.
"""

import sys

# Pre-import version guard. The runtime + this tooling target Python 3.11 (the box
# runs acos under python3.11). A stray `python3` (3.9 on the box) fails fast here.
if sys.version_info < (3, 11):
    sys.exit("reseed_health_plan_v2.py requires Python 3.11+ "
             "(run: /usr/bin/python3.11 scripts/reseed_health_plan_v2.py ...).")

import argparse
import json
import os
from contextlib import contextmanager
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

import artemis.health_office as office  # noqa: E402

GENERATED_BY_DB = office.GENERATED_BY

RAMP_RETIRED_MSG = (
    "--ramp is retired (HEALTH-2): the weeks 1-7 ramp window (7/25-9/11) passed "
    "undeployed and its delete-forward would wipe the office plan. "
    "Use --office."
)


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
    knowledge.db pool (EC2 path). The caller controls the transaction."""
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


def _preflight(cur) -> tuple[bool, str]:
    """Check the LIVE generated_by CHECK permits GENERATED_BY_DB. Never raises."""
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
        return False, (f"LIVE health.plan.generated_by CHECK rejects '{GENERATED_BY_DB}': "
                       f"{gb_checks[0]}")
    return True, f"generated_by '{GENERATED_BY_DB}' permitted by live schema."


def _logged_in_window(cur) -> list[tuple]:
    """(plan_date, n) for plan rows in the office window that already carry real
    session_log rows — rewriting their session_type would relabel logged history."""
    cur.execute(
        "SELECT p.plan_date, count(*) FROM health.session_log sl "
        "JOIN health.plan p ON p.plan_id = sl.plan_id "
        "WHERE p.plan_date BETWEEN %s AND %s AND sl.logged_via <> 'inferred' "
        "GROUP BY 1 ORDER BY 1",
        (office.OFFICE_START, office.OFFICE_END))
    return cur.fetchall()


def _read_existing(cur) -> dict:
    cur.execute(
        "SELECT plan_date, phase, week_num, session_type, blocks FROM health.plan "
        "WHERE plan_date BETWEEN %s AND %s ORDER BY plan_date",
        (office.OFFICE_START, office.OFFICE_END))
    out = {}
    for d, phase, wk, st, blocks in cur.fetchall():
        b = json.loads(blocks) if isinstance(blocks, str) else (blocks or {})
        out[d] = {"phase": phase, "week_num": wk, "session_type": st,
                  "display_name": b.get("display_name") or st}
    return out


# ---------------------------------------------------------------------------
# Diff rendering
# ---------------------------------------------------------------------------

def _row_label(r: dict) -> str:
    return f"p{r['phase']} wk{r['week_num']:<2} {r['session_type']:<14} {r['display_name']}"


def print_diff(existing: dict, rows: list[dict]) -> None:
    print(f"\n{'DATE':<11}{'WD':<4}{'CURRENT':<52}NEW")
    print("-" * 120)
    for r in rows:
        d = r["plan_date"]
        new = {"phase": r["phase"], "week_num": r["week_num"],
               "session_type": r["session_type"], "display_name": r["blocks"]["display_name"]}
        old = existing.get(d)
        old_s = _row_label(old) if old else "(no row)"
        print(f"{d.isoformat():<11}{d.strftime('%a'):<4}{old_s:<52}{_row_label(new)}")
    print("-" * 120)
    n_new = sum(1 for r in rows if r["plan_date"] not in existing)
    print(f"{len(rows)} office rows {office.OFFICE_START}..{office.OFFICE_END}: "
          f"{len(rows) - n_new} rewritten, {n_new} inserted.\n")


def print_samples(rows: list[dict]) -> None:
    """One full row per distinct (session_type, week_num) shape worth eyeballing."""
    picks = {office.OFFICE_START, office.WEEK1_MONDAY, office.WEEK1_MONDAY.replace(day=24),
             office.WEEK1_MONDAY.replace(day=25), office.WEEK1_MONDAY.replace(day=22)}
    fri_wk5 = office.WEEK1_MONDAY.replace(month=10, day=23)
    picks.add(fri_wk5)
    for r in rows:
        if r["plan_date"] in picks:
            meta = {k: r[k] for k in ("plan_date", "phase", "week_num", "session_type",
                                      "target_rpe", "target_hr_zone", "est_duration_min", "notes")}
            print(json.dumps(meta, default=str))
            print(json.dumps(r["blocks"], indent=2, ensure_ascii=False))
            print()


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------

def office_self_test() -> None:
    rows = office.build_rows()
    office.validate_rows(rows)
    print("OFFICE SELF-TEST (no DB)")
    print_diff({}, rows)
    print_samples(rows)
    print("Office self-test OK.")


def reseed_office(dry_run: bool) -> int:
    _load_dotenv()
    rows = office.build_rows()
    office.validate_rows(rows)
    with _connect() as conn:
        cur = conn.cursor()
        ok, msg = _preflight(cur)
        print(f"[preflight] {msg}")
        if not ok:
            conn.rollback()
            raise SystemExit(f"[ABORT] {msg}")

        logged = _logged_in_window(cur)
        if logged:
            conn.rollback()
            raise SystemExit(f"[ABORT] real session_log rows exist in the office window "
                             f"(would relabel logged history): {logged}")
        print("[preflight] no logged sessions in the office window.")

        print_diff(_read_existing(cur), rows)

        if dry_run:
            conn.rollback()
            print("[DRY-RUN] (default) No rows written. Re-run with --office --commit to write.")
            return 0

        try:
            office.write_rows(cur, rows)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        print(f"[OK] Wrote {len(rows)} office rows (generated_by='{GENERATED_BY_DB}') + audit row.")
        return len(rows)


def main() -> None:
    ap = argparse.ArgumentParser(description="Reseed health.plan (HEALTH-2 office gym).")
    ap.add_argument("--office", action="store_true",
                    help="Seed the 9/16-11/08 office gym schedule.")
    ap.add_argument("--commit", action="store_true",
                    help="Actually write rows. Without this the script is a dry-run.")
    ap.add_argument("--self-test", action="store_true", help="No DB. Print the schedule.")
    ap.add_argument("--ramp", action="store_true", help="RETIRED — refuses to run.")
    args = ap.parse_args()

    if args.ramp:
        raise SystemExit(RAMP_RETIRED_MSG)
    if not args.office:
        ap.error("choose a mode: --office (the legacy v2 home-gym reseed is retired)")
    if args.self_test:
        office_self_test()
        return
    reseed_office(dry_run=not args.commit)


if __name__ == "__main__":
    main()
