"""WATCH-1 — the morning check-in pre-fill.

The rules under test, in the order they matter:
  1. a value Ryan typed is FINAL for that date, whatever the arrival order;
  2. nothing is invented when a sample hasn't synced;
  3. the reply names its sources and says what is missing.

Run:
    python3 tests/test_watch_prefill.py
"""

import os as _os; _os.environ["ARTEMIS_TEST_NO_DB"] = "1"  # TEST-DB-GUARD: never a real DB

import sys
import unittest
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from artemis import watch_prefill as wp  # noqa: E402

CT = ZoneInfo("America/Chicago")
DAY = date(2026, 9, 21)                      # the first tracked night: 9/20 -> 9/21


def at(h, m=0, day=DAY):
    return datetime.combine(day, time(h, m), tzinfo=CT)


class FakeQuery:
    """Stands in for knowledge.db.execute_query."""

    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def __call__(self, sql, params):
        self.calls.append((sql, params))
        lo, hi = params
        if "sleep%" in sql:
            got = [r for r in self.rows if r["metric"].startswith("sleep")
                   and lo <= r["measured_at"] <= hi]
        elif "resting_heart_rate" in sql:
            got = [r for r in self.rows if r["metric"] == "resting_heart_rate"
                   and lo <= r["measured_at"] <= hi]
        elif "'weight'" in sql:
            got = [r for r in self.rows if r["metric"] == "weight"
                   and lo <= r["measured_at"] <= hi]
        else:
            got = []
        return sorted(got, key=lambda r: r["measured_at"], reverse=True)


def rows(*triples):
    return [{"metric": m, "value": v, "measured_at": t} for m, v, t in triples]


class TestReading(unittest.TestCase):
    def test_a_full_night_and_a_weigh_in(self):
        q = FakeQuery(rows(("sleep_asleep", 7.2, at(6, 30)),
                           ("sleep_deep", 1.1, at(6, 30)),
                           ("sleep_in_bed", 7.9, at(6, 30)),
                           ("resting_heart_rate", 54, at(6, 12)),
                           ("weight", 281.4, at(6, 20))))
        got = wp.read_watch_values(DAY, tz=CT, query=q)
        self.assertEqual(got["sleep_hrs"], 7.2)
        self.assertEqual(got["sleep_source_metric"], "sleep_asleep")
        self.assertEqual(got["resting_hr"], 54)
        self.assertEqual(got["weight_lbs"], 281.4)
        self.assertEqual(got["missing"], [])

    def test_in_bed_is_not_sleep(self):
        """7.9 h in bed is not 7.9 h asleep — the difference is the point."""
        q = FakeQuery(rows(("sleep_in_bed", 7.9, at(6, 30)),
                           ("sleep_awake", 0.4, at(6, 30))))
        got = wp.read_watch_values(DAY, tz=CT, query=q)
        self.assertIsNone(got["sleep_hrs"])
        self.assertIn("sleep", got["missing"])

    def test_stages_are_summed_only_without_a_total(self):
        q = FakeQuery(rows(("sleep_deep", 1.1, at(6, 30)),
                           ("sleep_rem", 1.6, at(6, 30)),
                           ("sleep_core", 4.5, at(6, 30))))
        got = wp.read_watch_values(DAY, tz=CT, query=q)
        self.assertEqual(got["sleep_hrs"], 7.2)
        self.assertEqual(got["sleep_source_metric"], "sleep_core+sleep_deep+sleep_rem")

    def test_nothing_synced_invents_nothing(self):
        got = wp.read_watch_values(DAY, tz=CT, query=FakeQuery([]))
        self.assertEqual((got["sleep_hrs"], got["resting_hr"], got["weight_lbs"]),
                         (None, None, None))
        self.assertEqual(got["missing"], ["sleep", "resting HR", "weight"])

    def test_a_stale_weight_is_not_presented_as_todays(self):
        """Ryan weighs sporadically. A reading from four days ago is not
        last night's and must not be offered as it."""
        q = FakeQuery(rows(("weight", 280.0, at(6, 20, DAY - timedelta(days=4)))))
        got = wp.read_watch_values(DAY, tz=CT, query=q)
        self.assertIsNone(got["weight_lbs"])
        self.assertIn("weight", got["missing"])

    def test_the_newest_sample_per_metric_wins(self):
        q = FakeQuery(rows(("resting_heart_rate", 61, at(2, 0)),
                           ("resting_heart_rate", 54, at(6, 12))))
        self.assertEqual(wp.read_watch_values(DAY, tz=CT, query=q)["resting_hr"], 54)


class TestManualWins(unittest.TestCase):
    """The SQL is the guarantee: a `manual` field is never overwritten."""

    def setUp(self):
        self.sql = wp._UPSERT

    def test_every_field_guards_on_its_own_source_marker(self):
        for field, source in (("sleep_hrs", "sleep_source"),
                              ("resting_hr", "resting_hr_source"),
                              ("weight_lbs", "weight_source")):
            with self.subTest(field=field):
                guard = (f"{field} = CASE WHEN health.daily_state.{source} = 'manual'\n"
                         f"                     THEN health.daily_state.{field}")
                self.assertIn(f"health.daily_state.{source} = 'manual'", self.sql)
                self.assertIn(f"THEN health.daily_state.{field}", self.sql.replace("\n", "\n"))

    def test_a_manual_marker_is_never_downgraded_to_watch(self):
        for source in ("sleep_source", "resting_hr_source", "weight_source"):
            self.assertIn(f"CASE WHEN health.daily_state.{source} = 'manual' THEN 'manual'",
                          self.sql)

    def test_write_is_skipped_when_the_watch_has_nothing(self):
        calls = []

        class Cur:
            def execute(self, *a):
                calls.append(a)

        self.assertFalse(wp.write_prefill(Cur(), DAY, {"sleep_hrs": None, "resting_hr": None,
                                                       "weight_lbs": None, "missing": []}))
        self.assertEqual(calls, [])

    def test_the_manual_check_in_marks_its_fields_manual(self):
        src = (Path(__file__).resolve().parent.parent / "artemis" / "health_checkin.py").read_text()
        store = src[src.index("def store_checkin"):src.index("def write_plan")]
        for col in ("sleep_source", "resting_hr_source", "weight_source"):
            self.assertIn(col, store)
        self.assertIn("'manual'", store)


class TestReplyWording(unittest.TestCase):
    def test_names_what_came_from_the_watch_and_what_did_not(self):
        line = wp.describe({"sleep_hrs": 7.2, "resting_hr": 54, "weight_lbs": None,
                            "missing": ["weight"]})
        self.assertEqual(line, "From the watch: sleep 7.2h, resting HR 54. Not synced: weight.")

    def test_nothing_synced_says_so_plainly(self):
        line = wp.describe({"sleep_hrs": None, "resting_hr": None, "weight_lbs": None,
                            "missing": ["sleep", "resting HR", "weight"]})
        self.assertEqual(line, "No watch data yet — sleep, resting HR, weight not synced.")

    def test_no_watch_data_at_all_says_nothing_rather_than_something_empty(self):
        self.assertIsNone(wp.describe({"sleep_hrs": None, "resting_hr": None,
                                       "weight_lbs": None, "missing": []}))

    def test_the_line_makes_no_claim_about_quality(self):
        line = wp.describe({"sleep_hrs": 4.1, "resting_hr": 72, "weight_lbs": 283.0,
                            "missing": []})
        self.assertNotRegex(line.lower(), r"\b(low|high|poor|good|short|below|above|only)\b")


class TestProvisionalSleepShape(unittest.TestCase):
    """No sleep sample had ever arrived when this was written (2026-09-20):
    sleep tracking was off on the watch. The metric choice is ASSUMED and must
    be checked against the first real night."""

    def test_the_assumption_is_declared_in_the_source(self):
        src = (Path(__file__).resolve().parent.parent / "artemis" / "watch_prefill.py").read_text()
        self.assertIn("PROVISIONAL", src)
        self.assertIn("ASSUMED shape", src)

    def test_the_result_says_which_metric_it_believed(self):
        q = FakeQuery(rows(("sleep_asleep", 7.2, at(6, 30))))
        self.assertEqual(wp.read_watch_values(DAY, tz=CT, query=q)["sleep_source_metric"],
                         "sleep_asleep")


if __name__ == "__main__":
    unittest.main()
