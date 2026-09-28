"""SESSION-LIB — the library is the seeder's own output, and absent means absent."""
import os as _os; _os.environ["ARTEMIS_TEST_NO_DB"] = "1"  # TEST-DB-GUARD: never a real DB
import unittest
from datetime import date
from unittest import mock

from artemis import cycle, health_office, session_library as sl

D = date(2026, 10, 6)          # a date inside the program window


def _build(**patches):
    with mock.patch.object(sl, "_load_week_rows", return_value=[]), \
         mock.patch.object(cycle, "override_for", return_value=None), \
         mock.patch.object(cycle, "locations", return_value=cycle.DEFAULT_LOCATIONS), \
         mock.patch.object(cycle, "anchor", return_value=cycle.DEFAULT_ANCHOR):
        return sl.build(D)


class TestLibrary(unittest.TestCase):
    def setUp(self):
        self.lib = _build()
        self.by = {l["key"]: {s["session_type"]: s for s in l["sessions"]}
                   for l in self.lib["locations"]}

    def test_no_room_to_train_is_not_a_location(self):
        self.assertNotIn("transit", self.by)
        self.assertNotIn("outside", self.by)

    def test_the_office_has_every_extra(self):
        self.assertEqual(list(self.by["office"]), list(sl.LIBRARY_TYPES))

    def test_unsupported_sessions_are_absent_not_greyed(self):
        for key, types in self.by.items():
            for st in types:
                if st.startswith("strength"):
                    self.assertTrue(health_office.can_hold(key, st), (key, st))
        self.assertNotIn("cardio_z2", self.by.get("msp_home", {}))

    def test_entries_are_the_seeders_rows(self):
        e = sl.entry_for("strength_a", "office", D)
        row = health_office.build_row({
            "plan_date": D, "slot": "morning", "session_type": "strength_a",
            "week_num": health_office.week_num_for(D), "location": "office gym",
            "location_key": "office", "day_type": e["blocks"].get("day_type"), "wk0": False})
        self.assertEqual(e["blocks"], row["blocks"])
        self.assertEqual(e["est_duration_min"], row["est_duration_min"])
        self.assertEqual(self.lib["week_num"], health_office.week_num_for(D))

    def test_every_entry_carries_its_location(self):
        for key, types in self.by.items():
            for st, e in types.items():
                self.assertEqual(e["blocks"]["location_key"], key, (key, st))

    def test_one_failing_build_hides_only_itself(self):
        real = sl.entry_for

        def boom(st, key, today):
            if key == "office":
                raise RuntimeError("synthetic")
            return real(st, key, today)
        with mock.patch.object(sl, "entry_for", side_effect=boom):
            lib = _build()
        by = {l["key"]: l["sessions"] for l in lib["locations"]}
        self.assertEqual(by["office"], [])
        self.assertTrue(by["brown_deer"])

    def test_the_nightly_job_is_registered_and_silent(self):
        from pathlib import Path
        src = (Path(__file__).resolve().parent.parent / "artemis" / "scheduler.py").read_text()
        self.assertRegex(src, r'CronSpec\(\s*"session_library",\s*"job_session_library",\s*0,\s*20')
        body = src[src.index("def job_session_library"):src.index("def job_nutrition_prefill")]
        for forbidden in ("post_to_channel", "_mm.post", "post_or_hold"):
            self.assertNotIn(forbidden, body)


if __name__ == "__main__":
    unittest.main()


class TestMakeupState(unittest.TestCase):
    """MAKEUP-2 (Ryan, 2026-09-27): one not-done session is made up on a rest
    day; more than one means the week repeats."""
    from datetime import date as _d
    WS = _d(2027, 3, 7)                       # a synthetic Sunday
    TODAY = _d(2027, 3, 11)

    def _row(self, pid, day, st, logged=False, skipped=False):
        from datetime import timedelta
        return {"plan_id": pid, "plan_date": self.WS + timedelta(days=day), "session_type": st,
                "display_name": st, "is_skipped": skipped, "logged": logged}

    def test_one_missed_on_a_rest_day_is_offered(self):
        rows = [self._row(1, 1, "strength_a", logged=True), self._row(2, 2, "strength_b"),
                self._row(3, 3, "cardio_z2", logged=True), self._row(4, 4, "rest")]
        mk = sl.makeup_state(rows, self.TODAY, self.WS)
        self.assertEqual(mk["offer"]["missed_plan_id"], 2)
        self.assertEqual(mk["offer"]["rest_plan_id"], 4)
        self.assertFalse(mk["repeat"])

    def test_a_skip_counts_as_not_done(self):
        rows = [self._row(2, 2, "strength_b", skipped=True), self._row(4, 4, "rest")]
        self.assertEqual(sl.makeup_state(rows, self.TODAY, self.WS)["not_done"][0]["skipped"], True)

    def test_two_missed_means_repeat_and_no_offer(self):
        rows = [self._row(1, 1, "strength_a"), self._row(2, 2, "strength_b"), self._row(4, 4, "rest")]
        mk = sl.makeup_state(rows, self.TODAY, self.WS)
        self.assertTrue(mk["repeat"])
        self.assertIsNone(mk["offer"])

    def test_no_offer_on_a_training_day_or_a_logged_rest_day(self):
        rows = [self._row(2, 2, "strength_b"), self._row(4, 4, "strength_c")]
        self.assertIsNone(sl.makeup_state(rows, self.TODAY, self.WS)["offer"])
        rows = [self._row(2, 2, "strength_b"), self._row(4, 4, "rest", logged=True)]
        self.assertIsNone(sl.makeup_state(rows, self.TODAY, self.WS)["offer"])

    def test_flows_and_rest_are_never_missed(self):
        rows = [self._row(1, 1, "recovery_flow"), self._row(2, 2, "rest"), self._row(4, 4, "rest")]
        self.assertEqual(sl.makeup_state(rows, self.TODAY, self.WS)["not_done"], [])

    def test_extras_only_in_the_general_list(self):
        from knowledge.session_types import is_training
        for st in sl.LIBRARY_TYPES:
            self.assertFalse(is_training(st), st)


class TestExtras(unittest.TestCase):
    """EXTRAS (draft, 2026-09-27): core and mobility, bodyweight + mat, anywhere."""

    def test_every_room_offers_all_extras(self):
        lib = _build()
        for loc in lib["locations"]:
            self.assertEqual([s["session_type"] for s in loc["sessions"]],
                             ["recovery_flow", "core", "mobility", "yoga_strength"],
                             loc["key"])

    def test_core_is_light_and_says_when(self):
        e = sl.entry_for("core", "office", D)
        b = e["blocks"]
        self.assertEqual(b["type"], "circuit")
        self.assertTrue(b["extra"])
        self.assertLessEqual(e["target_rpe"], 4)
        self.assertLessEqual(e["est_duration_min"], 15)
        self.assertTrue(any("after the day's lift" in n for n in b["setup_notes"]))
        for ex in b["exercises"]:
            self.assertEqual(ex["equipment_class"], "bodyweight", ex["name"])

    def test_every_extra_exercise_has_regions_for_the_pain_ladder(self):
        from artemis import health_regions
        for st in ("core", "mobility"):
            for ex in sl.entry_for(st, "office", D)["blocks"]["exercises"]:
                self.assertIn(ex["name"], health_regions.EXERCISE_REGIONS, ex["name"])

    def test_unknown_types_still_fall_through_only_for_what_was_there(self):
        # SESSION-LABELS: extras have their own branch, not the rest fallthrough.
        b, *_ = health_office._build_inner("core", 3, location="office gym")
        self.assertNotEqual(b["type"], "mobility")


class TestYoga6(unittest.TestCase):
    """YOGA-6 — Yoga, Strength & Balance. CONTENT IS A DRAFT pending approval."""

    def test_it_is_offered_wherever_a_mat_is(self):
        lib = _build()
        for loc in lib["locations"]:
            with self.subTest(location=loc["key"]):
                self.assertIn("yoga_strength",
                              [s["session_type"] for s in loc["sessions"]])

    def test_it_sits_in_the_specced_window(self):
        e = sl.entry_for("yoga_strength", "richfield", D)
        self.assertEqual(e["blocks"]["display_name"], "Yoga — Strength & Balance")
        self.assertGreaterEqual(e["est_duration_min"], 20)
        self.assertLessEqual(e["est_duration_min"], 25)
        self.assertGreaterEqual(e["target_rpe"], 4.0)
        self.assertLessEqual(e["target_rpe"], 5.0)

    def test_it_is_harder_than_the_recovery_flow_and_still_mat_only(self):
        y6 = sl.entry_for("yoga_strength", "richfield", D)
        rf = sl.entry_for("recovery_flow", "richfield", D)
        self.assertGreater(y6["target_rpe"], rf["target_rpe"])
        self.assertEqual(y6["blocks"]["equipment"], ["mat"])

    def test_it_holds_for_less_time_than_the_recovery_flow(self):
        from artemis import health_office as office
        self.assertLess(office.YOGA6_HOLD_SEC, office.FLOW_HOLD_SEC)

    def test_the_recovery_flow_is_unchanged_by_the_hold_parameter(self):
        """`_flow_step` gained a hold argument; the Recovery Flow's own rows must
        be byte-identical to what they were."""
        from artemis import health_office as office
        for spec in office.FLOW_STEPS:
            with self.subTest(step=spec["step"]):
                self.assertEqual(office._flow_step(spec)["duration_sec"],
                                 office.FLOW_HOLD_SEC)

    def test_every_pose_validates(self):
        """validate_flow is called in the builder; this pins what it checks —
        balanced sides, a known posture, Sanskrit, and a cue_mid key."""
        from artemis import health_office as office
        b = sl.entry_for("yoga_strength", "office", D)["blocks"]
        office.validate_flow(b)                       # must not raise
        for st in b["flow"]:
            with self.subTest(pose=st["name"]):
                self.assertIn(st["posture"], office.FLOW_POSTURES)
                self.assertTrue(st["sanskrit"] and st["sanskrit_spoken"])
                self.assertIn("cue_mid", st)

    def test_the_standing_block_is_balanced_left_and_right(self):
        from artemis import health_office as office
        b = sl.entry_for("yoga_strength", "office", D)["blocks"]
        sided = [s for s in b["flow"] if s["mirror_group"] == "standing-unit"]
        self.assertEqual(sum(s["duration_sec"] for s in sided if s["side"] == "R"),
                         sum(s["duration_sec"] for s in sided if s["side"] == "L"))
        self.assertEqual(len(sided), 8)

    def test_the_side_switch_lands_at_the_low_lunge_twist(self):
        """R,R,R,R then L,L,L,L in mirror order, so the switch is not between two
        different poses — the same reason the Recovery Flow runs its lunges R,R,L,L."""
        from artemis import health_office as office
        b = sl.entry_for("yoga_strength", "office", D)["blocks"]
        sided = [s for s in b["flow"] if s["mirror_group"] == "standing-unit"]
        self.assertEqual([s["side"] for s in sided], list("RRRRLLLL"))
        switch = sided[3], sided[4]
        self.assertEqual(switch[0]["name"], switch[1]["name"])

    def test_nothing_loaded_happens_cold(self):
        """The opening arc comes before the standing block."""
        from artemis import health_office as office
        b = sl.entry_for("yoga_strength", "office", D)["blocks"]
        names = [s["name"] for s in b["flow"]]
        self.assertLess(names.index("Downward dog"), names.index("Warrior III"))

    def test_the_draft_marker_is_present(self):
        """It must be obvious in the source that Ryan has not approved this yet."""
        from pathlib import Path
        src = Path(__file__).resolve().parents[1] / "artemis" / "health_office.py"
        self.assertIn("CONTENT IS A DRAFT pending Ryan's approval (2026-09-28)",
                      src.read_text())

    def test_it_adds_no_new_posture(self):
        """Adding one would need the same value in gym-display's POSTURES union in
        the same change (ENUM-EXPAND); a draft-content PR is not that change."""
        from artemis import health_office as office
        self.assertEqual(office.FLOW_POSTURES,
                         ("standing", "kneeling", "quadruped", "prone", "supine", "seated"))
