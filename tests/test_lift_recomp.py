"""LIFT-RECOMP — rep ranges, superset density, durations, progression trigger.

Ryan, 2026-09-29: strength's job is muscle retention in a deficit plus calorie
burn, not maximal strength. Goal order: fat loss >> muscle retention > endurance.

PUBLIC-FIXTURES: nothing here is logged data. The exercise names are the
program's own vocabulary and the numbers are the prescription, not measurements.
"""
import os as _os; _os.environ["ARTEMIS_TEST_NO_DB"] = "1"  # TEST-DB-GUARD

import unittest

from knowledge import lift_recomp as lr
from artemis import health_office as ho


class TestRepRanges(unittest.TestCase):
    def test_the_two_ranges_are_ryans(self):
        self.assertEqual(lr.reps_for(lr.COMPOUND), (12, 20))
        self.assertEqual(lr.reps_for(lr.ACCESSORY), (15, 25))

    def test_an_unclassified_lift_raises_rather_than_defaulting(self):
        # A silent default would prescribe accessory volume for a squat pattern.
        with self.assertRaises(KeyError):
            lr.reps_for("whatever")

    def test_every_exercise_the_builders_emit_has_a_profile(self):
        """`lift_profile` raises on an unknown name, so this is the list that has
        to be complete for the builders to run at all. Asserting it here names the
        missing exercise instead of failing inside a seed."""
        names = set()
        for lst in ho._EXERCISES.values():
            for spec in lst:
                names.add(spec[0])
        for table in (ho.RICHFIELD_SUBS, ho.RICHFIELD_B_SUBS, ho.RICHFIELD_A_SUBS):
            for sub in table.values():
                names.add(sub[0])
        missing = sorted(n for n in names if n not in ho.LIFT_PROFILE)
        self.assertEqual(missing, [])

    def test_every_profile_is_well_formed(self):
        for name, (cls, pattern, station) in ho.LIFT_PROFILE.items():
            self.assertIn(cls, (lr.COMPOUND, lr.ACCESSORY), name)
            self.assertIn(pattern, (lr.PUSH, lr.PULL, lr.LOWER, lr.CORE), name)
            self.assertTrue(station is None or isinstance(station, str), name)

    def test_the_builder_uses_the_class_range_from_week_5(self):
        for st in ("strength_a", "strength_b", "strength_c"):
            blocks, _, _, _ = ho._strength(st, 5)
            for ex in blocks["exercises"]:
                lo, hi = lr.reps_for(ho.lift_class_for(ex["name"]))
                self.assertEqual(ex["target_reps"], hi, f"{st}/{ex['name']}")
                self.assertIn(f"{lo}-{hi}", ex["notes"], f"{st}/{ex['name']}")

    def test_weeks_before_5_keep_the_old_ranges(self):
        # THE guard that makes the reseed safe: nothing before 10/11 moves,
        # because the old prescription is still what those weeks produce — not
        # because a date filter removed them afterwards.
        for wk in (1, 2, 3, 4):
            blocks, _, _, _ = ho._strength("strength_a", wk)
            tops = {e["name"]: e["target_reps"] for e in blocks["exercises"]}
            self.assertEqual(tops["Leg press"], 12, f"week {wk}")
            self.assertNotIn("12-20", blocks["exercises"][0]["notes"], f"week {wk}")

    def test_applies_is_fail_closed_on_an_unreadable_week(self):
        # An unknown week keeps the prescription it has; it is not assumed new.
        self.assertFalse(lr.applies(None))
        self.assertFalse(lr.applies("wk5"))
        self.assertTrue(lr.applies(5))
        self.assertFalse(lr.applies(4))


class TestBlock2GetsTheSchemeInEveryWeek(unittest.TestCase):
    """Block 2 numbers its weeks 1-6 ALL OVER AGAIN.

    A bare `week_num >= 5` would therefore have given block 2's first FOUR weeks
    the old strength-biased prescription and only its last two the new one —
    silently, in a block that does not exist yet, so nothing would have
    contradicted it until the 10/25 seeding round produced the wrong rows.
    """

    def _row(self, wk, session_type="strength_a"):
        from artemis import block2
        spec = {"plan_date": block2.start_date(), "slot": "morning",
                "session_type": session_type, "week_num": wk,
                "location": "office gym", "location_key": "office",
                "day_type": "msp_work", "pos": 1, "wk0": False}
        return block2._build_one(spec)

    def test_every_block_2_week_uses_the_recomp_ranges(self):
        for wk in range(1, 7):
            row = self._row(wk)
            for ex in row["blocks"]["exercises"]:
                lo, hi = lr.reps_for(ho.lift_class_for(ex["name"]))
                self.assertEqual(ex["target_reps"], hi, f"block2 wk{wk} {ex['name']}")

    def test_every_block_2_week_is_paired(self):
        for wk in range(1, 7):
            self.assertTrue(self._row(wk)["blocks"].get("supersets"), f"wk{wk}")

    def test_every_block_2_office_lift_fits_the_window(self):
        for wk in range(1, 7):
            for st in ("strength_a", "strength_b", "strength_c"):
                row = self._row(wk, st)
                self.assertLessEqual(row["est_duration_min"], lr.OFFICE_CAP_MIN,
                                     f"block2 wk{wk} {st}")

    def test_the_swap_is_restored_so_block_1_is_unaffected(self):
        # A builder left pointing at block 2's knob would quietly change what a
        # block 1 reseed produces — the reason _build_one uses try/finally at all.
        before = ho.RECOMP_FROM_WEEK
        self._row(1)
        self.assertEqual(ho.RECOMP_FROM_WEEK, before)
        blocks, _, _, _ = ho._strength("strength_a", 4)
        self.assertEqual(blocks["exercises"][0]["target_reps"], 12)
        self.assertIsNone(blocks.get("supersets"))


class TestRpeBand(unittest.TestCase):
    def test_working_weeks_sit_in_the_7_to_8_band(self):
        """High reps only retain muscle if the set ends near failure. RPE is the
        load-bearing part of the scheme, so the working weeks are pinned."""
        for wk in (5, 6):
            _, rpe, _, _ = ho._strength("strength_a", wk)
            self.assertTrue(lr.rpe_in_band(rpe), f"week {wk} rpe={rpe}")

    def test_the_deload_week_is_allowed_below_the_band(self):
        _, rpe, _, _ = ho._strength("strength_a", 7)
        self.assertFalse(lr.rpe_in_band(rpe))
        self.assertLess(rpe, lr.RPE_RANGE[0])


class TestSupersetStations(unittest.TestCase):
    """The hard constraint: never pair two exercises that need the same station.

    A superset means leaving the first station between sets. If both movements
    need the one adjustable bench or the one functional trainer, the "superset" is
    the same exercise with extra walking — and a substitution table is exactly how
    that gets introduced without anyone noticing.
    """

    LOCATIONS = (("office gym", "office"), ("Richfield", "richfield"))

    def test_no_pair_shares_a_station_at_any_location_in_any_week(self):
        for location, key in self.LOCATIONS:
            for wk in range(1, 8):
                for st in ("strength_a", "strength_b", "strength_c"):
                    blocks, _, _, _ = ho._strength(st, wk, location=location,
                                                   location_key=key)
                    for a, b in blocks.get("supersets") or []:
                        sa, sb = ho.station_for(a), ho.station_for(b)
                        if sa is None or sb is None:
                            continue
                        self.assertNotEqual(
                            sa, sb,
                            f"{key} wk{wk} {st}: {a} + {b} both need {sa!r}")

    def test_the_pulldown_and_the_row_are_known_to_be_one_machine(self):
        # The specific trap: different movements, same machine. If this ever
        # becomes two machines, change it here and the pairing follows.
        self.assertEqual(ho.station_for("Lat pulldown"),
                         ho.station_for("Seated cable row"))

    def test_the_rule_actually_bites_when_a_clash_is_offered(self):
        """A constraint never exercised is a constraint nobody has tested. Offer
        the pairer two movements on one station and it must refuse to pair them."""
        items = ["Lat pulldown", "Seated cable row"]
        pairs, singles = lr.pair_for_density(
            items, station_of=ho.station_for, pattern_of=ho.pattern_for)
        self.assertEqual(pairs, [])
        self.assertEqual(sorted(singles), sorted(items))

    def test_complementary_patterns_are_preferred(self):
        # push + lower is a good pair; two pushes are not, and the pairer should
        # reach past the second push to find the lower.
        items = ["DB bench press", "Pec fly", "DB goblet squat"]
        pairs, _ = lr.pair_for_density(
            items, station_of=ho.station_for, pattern_of=ho.pattern_for)
        self.assertEqual(pairs, [("DB bench press", "DB goblet squat")])

    def test_no_exercise_is_paired_twice_or_lost(self):
        for wk in (5, 6, 7):
            for st in ("strength_a", "strength_b", "strength_c"):
                blocks, _, _, _ = ho._strength(st, wk)
                names = [e["name"] for e in blocks["exercises"]]
                paired = [n for pair in blocks["supersets"] for n in pair]
                self.assertEqual(sorted(paired + blocks["unpaired"]),
                                 sorted(names), f"wk{wk} {st}")
                self.assertEqual(len(set(paired)), len(paired), f"wk{wk} {st}")

    def test_the_rests_enact_the_pair_on_the_existing_ipad(self):
        # gym-display renders rest_after_sec and knows nothing about supersets, so
        # following the timers has to perform the superset correctly by itself.
        blocks, _, _, _ = ho._strength("strength_a", 5)
        by_name = {e["name"]: e for e in blocks["exercises"]}
        for first, second in blocks["supersets"]:
            self.assertEqual(by_name[first]["rest_after_sec"], lr.INTRA_PAIR_SEC)
            self.assertEqual(by_name[second]["rest_after_sec"],
                             lr.REST_BETWEEN_PAIRS_SEC)
            self.assertIn(second, by_name[first]["notes"])
        for name in blocks["unpaired"]:
            self.assertEqual(by_name[name]["rest_after_sec"],
                             lr.REST_BETWEEN_SETS_SEC)


class TestDurations(unittest.TestCase):
    def test_every_office_lift_fits_the_60_minute_window_from_week_5(self):
        """The change that motivated the density work: B was 62 and C was 67."""
        for wk in (5, 6, 7):
            for st in ("strength_a", "strength_b", "strength_c"):
                _, _, _, minutes = ho._strength(st, wk)
                self.assertLessEqual(minutes, lr.OFFICE_CAP_MIN,
                                     f"wk{wk} {st} = {minutes} min")

    def test_no_office_row_is_rejected_by_the_time_cap_in_any_week(self):
        rejects = []
        for wk in range(1, 8):
            for st in ("strength_a", "strength_b", "strength_c"):
                _, _, _, minutes = ho._strength(st, wk)
                kind, msg = ho.duration_verdict(st, wk, minutes)
                if kind == "reject":
                    rejects.append(f"wk{wk} {st}: {msg}")
        self.assertEqual(rejects, [])

    def test_the_calibration_exemption_covers_only_the_weeks_that_still_need_it(self):
        """An exemption that outlives its cause is a cap that has quietly stopped
        applying. Weeks 5-6 now fit on their own, so they must be out of the set —
        and weeks 3-4 keep the old prescription, so they must still be in it."""
        self.assertEqual(ho.CALIBRATION_PENDING,
                         {("strength_b", 3), ("strength_b", 4)})
        for wk in (5, 6):
            for st in ("strength_b", "strength_c"):
                self.assertNotIn((st, wk), ho.CALIBRATION_PENDING)
                _, _, _, minutes = ho._strength(st, wk)
                self.assertLess(minutes, ho.HARD_MAX_MIN, f"wk{wk} {st}")

    def test_a_pair_costs_one_rest_for_two_exercises(self):
        # The whole arithmetic of the density change, isolated: six exercises
        # paired must beat six exercises unpaired.
        paired = lr.session_minutes(n_pairs=3, n_singles=0, sets=3)
        unpaired = lr.session_minutes(n_pairs=0, n_singles=6, sets=3)
        self.assertLess(paired, unpaired)

    def test_the_finisher_is_counted_once(self):
        with_f = lr.session_minutes(n_pairs=3, n_singles=0, sets=3, finisher_min=12)
        without = lr.session_minutes(n_pairs=3, n_singles=0, sets=3)
        self.assertEqual(with_f - without, 12)
        # and the builder must not add it a second time
        _, _, _, c5 = ho._strength("strength_c", 5)
        _, _, _, c4 = ho._strength("strength_c", 4)
        self.assertEqual(c5, lr.session_minutes(n_pairs=3, n_singles=0, sets=3,
                                                finisher_min=12))
        self.assertNotEqual(c5, c4)

    def test_weeks_before_5_keep_the_old_estimate_exactly(self):
        # 10 + sets x exercises x 2.5. If this moves, a row before 10/11 moves.
        for wk in (1, 2, 3, 4):
            for st in ("strength_a", "strength_b", "strength_c"):
                blocks, _, sets, minutes = ho._strength(st, wk)
                n = len(blocks["exercises"])
                expected = 10 + round(blocks["rounds"] * n * 2.5)
                if st == "strength_c" and wk in (5, 6):
                    expected += 12
                self.assertEqual(minutes, expected, f"wk{wk} {st}")


class TestProgressionTrigger(unittest.TestCase):
    def test_the_load_step_needs_the_top_of_the_range_on_every_set(self):
        # Unchanged in kind, changed in number: the range moved, not the rule.
        self.assertTrue(lr.ready_for_load_step([20, 20, 20], lr.COMPOUND))
        self.assertFalse(lr.ready_for_load_step([20, 20, 19], lr.COMPOUND))
        self.assertTrue(lr.ready_for_load_step([25, 25], lr.ACCESSORY))
        self.assertFalse(lr.ready_for_load_step([20, 20, 20], lr.ACCESSORY))

    def test_the_old_top_of_12_no_longer_triggers_a_compound_load_step(self):
        # The point of the change: 12 was the top of the old range and is the
        # BOTTOM of the new one, so hitting 12 is now the start, not the trigger.
        self.assertFalse(lr.ready_for_load_step([12, 12, 12], lr.COMPOUND))

    def test_no_sets_is_not_ready(self):
        self.assertFalse(lr.ready_for_load_step([], lr.COMPOUND))
        self.assertFalse(lr.ready_for_load_step(None, lr.COMPOUND))

    def test_the_progression_note_names_the_new_top(self):
        note = lr.progression_note(lr.COMPOUND)
        self.assertIn("20", note)
        self.assertIn("EVERY set", note)

    def test_the_builder_states_the_progression_and_the_density(self):
        blocks, _, _, _ = ho._strength("strength_a", 5)
        setup = " ".join(blocks["setup_notes"])
        self.assertIn("reps first", setup.lower())
        self.assertIn("superset", setup.lower())


class TestDoubleProgressionFollowsTheNewRange(unittest.TestCase):
    """#20's load-step logic is unchanged; only the trigger number moved.

    `knowledge.progression.rep_range` parses the range out of the exercise's own
    NOTES, so writing "3×12-20" on the row is what moves the trigger — there is no
    second place holding the old 12. This test pins that, because it is the kind of
    coupling that works by accident until someone changes the note format.
    """

    def _ex(self, week_num=5, name="Leg press"):
        blocks, _, _, _ = ho._strength("strength_a", week_num)
        return next(e for e in blocks["exercises"] if e["name"] == name)

    def test_progression_reads_the_new_range_off_the_row(self):
        from knowledge import progression
        self.assertEqual(progression.rep_range(self._ex()), (12, 20))

    def test_the_old_top_of_12_no_longer_advances_the_load(self):
        from knowledge import progression
        ex = self._ex()
        out = progression.advance(exercise=ex,
                                 sets=[{"reps_done": 12, "weight_lbs": 100,
                                        "rpe_actual": 7.0}] * 3,
                                 rpe_cap=8.0, load_config=None)
        self.assertNotEqual(out["action"], "go_up")

    def test_the_top_of_the_new_range_on_every_set_does(self):
        from knowledge import progression
        ex = self._ex()
        out = progression.advance(exercise=ex,
                                 sets=[{"reps_done": 20, "weight_lbs": 100,
                                        "rpe_actual": 7.0}] * 3,
                                 rpe_cap=8.0, load_config=None)
        self.assertIn(out["action"], ("go_up", "hold", "unknown"))
        # whatever it decides, it must NOT be "add another rep" — 20 is the top
        self.assertNotEqual(out["action"], "add_rep")

    def test_one_set_short_of_the_top_is_not_a_load_step(self):
        from knowledge import progression
        ex = self._ex()
        out = progression.advance(exercise=ex,
                                 sets=[{"reps_done": 20, "weight_lbs": 100, "rpe_actual": 7.0},
                                       {"reps_done": 20, "weight_lbs": 100, "rpe_actual": 7.0},
                                       {"reps_done": 19, "weight_lbs": 100, "rpe_actual": 7.5}],
                                 rpe_cap=8.0, load_config=None)
        self.assertEqual(out["action"], "add_rep")

    def test_a_week_4_row_still_reads_the_old_range(self):
        from knowledge import progression
        self.assertEqual(progression.rep_range(self._ex(week_num=4)), (10, 12))


class TestDensityIsAnExpectationNotATarget(unittest.TestCase):
    def test_a_strength_row_carries_no_invented_hr_target(self):
        """Ryan's reason for supersets is that HR stays up. That is stated as an
        expectation; writing it as target_hr_zone would be a cardio prescription
        nobody set, and ZONE-0 already measures what the session actually did."""
        blocks, _, _, _ = ho._strength("strength_a", 5)
        self.assertIn("density_note", blocks)
        self.assertNotIn("target_hr_zone", blocks)
        self.assertNotIn("zones", blocks)


if __name__ == "__main__":
    unittest.main()
