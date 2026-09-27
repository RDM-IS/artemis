"""MAKEUP-2 — POST /api/health/makeup swaps the missed row and today's rest row,
only when everything the box offered is still true. Synthetic data only."""
import os as _os; _os.environ["ARTEMIS_TEST_NO_DB"] = "1"  # TEST-DB-GUARD: never a real DB
import json
import sys
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

KEY = "test-key-makeup"


class _R:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return self._rows


class _DB:
    def __init__(self, lib, plans, not_done):
        self.lib, self.plans, self.not_done = lib, plans, not_done
        self.updates, self.audits, self.committed = [], [], False

    def execute(self, stmt, params=None):
        sql = " ".join(str(stmt).lower().split())
        if "from acos.system_state" in sql:
            return _R([{"value": json.dumps(self.lib)}] if self.lib else [])
        if sql.startswith("update health.plan"):
            self.updates.append((sql, params))
            return _R([])
        if "insert into acos.audit_log" in sql:
            self.audits.append(params)
            return _R([])
        if "where plan_id = :pid" in sql:
            p = self.plans.get(params["pid"])
            return _R([p] if p else [])
        if "plan_date >= :ws" in sql:
            return _R(self.not_done)
        return _R([])

    def commit(self):
        self.committed = True

    def rollback(self):
        pass


def _today():
    from api.app.routers.health import HOME_TIMEZONE
    return datetime.now(ZoneInfo(HOME_TIMEZONE)).date()


def _setup(**over):
    t = _today()
    ws = t - timedelta(days=3)
    offer = {"missed_plan_id": 11, "rest_plan_id": 22, "session_type": "strength_b",
             "display_name": "Test B", "location_key": "office",
             "entry": {"session_type": "strength_b", "target_rpe": 6, "target_hr_zone": None,
                       "est_duration_min": 44, "blocks": {"type": "circuit", "display_name": "Test B"}}}
    lib = {"generated_on": t.isoformat(),
           "makeup": {"week_start": ws.isoformat(), "offer": offer, "repeat": False}}
    plans = {
        11: {"plan_id": 11, "plan_date": t - timedelta(days=1), "slot": "morning",
             "session_type": "strength_b", "blocks": {"type": "circuit"}, "is_skipped": False,
             "logged": False},
        22: {"plan_id": 22, "plan_date": t, "slot": "morning", "session_type": "rest",
             "blocks": {"type": "rest"}, "is_skipped": False, "logged": False},
    }
    not_done = [{"plan_id": 11, "session_type": "strength_b"}]
    lib.update(over.pop("lib", {}))
    for k, v in over.pop("plans", {}).items():
        plans[k].update(v)
    if "not_done" in over:
        not_done = over.pop("not_done")
    return _DB(lib, plans, not_done)


def _post(db, body=None):
    try:
        from fastapi.testclient import TestClient
    except ImportError:
        raise unittest.SkipTest("fastapi not installed")
    from api.app.database import get_db
    from api.app.main import app
    from api.app.routers import health as h

    def _g():
        yield db
    app.dependency_overrides[get_db] = _g
    h._HEALTH_API_KEY = KEY
    try:
        return TestClient(app).post("/api/health/makeup", headers={"X-API-Key": KEY},
                                    json=body or {"missed_plan_id": 11, "rest_plan_id": 22})
    finally:
        app.dependency_overrides.clear()
        h._HEALTH_API_KEY = None


class TestMakeup(unittest.TestCase):
    def test_the_swap(self):
        db = _setup()
        r = _post(db)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(db.committed)
        (rest_sql, rest_p), (missed_sql, missed_p) = db.updates
        self.assertEqual(rest_p["pid"], 22)
        self.assertEqual(rest_p["t"], "strength_b")
        b = json.loads(rest_p["b"])
        self.assertEqual(b["makeup_of"]["plan_id"], 11)
        self.assertEqual(b["makeup_original"]["session_type"], "rest")
        self.assertNotIn("original", b)               # the check-in's undo key is untouched
        self.assertEqual(missed_p["pid"], 11)
        self.assertIn("session_type = 'rest'", missed_sql)
        mb = json.loads(missed_p["b"])
        self.assertEqual(mb["made_up_on"], _today().isoformat())
        self.assertEqual(mb["makeup_original"]["session_type"], "strength_b")
        self.assertEqual(len(db.audits), 1)

    def test_a_stale_library_is_refused(self):
        db = _setup(lib={"generated_on": "2027-01-01"})
        self.assertEqual(_post(db).status_code, 409)
        self.assertEqual(db.updates, [])

    def test_ids_not_on_offer_are_refused(self):
        db = _setup()
        self.assertEqual(_post(db, {"missed_plan_id": 99, "rest_plan_id": 22}).status_code, 409)
        self.assertEqual(db.updates, [])

    def test_today_no_longer_rest_is_refused(self):
        db = _setup(plans={22: {"session_type": "strength_c"}})
        self.assertEqual(_post(db).status_code, 409)

    def test_missed_since_logged_is_refused(self):
        db = _setup(plans={11: {"logged": True}})
        self.assertEqual(_post(db).status_code, 409)

    def test_two_not_done_means_repeat_not_makeup(self):
        db = _setup(not_done=[{"plan_id": 11, "session_type": "strength_b"},
                              {"plan_id": 12, "session_type": "cardio_z2"}])
        r = _post(db)
        self.assertEqual(r.status_code, 409)
        self.assertIn("repeats", r.json()["detail"]["reason"])
        self.assertEqual(db.updates, [])


if __name__ == "__main__":
    unittest.main()
