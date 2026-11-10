"""STATUS-3 — the redesigned Status screen's reads: trend series, the strength
volume tile, the nutrition plan comparison and the 7-day grid.

PUBLIC-FIXTURES: every number here is synthetic and outside his real ranges
(bodyweight ~180 lb, intake ~1,500 kcal, tiny lifted volumes) while staying
legal for the code that reads it.
"""
from __future__ import annotations

import os as _os; _os.environ["ARTEMIS_TEST_NO_DB"] = "1"  # TEST-DB-GUARD: never a real DB

import unittest
from datetime import date, datetime, timedelta, timezone

from tests.lambda_api.test_overview import Base, FakeSession, _Result

TODAY = date(2026, 11, 10)


class ScriptDb:
    """A db that answers by SQL substring. A value that is an Exception raises."""

    def __init__(self, answers: dict[str, object]):
        self.answers = answers
        self.seen: list[str] = []

    def execute(self, clause, params=None):
        sql = " ".join(str(clause).split())
        self.seen.append(sql)
        for needle, rows in self.answers.items():
            if needle in sql:
                if isinstance(rows, Exception):
                    raise rows
                return _Result(list(rows))
        return _Result([])


class TestRolling(unittest.TestCase):
    def setUp(self):
        from api.app.routers.health import rolling_7d
        self.rolling = rolling_7d

    def test_mean_skips_missing_days_and_drops_empty_windows(self):
        daily = {TODAY - timedelta(days=2): 180.0, TODAY: 182.0}
        pts = self.rolling(daily, TODAY, mode="mean", days=10)
        # Days before the first reading have an empty window: no point, not 0.
        self.assertEqual([p.date for p in pts], [TODAY - timedelta(days=2),
                                                 TODAY - timedelta(days=1), TODAY])
        self.assertEqual(pts[-1].value, 181.0)       # (180 + 182) / 2, not / 7
        self.assertEqual(pts[0].value, 180.0)

    def test_total_counts_a_day_without_a_session_as_zero(self):
        daily = {TODAY - timedelta(days=8): 30.0, TODAY - timedelta(days=1): 20.0}
        pts = self.rolling(daily, TODAY, mode="total", days=10)
        self.assertEqual(len(pts), 10)                # every day has a point
        self.assertEqual(pts[-1].value, 20.0)         # day -8 has left the window
        self.assertEqual(pts[0].value, 0.0)           # day -9: nothing yet
        self.assertEqual(pts[1].value, 30.0)          # day -8 itself

    def test_thirty_points_ending_today(self):
        pts = self.rolling({}, TODAY, mode="total")
        self.assertEqual(len(pts), 30)
        self.assertEqual(pts[-1].date, TODAY)
        self.assertEqual(pts[0].date, TODAY - timedelta(days=29))

    def test_unknown_mode_is_refused(self):
        with self.assertRaises(ValueError):
            self.rolling({}, TODAY, mode="avg")


class TestNutritionPlan(unittest.TestCase):
    def rows(self):
        return [
            # outside the 7-day tile window, inside the trend read
            {"logged_date": TODAY - timedelta(days=20), "k": 1400, "p": 90, "f": 20, "n": 3},
            {"logged_date": TODAY - timedelta(days=1), "k": 1500, "p": 100, "f": 25, "n": 4},
            {"logged_date": TODAY, "k": 1600, "p": 110, "f": 30, "n": 5},
        ]

    def test_reads_the_live_store_and_the_plan(self):
        from api.app.routers.health import _nutrition
        db = ScriptDb({"nutrition.day_totals": self.rows(),
                       "FROM nutrition.target": [{"kcal": 1550, "set_by": "test",
                                                  "provisional": True}]})
        goal, detail = _nutrition(db, TODAY)
        self.assertTrue(goal.section.ok and detail.section.ok)
        self.assertFalse(any("health.nutrition_log" in s for s in db.seen),
                         "the dead table must not be read")
        self.assertEqual(goal.days_logged, 2)              # day -20 is not in the 7
        self.assertEqual(goal.avg_kcal_7d, 1550.0)
        self.assertEqual((goal.plan_kcal, goal.plan_set_by, goal.plan_provisional),
                         (1550, "test", True))
        self.assertEqual(len(detail.days), 7)
        self.assertEqual(goal.trend_7d[-1].value, 1550.0)
        # The day -20 reading reaches the trend, on its own window.
        first = [p for p in goal.trend_7d if p.date == TODAY - timedelta(days=20)]
        self.assertEqual(first[0].value, 1400.0)

    def test_it_never_writes(self):
        from api.app.routers.health import _nutrition
        db = ScriptDb({"nutrition.day_totals": self.rows()})
        _nutrition(db, TODAY)
        for s in db.seen:
            self.assertFalse(s.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")), s)

    def test_no_plan_row_is_null_not_zero(self):
        from api.app.routers.health import _nutrition
        goal, _ = _nutrition(ScriptDb({"nutrition.day_totals": self.rows()}), TODAY)
        self.assertIsNone(goal.plan_kcal)
        self.assertTrue(goal.section.ok)

    def test_a_failed_plan_read_keeps_the_section(self):
        from api.app.routers.health import _nutrition
        goal, detail = _nutrition(ScriptDb({"nutrition.day_totals": self.rows(),
                                            "FROM nutrition.target": RuntimeError("down")}),
                                  TODAY)
        self.assertTrue(goal.section.ok and detail.section.ok)
        self.assertIsNone(goal.plan_kcal)
        self.assertEqual(goal.days_logged, 2)


class TestStrengthVolume(unittest.TestCase):
    def test_total_and_trend(self):
        from api.app.routers.health import _strength_volume
        db = ScriptDb({"AS v FROM health.session_log": [
            {"d": TODAY - timedelta(days=9), "v": 500.0},
            {"d": TODAY - timedelta(days=2), "v": 300.0},
            {"d": TODAY, "v": 200.0}]})
        v = _strength_volume(db, TODAY, "America/Chicago")
        self.assertTrue(v.section.ok)
        self.assertEqual(v.total_7d, 500.0)
        self.assertEqual(v.trend_7d[-1].value, v.total_7d)
        self.assertEqual(len(v.trend_7d), 30)

    def test_the_timezone_is_a_parameter(self):
        from api.app.routers.health import _strength_volume
        seen = {}

        class Db(ScriptDb):
            def execute(self, clause, params=None):
                seen.update(params or {})
                return super().execute(clause, params)
        _strength_volume(Db({}), TODAY, "Pacific/Honolulu")
        self.assertEqual(seen["tz"], "Pacific/Honolulu")

    def test_failure_is_a_status_not_a_zero(self):
        from api.app.routers.health import _strength_volume
        v = _strength_volume(ScriptDb({"health.session_log": RuntimeError("down")}),
                             TODAY, "America/Chicago")
        self.assertFalse(v.section.ok)
        self.assertIsNone(v.total_7d)
        self.assertEqual(v.trend_7d, [])


class TestCardioPlanned(unittest.TestCase):
    def test_minutes_vs_planned_from_one_read(self):
        from api.app.routers.health import _cardio
        db = ScriptDb({
            "JOIN health.session_log sl": [
                {"plan_date": TODAY - timedelta(days=12), "duration_sec": 600, "hr_avg": None, "plan_id": 1},
                {"plan_date": TODAY - timedelta(days=1), "duration_sec": 1200, "hr_avg": None, "plan_id": 2},
                {"plan_date": TODAY, "duration_sec": 900, "hr_avg": None, "plan_id": 3}],
            "SUM(est_duration_min)": [{"m": 90}],
        })
        goal, detail = _cardio(db, TODAY, TODAY - timedelta(days=2))
        self.assertTrue(goal.section.ok and detail.section.ok)
        self.assertEqual(goal.minutes_7d, 35)                  # 20 + 15; day -12 is out
        self.assertEqual(goal.planned_minutes_7d, 90)
        self.assertEqual(goal.trend_7d[-1].value, 35.0)

    def test_a_failed_planned_read_fails_the_whole_domain(self):
        from api.app.routers.health import _cardio
        goal, detail = _cardio(ScriptDb({"SUM(est_duration_min)": RuntimeError("down")}),
                               TODAY, TODAY)
        self.assertFalse(goal.section.ok)
        self.assertFalse(detail.section.ok)
        self.assertIsNone(goal.planned_minutes_7d)


class TestWeightTrend(unittest.TestCase):
    def test_last_point_is_the_tile_value(self):
        from api.app.routers.health import _weight_goal
        rows = [{"state_date": TODAY - timedelta(days=i), "weight_lbs": 180.0 + i}
                for i in (0, 2, 4, 10)]
        g = _weight_goal(ScriptDb({"FROM health.daily_state": rows}), TODAY, None)
        self.assertEqual(g.trend_7d[-1].value, g.avg_7d)
        self.assertEqual(g.avg_7d, 182.0)


class TestGrid(Base):
    """The grid rides on /overview: yesterday, today and five days ahead."""

    def test_grid_window_rows_and_burn(self):
        from tests.lambda_api.test_overview import PLANS
        today = date(2026, 9, 18)                       # Base's fixed clock
        burn = [{"local_date": today - timedelta(days=1), "slot": "morning", "kcal": 111.4, "n": 1},
                {"local_date": today, "slot": "evening", "kcal": 50.0, "n": 2}]
        out = self.overview(FakeSession(burn=burn))
        g = out["grid"]
        self.assertTrue(g["section"]["ok"], g)
        self.assertEqual(g["start"], "2026-09-17")
        self.assertEqual(g["end"], "2026-09-23")
        want = sorted({p["plan_date"].isoformat() for p in PLANS
                       if date(2026, 9, 17) <= p["plan_date"] <= date(2026, 9, 23)})
        self.assertEqual(sorted({d["plan_date"] for d in g["days"]}), want)
        self.assertEqual([(b["date"], b["slot"], b["kcal"], b["workouts"]) for b in g["burn"]],
                         [("2026-09-17", "morning", 111.0, 1), ("2026-09-18", "evening", 50.0, 2)])

    def test_a_failed_grid_read_is_its_own_status(self):
        class Down(FakeSession):
            def execute(self, clause, params=None):
                if "FROM health.watch_workout" in str(clause):
                    raise RuntimeError("down")
                return super().execute(clause, params)
        out = self.overview(Down())
        self.assertFalse(out["grid"]["section"]["ok"])
        self.assertEqual(out["grid"]["days"], [])
        # The rest of the page still renders.
        self.assertIsNotNone(out["today"])


if __name__ == "__main__":
    unittest.main()
