"""WATCH-1 — the morning pre-fill, against the shape that actually arrived.

The rules under test, in the order they matter:
  1. a value Ryan typed is FINAL for that date, whatever the arrival order;
  2. no value is presented as today's unless it was measured today — or last
     night, for sleep. Nothing is carried forward (the 2026-09-21 bug: 9/20's
     resting HR was pre-filled as 9/21's, and a 9/22 read returned 9/21's
     sleep, resting HR and weight);
  3. the posts name their sources and say what is missing.

The sleep fixture is the first real night (9/20 -> 9/21), verbatim.

Run:
    python3 tests/test_watch_prefill.py
"""

import os as _os; _os.environ["ARTEMIS_TEST_NO_DB"] = "1"  # TEST-DB-GUARD: never a real DB

import sys
import unittest
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from knowledge import watch_payload as payload  # noqa: E402
from knowledge import watch_prefill as wp  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CT = ZoneInfo("America/Chicago")
DAY = date(2026, 9, 21)

# The first real night, exactly as Health Auto Export sent it (2026-09-21 03:52 CDT).
REAL_NIGHT = {
    "rem": 0.8844365243117014, "core": 4.074595115019215,
    "date": "2026-09-21 00:00:00 -0500", "deep": 0.7685048173533545,
    "awake": 0.6366028973460198, "inBed": 0, "asleep": 0, "source": "RAW",
    "inBedEnd": "2026-09-21 03:19:20 -0500", "sleepEnd": "2026-09-21 03:19:20 -0500",
    "inBedStart": "2026-09-20 20:57:29 -0500", "sleepStart": "2026-09-20 20:57:29 -0500",
    "totalSleep": 5.7275364566842715,
}


def at(h, m=0, day=DAY):
    return datetime.combine(day, time(h, m), tzinfo=CT)


def stored(sample: dict) -> list[dict]:
    """What the ingest writes for one sleep record: parsed rows + local_date."""
    got = payload.parse_samples({"data": {"metrics": [
        {"name": "sleep_analysis", "units": "hr", "data": [sample]}]}})
    return [{"metric": r["metric"], "value": r["value"], "measured_at": r["measured_at"],
             "local_date": r["measured_at"].astimezone(CT).date(), "raw": r["raw"],
             "device": "watch"} for r in got]


def sample(metric, value, when):
    return {"metric": metric, "value": value, "measured_at": when,
            "local_date": when.astimezone(CT).date(), "raw": {}, "device": "watch"}


class FakeCursor:
    """A psycopg2-shaped cursor: TUPLE rows plus description, like the box."""

    def __init__(self, rows):
        self.rows = rows
        self.description, self._out = None, []
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        s = " ".join(sql.split())
        if "metric LIKE 'sleep%%'" in s:
            lo, hi = params
            cols = ("metric", "value", "measured_at", "local_date", "raw")
            got = [r for r in self.rows if r["metric"].startswith("sleep")
                   and lo <= r["local_date"] <= hi]
        elif "metric = 'resting_heart_rate'" in s or "metric = 'weight'" in s:
            metric = "resting_heart_rate" if "resting_heart_rate" in s else "weight"
            cols = ("value",)
            got = sorted((r for r in self.rows if r["metric"] == metric
                          and r["local_date"] == params[0] and r["value"] is not None),
                         key=lambda r: ((r.get("device") == "watch"), r["measured_at"]),
                         reverse=True)[:1]
        else:
            cols, got = (), []
        self.description = [(c,) for c in cols]
        self._out = [tuple(r[c] for c in cols) for r in got]

    def fetchall(self):
        return self._out


class TestTheRealNight(unittest.TestCase):
    def test_parsed_rows_skip_the_false_zeros(self):
        metrics = {r["metric"]: r["value"] for r in stored(REAL_NIGHT)}
        self.assertNotIn("sleep_asleep", metrics)      # asleep: 0 = "no unspecified stage"
        self.assertNotIn("sleep_in_bed", metrics)      # inBed: 0 = "watch doesn't write it"
        self.assertEqual(set(metrics), {"sleep_total_sleep", "sleep_core", "sleep_deep",
                                        "sleep_rem", "sleep_awake"})

    def test_bounds_are_read_from_sleep_start_and_end(self):
        start, end = payload.sleep_bounds(REAL_NIGHT)
        self.assertEqual((start.astimezone(CT).strftime("%m-%d %H:%M"),
                          end.astimezone(CT).strftime("%m-%d %H:%M")),
                         ("09-20 20:57", "09-21 03:19"))

    def test_hours_one_decimal_from_the_total(self):
        got = wp.read_values(FakeCursor(stored(REAL_NIGHT)), DAY, CT)
        self.assertEqual(got["sleep_hrs"], 5.7)
        self.assertEqual(got["sleep_source_metric"], "sleep_total_sleep")
        self.assertEqual(got["sleep_end"].astimezone(CT).strftime("%H:%M"), "03:19")

    def test_the_total_equals_the_stages_it_replaces(self):
        rows = [r for r in stored(REAL_NIGHT) if r["metric"] != "sleep_total_sleep"]
        hours, which = wp.sleep_hours(rows)
        self.assertEqual(hours, 5.7)
        self.assertEqual(which, "sleep_core+sleep_deep+sleep_rem")

    def test_in_bed_and_awake_are_not_sleep(self):
        self.assertEqual(wp.sleep_hours([{"metric": "sleep_in_bed", "value": 7.9},
                                         {"metric": "sleep_awake", "value": 0.4}]),
                         (None, None))


class TestNothingCarriedForward(unittest.TestCase):
    """The 2026-09-21 bug, reproduced from the real rows."""

    def rows(self):
        return stored(REAL_NIGHT) + [
            sample("resting_heart_rate", 64, at(9, 24, DAY - timedelta(days=1))),
            sample("weight", 282.0, at(3, 47)),
        ]

    def test_yesterdays_resting_hr_is_not_todays(self):
        got = wp.read_values(FakeCursor(self.rows()), DAY, CT)
        self.assertIsNone(got["resting_hr"])
        self.assertEqual(got["missing"], ["resting HR"])
        self.assertEqual(got["weight_lbs"], 282.0)

    def test_the_next_morning_gets_nothing_from_this_one(self):
        got = wp.read_values(FakeCursor(self.rows()), DAY + timedelta(days=1), CT)
        self.assertEqual((got["sleep_hrs"], got["resting_hr"], got["weight_lbs"]),
                         (None, None, None))
        self.assertEqual(got["missing"], ["sleep", "resting HR", "weight"])

    def test_todays_resting_hr_is_used_once_it_syncs(self):
        rows = self.rows() + [sample("resting_heart_rate", 65, at(0, 1))]
        self.assertEqual(wp.read_values(FakeCursor(rows), DAY, CT)["resting_hr"], 65)

    def test_the_watch_wins_over_the_phone_for_resting_hr(self):
        phone = dict(sample("resting_heart_rate", 70, at(7, 0)), device="iphone")
        rows = [phone, sample("resting_heart_rate", 58, at(0, 1))]
        self.assertEqual(wp.read_values(FakeCursor(rows), DAY, CT)["resting_hr"], 58)

    def test_there_is_no_lookback_window_left(self):
        src = (ROOT / "knowledge" / "watch_prefill.py").read_text()
        self.assertNotIn("_WINDOW_HOURS", src)
        self.assertNotIn("measured_at BETWEEN", src)


class TestWhichNight(unittest.TestCase):
    def night(self, end_day, end_h, label_day, **extra):
        rec = {"date": f"{label_day.isoformat()} 00:00:00 -0500", "totalSleep": 7.0,
               "sleepStart": f"{(end_day - timedelta(days=1)).isoformat()} 22:00:00 -0500",
               "sleepEnd": f"{end_day.isoformat()} {end_h:02d}:00:00 -0500", **extra}
        return stored(rec)

    def test_sleep_end_decides_not_the_label(self):
        # labelled today, but it ended yesterday morning — not last night
        rows = self.night(DAY - timedelta(days=1), 6, DAY)
        self.assertIsNone(wp.read_values(FakeCursor(rows), DAY, CT)["sleep_hrs"])

    def test_a_night_labelled_yesterday_that_ended_this_morning_counts(self):
        rows = self.night(DAY, 5, DAY - timedelta(days=1))
        self.assertEqual(wp.read_values(FakeCursor(rows), DAY, CT)["sleep_hrs"], 7.0)

    def test_without_a_sleep_end_the_label_must_be_today(self):
        today = stored({"date": "2026-09-21 00:00:00 -0500", "totalSleep": 6.2})
        yday = stored({"date": "2026-09-20 00:00:00 -0500", "totalSleep": 8.0})
        self.assertEqual(wp.read_values(FakeCursor(today + yday), DAY, CT)["sleep_hrs"], 6.2)
        self.assertIsNone(wp.read_values(FakeCursor(yday), DAY, CT)["sleep_hrs"])


class TestLaterNightReplaces(unittest.TestCase):
    def test_a_later_end_replaces_a_partial_night(self):
        partial = dict(REAL_NIGHT, sleepEnd="2026-09-21 01:00:00 -0500", totalSleep=3.9)
        self.assertTrue(payload.is_later_night(REAL_NIGHT, partial))
        self.assertFalse(payload.is_later_night(partial, REAL_NIGHT))

    def test_a_resend_of_the_same_night_is_a_duplicate(self):
        self.assertFalse(payload.is_later_night(REAL_NIGHT, dict(REAL_NIGHT)))

    def test_no_end_never_replaces_but_is_replaced(self):
        bare = {"date": "2026-09-21 00:00:00 -0500", "totalSleep": 4.0}
        self.assertFalse(payload.is_later_night(bare, REAL_NIGHT))
        self.assertTrue(payload.is_later_night(REAL_NIGHT, bare))


class TestManualWins(unittest.TestCase):
    """The SQL is the guarantee: a `manual` field is never overwritten."""

    def test_every_field_guards_on_its_own_source_marker(self):
        for field, source in (("sleep_hrs", "sleep_source"),
                              ("resting_hr", "resting_hr_source"),
                              ("weight_lbs", "weight_source")):
            with self.subTest(field=field):
                self.assertIn(f"{field} = CASE WHEN health.daily_state.{source} = 'manual'",
                              " ".join(wp.UPSERT.split()))
                self.assertIn(f"THEN health.daily_state.{field}", wp.UPSERT)

    def test_a_manual_marker_is_never_downgraded_to_watch(self):
        for source in ("sleep_source", "resting_hr_source", "weight_source"):
            self.assertIn(f"CASE WHEN health.daily_state.{source} = 'manual' THEN 'manual'",
                          wp.UPSERT)

    def test_write_is_skipped_when_the_watch_has_nothing(self):
        cur = FakeCursor([])
        self.assertFalse(wp.write(cur, DAY, {"sleep_hrs": None, "resting_hr": None,
                                             "weight_lbs": None}))
        self.assertEqual(cur.executed, [])

    def test_the_manual_check_in_marks_its_fields_manual(self):
        src = (ROOT / "artemis" / "health_checkin.py").read_text()
        store = src[src.index("def store_checkin"):src.index("def write_plan")]
        for col in ("sleep_source", "resting_hr_source", "weight_source"):
            self.assertIn(col, store)
        self.assertIn("'manual'", store)


class TestWording(unittest.TestCase):
    def test_the_wake_line_names_values_span_and_what_is_missing(self):
        got = wp.read_values(FakeCursor(stored(REAL_NIGHT) + [sample("weight", 282.0, at(3, 47))]),
                             DAY, CT)
        self.assertEqual(wp.describe(got, CT),
                         "From the watch: sleep 5.7h (20:57–03:19), weight 282. "
                         "Not synced: resting HR.")

    def test_nothing_synced_says_so_plainly(self):
        got = wp.read_values(FakeCursor([]), DAY, CT)
        self.assertEqual(wp.describe(got, CT),
                         "No watch data yet — sleep, resting HR, weight not synced.")

    def test_the_line_makes_no_claim_about_quality(self):
        line = wp.describe({"sleep_hrs": 4.1, "resting_hr": 72, "weight_lbs": 283.0,
                            "missing": []})
        self.assertNotRegex(line.lower(), r"\b(low|high|poor|good|short|below|above|only)\b")

    def test_the_audit_view_keeps_the_source_metric_and_serialises(self):
        got = wp.audit_view(wp.read_values(FakeCursor(stored(REAL_NIGHT)), DAY, CT))
        self.assertEqual(got["sleep_source_metric"], "sleep_total_sleep")
        self.assertIsInstance(got["sleep_end"], str)


class TestWiring(unittest.TestCase):
    """Source checks: the pieces that only run against the real DB / Lambda."""

    def test_every_ingest_reruns_the_prefill_for_today_inside_a_savepoint(self):
        src = (ROOT / "api" / "app" / "routers" / "health.py").read_text()
        ingest = src[src.index("def post_ingest"):src.index("def get_overview")]
        self.assertIn("prefill_day(raw_cur, datetime.now(tz).date(), tz)", ingest)
        self.assertIn("SAVEPOINT watch_prefill", ingest)
        self.assertIn("ROLLBACK TO SAVEPOINT watch_prefill", ingest)
        self.assertIn('"prefill": prefill', ingest)

    def test_ingest_local_date_uses_the_active_timezone(self):
        src = (ROOT / "api" / "app" / "routers" / "health.py").read_text()
        ingest = src[src.index("def post_ingest"):src.index("def get_overview")]
        self.assertIn("ZoneInfo(_active_timezone(db))", ingest)
        self.assertNotIn("astimezone(CT)", ingest)

    def test_sleep_nights_are_replaced_not_just_inserted(self):
        src = (ROOT / "api" / "app" / "routers" / "health.py").read_text()
        ingest = src[src.index("def post_ingest"):src.index("def get_overview")]
        self.assertIn("is_later_night(", ingest)
        self.assertIn("replaced_sleep_nights", ingest)

    def test_the_wake_job_audits_which_metric_it_used(self):
        src = (ROOT / "artemis" / "watch_prefill.py").read_text()
        self.assertIn("'watch_prefill'", src.replace('"', "'"))
        self.assertIn("audit_view(values)", src)

    def test_the_wake_post_carries_the_watch_line(self):
        src = (ROOT / "artemis" / "wake.py").read_text()
        body = src[src.index("def build_wake_message"):]
        self.assertLess(body.index("_workout_section(plan)"), body.index("_watch_section()"))
        self.assertLess(body.index("_watch_section()"), body.index("_checkin_section(plan)"))


class TestWakeSection(unittest.TestCase):
    def test_the_line_is_rendered_and_a_db_failure_costs_only_the_line(self):
        from contextlib import contextmanager
        from unittest.mock import MagicMock, patch
        from artemis import wake

        @contextmanager
        def conn():
            yield MagicMock()

        with patch("knowledge.db.get_connection", conn), \
             patch("artemis.watch_prefill.today_line", return_value="From the watch: weight 282."):
            self.assertEqual(wake._watch_section(), ["", "From the watch: weight 282."])
        with patch("knowledge.db.get_connection", side_effect=RuntimeError("db down")), \
             self.assertLogs("artemis.wake", "ERROR"):
            self.assertEqual(wake._watch_section(), [])


if __name__ == "__main__":
    unittest.main()
