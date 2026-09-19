#!/usr/bin/env python3.11
"""Validate health.plan against the HEALTH-2 office gym program — READ-ONLY.

Never writes, no LLM calls. Reads every live health.plan row on/after the office
start (9/16) and checks it against exactly what artemis.health_office seeds (one
source — no second copy of the program to drift):

  PRESENT       every date 9/16..11/08 has a row
  SESSION_TYPE  weekly pattern (+ ramp-up days) matches
  PHASE/WEEK    phase 1, week_num 1-7
  BLOCK_TYPE    circuit / steady / mobility as seeded
  DISPLAY_NAME  as seeded
  LOCATION      blocks.location matches (office gym / outside)
  NO_RETIRED    no rower / bike-on-trainer / road bike / PowerBlock / TRX /
                walking pad anywhere in blocks
  HARD_STOP     no rows after 11/08

    /usr/bin/python3.11 scripts/validate_health_plan.py                   # live
    /usr/bin/python3.11 scripts/validate_health_plan.py --self-test       # no DB
    /usr/bin/python3.11 scripts/validate_health_plan.py --self-test --fault

--ramp is retired (HEALTH-2) and refuses to run.
"""

import sys

if sys.version_info < (3, 11):
    sys.exit("validate_health_plan.py requires Python 3.11+ "
             "(run: /usr/bin/python3.11 scripts/validate_health_plan.py ...).")

import argparse
import copy
import json
import os
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

import artemis.health_office as office  # noqa: E402


def _coerce_blocks(blocks):
    if isinstance(blocks, str):
        try:
            return json.loads(blocks)
        except (ValueError, TypeError):
            return {}
    return blocks or {}


def _load_dotenv():
    p = _REPO_ROOT / ".env"
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


@contextmanager
def _connect():
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


def load_live() -> dict[date, dict]:
    """READ-ONLY: every row on/after the office start."""
    _load_dotenv()
    with _connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT plan_date, phase, week_num, session_type, blocks, est_duration_min "
            "FROM health.plan WHERE plan_date >= %s ORDER BY plan_date", (office.OFFICE_START,))
        rows = {d: {"phase": ph, "week_num": wk, "session_type": st, "blocks": _coerce_blocks(b),
                    "est_duration_min": est}
                for d, ph, wk, st, b, est in cur.fetchall()}
        conn.rollback()
        return rows


def evaluate(live: dict[date, dict]) -> list[dict]:
    expected = {r["plan_date"]: r for r in office.build_rows()}
    results: list[dict] = []

    def add(d, cat, ok, reason="", note=""):
        results.append({"date": d, "category": cat, "ok": ok, "reason": reason, "note": note})

    for d, exp in sorted(expected.items()):
        row = live.get(d)
        if row is None:
            add(d, "PRESENT", False, "missing office row")
            continue
        add(d, "PRESENT", True)
        b = _coerce_blocks(row["blocks"])
        session_type = row["session_type"]
        # FRIDAY-1: a check-in adjustment keeps the plan as written in
        # blocks.original — validate that, not the day's adjusted copy.
        if isinstance(b.get("original"), dict):
            session_type = b["original"].get("session_type") or session_type
            b = _coerce_blocks(b["original"].get("blocks"))
        eb = exp["blocks"]
        add(d, "SESSION_TYPE", session_type == exp["session_type"],
            f"session_type={session_type!r}, expected {exp['session_type']!r}")
        add(d, "PHASE/WEEK", (row["phase"], row["week_num"]) == (exp["phase"], exp["week_num"]),
            f"phase/week={row['phase']}/{row['week_num']}, expected {exp['phase']}/{exp['week_num']}")
        add(d, "BLOCK_TYPE", b.get("type") == eb["type"],
            f"blocks.type={b.get('type')!r}, expected {eb['type']!r}")
        add(d, "DISPLAY_NAME", b.get("display_name") == eb["display_name"],
            f"display_name={b.get('display_name')!r}, expected {eb['display_name']!r}")
        add(d, "LOCATION", b.get("location") == eb.get("location"),
            f"location={b.get('location')!r}, expected {eb.get('location')!r}")
        # TIME-CAP: 60+ fails (unless CALIBRATION_PENDING); 45-59 is a note.
        kind, msg = office.duration_verdict(session_type, row["week_num"], row.get("est_duration_min"))
        add(d, "DURATION", kind != "reject", msg, note=msg if kind in ("note", "pending") else "")

    for d in sorted(live):
        hits = office.forbidden_hits(live[d]["blocks"])
        add(d, "NO_RETIRED", not hits, f"retired equipment referenced: {hits}" if hits else "")
        if d > office.OFFICE_END:
            add(d, "HARD_STOP", False, f"unexpected row after {office.OFFICE_END.isoformat()}")
    return results


def report(results: list[dict]) -> int:
    print(f"Office plan validation — {office.OFFICE_START.isoformat()} .. "
          f"{office.OFFICE_END.isoformat()}\n")
    categories: list[str] = []
    for r in results:
        if r["category"] not in categories:
            categories.append(r["category"])
    for cat in categories:
        subset = [r for r in results if r["category"] == cat]
        fails = [r for r in subset if not r["ok"]]
        print(f"  [{'PASS' if not fails else 'FAIL'}] {cat:<14} {len(subset) - len(fails)}/{len(subset)}"
              + (f"   ({len(fails)} fail)" if fails else ""))
    fails = [r for r in results if not r["ok"]]
    notes = [r for r in results if r["ok"] and r.get("note")]
    if notes:
        print(f"\nNOTES (target {office.TARGET_MIN} min, never auto-cut):")
        for r in notes:
            print(f"  {r['date'].isoformat()} {r['category']:<14} {r['note']}")
    print("\n" + "=" * 70)
    if fails:
        print("FAILURES:")
        for r in sorted(fails, key=lambda x: (x["date"], x["category"])):
            print(f"  {r['date'].isoformat()} {r['category']:<14} {r['reason']}")
    print(f"\nSummary: assertions_pass={len(results) - len(fails)}  assertions_fail={len(fails)}")
    print(f"RESULT: {'PASS' if not fails else 'FAIL'}")
    return 0 if not fails else 1


def selftest_live(fault: bool) -> dict[date, dict]:
    live = {r["plan_date"]: {"phase": r["phase"], "week_num": r["week_num"],
                             "session_type": r["session_type"],
                             "blocks": copy.deepcopy(r["blocks"]),
                             "est_duration_min": r["est_duration_min"]}
            for r in office.build_rows()}
    if fault:
        # a 61-min week-1 Strength C (not CALIBRATION_PENDING) must fail DURATION
        live[office.WEEK1_START + timedelta(days=5)]["est_duration_min"] = 61
        live[office.WEEK1_START]["blocks"]["equipment"].append("water rower")
        live[office.WEEK1_START + timedelta(days=1)]["session_type"] = "cardio_intervals"
        live.pop(office.WEEK1_START + timedelta(days=3))
        live[office.OFFICE_END + timedelta(days=1)] = {
            "phase": 1, "week_num": 7, "session_type": "cardio_z2",
            "blocks": {"type": "steady", "equipment": ["bike on trainer"]}}
    return live


def main():
    ap = argparse.ArgumentParser(description="Validate health.plan against the office program (READ-ONLY).")
    ap.add_argument("--self-test", action="store_true", help="No DB. Validate the builders' own rows.")
    ap.add_argument("--fault", action="store_true", help="With --self-test: inject faults; expect FAIL.")
    ap.add_argument("--ramp", action="store_true", help="RETIRED — refuses to run.")
    args = ap.parse_args()
    if args.ramp:
        sys.exit("--ramp is retired (HEALTH-2); validate the office plan (no flag).")
    live = selftest_live(args.fault) if args.self_test else load_live()
    sys.exit(report(evaluate(live)))


if __name__ == "__main__":
    main()
