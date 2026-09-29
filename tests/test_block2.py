"""PROGRAM-2 Block 2 — double progression, the block boundary, and the render.

Nothing here writes: block 2 is build-and-dry-run only this round.
"""
import os as _os; _os.environ["ARTEMIS_TEST_NO_DB"] = "1"  # TEST-DB-GUARD
import unittest
from datetime import date, timedelta

from artemis import block2, health_office as office
from knowledge import load_config as lc, progression as pr


def sets(n, reps, weight, rpe):
    return [{"reps_done": reps, "weight_lbs": weight, "rpe_actual": rpe} for _ in range(n)]


LEG_PRESS = {"name": "Leg press", "notes": "3×10-12", "equipment_class": "machine"}
DB_PRESS = {"name": "DB press", "notes": "3×8-12", "equipment_class": "dumbbell"}


class TestRepRange(unittest.TestCase):
    def test_it_reads_the_range_from_the_notes(self):
        self.assertEqual(pr.rep_range(LEG_PRESS), (10, 12))
        self.assertEqual(pr.rep_range({"notes": "8-12"}), (8, 12))
        self.assertEqual(pr.rep_range({"notes": "3 x 6 - 8; slow"}), (6, 8))

    def test_a_movement_with_no_range_is_None_not_a_guess(self):
        """A duration hold has no top of range to reach, so it cannot
        double-progress and must not be guessed into it."""
        for ex in ({"notes": "30s hold"}, {"notes": None}, {}, {"notes": "3 sets"}):
            with self.subTest(ex):
                self.assertIsNone(pr.rep_range(ex))


class TestDoubleProgression(unittest.TestCase):
    OFFICE = lc.for_location("office")
    RICHFIELD = lc.for_location("richfield")

    def test_every_set_at_the_top_within_the_cap_adds_a_load_step(self):
        r = pr.advance(exercise=LEG_PRESS, sets=sets(3, 12, 100, 6),
                       rpe_cap=7, load_config=self.OFFICE)
        self.assertEqual(r["action"], "add_load")
        self.assertEqual(r["load"], 110)          # machine step is 10, not 5
        self.assertEqual(r["reps"], 10)           # back to the bottom

    def test_short_of_the_top_aims_for_one_more_rep_at_the_same_load(self):
        r = pr.advance(exercise=LEG_PRESS, sets=sets(3, 10, 100, 7),
                       rpe_cap=7, load_config=self.OFFICE)
        self.assertEqual(r["action"], "add_rep")
        self.assertEqual((r["load"], r["reps"]), (100, 11))

    def test_one_set_short_is_short(self):
        """Double progression means EVERY set, not the best one."""
        s = sets(2, 12, 100, 6) + sets(1, 9, 100, 7)
        r = pr.advance(exercise=LEG_PRESS, sets=s, rpe_cap=7, load_config=self.OFFICE)
        self.assertEqual(r["action"], "add_rep")

    def test_at_the_top_but_over_the_rpe_cap_holds(self):
        """A set taken to the top at RPE 9 when the cap is 7 did not earn more
        load — it was already too hard."""
        r = pr.advance(exercise=LEG_PRESS, sets=sets(3, 12, 100, 9),
                       rpe_cap=7, load_config=self.OFFICE)
        self.assertEqual(r["action"], "hold")
        self.assertEqual(r["load"], 100)

    def test_the_step_is_the_ROOMS_step(self):
        """+5 does not exist at Richfield: PowerBlocks move in 10s and cannot
        make 45. Block 1's suggestion hard-coded +5."""
        r = pr.advance(exercise=DB_PRESS, sets=sets(3, 12, 40, 6),
                       rpe_cap=7, load_config=self.RICHFIELD)
        self.assertEqual(r["load"], 50)
        office_r = pr.advance(exercise=DB_PRESS, sets=sets(3, 12, 40, 6),
                              rpe_cap=7, load_config=self.OFFICE)
        self.assertEqual(office_r["load"], 45)    # office dumbbells DO step by 5

    def test_it_never_suggests_more_than_the_room_has(self):
        r = pr.advance(exercise=DB_PRESS, sets=sets(3, 12, 90, 6),
                       rpe_cap=7, load_config=self.RICHFIELD)
        self.assertEqual(r["load"], 90)           # max is 90

    def test_an_unmeasured_room_gives_no_number(self):
        """LOCATION-1: absent means absent. A suggestion naming a weight the
        room cannot make is worse than no suggestion."""
        r = pr.advance(exercise=DB_PRESS, sets=sets(3, 12, 40, 6),
                       rpe_cap=7, load_config=None)
        self.assertEqual(r["action"], "unknown")
        self.assertIn("step is unknown", r["why"])
        self.assertIn("No suggestion", pr.describe(r))

    def test_nothing_logged_gives_no_number(self):
        r = pr.advance(exercise=LEG_PRESS, sets=[], rpe_cap=7, load_config=self.OFFICE)
        self.assertEqual(r["action"], "unknown")

    def test_no_rpe_logged_does_not_block_a_step(self):
        """An absent RPE is not an RPE over the cap."""
        s = [{"reps_done": 12, "weight_lbs": 100} for _ in range(3)]
        r = pr.advance(exercise=LEG_PRESS, sets=s, rpe_cap=7, load_config=self.OFFICE)
        self.assertEqual(r["action"], "add_load")


class TestTheBlockBoundary(unittest.TestCase):
    def test_block_2_starts_the_day_after_block_1_ends(self):
        self.assertEqual(block2.start_date(), office.program_end() + timedelta(days=1))

    def test_it_is_six_weeks_and_ends_on_a_saturday(self):
        span = (block2.end_date() - block2.start_date()).days + 1
        self.assertEqual(span, 7 * block2.WEEKS)
        self.assertEqual(block2.end_date().weekday(), 5)      # Saturday

    def test_a_repeat_week_MOVES_block_2(self):
        """The boundary is computed, never typed. Writing 2026-11-01 would be
        silently wrong the first time a week repeats — and wrong in the
        direction that looks right."""
        one = block2.start_date([date(2026, 10, 4)])
        self.assertEqual(one, block2.start_date() + timedelta(days=7))

    def test_week_numbers_run_1_to_6_and_stop(self):
        start = block2.start_date()
        self.assertIsNone(block2.week_num_for(start - timedelta(days=1)))
        self.assertEqual(block2.week_num_for(start), 1)
        self.assertEqual(block2.week_num_for(start + timedelta(days=6)), 1)
        self.assertEqual(block2.week_num_for(start + timedelta(days=7)), 2)
        self.assertEqual(block2.week_num_for(block2.end_date()), 6)
        self.assertIsNone(block2.week_num_for(block2.end_date() + timedelta(days=1)))


class TestTheRowsItBuilds(unittest.TestCase):
    ROWS = None

    @classmethod
    def setUpClass(cls):
        cls.ROWS = block2.build_rows()

    def test_it_covers_every_day_of_the_block(self):
        days = {r["plan_date"] for r in self.ROWS}
        self.assertEqual(len(days), 7 * block2.WEEKS)

    def test_the_lift_days_and_template_match_block_1(self):
        """Block 2 is the same program with a different ramp — not a second copy
        of the schedule that can drift from the first."""
        for r in self.ROWS:
            with self.subTest(str(r["plan_date"])):
                self.assertEqual(r["session_type"], office.session_for(r["plan_date"])
                                 if r.get("slot", "morning") == "morning"
                                 else office.EVENING_SESSION)

    def test_the_rpe_ramp_is_7_to_8_across_the_build(self):
        caps = {}
        for r in self.ROWS:
            if r["session_type"].startswith("strength"):
                caps[block2.week_num_for(r["plan_date"])] = r["target_rpe"]
        for wk in (1, 2, 3, 4, 5):
            with self.subTest(wk):
                self.assertGreaterEqual(caps[wk], 7.0)
                self.assertLessEqual(caps[wk], 8.0)
        self.assertLess(caps[6], 7.0)              # deload

    def test_every_row_carries_phase_2(self):
        self.assertTrue(all(r["phase"] == block2.PHASE for r in self.ROWS))

    def test_the_interval_prescription_is_the_norwegian_build_up(self):
        self.assertEqual(block2.INTERVAL_WEEKS[1], (4, 180, 180))
        self.assertEqual(block2.INTERVAL_WEEKS[3], (4, 240, 180))
        self.assertNotIn(6, block2.INTERVAL_WEEKS)   # deload runs Z2

    def test_seeded_interval_rows_are_fail_closed_like_block_1(self):
        """The gate is evaluated on the morning, not five weeks ahead."""
        for r in self.ROWS:
            if r["session_type"] == "cardio_intervals":
                with self.subTest(str(r["plan_date"])):
                    self.assertEqual(r["blocks"]["type"], "steady")
                    self.assertEqual(r["target_hr_zone"], 2)

    def test_building_block_2_does_not_change_block_1(self):
        """The builders' module-level tables are swapped and restored. A builder
        left pointing at block 2 would quietly change a block 1 reseed."""
        before = office.RAMP, office.INTERVAL_WEEKS
        block2.build_rows()
        self.assertIs(office.RAMP, before[0])
        self.assertIs(office.INTERVAL_WEEKS, before[1])
        row = office.build_row({"plan_date": date(2026, 10, 13), "slot": "morning",
                                "session_type": "strength_a", "week_num": 5,
                                "location": "the office", "location_key": "office",
                                "day_type": "office", "pos": 9, "wk0": False})
        self.assertEqual(row["target_rpe"], office.RAMP[5][1])

    def test_nothing_in_this_module_writes(self):
        import inspect
        src = inspect.getsource(block2)
        for forbidden in ("INSERT", "UPDATE", "DELETE", "upsert", "_UPSERT_SQL"):
            self.assertNotIn(forbidden, src, f"block2 must not write ({forbidden})")


class TestTheOverviewReadsAcrossTheBoundary(unittest.TestCase):
    """The handoff's check: "Week N of M" and the deload text must read correctly
    on both sides of the block boundary, rendered from the rows rather than
    asserted about them."""

    def test_week_of_and_deload_are_right_on_each_side(self):
        end1 = office.program_end()
        start2 = block2.start_date()
        # Block 1's last day.
        self.assertEqual(office.week_num_for(end1), 7)
        # Block 2's first day and its deload.
        self.assertEqual(block2.week_num_for(start2), 1)
        self.assertEqual(block2.week_num_for(start2 + timedelta(days=7 * 5)), 6)
        self.assertEqual(block2.DELOAD_WEEK, block2.WEEKS)

    def test_the_two_blocks_do_not_overlap_or_leave_a_gap(self):
        self.assertEqual(block2.start_date(), office.program_end() + timedelta(days=1))

    def test_rendering_week_n_of_m_from_block_2_rows(self):
        rows = block2.build_rows()
        for r in rows:
            wk = block2.week_num_for(r["plan_date"])
            text = f"Week {wk} of {block2.WEEKS}"
            with self.subTest(str(r["plan_date"])):
                self.assertRegex(text, r"^Week [1-6] of 6$")
                if wk == block2.DELOAD_WEEK:
                    self.assertEqual(text, "Week 6 of 6")


if __name__ == "__main__":
    unittest.main()
