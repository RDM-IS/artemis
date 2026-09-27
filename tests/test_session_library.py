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
