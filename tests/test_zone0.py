"""ZONE-0 — minutes per heart-rate zone from WATCH-1 samples.

PUBLIC-FIXTURES: every bpm here is synthetic. Ryan's real range sits around
58-71 resting and the program's zones are 105-157, so the series below use
values chosen to exercise boundaries, not to resemble anything he recorded.
"""
import os as _os; _os.environ["ARTEMIS_TEST_NO_DB"] = "1"  # TEST-DB-GUARD
import unittest
from datetime import datetime, timedelta, timezone

from artemis import hr_zones
from knowledge import zones

T0 = datetime(2099, 1, 1, 7, 0, tzinfo=timezone.utc)


def series(minutes, bpm, every_sec=30, start=T0):
    """A dense, evenly-spaced run at one heart rate."""
    n = int(minutes * 60 / every_sec)
    return [(start + timedelta(seconds=every_sec * i), bpm) for i in range(n)]


class TestTheZoneTable(unittest.TestCase):
    def test_all_five_zones_exist_and_ascend(self):
        lows = [zones.ZONES[z][0] for z in ("Z1", "Z2", "Z3", "Z4", "Z5")]
        self.assertEqual(lows, sorted(lows))
        self.assertEqual(len(zones.ZONES), 5)

    def test_the_approved_pair_is_unchanged(self):
        """Z2 and Z4 are Ryan's numbers. Adding three zones must not move them."""
        self.assertEqual(zones.ZONES["Z2"], (105, 122))
        self.assertEqual(zones.ZONES["Z4"], (139, 157))

    def test_the_derivation_stays_within_a_bpm_of_the_approved_pair(self):
        """`round()` on the percentages gives Z2 = 104-122 — one bpm under the
        approved 105. That gap is pinned rather than hidden, so changing HR_MAX
        surfaces it instead of silently moving a number he signed off."""
        for zone in ("Z2", "Z4"):
            lo_pct, hi_pct = zones.PERCENTS[zone]
            lo, hi = zones.ZONES[zone]
            self.assertLessEqual(abs(round(lo_pct * zones.HR_MAX) - lo), 1, zone)
            self.assertLessEqual(abs(round(hi_pct * zones.HR_MAX) - hi), 1, zone)

    def test_classification_never_double_counts_a_boundary(self):
        """The published ranges overlap at their edges (Z2 ends 122, Z3 starts
        122). Every reading must land in exactly one zone."""
        for bpm in range(80, 200):
            z = zones.classify_bpm(bpm)
            matches = [name for name in zones.ZONES if z == name]
            self.assertLessEqual(len(matches), 1, bpm)
        self.assertEqual(zones.classify_bpm(122), "Z3")
        self.assertEqual(zones.classify_bpm(121), "Z2")
        self.assertEqual(zones.classify_bpm(139), "Z4")
        self.assertEqual(zones.classify_bpm(138), "Z3")

    def test_below_z1_is_none_not_z1(self):
        self.assertIsNone(zones.classify_bpm(60))
        self.assertIsNone(zones.classify_bpm(86))
        self.assertEqual(zones.classify_bpm(87), "Z1")

    def test_a_non_numeric_reading_is_none_rather_than_a_crash(self):
        for bad in (None, "", "abc", object()):
            self.assertIsNone(zones.classify_bpm(bad))


class TestDenseSeries(unittest.TestCase):
    def test_a_steady_zone_two_session(self):
        out = zones.minutes_by_zone(series(30, 110), T0, T0 + timedelta(minutes=30))
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["zones"]["Z2"], 30)
        self.assertEqual(out["zones"]["Z4"], 0)
        self.assertEqual(out["unaccounted_sec"], 0)

    def test_an_interval_session_splits_between_two_zones(self):
        samples = series(20, 110) + series(10, 145, start=T0 + timedelta(minutes=20))
        out = zones.minutes_by_zone(samples, T0, T0 + timedelta(minutes=30))
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["zones"]["Z2"], 20)
        self.assertEqual(out["zones"]["Z4"], 10)

    def test_the_minutes_never_exceed_the_window(self):
        out = zones.minutes_by_zone(series(30, 110), T0, T0 + timedelta(minutes=30))
        self.assertLessEqual(sum(out["zones"].values()), 30)

    def test_readings_below_z1_are_counted_in_no_zone_but_still_use_time(self):
        out = zones.minutes_by_zone(series(30, 70), T0, T0 + timedelta(minutes=30))
        self.assertEqual(out["status"], "ok")
        self.assertEqual(sum(out["zones"].values()), 0)
        self.assertGreater(out["counted_sec"], 0)


class TestSparseAndMissing(unittest.TestCase):
    def test_too_few_samples_is_insufficient_never_a_number(self):
        out = zones.minutes_by_zone(
            [(T0, 110), (T0 + timedelta(minutes=20), 115)], T0, T0 + timedelta(minutes=30))
        self.assertEqual(out["status"], "insufficient_hr_data")
        self.assertIsNone(out["zones"])

    def test_no_samples_at_all(self):
        out = zones.minutes_by_zone([], T0, T0 + timedelta(minutes=30))
        self.assertEqual(out["status"], "no_samples")
        self.assertIsNone(out["zones"])

    def test_a_zero_length_window_is_bad_not_empty(self):
        out = zones.minutes_by_zone(series(30, 110), T0, T0)
        self.assertEqual(out["status"], "bad_window")
        self.assertIsNone(out["zones"])

    def test_exactly_at_the_density_floor_is_accepted(self):
        out = zones.minutes_by_zone(series(30, 110, every_sec=60), T0, T0 + timedelta(minutes=30))
        self.assertEqual(out["status"], "ok")

    def test_just_under_the_floor_is_not(self):
        s = series(30, 110, every_sec=60)[:-1]
        out = zones.minutes_by_zone(s, T0, T0 + timedelta(minutes=30))
        self.assertEqual(out["status"], "insufficient_hr_data")


class TestAGapInTheMiddle(unittest.TestCase):
    """The watch stopping is not the same as a long hold in one zone."""

    def test_a_gap_is_uncounted_rather_than_attributed(self):
        before = series(10, 110)
        after = series(10, 110, start=T0 + timedelta(minutes=20))
        out = zones.minutes_by_zone(before + after, T0, T0 + timedelta(minutes=30))
        self.assertEqual(out["status"], "ok")
        # 20 minutes of samples plus the capped credit across the 10-minute hole.
        self.assertLessEqual(out["zones"]["Z2"], 22)
        self.assertGreaterEqual(out["zones"]["Z2"], 20)
        self.assertGreater(out["unaccounted_sec"], 0)

    def test_the_cap_bounds_what_one_sample_can_claim(self):
        out = zones.minutes_by_zone(series(30, 110, every_sec=60), T0, T0 + timedelta(minutes=30))
        self.assertLessEqual(max(out["zones"].values()), 30)


class TestBoundaryAndOutsideSamples(unittest.TestCase):
    def test_samples_outside_the_window_are_ignored(self):
        inside = series(30, 110)
        outside = series(10, 150, start=T0 - timedelta(minutes=20))
        out = zones.minutes_by_zone(inside + outside, T0, T0 + timedelta(minutes=30))
        self.assertEqual(out["sample_count"], len(inside))
        self.assertEqual(out["zones"]["Z4"], 0)

    def test_samples_exactly_on_the_boundaries_are_included(self):
        end = T0 + timedelta(minutes=30)
        s = series(30, 110) + [(end, 110)]
        out = zones.minutes_by_zone(s, T0, end)
        self.assertEqual(out["sample_count"], len(s))

    def test_unordered_input_gives_the_same_answer(self):
        s = series(30, 110)
        a = zones.minutes_by_zone(s, T0, T0 + timedelta(minutes=30))
        b = zones.minutes_by_zone(list(reversed(s)), T0, T0 + timedelta(minutes=30))
        self.assertEqual(a["zones"], b["zones"])

    def test_a_none_bpm_is_dropped_not_counted_as_zero(self):
        s = series(30, 110) + [(T0 + timedelta(seconds=5), None)]
        out = zones.minutes_by_zone(s, T0, T0 + timedelta(minutes=30))
        self.assertEqual(out["sample_count"], 60)


class TestTheWindow(unittest.TestCase):
    class Cur:
        def __init__(self, rows): self.rows, self._res = rows, []
        def execute(self, sql, params=None):
            self._res = self.rows
        def fetchall(self): return self._res

    def test_a_cardio_block_duration_wins(self):
        end = T0 + timedelta(minutes=30)
        cur = self.Cur([{"log_type": "cardio_block", "duration_sec": 1800, "logged_at": end}])
        start, stop, src = hr_zones.window_for(cur, 1)
        self.assertEqual(stop, end)
        self.assertEqual(start, T0)
        self.assertIn("cardio_block", src)

    def test_otherwise_the_span_of_the_logs(self):
        cur = self.Cur([
            {"log_type": "strength_set", "duration_sec": None, "logged_at": T0},
            {"log_type": "strength_set", "duration_sec": None,
             "logged_at": T0 + timedelta(minutes=25)}])
        start, stop, src = hr_zones.window_for(cur, 1)
        self.assertEqual((start, stop), (T0, T0 + timedelta(minutes=25)))
        self.assertIn("logged_at", src)

    def test_one_log_row_is_no_window_rather_than_a_guess(self):
        cur = self.Cur([{"log_type": "strength_set", "duration_sec": None, "logged_at": T0}])
        start, stop, src = hr_zones.window_for(cur, 1)
        self.assertIsNone(start)
        self.assertIn("one log row", src)

    def test_no_logs_at_all(self):
        start, stop, src = hr_zones.window_for(self.Cur([]), 1)
        self.assertIsNone(start)
        self.assertEqual(src, "no logs")


class TestTheStoredShape(unittest.TestCase):
    def test_minutes_are_null_unless_the_status_is_ok(self):
        """The migration enforces this with a CHECK; the params must not try to
        write a zero that the constraint would reject -- or worse, accept."""
        row = {"plan_id": 7, "window_start": T0, "window_end": T0 + timedelta(minutes=30),
               "window_source": "x", "status": "insufficient_hr_data", "zones": None,
               "sample_count": 3, "counted_sec": 0, "unaccounted_sec": 1800}
        params = hr_zones.upsert_params(row)
        self.assertEqual(list(params[8:13]), [None] * 5)

    def test_an_ok_row_carries_all_five(self):
        row = {"plan_id": 7, "window_start": T0, "window_end": T0 + timedelta(minutes=30),
               "window_source": "x", "status": "ok",
               "zones": {"Z1": 0, "Z2": 20, "Z3": 0, "Z4": 10, "Z5": 0},
               "sample_count": 60, "counted_sec": 1800, "unaccounted_sec": 0}
        params = hr_zones.upsert_params(row)
        self.assertEqual(list(params[8:13]), [0, 20, 0, 10, 0])

    def test_the_hrmax_used_is_recorded_so_zone_1_can_be_seen_later(self):
        row = {"plan_id": 7, "window_start": T0, "window_end": T0, "window_source": "x",
               "status": "no_samples", "zones": None}
        self.assertEqual(hr_zones.upsert_params(row)[13], zones.HR_MAX)

    def test_a_session_with_no_window_is_not_stored_at_all(self):
        calls = []
        class C:
            def execute(self, *a): calls.append(a)
        hr_zones.store(C(), {"plan_id": 1, "window_start": None, "window_end": None,
                             "window_source": "no logs", "status": "bad_window", "zones": None})
        self.assertEqual(calls, [])


class TestTheLine(unittest.TestCase):
    def test_it_names_the_zones_that_have_minutes(self):
        line = hr_zones.describe({"status": "ok",
                                  "zones": {"Z1": 0, "Z2": 31, "Z3": 0, "Z4": 6, "Z5": 0}})
        self.assertEqual(line, "Z2 31 min · Z4 6 min")

    def test_it_says_why_rather_than_showing_zeros(self):
        for status, expect in (("insufficient_hr_data", "not enough"),
                               ("no_samples", "no heart-rate data"),
                               ("bad_window", "couldn't tell when")):
            self.assertIn(expect, hr_zones.describe({"status": status, "zones": None}))


if __name__ == "__main__":
    unittest.main()
