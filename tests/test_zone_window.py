"""ZONE-WINDOW — when did the session run, for its zone breakdown.

Every timestamp here is synthetic (June 2026, before any watch data existed),
never a real session's (PUBLIC-FIXTURES). The SHAPES are the real ones found on
2026-11-10: a row ladder POSTs all its 240 s rounds at Finish, each carrying
its own started_at/ended_at, beside a session_summary that spans the session.
"""
import os as _os; _os.environ["ARTEMIS_TEST_NO_DB"] = "1"  # TEST-DB-GUARD
import unittest
from datetime import datetime, timedelta, timezone

from knowledge import zone_window as zw

T0 = datetime(2026, 6, 2, 9, 0, 0, tzinfo=timezone.utc)
FINISH = T0 + timedelta(minutes=31)


def m(n, s=0):
    return T0 + timedelta(minutes=n, seconds=s)


def ladder(with_summary=True, with_times=True):
    """Five 240 s rounds every 5:15, all logged at FINISH, plus a summary."""
    rows = []
    for i in range(5):
        st = T0 + timedelta(seconds=10 + 315 * i)
        rows.append({"log_type": "cardio_block", "duration_sec": 240,
                     "logged_at": FINISH + timedelta(milliseconds=100 * i),
                     "started_at": st if with_times else None,
                     "ended_at": st + timedelta(seconds=240) if with_times else None})
    if with_summary:
        rows.append({"log_type": "session_summary", "duration_sec": 1860,
                     "logged_at": FINISH,
                     "started_at": T0 if with_times else None,
                     "ended_at": FINISH if with_times else None})
    return rows


def workout(kind, start, end, dur=None):
    return {"kind": kind, "started_at": start, "ended_at": end, "duration_sec": dur}


class TheBugThisFixes(unittest.TestCase):
    def test_a_ladder_is_not_scored_over_the_four_minutes_after_its_last_round(self):
        """The 2026-11-10 bug: the old rule took the longest cardio_block (240 s)
        and ended it at its logged_at (Finish), so the window was
        [FINISH - 4 min, FINISH] -- after the work, not during it."""
        start, end, src = zw.session_window(ladder(), [])
        self.assertEqual((start, end), (T0, FINISH))
        self.assertEqual(src, zw.SPAN_SOURCE)
        self.assertGreater((end - start).total_seconds(), 240 * 5)

    def test_rounds_alone_still_span_the_session(self):
        start, end, _ = zw.session_window(ladder(with_summary=False), [])
        self.assertEqual(start, T0 + timedelta(seconds=10))
        self.assertEqual(end, T0 + timedelta(seconds=10 + 315 * 4 + 240))


class TheWatchWorkoutWins(unittest.TestCase):
    def test_an_overlapping_workout_is_the_window(self):
        w = workout("Rowing", T0 - timedelta(seconds=20), FINISH + timedelta(minutes=14))
        start, end, src = zw.session_window(ladder(), [w])
        self.assertEqual((start, end), (w["started_at"], w["ended_at"]))
        self.assertEqual(src, "watch workout (Rowing)")

    def test_two_workouts_on_one_session_are_joined(self):
        """Cycle then run under one plan: one window across both."""
        a = workout("Indoor Cycling", m(0), m(16))
        b = workout("Indoor Run", m(16, 10), m(30))
        start, end, src = zw.session_window(ladder(), [a, b])
        self.assertEqual((start, end), (m(0), m(30)))
        self.assertIn("Indoor Cycling", src)
        self.assertIn("Indoor Run", src)

    def test_a_walk_never_stretches_the_window(self):
        """PB-009: a walk is daily life. A walk-down after the row must not
        drag twenty minutes of strolling into the breakdown."""
        row = workout("Rowing", m(0), m(30))
        walk = workout("Outdoor Walk", m(30, 30), m(50))
        start, end, _ = zw.session_window(ladder(), [row, walk])
        self.assertEqual((start, end), (m(0), m(30)))

    def test_only_a_walk_overlapping_leaves_the_logged_span(self):
        walk = workout("Indoor Walk", m(5), m(20))
        self.assertEqual(zw.session_window(ladder(), [walk])[2], zw.SPAN_SOURCE)

    def test_a_workout_elsewhere_in_the_day_does_not_match(self):
        """A morning row and an evening strength session share a plan_date;
        overlap in TIME is the test, never the date."""
        evening = workout("Traditional Strength Training",
                          T0 + timedelta(hours=11), T0 + timedelta(hours=12))
        self.assertEqual(zw.session_window(ladder(), [evening])[2], zw.SPAN_SOURCE)

    def test_touching_is_not_overlapping(self):
        before = workout("Rowing", T0 - timedelta(minutes=30), T0)
        self.assertEqual(zw.session_window(ladder(), [before])[2], zw.SPAN_SOURCE)

    def test_a_workout_with_no_end_uses_its_duration(self):
        w = workout("Rowing", m(0), None, dur=1800)
        start, end, _ = zw.session_window(ladder(), [w])
        self.assertEqual((start, end), (m(0), m(30)))

    def test_a_workout_with_neither_end_nor_duration_is_ignored(self):
        w = workout("Rowing", m(0), None)
        self.assertEqual(zw.session_window(ladder(), [w])[2], zw.SPAN_SOURCE)

    def test_no_logs_means_no_window_even_with_a_workout(self):
        """A workout alone is not a session: nothing is attached by time alone
        when nothing was logged (the same line WATCH-1 holds)."""
        w = workout("Rowing", m(0), m(30))
        self.assertEqual(zw.session_window([], [w]), (None, None, "no logs"))


class LegacyRowsWithoutTimes(unittest.TestCase):
    """Rows logged before migration 055 have no started_at/ended_at."""

    def test_the_summary_duration_beats_a_round(self):
        start, end, src = zw.session_window(ladder(with_times=False), [])
        self.assertEqual((start, end), (FINISH - timedelta(seconds=1860), FINISH))
        self.assertEqual(src, "session_summary duration")

    def test_a_lone_block_still_works(self):
        """Pre-ladder: one block per session, POSTed at Finish."""
        rows = [{"log_type": "cardio_block", "duration_sec": 1800, "logged_at": m(30)}]
        self.assertEqual(zw.session_window(rows, [])[:2], (m(0), m(30)))

    def test_a_strength_set_duration_is_not_a_session(self):
        rows = [{"log_type": "strength_set", "duration_sec": 40, "logged_at": m(3)},
                {"log_type": "strength_set", "duration_sec": 40, "logged_at": m(25)}]
        start, end, src = zw.session_window(rows, [])
        self.assertEqual((start, end, src), (m(3), m(25), "first..last logged_at"))

    def test_one_row_with_no_duration_is_no_window(self):
        rows = [{"log_type": "session_summary", "duration_sec": None, "logged_at": m(3)}]
        self.assertIsNone(zw.session_window(rows, [])[0])

    def test_zero_length_times_fall_through_to_the_duration(self):
        """A 1 s tap-through round with started == ended is not a span."""
        rows = [{"log_type": "cardio_block", "duration_sec": 1800, "logged_at": m(30),
                 "started_at": m(29), "ended_at": m(29)}]
        self.assertEqual(zw.session_window(rows, [])[2], "cardio_block duration")


class _Cur:
    """Answers recompute_recent's reads and records its writes. Dict rows."""
    description = ()

    def __init__(self, plan_ids, logs, workouts, samples):
        self.plan_ids, self.logs, self.workouts, self.samples = \
            plan_ids, logs, workouts, samples
        self.writes, self.selects, self._res = [], [], []
        self.final, self.final_reads = set(), []

    def execute(self, sql, params=None):
        if sql.lstrip().startswith("INSERT"):
            self.writes.append(params)
            self._res = []
        elif "SELECT DISTINCT p.plan_id" in sql:
            self.selects.append(params)
            self._res = [{"plan_id": p} for p in self.plan_ids]
        elif "FROM health.session_hr_zones" in sql:
            self.final_reads.append(params)
            self._res = [{"plan_id": p} for p in self.final if p in params[0]]
        elif "FROM health.session_log" in sql:
            self._res = self.logs
        elif "FROM health.watch_workout" in sql:
            self._res = self.workouts
        elif "FROM health.watch_heart_rate" in sql:
            self._res = [r for r in self.samples if params[0] <= r["measured_at"] <= params[1]]
        else:
            raise AssertionError(f"unexpected SQL {sql[:60]!r}")

    def fetchall(self):
        return self._res


class TheIngestRecompute(unittest.TestCase):
    """The Finish POST scores a session before the watch syncs; the ingest's
    recompute is the second look. Synthetic samples (112 bpm, every 10 s)."""

    def setUp(self):
        from knowledge import hr_zones
        self.hz = hr_zones
        self.today = T0.date()
        self.row = workout("Rowing", m(0), m(30))
        self.samples = [{"measured_at": m(0) + timedelta(seconds=10 * i), "bpm": 112}
                        for i in range(181)]

    def test_a_no_samples_session_becomes_ok_once_the_samples_exist(self):
        cur = _Cur([7], ladder(), [self.row], self.samples)
        out = self.hz.recompute_recent(cur, {self.today}, self.today)
        self.assertEqual(len(cur.writes), 1)
        self.assertEqual(out["recomputed"][0]["status"], "ok")
        self.assertTrue(out["recomputed"][0]["window_source"].startswith("watch workout"))
        params = cur.writes[0]
        self.assertEqual((params[1], params[2]), (m(0), m(30)))

    def test_an_old_day_in_the_push_is_not_backfilled(self):
        """A re-export carrying last month must not quietly rewrite it --
        scripts/backfill_hr_zones.py is the explicit path for history."""
        cur = _Cur([7], ladder(), [self.row], self.samples)
        out = self.hz.recompute_recent(
            cur, {self.today - timedelta(days=self.hz.RECOMPUTE_DAYS)}, self.today)
        self.assertEqual(out, {"days": [], "recomputed": []})
        self.assertEqual(cur.selects, [])
        self.assertEqual(cur.writes, [])

    def test_the_candidate_query_reads_only_finishers(self):
        cur = _Cur([], [], [], [])
        self.hz.recompute_recent(cur, {self.today}, self.today)
        days, finishers = cur.selects[0]
        self.assertEqual(days, [self.today])
        self.assertEqual(sorted(finishers), ["cardio_block", "session_summary"])

    def test_a_final_row_is_left_alone(self):
        """'ok' over a watch-workout window is final; recomputing it every push
        would only churn computed_at."""
        cur = _Cur([7, 8], ladder(), [self.row], self.samples)
        cur.final = {7}
        out = self.hz.recompute_recent(cur, {self.today}, self.today)
        self.assertEqual([r["plan_id"] for r in out["recomputed"]], [8])
        self.assertEqual(cur.final_reads[0][1], "watch workout%")

    def test_a_failed_read_raises_rather_than_storing_a_default(self):
        """FAIL-CLOSED-RESOLVERS: the caller's savepoint rolls this back."""
        cur = _Cur([7], ladder(), [self.row], self.samples)
        real = cur.execute

        def boom(sql, params=None):
            if "watch_workout" in sql:
                raise RuntimeError("read failed")
            return real(sql, params)
        cur.execute = boom
        with self.assertRaises(RuntimeError):
            self.hz.recompute_recent(cur, {self.today}, self.today)
        self.assertEqual(cur.writes, [])


class TheWiring(unittest.TestCase):
    def test_the_ingest_recomputes_in_a_savepoint_before_commit(self):
        from pathlib import Path
        src = (Path(__file__).resolve().parent.parent / "api" / "app" / "routers"
               / "health.py").read_text()
        ingest = src[src.index('@router.post("/ingest"'):src.index('@router.get("/overview"')]
        self.assertIn("from knowledge.hr_zones import recompute_recent", ingest)
        self.assertIn('SAVEPOINT zone_recompute"', ingest)
        self.assertIn("ROLLBACK TO SAVEPOINT zone_recompute", ingest)
        self.assertIn('"zone_recompute": zone_recompute', ingest)
        self.assertLess(ingest.index("recompute_recent(\n"), ingest.index("db.commit()"))

    def test_the_finish_post_uses_the_shared_rule(self):
        from pathlib import Path
        src = (Path(__file__).resolve().parent.parent / "api" / "app" / "routers"
               / "health.py").read_text()
        body = src[src.index("def _store_zone_minutes"):src.index("class LogExerciseIn")]
        self.assertIn("_zw.session_window(", body)
        self.assertNotIn('best["logged_at"]', body)


if __name__ == "__main__":
    unittest.main()
