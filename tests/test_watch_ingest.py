"""WATCH-1 — the Health Auto Export payload parser and POST /api/health/ingest.

The payload shape is an ASSUMPTION until a real export lands, so these tests
pin the BEHAVIOUR that must hold whatever the shape turns out to be: nothing is
rejected for being unfamiliar, nothing is invented, and the watch key is
accepted only here.

Run:
    python3 tests/test_watch_ingest.py
"""

import os as _os; _os.environ["ARTEMIS_TEST_NO_DB"] = "1"  # TEST-DB-GUARD: never a real DB

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from knowledge import watch_payload as wp  # noqa: E402

# What Health Auto Export is documented to post. Assumed, not observed.
SAMPLE = {
    "data": {
        "metrics": [
            {"name": "resting_heart_rate", "units": "bpm",
             "data": [{"date": "2026-09-25 06:12:00 -0500", "qty": 54}]},
            {"name": "heart_rate_variability", "units": "ms",
             "data": [{"date": "2026-09-25 06:12:00 -0500", "qty": 42.5}]},
            {"name": "weight_body_mass", "units": "lb",
             "data": [{"date": "2026-09-25 06:20:00 -0500", "qty": 281.4}]},
            {"name": "sleep_analysis", "units": "hr",
             "data": [{"date": "2026-09-25 06:00:00 -0500",
                       "asleep": 7.2, "deep": 1.1, "rem": 1.6, "core": 4.5,
                       "awake": 0.4, "inBed": 7.9}]},
            {"name": "active_energy", "units": "kcal",
             "data": [{"date": "2026-09-25 12:00:00 -0500", "qty": 512}]},
            {"name": "basal_energy_burned", "units": "kcal",
             "data": [{"date": "2026-09-25 12:00:00 -0500", "qty": 1980}]},
        ],
        "workouts": [
            {"name": "Traditional Strength Training",
             "start": "2026-09-25 06:35:00 -0500", "end": "2026-09-25 07:15:00 -0500",
             "duration": 2400, "heartRateAvg": {"Avg": 112, "Max": 141},
             "activeEnergyBurned": {"qty": 310, "units": "kcal"}},
        ],
    }
}


class TestParsesWhatRyanAskedFor(unittest.TestCase):
    def test_every_requested_metric_is_recognised(self):
        got = {s["metric"] for s in wp.parse_samples(SAMPLE)}
        self.assertIn("resting_heart_rate", got)
        self.assertIn("hrv", got)
        self.assertIn("weight", got)
        self.assertIn("active_energy", got)
        self.assertIn("basal_energy", got)

    def test_sleep_expands_to_one_row_per_stage(self):
        stages = {s["metric"]: s["value"] for s in wp.parse_samples(SAMPLE)
                  if s["metric"].startswith("sleep_")}
        self.assertEqual(stages, {"sleep_asleep": 7.2, "sleep_deep": 1.1,
                                  "sleep_rem": 1.6, "sleep_core": 4.5,
                                  "sleep_awake": 0.4, "sleep_in_bed": 7.9})

    def test_timestamps_keep_their_own_offset(self):
        rhr = next(s for s in wp.parse_samples(SAMPLE) if s["metric"] == "resting_heart_rate")
        self.assertEqual(rhr["measured_at"].utcoffset().total_seconds(), -5 * 3600)
        self.assertEqual(rhr["measured_at"].astimezone(timezone.utc).hour, 11)

    def test_workout_fields(self):
        w = wp.parse_workouts(SAMPLE)[0]
        self.assertEqual(w["kind"], "Traditional Strength Training")
        self.assertEqual((w["duration_sec"], w["hr_avg"], w["hr_max"]), (2400, 112, 141))
        self.assertEqual(w["kcal"], 310)
        self.assertEqual(w["started_at"].hour, 6)


class TestForgiving(unittest.TestCase):
    """Nothing unfamiliar is rejected; nothing is invented."""

    def test_an_unknown_metric_is_stored_under_its_own_name(self):
        payload = {"data": {"metrics": [
            {"name": "blood_oxygen_saturation", "units": "%",
             "data": [{"date": "2026-09-25 06:00:00 -0500", "qty": 97}]}]}}
        rows = wp.parse_samples(payload)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["metric"], "blood_oxygen_saturation")
        self.assertEqual(rows[0]["value"], 97)

    def test_an_unreadable_sample_keeps_its_raw_and_a_null_value(self):
        payload = {"data": {"metrics": [
            {"name": "resting_heart_rate",
             "data": [{"date": "2026-09-25 06:00:00 -0500", "shape": "unexpected"}]}]}}
        row = wp.parse_samples(payload)[0]
        self.assertIsNone(row["value"])
        self.assertEqual(row["raw"], {"date": "2026-09-25 06:00:00 -0500", "shape": "unexpected"})

    def test_a_sample_with_no_readable_date_is_kept_but_flagged(self):
        payload = {"data": {"metrics": [
            {"name": "hrv", "data": [{"date": "not a date", "qty": 1}]}]}}
        rows = wp.parse_samples(payload)
        self.assertIsNone(rows[0]["measured_at"])
        self.assertEqual(wp.parse_counts(payload)["undated_samples"], 1)

    def test_empty_and_odd_payloads_do_not_raise(self):
        for payload in ({}, {"data": {}}, {"data": {"metrics": None, "workouts": None}},
                        {"data": {"metrics": ["not a dict"]}}):
            with self.subTest(payload=payload):
                self.assertEqual(wp.parse_samples(payload), [])
                self.assertEqual(wp.parse_workouts(payload), [])

    def test_several_date_formats_parse(self):
        for raw, hour in (("2026-09-25 06:12:00 -0500", 6),
                          ("2026-09-25T06:12:00-05:00", 6),
                          ("2026-09-25T11:12:00Z", 11),
                          ("2026-09-25", 0)):
            with self.subTest(raw=raw):
                self.assertEqual(wp.parse_date(raw).hour, hour)
        self.assertIsNone(wp.parse_date("yesterday"))

    def test_counts_report_what_arrived(self):
        c = wp.parse_counts(SAMPLE)
        self.assertEqual((c["metrics_in"], c["workouts_in"]), (6, 1))
        self.assertEqual(c["samples"], 11)          # 5 scalars + 6 sleep stages
        self.assertEqual(c["undated_samples"], 0)


class TestKeySeparation(unittest.TestCase):
    """The watch key is accepted on /ingest ONLY, and the display key is not
    accepted there — different dependencies, different secrets (HARDEN-1)."""

    def test_the_router_wires_two_distinct_dependencies(self):
        src = (Path(__file__).resolve().parent.parent / "api" / "app" / "routers"
               / "health.py").read_text()
        ingest = src[src.index('@router.post("/ingest"'):src.index('@router.get("/overview"')]
        self.assertIn("Depends(verify_watch_ingest_key)", ingest)
        self.assertNotIn("Depends(verify_health_api_key)", ingest)
        # and no other route uses the watch dependency
        self.assertEqual(src.count("Depends(verify_watch_ingest_key)"), 1)
        # the two checks read different secrets
        self.assertIn("get_watch_ingest_key", src)
        self.assertIn("get_health_api_key", src)

    def test_the_secret_helper_points_at_its_own_secret(self):
        src = (Path(__file__).resolve().parent.parent / "knowledge" / "secrets.py").read_text()
        self.assertIn("rdmis/dev/watch-ingest-key", src)
        fn = src[src.index("def get_watch_ingest_key"):src.index("def get_vault_repo")]
        self.assertNotIn("health-api-key", fn)


if __name__ == "__main__":
    unittest.main()
