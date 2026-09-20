"""The planned set count, and the three ways a finished session read `partial`.

`_planned_set_count` multiplied rounds × len(exercises) flat while its
docstring claimed it mirrored gym-display's totalSetsFor. It didn't, and the
three consequences all look the same on the Status page:

  1. a check-in adjustment caps ONE exercise below `rounds` (health_checkin
     writes ex["sets"] and persists blocks) — 11 done of a planned 12;
  2. a finisher's rounds are planned, and until now nobody had checked whether
     they are loggable — they are, and were, on 2026-06-08;
  3. an exercise skipped on the Done screen — 11 logged, 1 skipped, of 12.

(1) is fixed here. (2) is now arithmetically right and its remaining question
is for Ryan. (3) is deliberately NOT changed — see TestTheSkippedExerciseCase.

Every one of them was masked by the session_summary rule, which returns `done`
before the arithmetic runs. Weeks 5–6 (from 2026-10-11) put a 6-round finisher
on strength_c, which is why this had a date on it.

Run:
    python3 tests/api/test_planned_sets.py
"""

import os as _os; _os.environ["ARTEMIS_TEST_NO_DB"] = "1"  # TEST-DB-GUARD: never a real DB

import os
import sys
import unittest
from datetime import date
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))
os.environ.setdefault("RDS_HOST", "test-host")
os.environ.setdefault("RDS_DB", "test-db")

from api.app.routers.health import (  # noqa: E402
    _exercise_sets, _planned_set_count, derive_day_status,
)

TODAY = date(2026, 10, 16)      # week 5 strength_c — the plan with the finisher


def ex(name, **kw):
    return {"name": name, **kw}


def circuit(rounds=3, n=6, exercises=None, finisher=None):
    blocks = {"type": "circuit", "rounds": rounds,
              "exercises": exercises or [ex(f"E{i}") for i in range(n)]}
    if finisher:
        blocks["finisher"] = finisher
    return blocks


def sets(n, *, skipped=0, log_type="strength_set"):
    """n rows, the last `skipped` of them pressed Skip on the Done screen."""
    return [{"log_type": log_type, "logged_via": "ipad",
             "is_skipped": i >= n - skipped} for i in range(n)]


def status(blocks, logs, *, day=TODAY, today=TODAY, is_skipped=False):
    return derive_day_status({"plan_date": day, "blocks": blocks,
                              "session_type": "strength_c",
                              "is_skipped": is_skipped}, logs, today)


class TestTheAdjustmentCase(unittest.TestCase):
    """A check-in that lightens one exercise must not cost Ryan a `done`."""

    def test_a_capped_exercise_lowers_the_plan(self):
        blocks = circuit(rounds=3, exercises=[ex("Row"), ex("Press"),
                                              ex("Squat", sets=2), ex("Curl")])
        self.assertEqual(_planned_set_count(blocks), 11)   # 3+3+2+3, not 12

    def test_eleven_of_eleven_is_done(self):
        blocks = circuit(rounds=3, exercises=[ex("Row"), ex("Press"),
                                              ex("Squat", sets=2), ex("Curl")])
        self.assertEqual(status(blocks, sets(11)), "done")

    def test_ten_of_eleven_is_still_partial(self):
        """The fix corrects the target; it does not make everything `done`."""
        blocks = circuit(rounds=3, exercises=[ex("Row"), ex("Press"),
                                              ex("Squat", sets=2), ex("Curl")])
        self.assertEqual(status(blocks, sets(10)), "partial")

    def test_the_global_recovery_cap_lowers_every_exercise(self):
        """health_checkin's recovery rule writes sets=2 on all of them."""
        blocks = circuit(rounds=3, exercises=[ex(f"E{i}", sets=2) for i in range(6)])
        self.assertEqual(_planned_set_count(blocks), 12)
        self.assertEqual(status(blocks, sets(12)), "done")

    def test_an_unadjusted_session_is_unchanged(self):
        """9/16 was rounds 2 × 6 exercises = 12, and 9/18 was 2 × 7 = 14. Both
        logged exactly that and classified `done`; they still must."""
        self.assertEqual(_planned_set_count(circuit(rounds=2, n=6)), 12)
        self.assertEqual(_planned_set_count(circuit(rounds=2, n=7)), 14)
        self.assertEqual(status(circuit(rounds=2, n=6), sets(12)), "done")
        self.assertEqual(status(circuit(rounds=2, n=7), sets(14)), "done")


class TestTheFinisherCase(unittest.TestCase):
    """10/16 and 10/22: main 3×6 plus a finisher of 6 rounds × 1 exercise."""

    FIN = {"rounds": 6, "exercises": [ex("Farmer carry")]}

    def test_the_finisher_rounds_are_planned(self):
        self.assertEqual(_planned_set_count(circuit(rounds=3, n=6, finisher=self.FIN)), 24)

    def test_the_main_circuit_alone_is_partial(self):
        """18 of 24 — which is the honest answer, because the finisher IS
        loggable: LogPanel renders a card per finisher exercise and the Done
        screen chases the unlogged rounds. Eight such rows exist for
        2026-06-08, four exercises × two rounds, logged for real."""
        self.assertEqual(status(circuit(rounds=3, n=6, finisher=self.FIN), sets(18)),
                         "partial")

    def test_the_whole_session_including_the_finisher_is_done(self):
        self.assertEqual(status(circuit(rounds=3, n=6, finisher=self.FIN), sets(24)),
                         "done")

    def test_a_skipped_finisher_does_not_close_the_day(self):
        """Same open question as TestTheSkippedExerciseCase, and it will bite
        on 10/16: six skipped carry rounds leave the day at 18 of 24."""
        self.assertEqual(status(circuit(rounds=3, n=6, finisher=self.FIN),
                                sets(24, skipped=6)), "partial")

    def test_the_finisher_ignores_per_exercise_sets_as_gym_display_does(self):
        """gym-display's finisher branch is `occurrences × rounds` and never
        consults ex.sets. Mirroring means being wrong in the same direction."""
        fin = {"rounds": 6, "exercises": [ex("Farmer carry", sets=2)]}
        self.assertEqual(_planned_set_count(circuit(rounds=3, n=6, finisher=fin)), 24)

    def test_the_old_finisher_shape(self):
        """6/8's plan: 3×6 main plus 2 rounds × 4 exercises = 26."""
        fin = {"rounds": 2, "exercises": [ex(f"F{i}") for i in range(4)]}
        self.assertEqual(_planned_set_count(circuit(rounds=3, n=6, finisher=fin)), 26)


class TestTheSkippedExerciseCase(unittest.TestCase):
    """Skip on the Done screen writes a session_log row with is_skipped=true
    and no fake metrics.

    A skipped set does NOT complete its slot. That rule is deliberate and
    predates this change, and the reason it must stay is the degenerate case:
    a `walk` is one cardio_block, so counting a skip as complete would make a
    skipped walk read `done`. Whether ONE skipped exercise inside an otherwise
    finished twelve-slot circuit should read `done` is a separate question —
    open, and Ryan's, not mine. These tests pin today's answer so a change to
    it has to be deliberate.
    """

    def test_eleven_logged_plus_one_skipped_is_partial_today(self):
        self.assertEqual(status(circuit(rounds=2, n=6), sets(12, skipped=1)), "partial")

    def test_eleven_logged_with_nothing_for_the_twelfth_is_also_partial(self):
        """Today the two are indistinguishable in the status, which is the
        substance of the open question."""
        self.assertEqual(status(circuit(rounds=2, n=6), sets(11)), "partial")

    def test_a_skipped_walk_is_not_a_walk_that_happened(self):
        """The case that settles the rule for single-block sessions."""
        self.assertEqual(status({"type": "steady"},
                                sets(1, skipped=1, log_type="cardio_block")), "partial")

    def test_a_session_summary_still_masks_all_of_it(self):
        """Ryan's debrief returns `done` before any arithmetic runs — which is
        why none of these three failure modes were ever visible."""
        logs = sets(11) + [{"log_type": "session_summary", "logged_via": "ipad",
                            "is_skipped": False, "notes": "felt good"}]
        self.assertEqual(status(circuit(rounds=2, n=6), logs), "done")

    def test_a_wholly_skipped_session_is_not_silently_done_by_arithmetic(self):
        """Every slot skipped still reaches the planned count — but a day
        skipped out loud with nothing logged reads `skipped` (MAKEUP-1), not
        `done`. Checked the day after, so it isn't answered by "today"."""
        self.assertEqual(status(circuit(rounds=2, n=6), [], is_skipped=True,
                                today=date(2026, 10, 17)), "skipped")

    def test_the_skipped_rows_survive_for_the_weekly_report(self):
        """Nothing here deletes or rewrites them; EVAL-1 reads session_log."""
        rows = sets(12, skipped=3)
        status(circuit(rounds=2, n=6), rows)
        self.assertEqual(sum(1 for r in rows if r["is_skipped"]), 3)


class TestExerciseSetsMirrorsGymDisplay(unittest.TestCase):
    """src/lib/adjustment.ts: min(ex.sets, max(1, rounds)) when sets > 0."""

    def test_a_cap_below_rounds_wins(self):
        self.assertEqual(_exercise_sets(ex("A", sets=2), 3), 2)

    def test_a_cap_above_rounds_is_clamped(self):
        self.assertEqual(_exercise_sets(ex("A", sets=9), 3), 3)

    def test_no_cap_means_rounds(self):
        for value in ({}, {"sets": None}, {"sets": 0}, {"sets": "three"}):
            self.assertEqual(_exercise_sets(dict(name="A", **value), 3), 3, value)

    def test_rounds_below_one_still_plans_one_set(self):
        self.assertEqual(_exercise_sets(ex("A"), 0), 1)
        self.assertEqual(_exercise_sets(ex("A", sets=5), 0), 1)

    def test_a_malformed_exercise_does_not_crash_the_status_page(self):
        self.assertEqual(_exercise_sets("not a dict", 3), 3)
        self.assertEqual(_planned_set_count({"type": "circuit", "rounds": 2,
                                             "exercises": "nonsense"}), 0)


class TestUnchangedShapes(unittest.TestCase):
    def test_a_flow_plans_no_sets(self):
        self.assertEqual(_planned_set_count({"type": "recovery_flow", "rounds": 2,
                                             "flow": [{}] * 20}), 0)

    def test_the_single_block_types(self):
        for t in ("intervals", "steady", "walk", "mobility"):
            self.assertEqual(_planned_set_count({"type": t}), 1, t)

    def test_not_a_dict(self):
        self.assertEqual(_planned_set_count(None), 0)
        self.assertEqual(_planned_set_count("circuit"), 0)


class TestTheDocstringIsTrueNow(unittest.TestCase):
    def test_it_no_longer_claims_a_flat_multiplication_mirrors_totalSetsFor(self):
        src = (_REPO_ROOT / "api" / "app" / "routers" / "health.py").read_text()
        body = src[src.index("def _planned_set_count"):]
        body = body[:body.index("def _contains_pain_keyword")]
        self.assertIn("exerciseSets", body)
        self.assertNotIn("circuit rounds × occurrences", body)


if __name__ == "__main__":
    unittest.main()
