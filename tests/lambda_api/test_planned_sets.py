"""The planned set count, and the three ways a finished session read `partial`.

`_planned_set_count` multiplied rounds × len(exercises) flat while its
docstring claimed it mirrored gym-display's totalSetsFor. It didn't, and the
three consequences all look the same on the Status page:

  1. a check-in adjustment caps ONE exercise below `rounds` (health_checkin
     writes ex["sets"] and persists blocks) — 11 done of a planned 12;
  2. a finisher's rounds are planned, and until now nobody had checked whether
     they are loggable — they are, and were, on 2026-06-08;
  3. an exercise skipped on the Done screen — 11 logged, 1 skipped, of 12.

All three are fixed. (3) follows Ryan's rule of 2026-09-20: a skipped set
completes its slot in a session with at least one real logged set, and a
session of nothing but skips stays missed.

Every one of them was masked by the session_summary rule, which returns `done`
before the arithmetic runs. Weeks 5–6 (from 2026-10-11) put a 6-round finisher
on strength_c, which is why this had a date on it.

Run:
    python3 tests/lambda_api/test_planned_sets.py
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

    def test_a_skipped_finisher_closes_the_day_once_the_main_work_is_logged(self):
        """10/16: 18 real sets then six skipped carry rounds. He trained, and
        he decided about the carries — that is `done`, not a standing 18 of 24."""
        self.assertEqual(status(circuit(rounds=3, n=6, finisher=self.FIN),
                                sets(24, skipped=6)), "done")

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

    Ryan's rule (2026-09-20): a skipped set COMPLETES its slot — but only in a
    session he actually trained, meaning at least one real logged set. A
    session of nothing but skips stays missed.

    The second half is not hypothetical. My first attempt made every skip
    complete its slot, and tests/lambda_api/test_plan_range.py caught the cost: a
    `walk` is a single cardio_block, so a skipped walk read `done`. That case
    is pinned below and is the reason the rule has two halves.
    """

    def test_eleven_logged_plus_one_skipped_is_done(self):
        self.assertEqual(status(circuit(rounds=2, n=6), sets(12, skipped=1)), "done")

    def test_eleven_logged_with_nothing_for_the_twelfth_is_partial(self):
        """Saying nothing about a slot is not the same as skipping it."""
        self.assertEqual(status(circuit(rounds=2, n=6), sets(11)), "partial")

    def test_one_real_set_is_enough_to_make_the_skips_count(self):
        """The threshold is 'he trained', not 'he trained a lot'. 1 logged +
        11 skipped closes the day; whether that is a good session is EVAL-1's
        business, and the skipped rows are all still there for it to read."""
        self.assertEqual(status(circuit(rounds=2, n=6), sets(12, skipped=11)), "done")

    def test_a_session_of_nothing_but_skips_is_missed(self):
        """Every slot skipped, nothing logged: the session did not happen."""
        self.assertEqual(status(circuit(rounds=2, n=6), sets(12, skipped=12),
                                day=date(2026, 10, 15), today=TODAY), "missed")

    def test_a_skipped_walk_is_not_a_walk_that_happened(self):
        """The case that caught the first attempt: one cardio_block, skipped.
        Without the 'at least one real set' half it would read `done` — a walk
        marked complete by the act of skipping it."""
        self.assertEqual(status({"type": "steady"},
                                sets(1, skipped=1, log_type="cardio_block"),
                                day=date(2026, 10, 15), today=TODAY), "missed")

    def test_a_walk_actually_logged_is_done(self):
        self.assertEqual(status({"type": "steady"},
                                sets(1, log_type="cardio_block"),
                                day=date(2026, 10, 15), today=TODAY), "done")

    def test_an_all_skipped_day_that_was_declared_skipped_reads_skipped(self):
        """MAKEUP-1 still wins over `missed` when Ryan said so out loud."""
        self.assertEqual(status(circuit(rounds=2, n=6), sets(12, skipped=12),
                                day=date(2026, 10, 15), today=TODAY,
                                is_skipped=True), "skipped")

    def test_an_all_skipped_session_today_still_reads_today(self):
        """Mid-session, having skipped the first two: the day is not over."""
        self.assertEqual(status(circuit(rounds=2, n=6), sets(2, skipped=2)), "today")

    def test_a_session_summary_still_masks_all_of_it(self):
        """Ryan's debrief returns `done` before any arithmetic runs — which is
        why none of these failure modes were ever visible."""
        logs = sets(11) + [{"log_type": "session_summary", "logged_via": "ipad",
                            "is_skipped": False, "notes": "felt good"}]
        self.assertEqual(status(circuit(rounds=2, n=6), logs), "done")

    def test_the_skipped_rows_survive_for_the_weekly_report(self):
        """Nothing here deletes or rewrites them; EVAL-1 reads session_log."""
        rows = sets(12, skipped=3)
        status(circuit(rounds=2, n=6), rows)
        self.assertEqual(sum(1 for r in rows if r["is_skipped"]), 3)


class TestProgressAgreesWithTheStatus(unittest.TestCase):
    """The ring and the day status must not contradict each other."""

    def day(self, blocks):
        from api.app.routers.health import PlanDay
        return PlanDay(plan_date=TODAY, plan_id=1, session_type="strength_c",
                       phase=2, week_num=5, blocks=blocks, status="today")

    def progress(self, blocks, logs):
        from api.app.routers.health import progress_for
        return progress_for(self.day(blocks), logs)

    def test_a_skipped_slot_counts_once_the_session_is_real(self):
        p = self.progress(circuit(rounds=2, n=6), sets(12, skipped=1))
        self.assertEqual((p.done, p.planned), (12, 12))

    def test_nothing_but_skips_counts_nothing(self):
        p = self.progress(circuit(rounds=2, n=6), sets(12, skipped=12))
        self.assertEqual((p.done, p.planned), (0, 12))

    def test_an_adjusted_session_targets_the_adjusted_count(self):
        blocks = circuit(rounds=3, exercises=[ex("Row"), ex("Press"),
                                              ex("Squat", sets=2), ex("Curl")])
        self.assertEqual(self.progress(blocks, sets(11)).planned, 11)


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
        # `walk` was removed from this list by 11a818b (WALK-RETIRE, 2026-09-22):
        # walking is activity, never a planned session, so a walk block plans no
        # sets. The test asserted the old behaviour and had been failing since.
        for t in ("intervals", "steady", "mobility"):
            self.assertEqual(_planned_set_count({"type": t}), 1, t)

    def test_a_walk_plans_no_sets(self):
        """WALK-RETIRE: a walk is activity, so it contributes no planned set."""
        self.assertEqual(_planned_set_count({"type": "walk"}), 0)

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
