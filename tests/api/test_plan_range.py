"""GD-WEEK — GET /api/health/plan (read-only plan range for Tomorrow / Week).

Mocks the DB session; no AWS or RDS.

Run:
    python tests/api/test_plan_range.py
"""

import os as _os; _os.environ["ARTEMIS_TEST_NO_DB"] = "1"  # TEST-DB-GUARD: never a real DB

import os
import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))
os.environ.setdefault("RDS_HOST", "test-host")
os.environ.setdefault("RDS_DB", "test-db")

VALID_KEY = "test-health-api-key-xyz"
TODAY = date(2026, 9, 21)       # Mon, week 1 day 6 (Wed–Tue weeks from 9/16)


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return self._rows


class FakeSession:
    def __init__(self, plans, logs, tz_override=None):
        self.plans, self.logs, self.tz = plans, logs, tz_override
        self.plan_queries = []

    def execute(self, clause, params=None):
        sql = " ".join(str(clause).split())
        if "acos.timezone_overrides" in sql:
            return _Result([{"timezone": self.tz}] if self.tz else [])
        if "FROM health.plan" in sql:
            self.plan_queries.append(params)
            return _Result([p for p in self.plans if params["s"] <= p["plan_date"] <= params["e"]])
        if "FROM health.session_log" in sql:
            return _Result([l for l in self.logs if l["plan_id"] in params["pids"]])
        raise AssertionError(f"unexpected SQL: {sql[:80]}")


def circuit(n=3, rounds=2, **extra):
    b = {"type": "circuit", "display_name": "Office Strength B", "location": "office gym",
         "rounds": rounds,
         "exercises": [{"name": f"Ex{i}", "format": "reps", "notes": f"{rounds}×8-12"} for i in range(n)]}
    b.update(extra)
    return b


def plan(pid, d, st="strength_b", blocks=None, **extra):
    row = {"plan_id": pid, "plan_date": d, "phase": 1, "week_num": 1, "session_type": st,
           "blocks": blocks if blocks is not None else circuit(), "target_rpe": 6.0,
           "est_duration_min": 45, "is_skipped": False, "status": "planned"}
    row.update(extra)
    return row


def log(pid, log_type="strength_set", exercise="Ex0", via="manual", **extra):
    row = {"plan_id": pid, "log_type": log_type, "exercise": exercise, "set_num": 1,
           "reps_done": 10, "weight_lbs": 25.0, "duration_sec": None, "notes": None,
           "is_skipped": False, "logged_via": via}
    row.update(extra)
    return row


def full_session(pid):
    return [log(pid, exercise=f"Ex{i}", set_num=s, weight_lbs=20.0 + s) for i in range(3) for s in (1, 2)]


class Base(unittest.TestCase):
    def client(self, plans=(), logs=(), tz=None, now=None):
        try:
            from fastapi.testclient import TestClient
        except ImportError:
            raise unittest.SkipTest("fastapi not installed")
        from api.app.database import get_db
        from api.app.main import app
        from api.app.routers import health as h

        self.session = FakeSession(list(plans), list(logs), tz)

        def _db():
            yield self.session
        app.dependency_overrides[get_db] = _db
        h._HEALTH_API_KEY = VALID_KEY
        self.addCleanup(app.dependency_overrides.clear)
        self.addCleanup(setattr, h, "_HEALTH_API_KEY", None)

        real = datetime
        fixed = now or real(2026, 9, 21, 12, 0, tzinfo=timezone.utc)

        class _DT(real):
            @classmethod
            def now(cls, tz=None):
                return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)
        p = patch.object(h, "datetime", _DT)
        p.start()
        self.addCleanup(p.stop)
        return TestClient(app)

    def get(self, client, qs=""):
        return client.get(f"/api/health/plan{qs}", headers={"X-API-Key": VALID_KEY})


class TestAuthAndRange(Base):
    def test_401_without_key(self):
        c = self.client()
        self.assertEqual(c.get("/api/health/plan").status_code, 401)

    def test_default_range_is_today_plus_six(self):
        c = self.client()
        body = self.get(c).json()
        self.assertEqual((body["today"], body["range_from"], body["range_to"]),
                         ("2026-09-21", "2026-09-21", "2026-09-27"))
        self.assertEqual(body["timezone"], "America/Chicago")

    def test_fourteen_days_is_the_cap(self):
        c = self.client()
        self.assertEqual(self.get(c, "?from=2026-09-16&to=2026-09-29").status_code, 200)
        r = self.get(c, "?from=2026-09-16&to=2026-09-30")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["detail"], {"error": "range_too_large", "max_days": 14})
        self.assertEqual(len(self.session.plan_queries), 1, "the oversized range never hit the DB")

    def test_bad_input(self):
        c = self.client()
        self.assertEqual(self.get(c, "?from=2026-09-20&to=2026-09-19").json()["detail"],
                         {"error": "bad_range"})
        self.assertEqual(self.get(c, "?from=tomorrow").json()["detail"],
                         {"error": "bad_date", "param": "from"})

    def test_active_timezone_decides_today(self):
        # 04:30 UTC on 9/21 is still 9/20 in Chicago but 9/21 in Paris.
        at = datetime(2026, 9, 21, 4, 30, tzinfo=timezone.utc)
        home = self.get(self.client(now=at)).json()
        self.assertEqual(home["today"], "2026-09-20")
        away = self.get(self.client(tz="Europe/Paris", now=at)).json()
        self.assertEqual((away["today"], away["timezone"]), ("2026-09-21", "Europe/Paris"))

    def test_bad_override_falls_back_home(self):
        body = self.get(self.client(tz="Mars/Olympus")).json()
        self.assertEqual(body["timezone"], "America/Chicago")


class TestStatus(Base):
    def days(self, plans, logs):
        c = self.client(plans, logs)
        body = self.get(c, "?from=2026-09-16&to=2026-09-22").json()
        return {d["plan_date"]: d for d in body["days"]}

    def test_week_statuses(self):
        W = date(2026, 9, 16)
        plans = [
            plan(1, W, "strength_a"),                                   # done: all sets
            plan(2, W + timedelta(1), "recovery_flow",
                 {"type": "recovery_flow", "display_name": "Recovery Flow", "location": "office gym"}),
            plan(3, W + timedelta(2), "strength_b"),                    # partial: some sets
            plan(4, W + timedelta(3), "rest_mobility",
                 {"type": "mobility", "display_name": "Rest / Mobility"}),
            plan(5, W + timedelta(4), "walk",
                 {"type": "steady", "display_name": "Walk", "location": "outside"}),   # missed
            plan(6, TODAY, "strength_c"),                               # today
            plan(7, TODAY + timedelta(1), "cardio_z2",
                 {"type": "steady", "display_name": "Zone 2 Cardio"}),  # upcoming
        ]
        logs = (full_session(1)
                + [log(2, "session_summary", None, notes="recovery_flow: complete 37 min", duration_sec=2250)]
                + [log(3, exercise="Ex0"), log(3, exercise="Ex1")]
                + [log(5, "session_summary", None, via="inferred", notes="no debrief — assumed")])
        d = self.days(plans, logs)
        self.assertEqual({k: v["status"] for k, v in d.items()}, {
            "2026-09-16": "done", "2026-09-17": "done", "2026-09-18": "partial",
            "2026-09-19": "done", "2026-09-20": "missed", "2026-09-21": "today",
            "2026-09-22": "upcoming",
        })
        for k, v in d.items():
            self.assertIn(v["status"], ("done", "partial", "missed", "upcoming", "today"))
        self.assertEqual(d["2026-09-17"]["location"], "office gym")
        self.assertEqual(d["2026-09-20"]["location"], "outside")
        self.assertEqual(d["2026-09-20"]["logged"], [], "inferred rows are never shown as logged")

    def test_logged_detail_on_past_days_only(self):
        W = date(2026, 9, 16)
        logs = full_session(1) + [log(1, exercise="Ex2", set_num=3, is_skipped=True)]
        d = self.days([plan(1, W), plan(7, TODAY + timedelta(1))], logs + [log(7)])
        ex = {e["exercise"]: e for e in d["2026-09-16"]["logged"]}
        self.assertEqual(list(ex), ["Ex0", "Ex1", "Ex2"])
        self.assertEqual((ex["Ex0"]["sets"], ex["Ex0"]["reps"], ex["Ex0"]["top_weight_lbs"]),
                         (2, [10, 10], 22.0))
        self.assertEqual((ex["Ex2"]["sets"], ex["Ex2"]["skipped"]), (2, 1))
        self.assertEqual(d["2026-09-22"]["logged"], [])

    def test_flow_partial_and_skipped_sets(self):
        W = date(2026, 9, 16)
        d = self.days(
            [plan(2, W + timedelta(1), "recovery_flow", {"type": "recovery_flow"}),
             plan(3, W + timedelta(2)),
             plan(4, W + timedelta(3), "rest_mobility", {"type": "recovery_flow"})],
            [log(2, "session_summary", None, notes="recovery_flow: partial 14 of 30 min"),
             *[log(3, exercise=f"Ex{i}", set_num=s, is_skipped=(i == 0)) for i in range(3) for s in (1, 2)]])
        self.assertEqual(d["2026-09-17"]["status"], "partial")
        self.assertEqual(d["2026-09-18"]["status"], "done",
                         "Ryan trained (Ex1, Ex2) and decided about Ex0 — skipped slots count")
        # 9/17-style flow on a rest_mobility row: a missed flow is missed, not a rest day.
        self.assertEqual(d["2026-09-19"]["status"], "missed")
        self.assertEqual(d["2026-09-17"]["summary_notes"], "recovery_flow: partial 14 of 30 min")

    def test_a_debrief_summary_completes_a_day(self):
        """PLAN-STATUS-DEBRIS: session_log is the ONLY source for `done` now.
        `plan.status='completed'` used to be a second one; migration 039 drops
        the column, so a day with no logs is missed however status reads."""
        W = date(2026, 9, 16)
        d = self.days([plan(1, W), plan(3, W + timedelta(2), status="completed")],
                      [log(1, "session_summary", None, rpe_actual=7, notes="felt good")])
        self.assertEqual(d["2026-09-16"]["status"], "done")     # from its summary
        self.assertEqual(d["2026-09-18"]["status"], "missed")   # no logs, status ignored

    def test_a_skipped_past_day_is_skipped_not_missed(self):
        """MAKEUP-1: a deliberate skip is a different fact from a missed day."""
        W = date(2026, 9, 16)
        d = self.days([plan(4, W + timedelta(3), "rest_mobility", {"type": "mobility"}, is_skipped=True)], [])
        self.assertEqual(d["2026-09-19"]["status"], "skipped")
        self.assertTrue(d["2026-09-19"]["is_skipped"])

    def test_adjusted_day_returns_the_stored_blocks(self):
        adj = {"reason": "Pain shoulder 4/5 → day off.", "rules_fired": ["pain_day_off"],
               "summary": ["Pain shoulder 4/5 → day off."]}
        day_off = {"type": "mobility", "display_name": "Day off", "duration_min": 0,
                   "adjustment": adj, "original": {"session_type": "strength_c"}}
        d = self.days([plan(6, TODAY, "rest_mobility", day_off, est_duration_min=0),
                       plan(3, date(2026, 9, 18), blocks=circuit(adjustment={"summary": ["x"]}))], [])
        t = d["2026-09-21"]
        self.assertTrue(t["adjusted"])
        self.assertEqual(t["blocks"]["adjustment"], adj)
        self.assertEqual(t["display_name"], "Day off")
        self.assertEqual(t["status"], "today")
        self.assertTrue(d["2026-09-18"]["adjusted"])
        self.assertEqual(d["2026-09-18"]["status"], "missed")

    def test_pure_status_function(self):
        from api.app.routers.health import derive_day_status
        p = plan(1, TODAY - timedelta(1), "walk", {"type": "steady"})
        self.assertEqual(derive_day_status(p, [], TODAY), "missed")
        self.assertEqual(derive_day_status(p, [log(1, "cardio_block", "Walk")], TODAY), "done")   # 1 of 1
        # A walk is one cardio_block. Skipping it is not doing it, and there
        # is no other real set in the session to make the skip count.
        self.assertEqual(derive_day_status(p, [log(1, "cardio_block", "Walk", is_skipped=True)], TODAY), "missed")
        self.assertEqual(derive_day_status(p, [log(1, "session_summary", None)], TODAY), "done")
        self.assertEqual(derive_day_status(plan(2, TODAY, "rest_mobility", {"type": "mobility"}), [], TODAY), "today")


class TestFlowPlannedSets(unittest.TestCase):
    def test_a_flow_plans_no_sets(self):
        from api.app.routers.health import _planned_set_count
        self.assertEqual(_planned_set_count({"type": "recovery_flow", "rounds": 2, "flow": [{}] * 20}), 0)
        self.assertEqual(_planned_set_count({"type": "mobility"}), 1)


class TestProxyAllowsPlan(unittest.TestCase):
    def test_route_is_registered_under_health(self):
        try:
            from api.app.main import app
        except ImportError as e:  # pragma: no cover
            raise unittest.SkipTest(str(e))
        paths = {r.path for r in app.routes}
        self.assertIn("/api/health/plan", paths)


if __name__ == "__main__":
    unittest.main(verbosity=2)
