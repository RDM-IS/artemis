"""Validate health.plan against the locked PB-009 program — READ-ONLY.

This script NEVER writes and makes NO LLM calls. It reads the next 28 days of
health.plan, derives day-of-week in America/Chicago, and asserts every row
matches the locked weekly program. It prints a per-day table and a PASS/FAIL
line per rule, then a final summary. Exit code is non-zero if anything FAILs.

Locked program (weekday -> expectations):
    Mon  strength_a        circuit    strength movements      core finisher PRESENT
    Tue  rest_mobility     mobility   no exercises/finisher
    Wed  cardio_intervals  intervals  bike/rower (NO treadmill)  core finisher ABSENT
    Thu  strength_b        circuit    strength movements      core finisher PRESENT
    Fri  rest_mobility     mobility   no exercises/finisher
    Sat  cardio_z2         steady     bike equipment          core finisher PRESENT
    Sun  cardio_z2         steady     run-walk (NO bike)      core finisher ABSENT

Sat and Sun share the cardio_z2 LABEL but must differ in blocks: Sat is a bike
ride, Sun is a run-walk — asserted explicitly so they can't be duplicates.

Run (the script loads .env itself; live RDS must be reached from inside the VPC):
    python scripts/validate_health_plan.py            # validate live health.plan
    python scripts/validate_health_plan.py --self-test          # no DB; synthetic clean rows
    python scripts/validate_health_plan.py --self-test --fault  # inject faults; expect FAIL/exit 1
"""

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

CT = ZoneInfo("America/Chicago")
WINDOW_DAYS = 28

# weekday (Mon=0 .. Sun=6) -> locked expectations
EXPECTED = {
    0: {"session": "strength_a",       "btype": "circuit",   "core": True,  "kind": "strength"},
    1: {"session": "rest_mobility",    "btype": "mobility",  "core": False, "kind": "rest"},
    2: {"session": "cardio_intervals", "btype": "intervals", "core": False, "kind": "cardio"},
    3: {"session": "strength_b",       "btype": "circuit",   "core": True,  "kind": "strength"},
    4: {"session": "rest_mobility",    "btype": "mobility",  "core": False, "kind": "rest"},
    5: {"session": "cardio_z2",        "btype": "steady",    "core": True,  "kind": "cardio"},  # Sat bike
    6: {"session": "cardio_z2",        "btype": "steady",    "core": False, "kind": "cardio"},  # Sun run-walk
}

# Allowed equipment tokens (substring match, lowercase). Anything that matches
# none of these — or matches a forbidden token — FAILs.
ALLOWED_EQUIP = [
    "powerblock", "water rower", "rower", "road bike", "indoor trainer", "bike",
    "trx", "exercise ball", "yoga mat", "mat", "resistance band", "band",
    "curl bar", "flat bench", "bench",
]
FORBIDDEN_EQUIP = ["treadmill", "elliptical", "stair climber", "stairmaster"]

# Strength movements (substring keywords). Deliberately uses COMPOUND press
# keywords so the core "Pallof press" is never miscounted as strength.
STRENGTH_MOVEMENTS = [
    "goblet squat", "squat", "rdl", "deadlift", "single-leg dl",
    "floor press", "chest press", "bench press", "overhead press", "shoulder press",
    "row", "reverse lunge", "lunge", "bicep curl", "curl",
    "band pull-apart", "pull-apart", "pull apart", "glute bridge", "hip thrust", "step-up",
]
# Core movements allowed in a core_circuit finisher.
CORE_MOVEMENTS = [
    "dead bug", "bird dog", "side plank", "hollow hold",
    "ball plank", "trx fallout", "pallof press", "plank",
]


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _coerce_blocks(blocks):
    if isinstance(blocks, str):
        try:
            return json.loads(blocks)
        except (ValueError, TypeError):
            return {}
    return blocks or {}


def _names(items):
    return [str(e.get("name")) for e in (items or []) if isinstance(e, dict) and e.get("name")]


def _main_exercise_names(blocks):
    return _names(blocks.get("exercises"))


def _finisher(blocks):
    f = blocks.get("finisher")
    return f if isinstance(f, dict) else None


def _finisher_names(blocks):
    f = _finisher(blocks)
    return _names(f.get("exercises")) if f else []


def _all_exercise_names(blocks):
    return _main_exercise_names(blocks) + _finisher_names(blocks)


def _is_strength_name(name):
    n = name.lower()
    return any(kw in n for kw in STRENGTH_MOVEMENTS)


def _is_core_name(name):
    n = name.lower()
    return any(kw in n for kw in CORE_MOVEMENTS)


def _equipment(blocks):
    eq = blocks.get("equipment")
    return [str(e) for e in eq] if isinstance(eq, list) else []


def _has_bike_equipment(blocks):
    return any("bike" in e.lower() for e in _equipment(blocks))


def _has_runwalk_structure(blocks):
    """True if the block self-identifies as a run-walk (display_name / setup_notes
    / interval settings mention run/jog/walk)."""
    dn = str(blocks.get("display_name") or "").lower()
    if "run" in dn or "walk" in dn:
        return True
    notes = blocks.get("setup_notes") or []
    if any(("run" in str(s).lower() or "walk" in str(s).lower() or "jog" in str(s).lower()) for s in notes):
        return True
    it = blocks.get("intervals_template") or {}
    blob = " ".join(str(v).lower() for v in it.values())
    return ("jog" in blob or "walk" in blob or "run" in blob)


# ---------------------------------------------------------------------------
# Per-day rule evaluation. Returns list of (category, ok, reason).
# ---------------------------------------------------------------------------

def evaluate_day(d: date, session_type: str, blocks: dict) -> list[tuple[str, bool, str]]:
    exp = EXPECTED[d.weekday()]
    out: list[tuple[str, bool, str]] = []

    def add(cat, ok, reason=""):
        out.append((cat, ok, reason))

    btype = blocks.get("type")
    has_core = _finisher(blocks) is not None

    # 1. weekday -> session_type mapping (catches a drifted week)
    add("SESSION_TYPE", session_type == exp["session"],
        f"session_type={session_type!r}, expected {exp['session']!r}")

    # 2. block type
    add("BLOCK_TYPE", btype == exp["btype"],
        f"blocks.type={btype!r}, expected {exp['btype']!r}")

    # 3. core finisher placement (exactly Mon/Thu/Sat)
    if exp["core"]:
        if not has_core:
            add("CORE_PLACEMENT", False, "core finisher MISSING (expected present)")
        else:
            f = _finisher(blocks)
            ftype_ok = f.get("type") == "core_circuit"
            bad = [n for n in _finisher_names(blocks) if not _is_core_name(n)]
            if not ftype_ok:
                add("CORE_PLACEMENT", False, f"finisher.type={f.get('type')!r}, expected 'core_circuit'")
            elif bad:
                add("CORE_PLACEMENT", False, f"non-core movements in finisher: {bad}")
            else:
                add("CORE_PLACEMENT", True)
    else:
        add("CORE_PLACEMENT", not has_core,
            "unexpected finisher present (core must be ABSENT this day)")

    # 4. equipment allow-list (FAIL on treadmill or anything off-list)
    bad_equip = []
    for e in _equipment(blocks):
        el = e.lower()
        if any(f in el for f in FORBIDDEN_EQUIP):
            bad_equip.append(f"{e} (forbidden)")
        elif not any(a in el for a in ALLOWED_EQUIP):
            bad_equip.append(f"{e} (off-list)")
    add("EQUIPMENT_ALLOWED", not bad_equip, f"bad equipment: {bad_equip}" if bad_equip else "")

    # 5. day-kind specific
    if exp["kind"] == "strength":
        main = _main_exercise_names(blocks)
        offenders = [n for n in main if not _is_strength_name(n)]
        ok = len(main) >= 5 and not offenders
        reason = ""
        if len(main) < 5:
            reason = f"only {len(main)} exercises (need >=5): {main}"
        elif offenders:
            reason = f"non-strength movements present: {offenders}"
        add("STRENGTH_HAS_5PLUS", ok, reason)
        # No cardio-interval/steady structure on a strength day.
        add("STRENGTH_NO_CARDIO_STRUCT", "intervals_template" not in blocks,
            "intervals_template present on a strength day")

    elif exp["kind"] == "rest":
        ok = not _main_exercise_names(blocks) and not has_core
        reason = ""
        if _main_exercise_names(blocks):
            reason = "rest day must have NO exercises[]"
        elif has_core:
            reason = "rest day must have NO finisher"
        add("REST_EMPTY", ok, reason)

    elif exp["kind"] == "cardio":
        # ZERO strength movements anywhere in a cardio block.
        strength_hits = [n for n in _all_exercise_names(blocks) if _is_strength_name(n)]
        add("CARDIO_NO_STRENGTH", not strength_hits,
            f"strength movement(s) in cardio block: {strength_hits}" if strength_hits else "")

        if d.weekday() == 2:  # Wed intervals — bike/rower, no treadmill (treadmill caught above)
            add("WED_BIKE_OR_ROWER", _has_bike_equipment(blocks) or any("rower" in e.lower() for e in _equipment(blocks)),
                "Wed intervals must use bike/rower equipment")
        elif d.weekday() == 5:  # Sat — must be the BIKE ride
            add("SAT_HAS_BIKE", _has_bike_equipment(blocks),
                "Sat cardio_z2 must contain bike equipment (bike ride)")
        elif d.weekday() == 6:  # Sun — must be RUN-WALK, NO bike
            no_bike = not _has_bike_equipment(blocks)
            runwalk = _has_runwalk_structure(blocks)
            ok = no_bike and runwalk
            reason = ""
            if not no_bike:
                reason = "Sun cardio_z2 must NOT contain bike equipment (it's run-walk)"
            elif not runwalk:
                reason = "Sun cardio_z2 lacks run-walk structure"
            add("SUN_RUNWALK_NO_BIKE", ok, reason)

    return out


# ---------------------------------------------------------------------------
# Row loading
# ---------------------------------------------------------------------------

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


def _load_rows_live(start: date, end: date) -> dict[date, dict]:
    """READ-ONLY SELECT of health.plan over [start, end]. Returns {plan_date: row}."""
    import psycopg2
    _load_dotenv()
    url = os.environ.get("DATABASE_URL")
    if url:
        conn = psycopg2.connect(url, connect_timeout=10)
        close = conn.close
    else:
        from knowledge.db import get_connection
        cm = get_connection()
        conn = cm.__enter__()
        close = lambda: cm.__exit__(None, None, None)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT plan_date, session_type, blocks FROM health.plan "
            "WHERE plan_date BETWEEN %s AND %s ORDER BY plan_date",
            (start, end),
        )
        rows = {}
        for plan_date, session_type, blocks in cur.fetchall():
            rows[plan_date] = {"session_type": session_type, "blocks": _coerce_blocks(blocks)}
        return rows
    finally:
        close()


def _load_rows_selftest(start: date, end: date, fault: bool) -> dict[date, dict]:
    """Build synthetic rows from the reseed builders (no DB). Optionally inject
    faults to exercise the FAIL path."""
    import scripts.reseed_health_plan_v2 as reseed
    rows: dict[date, dict] = {}
    d = start
    i = 0
    while d <= end:
        r = reseed.build_row(d, phase=2, week_num=1 + (i // 7))
        rows[d] = {"session_type": r["session_type"], "blocks": r["blocks"]}
        d += timedelta(days=1)
        i += 1

    if fault:
        wd_index = {}  # first occurrence of each weekday in the window
        for dd in sorted(rows):
            wd_index.setdefault(dd.weekday(), dd)
        # a) treadmill on the first Wed (equipment off-list/forbidden)
        if 2 in wd_index:
            rows[wd_index[2]]["blocks"].setdefault("equipment", []).append("Treadmill")
        # b) a strength movement injected into the first Sat (cardio) block
        if 5 in wd_index:
            rows[wd_index[5]]["blocks"]["exercises"] = [
                {"name": "Goblet squat", "format": "reps", "target_reps": 12}
            ]
        # c) bike equipment on the first Sun (should be run-walk, no bike)
        if 6 in wd_index:
            rows[wd_index[6]]["blocks"].setdefault("equipment", []).append("road bike + indoor trainer")
        # d) drop a day to create a gap (date integrity)
        gap_day = start + timedelta(days=10)
        rows.pop(gap_day, None)
        # e) drift a Tue into a strength session (weekday-mapping)
        if 1 in wd_index:
            rows[wd_index[1]]["session_type"] = "strength_a"
    return rows


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(rows: dict[date, dict], start: date, end: date) -> int:
    window = [start + timedelta(days=i) for i in range(WINDOW_DAYS)]

    # Collect results: list of dicts {date, category, ok, reason}
    results: list[dict] = []
    table_rows: list[tuple] = []

    for d in window:
        row = rows.get(d)
        if row is None:
            results.append({"date": d, "category": "DATE_INTEGRITY", "ok": False,
                            "reason": "no plan row for this date (gap)"})
            table_rows.append((d, d.strftime("%a"), "—", "—", "—", "MISSING ROW"))
            continue

        results.append({"date": d, "category": "DATE_INTEGRITY", "ok": True, "reason": ""})
        blocks = _coerce_blocks(row["blocks"])
        session_type = row["session_type"]

        for cat, ok, reason in evaluate_day(d, session_type, blocks):
            results.append({"date": d, "category": cat, "ok": ok, "reason": reason})

        main = _main_exercise_names(blocks)
        has_core = _finisher(blocks) is not None
        ex_col = ", ".join(main) if main else f"({blocks.get('type')})"
        if has_core:
            ex_col += "  +core: " + ", ".join(_finisher_names(blocks))
        table_rows.append((d, d.strftime("%a"), session_type, blocks.get("type"),
                           "yes" if has_core else "no", ex_col))

    # ── Per-day table ──
    print(f"Health plan validation — {WINDOW_DAYS}-day window (America/Chicago)")
    print(f"Window: {start.isoformat()} .. {end.isoformat()}\n")
    print(f"{'DATE':<11}{'WD':<5}{'SESSION_TYPE':<18}{'BLOCK':<11}{'CORE':<6}EXERCISES")
    print("-" * 100)
    for d, wd, st, bt, core, ex in table_rows:
        print(f"{d.isoformat():<11}{wd:<5}{str(st):<18}{str(bt):<11}{core:<6}{ex}")
    print("-" * 100)

    # ── Per-rule PASS/FAIL ──
    categories = []
    for r in results:
        if r["category"] not in categories:
            categories.append(r["category"])
    print("\nRule results (per rule, aggregated across the window):")
    for cat in categories:
        subset = [r for r in results if r["category"] == cat]
        fails = [r for r in subset if not r["ok"]]
        status = "PASS" if not fails else "FAIL"
        print(f"  [{status}] {cat:<26} {len(subset) - len(fails)}/{len(subset)}"
              + (f"   ({len(fails)} fail)" if fails else ""))

    # ── Summary + failure list ──
    total = len(results)
    fails = [r for r in results if not r["ok"]]
    print("\n" + "=" * 100)
    if fails:
        print("FAILURES:")
        for r in sorted(fails, key=lambda x: (x["date"], x["category"])):
            print(f"  {r['date'].isoformat()} {r['date'].strftime('%a')}  "
                  f"{r['category']:<26} {r['reason']}")
    print(f"\nSummary: rows_checked={len([d for d in window if d in rows])}/{WINDOW_DAYS}  "
          f"assertions_pass={total - len(fails)}  assertions_fail={len(fails)}")
    print(f"RESULT: {'PASS' if not fails else 'FAIL'}")
    return 0 if not fails else 1


def main():
    ap = argparse.ArgumentParser(description="Validate health.plan against the locked program (READ-ONLY).")
    ap.add_argument("--self-test", action="store_true", help="No DB. Build 28 days from the reseed builders.")
    ap.add_argument("--fault", action="store_true", help="With --self-test: inject faults to prove the FAIL path.")
    args = ap.parse_args()

    today = datetime.now(CT).date()
    start, end = today, today + timedelta(days=WINDOW_DAYS - 1)

    if args.self_test:
        rows = _load_rows_selftest(start, end, fault=args.fault)
    else:
        rows = _load_rows_live(start, end)

    sys.exit(run(rows, start, end))


if __name__ == "__main__":
    main()
