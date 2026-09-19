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

YOGA-1 — rewrite ONLY the Recovery Flow days (Thu office / Sat home) from a date:

    /usr/bin/python3.11 scripts/reseed_health_plan_v2.py --office --flow-days --from 2026-09-19
    /usr/bin/python3.11 scripts/reseed_health_plan_v2.py --office --flow-days --from 2026-09-19 --commit

It needs migration 033 (session_type CHECK allows 'recovery_flow'), refuses any
date that already has a real session_log row, and touches no other day.

Rewrite ONLY the strength rows whose canonical blocks changed (e.g. an exercise
rename), from a date. Prints a per-row JSON diff; rows already matching the
builder are left alone:

    /usr/bin/python3.11 scripts/reseed_health_plan_v2.py --office --strength-days --from 2026-09-18
    /usr/bin/python3.11 scripts/reseed_health_plan_v2.py --office --strength-days --from 2026-09-18 --allow-logged --commit

It refuses a row carrying a check-in adjustment, an override, or a different
session_type, and refuses a date with real session_log rows unless
--allow-logged (the logs keep their plan_id; only the plan row is rewritten).

Retired (HEALTH-2): the legacy v2 whole-table home-gym reseed (PowerBlocks /
TRX / rower / bike). The --ramp mode and the ramp engine were deleted in
RAMP-RETIRE (2026-09-19).

CONSTRAINTS (live RDS): session_type CHECK (strength_a/b/c, cardio_intervals,
cardio_z2, walk, rest_mobility, recovery_flow [033]); week_num CHECK 1..19; phase CHECK 1..4;
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
from datetime import date
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

import artemis.health_office as office  # noqa: E402

GENERATED_BY_DB = office.GENERATED_BY

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
    from knowledge.dbguard import refuse_real_db
    url = os.environ.get("DATABASE_URL")
    if url:
        refuse_real_db(__name__ + "._connect")
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


def _read_tail(cur) -> list[tuple]:
    """Rows PAST the program end — left behind when the window shortened
    (HEALTH-2 seeded through 11/08; the GO-LIVE reset ends 11/03). They are
    orphans of the old schedule and must not linger as real plan days."""
    cur.execute(
        "SELECT p.plan_date, p.session_type, "
        "       (SELECT count(*) FROM health.session_log sl "
        "        WHERE sl.plan_id = p.plan_id AND sl.logged_via <> 'inferred') "
        "FROM health.plan p WHERE p.plan_date > %s ORDER BY p.plan_date",
        (office.OFFICE_END,))
    return cur.fetchall()


def _delete_tail(cur) -> int:
    """Delete post-window rows. Refuses if any carries a real session_log."""
    tail = _read_tail(cur)
    logged = [(d, st, n) for d, st, n in tail if n]
    if logged:
        raise SystemExit(f"[ABORT] rows after {office.OFFICE_END} carry real session_log "
                         f"rows — refusing to delete: {logged}")
    if not tail:
        return 0
    cur.execute("DELETE FROM health.plan WHERE plan_date > %s", (office.OFFICE_END,))
    return cur.rowcount


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
    from datetime import timedelta as _td
    wk1 = office.WEEK1_START
    picks = {wk1,                 # Wed  wk1 strength_a (go-live day)
             wk1 + _td(days=2),   # Fri  wk1 strength_b
             wk1 + _td(days=4),   # Sun  wk1 walk
             wk1 + _td(days=5),   # Mon  wk1 strength_c
             wk1 + _td(days=6),   # Tue  wk1 cardio_z2
             wk1 + _td(days=1),   # Thu  wk1 rest
             wk1 + _td(days=33)}  # Mon  wk5 strength_c (finisher)
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

def print_duration_notes(notes: list[str]) -> None:
    """TIME-CAP: 45-59 min (and CALIBRATION_PENDING 60+) rows, noted — never cut."""
    if notes:
        print(f"DURATION NOTES (target {office.TARGET_MIN} min; {office.HARD_MAX_MIN}+ rejected):")
        for n in notes:
            print(f"  {n}")
        print()


def office_self_test() -> None:
    rows = office.build_rows()
    notes = office.validate_rows(rows)
    print("OFFICE SELF-TEST (no DB)")
    print_diff({}, rows)
    print_duration_notes(notes)
    print_samples(rows)
    print("Office self-test OK.")


def reseed_office(dry_run: bool) -> int:
    _load_dotenv()
    rows = office.build_rows()
    notes = office.validate_rows(rows)
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
        print_duration_notes(notes)

        tail = _read_tail(cur)
        if tail:
            print(f"ROWS PAST {office.OFFICE_END} (to DELETE — orphans of the old window):")
            for d, st, n in tail:
                flag = f"  << {n} REAL session_log row(s)" if n else ""
                print(f"  {d.isoformat()}  {d.strftime('%a')}  {st}{flag}")
            print(f"  ({len(tail)} row(s) to delete)\n")
        else:
            print(f"No rows past {office.OFFICE_END}.\n")

        if dry_run:
            conn.rollback()
            print("[DRY-RUN] (default) No rows written. Re-run with --office --commit to write.")
            return 0

        try:
            n_deleted = _delete_tail(cur)
            office.write_rows(cur, rows)
            conn.commit()
            if n_deleted:
                print(f"[OK] Deleted {n_deleted} row(s) past {office.OFFICE_END}.")
        except Exception:
            conn.rollback()
            raise
        print(f"[OK] Wrote {len(rows)} office rows (generated_by='{GENERATED_BY_DB}') + audit row.")
        return len(rows)


def flow_rows(start: date) -> list[dict]:
    """The Recovery Flow rows from `start` through the program end."""
    rows = [r for r in office.build_rows()
            if r["session_type"] == "recovery_flow" and start <= r["plan_date"] <= office.OFFICE_END]
    for r in rows:
        office.validate_flow(r["blocks"])
    return rows


def _flow_preflight(cur) -> tuple[bool, str]:
    cur.execute(
        """SELECT pg_get_constraintdef(c.oid) FROM pg_constraint c
           JOIN pg_class t ON t.oid = c.conrelid
           JOIN pg_namespace n ON n.oid = t.relnamespace
           WHERE n.nspname='health' AND t.relname='plan'
             AND c.conname = 'plan_session_type_check'""")
    row = cur.fetchone()
    if row and "recovery_flow" not in row[0]:
        return False, "session_type CHECK lacks 'recovery_flow' — apply migration 033 first."
    return True, "session_type CHECK permits 'recovery_flow'."


def flow_diff_lines(existing: dict, rows: list[dict]) -> list[str]:
    out = [f"{'DATE':<11}{'WD':<4}{'CURRENT':<52}NEW", "-" * 120]
    for r in rows:
        d = r["plan_date"]
        old = existing.get(d)
        old_s = _row_label(old) if old else "(no row)"
        b = r["blocks"]
        new_s = (f"{_row_label({'phase': r['phase'], 'week_num': r['week_num'], 'session_type': r['session_type'], 'display_name': b['display_name']})}"
                 f" · {b['location']} · {r['est_duration_min']} min · RPE {r['target_rpe']:g}")
        out.append(f"{d.isoformat():<11}{d.strftime('%a'):<4}{old_s:<52}{new_s}")
    out.append("-" * 120)
    out.append(f"{len(rows)} Recovery Flow rows rewritten; no other dates touched.")
    out.append(f"acos.system_state {office.PROGRAM_STATE_KEY} := {json.dumps(office.program_state())}")
    return out


def reseed_flow_days(start: date, dry_run: bool) -> int:
    _load_dotenv()
    rows = flow_rows(start)
    with _connect() as conn:
        cur = conn.cursor()
        ok, msg = _flow_preflight(cur)
        print(f"[preflight] {msg}")
        if not ok:
            conn.rollback()
            raise SystemExit(f"[ABORT] {msg}")
        dates = [r["plan_date"] for r in rows]
        cur.execute(
            "SELECT p.plan_date, count(*) FROM health.session_log sl "
            "JOIN health.plan p ON p.plan_id = sl.plan_id "
            "WHERE p.plan_date = ANY(%s) AND sl.logged_via <> 'inferred' GROUP BY 1",
            (dates,))
        logged = cur.fetchall()
        if logged:
            conn.rollback()
            raise SystemExit(f"[ABORT] Recovery Flow dates already have real logs: {logged}")
        print("\n".join(flow_diff_lines(_read_existing(cur), rows)))
        if dry_run:
            conn.rollback()
            print("[DRY-RUN] (default) No rows written. Re-run with --commit to write.")
            return 0
        try:
            for r in rows:
                cur.execute(office._UPSERT_SQL, (
                    r["plan_date"], r["phase"], r["week_num"], r["session_type"],
                    json.dumps(r["blocks"]), r["target_rpe"], r["target_hr_zone"],
                    r["est_duration_min"], r["generated_by"], r["notes"]))
            cur.execute(
                "INSERT INTO acos.audit_log (agent, persona, action, domain, confidence, "
                "outcome, token_count, api_cost_usd, metadata) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)",
                ("health_office", None, "recovery_flow_reseed", "health", None, "executed",
                 0, 0.0, json.dumps({"from": start.isoformat(), "dates": [d.isoformat() for d in dates]})))
            office.write_program_state(cur)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        print(f"[OK] Wrote {len(rows)} Recovery Flow rows + audit row.")
        return len(rows)


_ROW_FIELDS = ("session_type", "week_num", "phase", "target_rpe", "target_hr_zone",
               "est_duration_min", "notes")


def _live_rows(cur, dates) -> dict:
    cur.execute(
        "SELECT plan_date, session_type, week_num, phase, target_rpe, target_hr_zone, "
        "est_duration_min, notes, blocks, is_override FROM health.plan "
        "WHERE plan_date = ANY(%s)", (list(dates),))
    out = {}
    for d, st, wk, ph, rpe, zone, est, notes, blocks, override in cur.fetchall():
        b = json.loads(blocks) if isinstance(blocks, str) else (blocks or {})
        out[d] = {"session_type": st, "week_num": wk, "phase": ph,
                  "target_rpe": float(rpe) if rpe is not None else None,
                  "target_hr_zone": zone, "est_duration_min": est, "notes": notes,
                  "blocks": b, "is_override": override}
    return out


def strength_changes(live: dict, rows: list[dict]) -> tuple[list[dict], list[str]]:
    """(rows whose live copy differs from the builder, refusal reasons)."""
    changed, refusals = [], []
    for r in rows:
        d = r["plan_date"]
        cur = live.get(d)
        if cur is None:
            refusals.append(f"{d}: no live row")
            continue
        if cur["session_type"] != r["session_type"]:
            refusals.append(f"{d}: live session_type {cur['session_type']} != {r['session_type']}")
        elif cur["is_override"] or "adjustment" in cur["blocks"] or "original" in cur["blocks"]:
            refusals.append(f"{d}: live row carries an override / check-in adjustment")
        elif cur["blocks"] != r["blocks"] or any(
                cur[k] != (float(r[k]) if k == "target_rpe" and r[k] is not None else r[k])
                for k in _ROW_FIELDS):
            changed.append(r)
    return changed, refusals


def strength_diff_lines(live: dict, rows: list[dict]) -> list[str]:
    import difflib
    out = []
    for r in rows:
        d = r["plan_date"]
        old = live[d]
        a = {k: old[k] for k in _ROW_FIELDS} | {"blocks": old["blocks"]}
        b = {k: r[k] for k in _ROW_FIELDS} | {"blocks": r["blocks"]}
        out.append(f"=== {d.isoformat()} {d.strftime('%a')} {r['session_type']} wk{r['week_num']}")
        out.extend(difflib.unified_diff(
            json.dumps(a, indent=1, ensure_ascii=False, sort_keys=True, default=str).splitlines(),
            json.dumps(b, indent=1, ensure_ascii=False, sort_keys=True, default=str).splitlines(),
            "live", "new", n=1, lineterm=""))
    return out


def reseed_strength_days(start: date, dry_run: bool, allow_logged: bool) -> int:
    _load_dotenv()
    rows = [r for r in office.build_rows()
            if r["session_type"].startswith("strength") and start <= r["plan_date"] <= office.OFFICE_END]
    office.validate_rows(office.build_rows())
    with _connect() as conn:
        cur = conn.cursor()
        live = _live_rows(cur, [r["plan_date"] for r in rows])
        changed, refusals = strength_changes(live, rows)
        _, notes = office.duration_findings(changed)
        if refusals:
            conn.rollback()
            raise SystemExit("[ABORT] " + "; ".join(refusals))
        dates = [r["plan_date"] for r in changed]
        cur.execute(
            "SELECT p.plan_date, count(*) FROM health.session_log sl "
            "JOIN health.plan p ON p.plan_id = sl.plan_id "
            "WHERE p.plan_date = ANY(%s) AND sl.logged_via <> 'inferred' GROUP BY 1 ORDER BY 1",
            (dates,))
        logged = cur.fetchall()
        print("\n".join(strength_diff_lines(live, changed)))
        print(f"\n{len(changed)} of {len(rows)} strength rows from {start} differ from the "
              f"builder; no other dates touched.")
        print_duration_notes(notes)
        if logged:
            print(f"Logged dates in the set (logs keep their plan_id): {logged}")
            if not allow_logged:
                conn.rollback()
                raise SystemExit("[ABORT] re-run with --allow-logged to rewrite logged dates.")
        if dry_run or not changed:
            conn.rollback()
            print("[DRY-RUN] (default) No rows written. Re-run with --commit to write.")
            return 0
        try:
            for r in changed:
                cur.execute(office._UPSERT_SQL, (
                    r["plan_date"], r["phase"], r["week_num"], r["session_type"],
                    json.dumps(r["blocks"]), r["target_rpe"], r["target_hr_zone"],
                    r["est_duration_min"], r["generated_by"], r["notes"]))
            cur.execute(
                "INSERT INTO acos.audit_log (agent, persona, action, domain, confidence, "
                "outcome, token_count, api_cost_usd, metadata) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)",
                ("health_office", None, "strength_days_reseed", "health", None, "executed",
                 0, 0.0, json.dumps({"from": start.isoformat(),
                                     "dates": [d.isoformat() for d in dates],
                                     "duration_notes": notes})))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        print(f"[OK] Wrote {len(changed)} strength rows + audit row.")
        return len(changed)


def main() -> None:
    ap = argparse.ArgumentParser(description="Reseed health.plan (HEALTH-2 office gym).")
    ap.add_argument("--office", action="store_true",
                    help="Seed the office gym schedule (see health_office window).")
    ap.add_argument("--commit", action="store_true",
                    help="Actually write rows. Without this the script is a dry-run.")
    ap.add_argument("--self-test", action="store_true", help="No DB. Print the schedule.")
    ap.add_argument("--flow-days", action="store_true",
                    help="YOGA-1: rewrite only the Thu/Sat Recovery Flow rows (with --from).")
    ap.add_argument("--strength-days", action="store_true",
                    help="Rewrite only strength rows that differ from the builder (with --from).")
    ap.add_argument("--allow-logged", action="store_true",
                    help="--strength-days: also rewrite dates that already have real logs.")
    ap.add_argument("--from", dest="start", type=date.fromisoformat,
                    help="First date for --flow-days / --strength-days (YYYY-MM-DD).")
    args = ap.parse_args()

    if not args.office:
        ap.error("choose a mode: --office (the legacy v2 home-gym reseed is retired)")
    if args.flow_days:
        if not args.start:
            ap.error("--flow-days needs --from YYYY-MM-DD")
        reseed_flow_days(args.start, dry_run=not args.commit)
        return
    if args.strength_days:
        if not args.start:
            ap.error("--strength-days needs --from YYYY-MM-DD")
        reseed_strength_days(args.start, dry_run=not args.commit, allow_logged=args.allow_logged)
        return
    if args.self_test:
        office_self_test()
        return
    reseed_office(dry_run=not args.commit)


if __name__ == "__main__":
    main()
