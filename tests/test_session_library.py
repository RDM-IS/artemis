"""SESSION-LIB — the library is the seeder's own output, and absent means absent."""
import os as _os; _os.environ["ARTEMIS_TEST_NO_DB"] = "1"  # TEST-DB-GUARD: never a real DB
import unittest
from datetime import date
from unittest import mock

from artemis import cycle, health_office, session_library as sl

D = date(2026, 10, 6)          # a date inside the program window


def _build(**patches):
    with mock.patch.object(cycle, "override_for", return_value=None), \
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

    def test_the_office_has_every_session(self):
        self.assertEqual(list(self.by["office"]), list(sl.LIBRARY_TYPES))

    def test_unsupported_sessions_are_absent_not_greyed(self):
        for key, types in self.by.items():
            for st in types:
                if st.startswith("strength"):
                    self.assertTrue(health_office.can_hold(key, st), (key, st))
        self.assertNotIn("cardio_z2", self.by.get("msp_home", {}))

    def test_entries_are_the_seeders_rows(self):
        e = self.by["office"]["strength_a"]
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
            if st == "strength_b":
                raise RuntimeError("synthetic")
            return real(st, key, today)
        with mock.patch.object(sl, "entry_for", side_effect=boom):
            lib = _build()
        office = [s["session_type"] for s in lib["locations"][0]["sessions"]]
        self.assertNotIn("strength_b", office)
        self.assertIn("strength_a", office)

    def test_the_nightly_job_is_registered_and_silent(self):
        from pathlib import Path
        src = (Path(__file__).resolve().parent.parent / "artemis" / "scheduler.py").read_text()
        self.assertRegex(src, r'CronSpec\(\s*"session_library",\s*"job_session_library",\s*0,\s*20')
        body = src[src.index("def job_session_library"):src.index("def job_nutrition_prefill")]
        for forbidden in ("post_to_channel", "_mm.post", "post_or_hold"):
            self.assertNotIn(forbidden, body)


if __name__ == "__main__":
    unittest.main()
