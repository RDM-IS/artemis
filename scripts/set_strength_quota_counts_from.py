"""SESSION-OPEN deploy step — set acos.system_state['strength_quota_counts_from'].

The strength quota counts only weeks from the deploy week on (Ryan, 2026-11-09),
and reads that Sunday from system_state FAIL-CLOSED: until this runs, every
quota reader answers "couldn't read" — no nag, no repeat proposal, and the
Status page shows the strength section as unreadable. That is deliberate.

    python3.11 scripts/set_strength_quota_counts_from.py            # dry run: prints what it would set
    python3.11 scripts/set_strength_quota_counts_from.py --commit   # writes, re-reads, asserts
    ... --sunday 2026-11-15                                          # an explicit Sunday instead

Default: the Sunday of TODAY's Sun..Sat week in the ACTIVE timezone. Refuses to
move an existing value unless --force — moving it re-opens or closes weeks that
have already been judged.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main(argv=None) -> int:
    from artemis.quiet_hours import local_today
    from knowledge import strength_quota as sq
    from knowledge.db import execute_one, execute_write

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--sunday", type=date.fromisoformat, help="explicit Sunday (default: this week's)")
    ap.add_argument("--commit", action="store_true", help="write it (default: dry run)")
    ap.add_argument("--force", action="store_true", help="replace an existing different value")
    a = ap.parse_args(argv)

    sunday = a.sunday or sq.week_of(local_today())[0]
    sq.parse_counts_from(sunday.isoformat())          # raises unless a Sunday
    row = execute_one("SELECT value FROM acos.system_state WHERE key = %s", (sq.COUNTS_FROM_KEY,))
    current = row["value"] if row else None
    print(f"{sq.COUNTS_FROM_KEY}: current={current!r} → {sunday.isoformat()}")
    if current == sunday.isoformat():
        print("already set — nothing to do")
        return 0
    if current and not a.force:
        print("REFUSED: a different value is already set; pass --force to move it")
        return 1
    if not a.commit:
        print("dry run — re-run with --commit to write")
        return 0
    execute_write(
        "INSERT INTO acos.system_state (key, value, updated_at) VALUES (%s, %s, now()) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
        (sq.COUNTS_FROM_KEY, sunday.isoformat()))
    back = execute_one("SELECT value FROM acos.system_state WHERE key = %s", (sq.COUNTS_FROM_KEY,))
    if not back or sq.parse_counts_from(back["value"]) != sunday:
        print(f"FAILED: re-read {back!r}")
        return 1
    print(f"set and verified: {sq.COUNTS_FROM_KEY} = {back['value']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
