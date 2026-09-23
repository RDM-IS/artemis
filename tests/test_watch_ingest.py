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


class TestDefensiveFilter(unittest.TestCase):
    """WATCH-2: only allow-listed metrics are stored, but nothing vanishes in
    silence — everything else is counted and named."""

    REAL_SHAPE = {"data": {"metrics": [
        {"name": "resting_heart_rate", "units": "bpm",
         "data": [{"date": "2026-09-19 06:12:00 -0500", "qty": 54, "source": "RAW"}]},
        {"name": "heart_rate", "units": "count/min",
         "data": [{"date": "2026-09-19 06:13:00 -0500", "Avg": 84, "Min": 80, "Max": 92,
                   "source": "RAW"}]},
        {"name": "step_count", "units": "count",
         "data": [{"date": "2026-09-19 06:14:00 -0500", "qty": 30, "source": "RAW|RIP"}]},
        {"name": "physical_effort", "units": "kcal/hr·kg",
         "data": [{"date": "2026-09-19 06:15:00 -0500", "qty": 3.2}]}]}}

    def test_only_wanted_metrics_are_storable(self):
        self.assertTrue(wp.is_wanted("resting_heart_rate"))
        self.assertTrue(wp.is_wanted("hrv"))
        self.assertTrue(wp.is_wanted("weight"))
        self.assertTrue(wp.is_wanted("active_energy"))
        self.assertTrue(wp.is_wanted("basal_energy"))
        self.assertTrue(wp.is_wanted("sleep_asleep"))
        for junk in ("heart_rate", "step_count", "physical_effort", "stair_speed_up",
                     "apple_stand_time", "walking_speed", "respiratory_rate"):
            self.assertFalse(wp.is_wanted(junk), junk)

    def test_counts_name_what_was_ignored(self):
        c = wp.parse_counts(self.REAL_SHAPE)
        self.assertEqual(c["stored_samples"], 1)          # resting_heart_rate only
        self.assertEqual(c["heart_rate_samples"], 1)      # held out separately
        self.assertEqual(c["hourly_samples"], 1)          # STEPS-HOURLY: steps are stored now
        self.assertEqual(c["ignored_samples"], 1)
        self.assertEqual(set(c["ignored_metrics"]), {"physical_effort"})

    def test_hr_min_max_and_device_are_captured(self):
        hr = next(r for r in wp.parse_samples(self.REAL_SHAPE) if r["metric"] == "heart_rate")
        self.assertEqual((hr["value"], hr["value_min"], hr["value_max"]), (84.0, 80.0, 92.0))
        self.assertEqual(hr["device"], "RAW")

    def test_a_workout_with_no_start_is_reported_not_dropped(self):
        payload = {"data": {"workouts": [
            {"name": "no start here"},
            {"name": "fine", "start": "2026-09-19 06:35:00 -0500"}]}}
        skipped = []
        got = wp.parse_workouts(payload, skipped)
        self.assertEqual(len(got), 1)
        self.assertEqual(len(skipped), 1)
        self.assertEqual(wp.parse_counts(payload)["workouts_without_a_readable_start"], 1)


class TestStepsHourly(unittest.TestCase):
    """STEPS-HOURLY (2026-09-21): step_count and apple_exercise_time become
    hourly totals. A push never ADDS to an hour: minutes merge by key, so an
    overlapping resend can't double-count and a partial push can't shrink it."""

    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("America/Chicago")   # a fixture zone, not app config

    @staticmethod
    def _payload(metric, points, source="RAW|RIP"):
        return {"data": {"metrics": [{"name": metric, "units": "count", "data": [
            {"date": d, "qty": q, "source": source} for d, q in points]}]}}

    def _buckets(self, payload):
        return wp.bucket_hourly(wp.parse_samples(payload), self.TZ)

    def test_both_metrics_are_hourly_and_nothing_else(self):
        self.assertTrue(wp.is_hourly("step_count"))
        self.assertTrue(wp.is_hourly("apple_exercise_time"))
        for other in ("active_energy", "physical_effort", "walking_running_distance"):
            self.assertFalse(wp.is_hourly(other), other)
        self.assertFalse(wp.is_wanted("step_count"))     # never into watch_sample

    def test_samples_bucket_by_local_hour(self):
        b = self._buckets(self._payload("step_count", [
            ("2026-09-22 06:05:00 -0500", 100), ("2026-09-22 06:59:00 -0500", 50),
            ("2026-09-22 07:00:00 -0500", 20)]))
        self.assertEqual(len(b), 2)
        six = next(v for (m, h), v in b.items() if h.hour == 6)
        self.assertEqual(sum(six["minutes"].values()), 150)
        self.assertEqual(six["local_date"].isoformat(), "2026-09-22")

    def test_hour_and_date_follow_the_active_timezone(self):
        # 23:30 local on the 21st is 04:30Z on the 22nd: it belongs to the 21st
        b = self._buckets(self._payload("step_count", [("2026-09-22T04:30:00+00:00", 10)]))
        (_, hour), v = next(iter(b.items()))
        self.assertEqual(v["local_date"].isoformat(), "2026-09-21")
        self.assertEqual((hour.hour, hour.minute), (23, 0))

    def test_an_overlapping_resend_does_not_double_count(self):
        first = self._buckets(self._payload("step_count", [
            ("2026-09-22 06:05:00 -0500", 100), ("2026-09-22 06:06:00 -0500", 40)]))
        again = self._buckets(self._payload("step_count", [
            ("2026-09-22 06:05:00 -0500", 100), ("2026-09-22 06:06:00 -0500", 40),
            ("2026-09-22 06:07:00 -0500", 10)]))
        key = next(iter(first))
        merged = wp.merge_minutes(first[key]["minutes"], again[key]["minutes"])
        self.assertEqual(wp.summarise_minutes(merged)["value"], 150)   # not 290

    def test_a_partial_push_cannot_shrink_the_hour(self):
        full = self._buckets(self._payload("step_count", [
            ("2026-09-22 06:05:00 -0500", 100), ("2026-09-22 06:40:00 -0500", 60)]))
        tail = self._buckets(self._payload("step_count", [("2026-09-22 06:40:00 -0500", 60)]))
        key = next(iter(full))
        merged = wp.merge_minutes(full[key]["minutes"], tail[key]["minutes"])
        self.assertEqual(wp.summarise_minutes(merged)["value"], 160)

    def test_a_revised_minute_replaces_itself(self):
        a = self._buckets(self._payload("step_count", [("2026-09-22 06:05:00 -0500", 30)]))
        b = self._buckets(self._payload("step_count", [("2026-09-22 06:05:00 -0500", 45)]))
        key = next(iter(a))
        merged = wp.merge_minutes(a[key]["minutes"], b[key]["minutes"])
        self.assertEqual(wp.summarise_minutes(merged)["value"], 45)

    def test_two_devices_in_one_minute_are_kept_and_counted_once(self):
        """Both samples are STORED and the minute is flagged, but the hour
        counts the minute once — the larger value (Ryan, 2026-09-22)."""
        watch = self._payload("step_count", [("2026-09-22 06:05:00 -0500", 30)], "RAW")
        phone = self._payload("step_count", [("2026-09-22 06:05:00 -0500", 28)], "RIP")
        watch["data"]["metrics"] += phone["data"]["metrics"]
        b = self._buckets(watch)
        minutes = next(iter(b.values()))["minutes"]
        s = wp.summarise_minutes(minutes)
        self.assertEqual(len(minutes), 2)                # nothing dropped
        self.assertEqual(s["overlap_minutes"], 1)        # still reported
        self.assertEqual(s["value"], 30)                 # counted once, the larger
        self.assertEqual(s["sample_count"], 2)
        self.assertEqual(s["devices"], ["RAW", "RIP"])

    def test_the_same_minute_under_both_markers_counts_once(self):
        """The real 9/21 shape: RAW 18.0 and RAW|RIP 18.0 at 06:08 is one set
        of steps described twice, not 36."""
        watch = self._payload("step_count", [("2026-09-21 01:08:00 -0500", 18.0)], "RAW")
        both = self._payload("step_count", [("2026-09-21 01:08:00 -0500", 18.0)], "RAW|RIP")
        watch["data"]["metrics"] += both["data"]["metrics"]
        s = wp.summarise_minutes(next(iter(self._buckets(watch).values()))["minutes"])
        self.assertEqual((s["value"], s["overlap_minutes"], s["sample_count"]), (18.0, 1, 2))

    def test_a_single_marker_minute_is_unchanged(self):
        one = self._payload("step_count", [("2026-09-22 06:05:00 -0500", 42),
                                           ("2026-09-22 06:06:00 -0500", 13)], "RAW|RIP")
        s = wp.summarise_minutes(next(iter(self._buckets(one).values()))["minutes"])
        self.assertEqual((s["value"], s["overlap_minutes"]), (55, 0))

    def test_exercise_minutes_dedupe_the_same_way(self):
        """The rule is per hourly metric keyed minute|device, not per metric
        name. apple_exercise_time has the same shape."""
        watch = self._payload("apple_exercise_time", [("2026-09-22 06:05:00 -0500", 1)], "RAW")
        both = self._payload("apple_exercise_time", [("2026-09-22 06:05:00 -0500", 1)], "RAW|RIP")
        watch["data"]["metrics"] += both["data"]["metrics"]
        s = wp.summarise_minutes(next(iter(self._buckets(watch).values()))["minutes"])
        self.assertEqual((s["value"], s["overlap_minutes"]), (1, 1))

    def test_summary_bounds_and_counts(self):
        b = self._buckets(self._payload("apple_exercise_time", [
            ("2026-09-22 06:05:00 -0500", 1), ("2026-09-22 06:30:00 -0500", 1)]))
        s = wp.summarise_minutes(next(iter(b.values()))["minutes"])
        self.assertEqual((s["value"], s["sample_count"], s["overlap_minutes"]), (2, 2, 0))
        self.assertEqual(s["first_at"].isoformat(), "2026-09-22T11:05:00+00:00")
        self.assertEqual(s["last_at"].isoformat(), "2026-09-22T11:31:00+00:00")

    def test_undated_or_valueless_samples_are_not_bucketed(self):
        b = self._buckets({"data": {"metrics": [{"name": "step_count", "data": [
            {"qty": 5}, {"date": "2026-09-22 06:05:00 -0500"}]}]}})
        self.assertEqual(b, {})

    def test_the_endpoint_upsert_is_a_merge_not_an_add(self):
        src = (Path(__file__).resolve().parent.parent / "api" / "app" / "routers"
               / "health.py").read_text()
        fn = src[src.index("def _upsert_hourly"):src.index('@router.post("/ingest"')]
        self.assertIn("FOR UPDATE", fn)
        self.assertIn("merge_minutes(previous", fn)
        self.assertNotIn("value + EXCLUDED.value", fn)
        self.assertNotIn("watch_hourly.value +", fn)


class TestEndpointGuards(unittest.TestCase):
    """The handler refuses early with a useful message instead of hanging to
    the 30 s ceiling (two timeouts, 2026-09-19)."""

    def test_the_limits_are_set_and_the_insert_is_batched(self):
        src = (Path(__file__).resolve().parent.parent / "api" / "app" / "routers"
               / "health.py").read_text()
        self.assertIn("MAX_SAMPLES_PER_REQUEST", src)
        self.assertIn("HTTP_413_REQUEST_ENTITY_TOO_LARGE", src)
        # one INSERT per chunk, not per row
        ingest = src[src.index("def _bulk_insert"):src.index('@router.get("/overview"')]
        self.assertIn("VALUES {', '.join(values)}", ingest)
        self.assertNotIn("for row in samples:\n        res = db.execute", ingest)

    def test_every_ingest_writes_an_audit_row(self):
        src = (Path(__file__).resolve().parent.parent / "api" / "app" / "routers"
               / "health.py").read_text()
        ingest = src[src.index('@router.post("/ingest"'):src.index('@router.get("/overview"')]
        self.assertIn("acos.audit_log", ingest)
        self.assertIn("'watch_ingest'", ingest)


class TestDeviceDecode(unittest.TestCase):
    """RAW = the watch, RIP = the iPhone, RAW|RIP = both (Ryan, 2026-09-20)."""

    def test_decode(self):
        self.assertEqual(wp.decode_device("RAW"), "watch")
        self.assertEqual(wp.decode_device("RIP"), "iphone")
        self.assertEqual(wp.decode_device("RAW|RIP"), "watch+iphone")
        self.assertEqual(wp.decode_device("rip"), "iphone")
        # an unknown marker is NOT guessed at; the raw string is still kept
        self.assertIsNone(wp.decode_device("PHONE9"))
        self.assertIsNone(wp.decode_device(None))

    def test_watch_is_preferred_where_it_matters(self):
        for m in ("resting_heart_rate", "hrv", "heart_rate", "sleep_asleep", "sleep_deep"):
            self.assertTrue(wp.prefers_watch(m), m)
        for m in ("weight", "active_energy", "basal_energy"):
            self.assertFalse(wp.prefers_watch(m), m)

    def test_the_endpoint_stores_both_forms_in_the_right_columns(self):
        """038 renamed 037's `device` to `device_raw` and added a decoded
        `device`; writing the raw string into the decoded column would be
        silent corruption."""
        src = (Path(__file__).resolve().parent.parent / "api" / "app" / "routers"
               / "health.py").read_text()
        ingest = src[src.index('@router.post("/ingest"'):src.index('@router.get("/overview"')]
        self.assertIn('"device_raw": row.get("device")', ingest)
        self.assertIn('"device": decode_device(row.get("device"))', ingest)
        for table in ("watch_heart_rate", "watch_sample"):
            stmt = ingest[ingest.index(f"INSERT INTO health.{table}"):]
            self.assertIn("device_raw", stmt[:400], table)


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
