#!/usr/bin/env python3
"""Offline demo: feed Sunday's run-walk paste through the capture parser and
print the proposed rows. No DB, no API key, no writes.

The LLM *extract* step is stubbed (it only pulls raw values); the deterministic
mi/ft->m and MM:SS->sec conversion and the proposal formatting are the real code
under test. This is the one-line verification from the plan.

Run:
    python3 scripts/cardio_parse_demo.py
"""

import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("RDS_HOST", "demo")  # never connected; parse uses plan=None

from artemis import health  # noqa: E402

SUNDAY_PASTE = (
    "run-walk done. time 51:36, distance 3.28 miles, 121 bpm avg HR. "
    "Run #1: .16 mile | 1:48 | RPE 8 | HR 147. "
    "Run #2: .15 mile | 1:45 | RPE 9 | HR 151. "
    "Run #3: .17 mile | 1:52 | RPE 8 | HR 149. "
    "Run #4: .14 mile | 1:40 | RPE 9 | HR 153. "
    "Run #5: .16 mile | 1:50 | RPE 9 | HR 150. "
    "overall walk RPE 4 run RPE 9. runs uphill, walks downhill, felt good."
)

# What the LLM extract step returns — RAW values only (Python converts). Stubbed
# so the demo needs no API key.
_SEGMENTS = [
    (0.16, "1:48", 8.0, 147),
    (0.15, "1:45", 9.0, 151),
    (0.17, "1:52", 8.0, 149),
    (0.14, "1:40", 9.0, 153),
    (0.16, "1:50", 9.0, 150),
]
_LLM_JSON = {
    "exercises": [
        {
            "exercise": f"Run {i}", "log_type": "cardio_block", "set_num": None,
            "round_num": i, "reps_done": None, "weight_lbs": None,
            "duration": dur, "distance": dist, "distance_unit": "mi",
            "rpe_actual": rpe, "hr_avg": hr, "hr_peak": None,
            "notes": None, "user_suggestion": None, "is_skipped": False,
        }
        for i, (dist, dur, rpe, hr) in enumerate(_SEGMENTS, start=1)
    ],
    "session_summary": {
        "duration": "51:36", "distance": 3.28, "distance_unit": "mi",
        "hr_avg": 121, "rpe_actual": 9.0,
        "notes": "walk RPE 4, run RPE 9; runs uphill, walks downhill, felt good",
        "user_suggestion": None,
    },
}


def main() -> None:
    with patch.object(health, "_call_claude_json", return_value=_LLM_JSON):
        reports = health.parse_workout_debrief(SUNDAY_PASTE, plan=None)

    print("=== Proposed rows (NOTHING written) ===")
    for r in reports:
        print(
            f"[{r.log_type}] {r.exercise}: round_num={r.round_num} "
            f"distance_m={r.distance_m} duration_sec={r.duration_sec} "
            f"rpe={r.rpe_actual} hr_avg={r.hr_avg}"
        )

    cardio = [r for r in reports if r.log_type == "cardio_block"]
    summary = [r for r in reports if r.log_type == "session_summary"]
    print(f"\n{len(cardio)} cardio_block + {len(summary)} session_summary row(s).")
    assert len(cardio) == 5 and len(summary) == 1, "expected 5 cardio + 1 summary"

    print("\n=== Proposal that would post to #artemis-ryan ===")
    print(health.format_capture_proposal(reports, plan_id=3, plan_note=""))


if __name__ == "__main__":
    main()
