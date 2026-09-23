"""WATCH-1 workout match (knowledge.watch_match).

The fixture is the first real export, 2026-09-22: the eight workouts Health
Auto Export sent for 9/16 to 9/22, and the session_log timestamps of plans
103 to 109, copied from RDS as they were (UTC).

Run:
    python3 -m unittest tests.test_watch_match
"""
import os as _os; _os.environ["ARTEMIS_TEST_NO_DB"] = "1"  # TEST-DB-GUARD: never a real DB
import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from knowledge import watch_match as wm  # noqa: E402
from knowledge import watch_payload as wp  # noqa: E402

UTC = timezone.utc


def ts(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)


def W(wid, kind, day, start, end, dur):
    return {"workout_id": wid, "kind": kind, "local_date": date.fromisoformat(day),
            "started_at": ts(start), "ended_at": ts(end), "duration_sec": dur}


WORKOUTS = [
    W(10, "Functional Strength Training", "2026-09-16", "2026-09-16 10:20:44", "2026-09-16 10:54:10", 2006),
    W(9, "Functional Strength Training", "2026-09-18", "2026-09-18 10:16:50", "2026-09-18 10:56:36", 2386),
    W(8, "Outdoor Walk", "2026-09-19", "2026-09-19 23:55:31", "2026-09-20 00:19:00", 1409),
    W(7, "Yoga", "2026-09-20", "2026-09-20 19:28:54", "2026-09-20 20:02:18", 2003),
    W(6, "Traditional Strength Training", "2026-09-21", "2026-09-21 10:21:13", "2026-09-21 10:51:36", 1823),
    W(5, "Outdoor Walk", "2026-09-21", "2026-09-21 21:47:50", "2026-09-21 22:12:28", 1477),
    W(4, "Indoor Cycling", "2026-09-22", "2026-09-22 10:28:02", "2026-09-22 10:49:36", 1294),
    W(3, "Indoor Walk", "2026-09-22", "2026-09-22 10:49:47", "2026-09-22 10:58:02", 494),
]

PLANS = [
    {"plan_id": 103, "plan_date": date(2026, 9, 16), "session_type": "strength_a", "is_skipped": False},
    {"plan_id": 104, "plan_date": date(2026, 9, 17), "session_type": "rest_mobility", "is_skipped": False},
    {"plan_id": 105, "plan_date": date(2026, 9, 18), "session_type": "strength_b", "is_skipped": False},
    {"plan_id": 106, "plan_date": date(2026, 9, 19), "session_type": "recovery_flow", "is_skipped": False},
    {"plan_id": 107, "plan_date": date(2026, 9, 20), "session_type": "recovery_flow", "is_skipped": False},
    {"plan_id": 108, "plan_date": date(2026, 9, 21), "session_type": "strength_a", "is_skipped": False},
    {"plan_id": 109, "plan_date": date(2026, 9, 22), "session_type": "cardio_z2", "is_skipped": False},
]

_LOG_TIMES = {
    (103, "2026-09-16"): ["10:30:43", "10:32:49", "10:35:57", "10:37:16", "10:39:33", "10:40:30",
                          "10:41:42", "10:42:56", "10:43:52", "10:45:01", "10:47:20", "10:54:40",
                          "10:54:45"],
    (105, "2026-09-18"): ["10:23:17", "10:24:57", "10:26:42", "10:28:40", "10:30:37", "10:33:24",
                          "10:35:10", "10:41:13", "10:42:32", "10:43:46", "10:45:07", "10:46:14",
                          "10:48:34", "10:55:55", "10:56:00"],
    (106, "2026-09-19"): ["16:27:43"],
    (107, "2026-09-20"): ["19:28:59", "20:02:06"],
    (108, "2026-09-21"): ["10:29:28", "10:31:28", "10:33:19", "10:35:16", "10:36:37", "10:37:48",
                          "10:40:35", "10:42:07", "10:42:59", "10:44:03", "10:44:50", "10:51:13",
                          "10:51:15", "10:51:20"],
    (109, "2026-09-22"): ["10:48:48"],
}
LOGS = [{"plan_id": pid, "logged_at": ts(f"{day} {t}")}
        for (pid, day), times in _LOG_TIMES.items() for t in times]


def by_id(decisions):
    return {d["workout_id"]: d for d in decisions}


class TestFirstRealExport(unittest.TestCase):
    """The eight workouts of 2026-09-22, matched by hand before the build."""

    def test_every_decision(self):
        got = {wid: (d["plan_id"], d["outcome"])
               for wid, d in by_id(wm.decide(WORKOUTS, PLANS, LOGS)).items()}
        self.assertEqual(got, {
            10: (103, wm.MATCHED),          # 9/16 strength A, 13 rows
            9: (105, wm.MATCHED),           # 9/18 strength B, 15 rows
            8: (None, wm.KIND_MISMATCH),    # 9/19 evening walk on a flow day
            7: (107, wm.MATCHED),           # 9/20 yoga = recovery flow, 2 rows
            6: (108, wm.MATCHED),           # 9/21 strength A, 14 rows
            5: (None, wm.KIND_MISMATCH),    # 9/21 evening walk on a strength day
            4: (109, wm.MATCHED),           # 9/22 Z2 on the bike; summary at 10:48:48
            3: (None, wm.KIND_MISMATCH),    # 9/22 walk: activity, never a session
        })

    def test_no_walk_ever_matches(self):
        """WALK-RETIRE: every walk in the real export stays unattached — the
        two evening ones and this morning's, which followed the bike by 11 s."""
        got = by_id(wm.decide(WORKOUTS, PLANS, LOGS))
        for wid in (8, 5, 3):
            with self.subTest(workout=wid):
                self.assertEqual((got[wid]["plan_id"], got[wid]["outcome"]),
                                 (None, wm.KIND_MISMATCH))

    def test_the_921_evidence(self):
        """The design's check: 9/21 05:21-05:54 CDT, ~217 kcal, ~104 avg HR."""
        d = by_id(wm.decide(WORKOUTS, PLANS, LOGS))[6]
        self.assertEqual((d["plan_id"], d["detail"]), (108, "14 log row(s) in window"))

    def test_decisions_keep_input_order(self):
        self.assertEqual([d["workout_id"] for d in wm.decide(WORKOUTS, PLANS, LOGS)],
                         [w["workout_id"] for w in WORKOUTS])


class TestRules(unittest.TestCase):
    def _one(self, workout, plans=None, logs=None):
        return wm.decide([workout], plans if plans is not None else PLANS,
                         logs if logs is not None else LOGS)[0]

    def test_the_window_runs_ten_minutes_past_the_end(self):
        w = WORKOUTS[6]                                   # cycling, ends 10:49:36
        for offset, want in ((timedelta(minutes=10), wm.MATCHED),
                             (timedelta(minutes=10, seconds=1), wm.NO_LOGGED_ROWS)):
            with self.subTest(offset=offset):
                logs = [{"plan_id": 109, "logged_at": w["ended_at"] + offset}]
                self.assertEqual(self._one(w, logs=logs)["outcome"], want)

    def test_a_row_before_the_start_does_not_count(self):
        w = WORKOUTS[6]
        logs = [{"plan_id": 109, "logged_at": w["started_at"] - timedelta(seconds=1)}]
        self.assertEqual(self._one(w, logs=logs)["outcome"], wm.NO_LOGGED_ROWS)

    def test_never_attached_by_date_alone(self):
        """Right day, right kind, but the sets are hours away: unmatched."""
        w = WORKOUTS[4]                                   # 9/21 strength
        logs = [{"plan_id": 108, "logged_at": ts("2026-09-21 18:00:00")}]
        d = self._one(w, logs=logs)
        self.assertEqual((d["plan_id"], d["outcome"]), (None, wm.NO_LOGGED_ROWS))

    def test_no_plan_and_skipped_plan(self):
        w = dict(WORKOUTS[4], local_date=date(2026, 9, 30))
        self.assertEqual(self._one(w)["outcome"], wm.NO_PLAN)
        skipped = [dict(PLANS[5], is_skipped=True)]
        self.assertEqual(self._one(WORKOUTS[4], plans=skipped)["outcome"], wm.PLAN_SKIPPED)

    def test_no_end_uses_duration(self):
        w = dict(WORKOUTS[6], ended_at=None)              # 1294 s from 10:28:02
        logs = [{"plan_id": 109, "logged_at": ts("2026-09-22 10:59:30")}]  # end+9:54
        self.assertEqual(self._one(w, logs=logs)["outcome"], wm.MATCHED)

    def test_most_rows_wins_the_rest_are_reported(self):
        a = W(1, "Indoor Cycling", "2026-09-22", "2026-09-22 10:00:00", "2026-09-22 10:20:00", 1200)
        b = W(2, "Elliptical", "2026-09-22", "2026-09-22 10:30:00", "2026-09-22 10:40:00", 600)
        logs = [{"plan_id": 109, "logged_at": ts(t)} for t in (
            "2026-09-22 10:05:00", "2026-09-22 10:10:00", "2026-09-22 10:35:00")]
        got = by_id(wm.decide([a, b], PLANS, logs))
        self.assertEqual((got[1]["plan_id"], got[1]["outcome"]), (109, wm.MATCHED))
        self.assertEqual((got[2]["plan_id"], got[2]["outcome"]), (None, wm.OUTRANKED))

    def test_a_tie_matches_neither(self):
        a = W(1, "Indoor Cycling", "2026-09-22", "2026-09-22 10:00:00", "2026-09-22 10:20:00", 1200)
        b = W(2, "Elliptical", "2026-09-22", "2026-09-22 10:30:00", "2026-09-22 10:40:00", 600)
        logs = [{"plan_id": 109, "logged_at": ts(t)}
                for t in ("2026-09-22 10:05:00", "2026-09-22 10:35:00")]
        got = by_id(wm.decide([a, b], PLANS, logs))
        self.assertEqual({d["outcome"] for d in got.values()}, {wm.TIED})
        self.assertEqual({d["plan_id"] for d in got.values()}, {None})

    def test_kind_gate(self):
        cases = {
            ("Traditional Strength Training", "strength_a"): True,
            ("Functional Strength Training", "strength_c"): True,
            ("Traditional Strength Training", "cardio_z2"): False,
            ("Yoga", "recovery_flow"): True,
            ("Flexibility", "recovery_flow"): True,
            ("Yoga", "strength_b"): False,
            # WALK-RETIRE (2026-09-22): a walk is activity, never a session
            ("Outdoor Walk", "cardio_z2"): False,
            ("Indoor Walk", "cardio_z2"): False,
            ("Outdoor Walk", "recovery_flow"): False,
            ("Outdoor Walk", "strength_a"): False,
            ("Indoor Cycling", "cardio_z2"): True,
            ("Elliptical", "cardio_z2"): True,
            ("Swimming", "cardio_z2"): False,         # not in the approved list
            ("", "cardio_z2"): False,
        }
        for (kind, st), want in cases.items():
            with self.subTest(kind=kind, st=st):
                self.assertIs(wm.kind_fits(kind, st), want)


class FakeCursor:
    """Answers rematch's three SELECTs and records every statement."""

    def __init__(self, workouts, plans, logs):
        self.tables = {"watch_workout": workouts, "plan": plans, "session_log": logs}
        self.statements, self._rows, self.description = [], [], None

    def execute(self, sql, params=None):
        self.statements.append((sql, params))
        if sql.startswith("SELECT"):
            if "FROM health.watch_workout" in sql:
                rows = [w for w in self.tables["watch_workout"] if w["local_date"] in params["days"]]
            elif "FROM health.plan" in sql:
                rows = [p for p in self.tables["plan"] if p["plan_date"] in params["days"]]
            else:
                rows = [r for r in self.tables["session_log"] if r["plan_id"] in params["ids"]]
            self._rows = [dict(r) for r in rows]
            self.description = [(k,) for k in (rows[0] if rows else {})]

    def fetchall(self):
        return self._rows


class TestRematch(unittest.TestCase):
    def test_writes_only_changed_plan_ids_and_only_watch_workout(self):
        stored = [dict(w, plan_id=None) for w in WORKOUTS[6:]]           # 9/22's two
        stored[0]["plan_id"] = 109                                       # already right
        cur = FakeCursor(stored, PLANS, LOGS)
        result = wm.rematch(cur, [date(2026, 9, 22)])
        self.assertEqual(result["changed"], 0)
        self.assertEqual([m["workout_id"] for m in result["matched"]], [4])
        self.assertEqual([(u["workout_id"], u["outcome"]) for u in result["unmatched"]],
                         [(3, wm.KIND_MISMATCH)])
        writes = [s for s, _ in cur.statements if not s.startswith("SELECT")]
        self.assertEqual(writes, [])

    def test_a_match_that_is_no_longer_true_is_cleared(self):
        stored = [dict(WORKOUTS[7], plan_id=109)]                        # walk, wrongly set
        cur = FakeCursor(stored, PLANS, LOGS)
        result = wm.rematch(cur, [date(2026, 9, 22)])
        self.assertEqual(result["changed"], 1)
        writes = [(s, p) for s, p in cur.statements if not s.startswith("SELECT")]
        self.assertEqual(len(writes), 1)
        self.assertTrue(writes[0][0].startswith("UPDATE health.watch_workout SET plan_id"))
        self.assertEqual(writes[0][1], {"plan_id": None, "id": 3})

    def test_no_days_or_no_workouts_touch_nothing(self):
        cur = FakeCursor([], PLANS, LOGS)
        self.assertEqual(wm.rematch(cur, [])["changed"], 0)
        self.assertEqual(cur.statements, [])
        self.assertEqual(wm.rematch(cur, [date(2026, 9, 30)])["matched"], [])
        self.assertEqual(len(cur.statements), 1)                         # the workout SELECT only


class TestRealRecordShape(unittest.TestCase):
    """The first real record (9/21 strength, slimmed): heart rate arrives as
    heartRate {avg, max, min} of {qty, units}, and duration is seconds."""
    REAL = {"data": {"workouts": [{
        "name": "Traditional Strength Training",
        "start": "2026-09-21 05:21:13 -0500", "end": "2026-09-21 05:51:36 -0500",
        "duration": 1823.0370080471039,
        "heartRate": {"avg": {"qty": 104.55535378599895, "units": "bpm"},
                      "max": {"qty": 125.00000000000001, "units": "bpm"},
                      "min": {"qty": 68, "units": "bpm"}},
        "avgHeartRate": {"qty": 104.55535378599895, "units": "bpm"},
        "maxHeartRate": {"qty": 125.00000000000001, "units": "bpm"},
        "activeEnergyBurned": {"qty": 215.68535605103483, "units": "kcal"},
        "totalEnergy": {"qty": 297.7806147225701, "units": "kcal"},
    }]}}

    def test_parses_as_stored(self):
        w = wp.parse_workouts(self.REAL)[0]
        self.assertEqual((w["duration_sec"], w["hr_avg"], w["hr_max"]), (1823, 104, 125))
        self.assertAlmostEqual(float(w["kcal"]), 215.685, places=2)      # active, not total
        self.assertAlmostEqual((w["ended_at"] - w["started_at"]).total_seconds(),
                               w["duration_sec"], delta=1)


class TestWiring(unittest.TestCase):
    ROOT = Path(__file__).resolve().parent.parent

    def test_the_ingest_matches_in_a_savepoint_and_audits_it(self):
        src = (self.ROOT / "api" / "app" / "routers" / "health.py").read_text()
        ingest = src[src.index('@router.post("/ingest"'):src.index('@router.get("/overview"')]
        self.assertIn("from knowledge.watch_match import rematch", ingest)
        self.assertIn('SAVEPOINT watch_match"', ingest)
        self.assertIn("ROLLBACK TO SAVEPOINT watch_match", ingest)
        self.assertIn('"workout_match": workout_match', ingest)
        # the match runs before the audit row is written and before commit
        self.assertLess(ingest.index("rematch(raw_cur"), ingest.index("db.commit()"))

    def test_the_box_job_is_registered_and_silent(self):
        src = (self.ROOT / "artemis" / "scheduler.py").read_text()
        self.assertIn('id="watch_workout_match"', src)
        job = src[src.index("def job_watch_workout_match"):src.index("def job_pain_pattern_recompute")]
        self.assertNotIn("_post(", job)
        self.assertNotIn("post_message", job)
        self.assertIn("rematch(cur", job)


if __name__ == "__main__":
    unittest.main()
