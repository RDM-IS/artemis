"""ROW-CIRCUIT — the prescription math (knowledge/row_circuit.py). Synthetic only.

PUBLIC-FIXTURES: stroke rates and lengths here are the ladder's own rule values
(START_RATE, START_WORK_SEC) or obviously invented; no real session's numbers.
"""
import os as _os; _os.environ["ARTEMIS_TEST_NO_DB"] = "1"  # TEST-DB-GUARD: never a real DB
import sys
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from knowledge import row_circuit as rc  # noqa: E402
from knowledge.row_circuit import RowRound as R  # noqa: E402


def rates(opt):
    return [r["spm"] for r in opt["rounds"]]


def lengths(opt):
    return [r["work_sec"] for r in opt["rounds"]]


def session(*pairs):
    """(work_sec, spm) per round."""
    return [R(i + 1, w, s) for i, (w, s) in enumerate(pairs)]


class TestRyansExample(unittest.TestCase):
    """Ryan, 2026-10-19 17:26: last time 4 rounds at 20/21/22/23 -> this time
    21/22/23/24, or the same rates one minute longer each round."""

    def setUp(self):
        self.last = session((240, 20), (240, 21), (240, 22), (240, 23))
        self.p = rc.prescription([self.last], 4)

    def test_rate_option(self):
        self.assertEqual(rates(self.p["options"]["rate"]), [21, 22, 23, 24])
        self.assertEqual(lengths(self.p["options"]["rate"]), [240] * 4)

    def test_time_option(self):
        self.assertEqual(rates(self.p["options"]["time"]), [20, 21, 22, 23])
        self.assertEqual(lengths(self.p["options"]["time"]), [300] * 4)

    def test_repeat_is_always_offered(self):
        self.assertEqual(rates(self.p["options"]["repeat"]), [20, 21, 22, 23])
        self.assertEqual(lengths(self.p["options"]["repeat"]), [240] * 4)

    def test_one_session_defaults_to_rate(self):
        self.assertEqual(self.p["default"], "rate")


class TestDefaultAlternates(unittest.TestCase):
    def test_after_a_rate_step_the_default_is_time(self):
        prev = session((240, 20), (240, 21))
        last = session((240, 21), (240, 22))
        self.assertEqual(rc.prescription([last, prev], 2)["default"], "time")

    def test_after_a_time_step_the_default_is_rate(self):
        prev = session((240, 20), (240, 21))
        last = session((300, 20), (300, 21))
        self.assertEqual(rc.prescription([last, prev], 2)["default"], "rate")

    def test_both_moved_is_not_a_single_lever(self):
        prev = session((240, 20))
        last = session((300, 21))
        self.assertIsNone(rc.moved_lever(last, prev))
        self.assertEqual(rc.prescription([last, prev], 1)["default"], "rate")

    def test_a_capped_default_falls_to_the_other_lever(self):
        last = session((240, rc.MAX_RATE))
        p = rc.prescription([last], 1)
        self.assertFalse(p["options"]["rate"]["ok"])
        self.assertIn(str(rc.MAX_RATE), p["options"]["rate"]["reason"])
        self.assertEqual(p["default"], "time")

    def test_both_capped_defaults_to_repeat(self):
        last = session((rc.MAX_WORK_SEC, rc.MAX_RATE))
        self.assertEqual(rc.prescription([last], 1)["default"], "repeat")


class TestBaseIsWhatHeDid(unittest.TestCase):
    def test_a_missed_round_is_repeated_not_stacked(self):
        # Target was 23 in round 2, he held 22 and the card logged 22.
        last = session((240, 20), (240, 22))
        self.assertEqual(rates(rc.prescription([last], 2)["options"]["rate"]), [21, 23])


class TestRoundCount(unittest.TestCase):
    def test_no_history_starts_the_ladder(self):
        p = rc.prescription([], 4)
        self.assertEqual(rates({"rounds": p["base"]}), [20, 21, 22, 23])
        self.assertEqual(lengths({"rounds": p["base"]}), [rc.START_WORK_SEC] * 4)
        self.assertFalse(p["has_history"])

    def test_fewer_rounds_keep_the_first(self):
        last = session((240, 20), (240, 21), (240, 22), (240, 23))
        self.assertEqual(rates(rc.prescription([last], 2)["options"]["repeat"]), [20, 21])

    def test_more_rounds_continue_the_climb(self):
        last = session((240, 20), (300, 21))
        p = rc.prescription([last], 4)
        self.assertEqual(rates(p["options"]["repeat"]), [20, 21, 22, 23])
        self.assertEqual(lengths(p["options"]["repeat"]), [240, 300, 300, 300])

    def test_rounds_are_clamped(self):
        self.assertEqual(rc.prescription([], 0)["rounds"], rc.MIN_ROUNDS)
        self.assertEqual(rc.prescription([], 99)["rounds"], rc.MAX_ROUNDS)
        self.assertEqual(rc.prescription([], None)["rounds"], rc.DEFAULT_ROUNDS)

    def test_stations_follow_the_round_count(self):
        p = rc.prescription([], 5)
        self.assertEqual(len(p["stations"]), 5)
        for st in p["stations"]:
            self.assertEqual(st[0]["name"], "Push-up")
            self.assertEqual(len(st), 3)


class TestStations(unittest.TestCase):
    def test_no_burpees_in_anything_built(self):
        for n in range(1, rc.MAX_ROUNDS + 1):
            for st in rc.stations_for_round(n):
                for bad in rc.FORBIDDEN_MOVEMENTS:
                    self.assertNotIn(bad, st["name"].lower())
                    self.assertNotIn(bad, st["cue"].lower())

    def test_formats_are_known(self):
        for n in range(1, len(rc.CORE_ROTATION) + 1):
            for st in rc.stations_for_round(n):
                self.assertIn(st["format"], ("reps", "hold"))


T0 = datetime(2031, 3, 10, 10, 0, tzinfo=timezone.utc)
T1 = datetime(2031, 3, 10, 10, 30, tzinfo=timezone.utc)


class TestGroupHistory(unittest.TestCase):
    def test_sessions_newest_first_and_resaves_keep_the_latest(self):
        rows = [
            {"day": date(2031, 3, 8), "set_num": 1, "duration_sec": 200, "stroke_rate": 15, "logged_at": T0},
            {"day": date(2031, 3, 10), "set_num": 2, "duration_sec": 200, "stroke_rate": 17, "logged_at": T0},
            {"day": date(2031, 3, 10), "set_num": 1, "duration_sec": 200, "stroke_rate": 15, "logged_at": T0},
            {"day": date(2031, 3, 10), "set_num": 1, "duration_sec": 200, "stroke_rate": 16, "logged_at": T1},
        ]
        h = rc.group_history(rows)
        self.assertEqual([[r.spm for r in s] for s in h], [[16, 17], [15]])

    def test_rows_without_a_rate_or_duration_are_skipped(self):
        rows = [{"day": date(2031, 3, 10), "set_num": 1, "duration_sec": None,
                 "stroke_rate": 15, "logged_at": T0},
                {"day": date(2031, 3, 10), "set_num": 2, "duration_sec": 200,
                 "stroke_rate": None, "logged_at": T0}]
        self.assertEqual(rc.group_history(rows), [])

    def test_last_reps_reads_the_latest_day_only(self):
        rows = [
            {"day": date(2031, 3, 8), "set_num": 1, "exercise": "Push-up", "reps_done": 3, "logged_at": T0},
            {"day": date(2031, 3, 10), "set_num": 1, "exercise": "Push-up", "reps_done": 4, "logged_at": T0},
            {"day": date(2031, 3, 10), "set_num": 1, "exercise": "Push-up", "reps_done": 5, "logged_at": T1},
            {"day": date(2031, 3, 10), "set_num": 2, "exercise": "Dead bug", "reps_done": 6, "logged_at": T0},
        ]
        self.assertEqual(rc.last_reps(rows), {"1": {"Push-up": 5}, "2": {"Dead bug": 6}})


if __name__ == "__main__":
    unittest.main()
