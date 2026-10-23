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
    21/22/23/24, or the same rates one time step longer each round (a minute
    until 2026-10-23, now 15 seconds)."""

    def setUp(self):
        self.last = session((240, 20), (240, 21), (240, 22), (240, 23))
        self.p = rc.prescription([self.last], 4)

    def test_rate_option(self):
        self.assertEqual(rates(self.p["options"]["rate"]), [21, 22, 23, 24])
        self.assertEqual(lengths(self.p["options"]["rate"]), [240] * 4)

    def test_time_option(self):
        self.assertEqual(rates(self.p["options"]["time"]), [20, 21, 22, 23])
        self.assertEqual(lengths(self.p["options"]["time"]), [240 + rc.TIME_STEP_SEC] * 4)

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



class TestLadder(unittest.TestCase):
    """ROW-LADDER: the same climb with no floor work and its own defaults."""

    def test_no_floor_work_but_one_entry_per_round(self):
        p = rc.prescription([], None, "ladder")
        self.assertEqual(p["rounds"], rc.KINDS["ladder"]["default_rounds"])
        self.assertEqual(p["stations"], [[]] * p["rounds"])
        self.assertEqual(p["session_type"], "row_ladder")
        self.assertEqual(p["row_exercise"], "Row ladder")

    def test_progresses_the_same_way(self):
        last = session((240, 15), (240, 16))
        p = rc.prescription([last], 2, "ladder")
        self.assertEqual(rates(p["options"]["rate"]), [16, 17])
        self.assertEqual(lengths(p["options"]["time"]), [240 + rc.TIME_STEP_SEC] * 2)

    def test_the_circuit_keeps_its_stations_and_names(self):
        p = rc.prescription([], None, "circuit")
        self.assertEqual(p["row_exercise"], "Row")
        self.assertTrue(all(len(st) == 3 for st in p["stations"]))

    def test_the_two_kinds_never_share_an_exercise_name(self):
        names = [k["exercise"] for k in rc.KINDS.values()]
        self.assertEqual(len(names), len(set(names)))

    def test_an_unknown_kind_raises(self):
        with self.assertRaises(KeyError):
            rc.prescription([], 4, "circuitt")



class TestDidNotFinish(unittest.TestCase):
    """ROW-DNF (Ryan, 2026-10-20): "if I can't do all sets at their target, we
    shouldn't advance." Synthetic: 15-18 spm, 200 s rounds, year 2031."""

    def _rows(self, *rounds):
        """(set_num, target_spm, target_sec, held_spm, dnf)"""
        return [{"day": date(2031, 3, 10), "set_num": n, "target_spm": t, "target_sec": ts,
                 "stroke_rate": h, "duration_sec": ts, "dnf": dnf, "logged_at": T0}
                for n, t, ts, h, dnf in rounds]

    def test_a_dnf_round_blocks_both_levers_and_repeats_the_targets(self):
        h = rc.group_history(self._rows((1, 15, 200, 15, False), (2, 16, 200, 16, True)))
        p = rc.prescription(h, 2)
        self.assertEqual(p["default"], "repeat")
        self.assertFalse(p["options"]["rate"]["ok"])
        self.assertFalse(p["options"]["time"]["ok"])
        self.assertIn("round 2", p["options"]["rate"]["reason"])
        self.assertEqual(rates(p["options"]["repeat"]), [15, 16])
        self.assertEqual(lengths(p["options"]["repeat"]), [200, 200])
        self.assertEqual([r["round"] for r in p["unfinished"]], [2])

    def test_the_base_is_the_target_not_the_seconds_actually_rowed(self):
        rows = self._rows((1, 16, 200, 16, True))
        rows[0]["duration_sec"] = 70           # held 70 s of a 200 s round
        p = rc.prescription(rc.group_history(rows), 1)
        self.assertEqual(lengths(p["options"]["repeat"]), [200])

    def test_holding_below_target_is_unfinished_even_without_the_flag(self):
        h = rc.group_history(self._rows((1, 16, 200, 15, False)))
        self.assertFalse(h[0][0].finished)
        self.assertEqual(rc.prescription(h, 1)["default"], "repeat")

    def test_all_finished_advances_from_the_targets(self):
        h = rc.group_history(self._rows((1, 15, 200, 15, False), (2, 16, 200, 17, False)))
        p = rc.prescription(h, 2)
        self.assertEqual(p["unfinished"], [])
        self.assertEqual(rates(p["options"]["rate"]), [16, 17])

    def test_a_dropped_dnf_round_does_not_block(self):
        h = rc.group_history(self._rows((1, 15, 200, 15, False), (2, 16, 200, 16, True)))
        p = rc.prescription(h, 1)
        self.assertTrue(p["options"]["rate"]["ok"])

    def test_the_first_ever_session_defaults_to_the_ladder_itself(self):
        p = rc.prescription([], 6, "ladder")
        self.assertEqual(p["default"], "repeat")
        self.assertEqual(rates(p["options"]["repeat"]), [20, 21, 22, 23, 24, 25])

    def test_rows_from_before_056_still_read_as_held_and_finished(self):
        rows = [{"day": date(2031, 3, 10), "set_num": 1, "duration_sec": 200,
                 "stroke_rate": 15, "logged_at": T0}]
        h = rc.group_history(rows)
        self.assertTrue(h[0][0].finished)
        self.assertEqual((h[0][0].spm, h[0][0].work_sec), (15, 200))



class TestZoneLadders(unittest.TestCase):
    """QUICK-START: ride and run climb a heart-rate target in 5 bpm steps."""

    def test_first_ride_is_the_ladder_from_110(self):
        p = rc.prescription([], None, "ride")
        self.assertEqual(p["unit"], "bpm")
        self.assertEqual(p["default"], "repeat")
        self.assertEqual(rates(p["options"]["repeat"]), [110, 115, 120, 125, 130, 135])
        self.assertEqual(p["modality"], "bike")

    def test_the_rate_lever_is_plus_five_bpm(self):
        last = session((200, 100), (200, 105))
        p = rc.prescription([last], 2, "run")
        self.assertEqual(rates(p["options"]["rate"]), [105, 110])
        self.assertIn("+5 bpm", p["options"]["rate"]["label"])
        self.assertEqual(p["modality"], "treadmill")

    def test_the_bpm_cap_disables_the_lever(self):
        last = session((200, 160))
        p = rc.prescription([last], 1, "ride")
        self.assertFalse(p["options"]["rate"]["ok"])
        self.assertIn("bpm", p["options"]["rate"]["reason"])

    def test_dnf_holds_a_zone_ladder_too(self):
        rows = [{"day": date(2031, 3, 10), "set_num": 1, "target_spm": 100, "target_sec": 200,
                 "stroke_rate": 100, "duration_sec": 90, "dnf": True, "logged_at": T0}]
        p = rc.prescription(rc.group_history(rows), 1, "run")
        self.assertEqual(p["default"], "repeat")
        self.assertFalse(p["options"]["time"]["ok"])

    def test_no_two_kinds_share_an_exercise(self):
        names = [k["exercise"] for k in rc.KINDS.values()]
        self.assertEqual(len(names), len(set(names)))


if __name__ == "__main__":
    unittest.main()


class TestTimeStep(unittest.TestCase):
    """2026-10-23: the time lever is +0:15, not +1:00 (+25% on a 4:00 round)."""

    def test_the_step_is_fifteen_seconds(self):
        self.assertEqual(rc.TIME_STEP_SEC, 15)

    def test_the_label_is_built_from_the_step(self):
        p = rc.prescription([session((240, 20), (240, 21))], 2)
        self.assertTrue(p["options"]["time"]["label"].startswith("+0:15 each round"))
        self.assertEqual(p["time_label"], "+0:15 each round")
        self.assertEqual(p["time_step_sec"], 15)

    def test_no_copy_still_says_a_minute(self):
        prev = session((240, 20), (240, 21))
        last = session((240, 21), (240, 22))
        why = rc.prescription([last, prev], 2)["why"]
        self.assertNotIn("minute", why)
        self.assertIn("+0:15", why)


class TestMinRound(unittest.TestCase):
    """2026-10-23: a test run's 1-2 s rows became the next circuit's base."""

    def _row(self, n, **kw):
        base = {"day": date(2031, 3, 10), "set_num": n, "stroke_rate": 15, "logged_at": T0}
        return {**base, **kw}

    def test_rows_shorter_than_a_minute_are_not_rounds(self):
        rows = [self._row(1, duration_sec=2), self._row(2, duration_sec=1)]
        self.assertEqual(rc.group_history(rows), [])

    def test_a_short_target_is_not_a_round_either(self):
        rows = [self._row(1, duration_sec=200, target_spm=15, target_sec=30)]
        self.assertEqual(rc.group_history(rows), [])

    def test_a_dnf_cut_short_still_counts_by_its_target(self):
        # Stopped at 20 s of a 4:00 round: the round is real, and unfinished.
        rows = [self._row(1, duration_sec=20, target_spm=15, target_sec=240, dnf=True)]
        h = rc.group_history(rows)
        self.assertEqual([(r.work_sec, r.finished) for r in h[0]], [(240, False)])

    def test_exactly_a_minute_counts(self):
        rows = [self._row(1, duration_sec=rc.MIN_ROUND_SEC)]
        self.assertEqual(len(rc.group_history(rows)), 1)


class TestRoundsDefaultToLastTime(unittest.TestCase):
    def test_no_count_asked_uses_last_times_count(self):
        last = session((240, 20), (240, 21), (240, 22), (240, 23), (240, 24))
        p = rc.prescription([last], None, "circuit")
        self.assertEqual(p["rounds"], 5)
        self.assertEqual(p["extrapolated"], [])
        self.assertIsNone(p["extrapolated_note"])

    def test_first_session_uses_the_kinds_default(self):
        self.assertEqual(rc.prescription([], None, "ladder")["rounds"],
                         rc.KINDS["ladder"]["default_rounds"])

    def test_more_rounds_than_last_time_are_flagged(self):
        last = session((240, 20), (240, 21), (240, 22), (240, 23))
        p = rc.prescription([last], 6)
        self.assertEqual(p["extrapolated"], [5, 6])
        self.assertIn("Rounds 5–6 are new", p["extrapolated_note"])
        self.assertIn("last time had 4", p["extrapolated_note"])

    def test_one_extra_round_reads_singular(self):
        last = session((240, 20), (240, 21))
        p = rc.prescription([last], 3)
        self.assertEqual(p["extrapolated"], [3])
        self.assertTrue(p["extrapolated_note"].startswith("Round 3 is new"))

    def test_no_history_flags_nothing(self):
        self.assertEqual(rc.prescription([], 6)["extrapolated"], [])
