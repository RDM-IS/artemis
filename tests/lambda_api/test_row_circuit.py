"""ROW-CIRCUIT — GET /api/health/row-circuit and the new /log columns.

Synthetic values only (PUBLIC-FIXTURES): rates 11-13 spm, lengths 100-130 s,
year 2031 -- legal for the code, implausible for him.
"""
import os as _os; _os.environ["ARTEMIS_TEST_NO_DB"] = "1"  # TEST-DB-GUARD: never a real DB
import sys
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sqlalchemy.exc import SQLAlchemyError  # noqa: E402

from tests.lambda_api.test_health import VALID_KEY, _build_log_client  # noqa: E402

T = datetime(2031, 3, 10, 10, 0, tzinfo=timezone.utc)


class _Rows:
    def __init__(self, rows): self.rows = rows
    def mappings(self): return self
    def all(self): return self.rows
    def first(self): return self.rows[0] if self.rows else None


class _HistorySession:
    def __init__(self, row_rows=None, rep_rows=None, fail=False):
        self.row_rows = row_rows or []
        self.rep_rows = rep_rows or []
        self.fail = fail
        self.params = []

    def execute(self, stmt, params=None):
        sql = str(stmt).lower()
        if "health.session_log" in sql:
            if self.fail:
                raise SQLAlchemyError("boom")
            self.params.append(params)
            return _Rows(self.row_rows if "cardio_block" in sql else self.rep_rows)
        return _Rows([])           # timezone override: none


def _client(sess):
    try:
        from fastapi.testclient import TestClient
    except ImportError:
        raise unittest.SkipTest("fastapi not installed")
    from api.app.database import get_db
    from api.app.main import app
    from api.app.routers import health as health_router

    def _db():
        yield sess
    app.dependency_overrides[get_db] = _db
    health_router._HEALTH_API_KEY = VALID_KEY
    return TestClient(app)


class TestPrescriptionEndpoint(unittest.TestCase):
    def tearDown(self):
        from api.app.main import app
        from api.app.routers import health as health_router
        app.dependency_overrides.clear()
        health_router._HEALTH_API_KEY = None

    def _get(self, sess, q=""):
        return _client(sess).get(f"/api/health/row-circuit{q}", headers={"X-API-Key": VALID_KEY})

    def test_requires_the_key(self):
        r = _client(_HistorySession()).get("/api/health/row-circuit")
        self.assertEqual(r.status_code, 401)

    def test_progresses_from_the_logged_session(self):
        rows = [{"day": date(2031, 3, 10), "set_num": n, "duration_sec": 100,
                 "stroke_rate": 10 + n, "logged_at": T} for n in (1, 2, 3)]
        body = self._get(_HistorySession(rows), "?rounds=3").json()
        self.assertTrue(body["available"])
        self.assertEqual([x["spm"] for x in body["options"]["rate"]["rounds"]], [12, 13, 14])
        self.assertEqual([x["work_sec"] for x in body["options"]["time"]["rounds"]], [160] * 3)
        self.assertEqual(body["default"], "rate")

    def test_reps_come_back_for_the_record_card(self):
        reps = [{"day": date(2031, 3, 10), "set_num": 1, "exercise": "Push-up",
                 "reps_done": 7, "logged_at": T}]
        body = self._get(_HistorySession(rep_rows=reps)).json()
        self.assertEqual(body["last_reps"], {"1": {"Push-up": 7}})

    def test_the_session_type_and_timezone_are_bound_not_interpolated(self):
        sess = _HistorySession()
        self._get(sess)
        self.assertTrue(sess.params)
        for p in sess.params:
            self.assertEqual(p["t"], "row_circuit")
            self.assertIn("tz", p)

    def test_a_failed_read_is_unavailable_never_the_first_session(self):
        body = self._get(_HistorySession(fail=True)).json()
        self.assertFalse(body["available"])
        self.assertNotIn("options", body)
        self.assertIn("Nothing was reset", body["reason"])

    def test_rounds_out_of_range_is_refused(self):
        self.assertEqual(self._get(_HistorySession(), "?rounds=9").status_code, 422)


class TestLogColumns(unittest.TestCase):
    def tearDown(self):
        from api.app.main import app
        from api.app.routers import health as health_router
        app.dependency_overrides.clear()
        health_router._HEALTH_API_KEY = None

    def _post(self, body):
        client, _, sess = _build_log_client()
        seen = []
        orig = sess.execute

        def execute(stmt, params=None):
            if str(stmt).strip().lower().startswith("insert") and params and "log_type" in params:
                seen.append(dict(params))
            return orig(stmt, params)
        sess.execute = execute
        r = client.post("/api/health/log", headers={"X-API-Key": VALID_KEY}, json=body)
        return r, seen

    def test_stroke_rate_and_window_are_written(self):
        r, seen = self._post({
            "adhoc_session_type": "row_circuit", "exercise": "Row", "log_type": "cardio_block",
            "modality": "row", "device": "water",
            "sets": [{"set_num": 1, "duration_sec": 100, "distance_m": 300, "stroke_rate": 11,
                      "started_at": "2031-03-10T10:00:00Z", "ended_at": "2031-03-10T10:01:40Z"}],
        })
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(seen[0]["stroke_rate"], 11)
        self.assertEqual(seen[0]["started_at"].isoformat(), "2031-03-10T10:00:00+00:00")
        self.assertEqual(seen[0]["adhoc_session_type"], "row_circuit")

    def test_the_in_band_summary_binds_the_new_columns_as_null(self):
        r, seen = self._post({
            "adhoc_session_type": "row_circuit", "exercise": "Row", "log_type": "cardio_block",
            "modality": "row", "session_rpe": 6,
            "sets": [{"set_num": 1, "duration_sec": 100, "stroke_rate": 11}],
        })
        self.assertEqual(r.status_code, 200, r.text)
        summary = [p for p in seen if p["log_type"] == "session_summary"][0]
        self.assertIsNone(summary["stroke_rate"])
        self.assertIsNone(summary["started_at"])

    def test_a_reversed_window_is_refused(self):
        r, seen = self._post({
            "adhoc_session_type": "row_circuit", "exercise": "Push-up", "log_type": "strength_set",
            "sets": [{"set_num": 1, "reps_done": 4,
                      "started_at": "2031-03-10T10:05:00Z", "ended_at": "2031-03-10T10:04:00Z"}],
        })
        self.assertEqual(r.status_code, 400)
        self.assertEqual(seen, [])

    def test_a_stroke_rate_out_of_range_is_refused(self):
        r, _ = self._post({
            "adhoc_session_type": "row_circuit", "exercise": "Row", "log_type": "cardio_block",
            "sets": [{"set_num": 1, "duration_sec": 100, "stroke_rate": 99}],
        })
        self.assertEqual(r.status_code, 422)


if __name__ == "__main__":
    unittest.main()
