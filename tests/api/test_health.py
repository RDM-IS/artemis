"""Smoke tests for GET /api/health/today.

Covers:
  - happy path with valid X-API-Key
  - 401 without key
  - 401 with wrong key
  - 404 when no row for today
  - is_skipped row still returns 200
  - JSONB blocks returned as object (not stringified)
  - timezone helper resolves to a date
  - CORS preflight works for gym.rdm.is

Mocks the DB session (FastAPI dependency_overrides) and the health-key
Secrets Manager fetch. Does not require live AWS or RDS.

Run:
    python tests/api/test_health.py
"""

import json
import os
import sys
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

# Repo root on path
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))

# FastAPI app construction touches database.py which expects RDS env;
# values are unused by tests because get_db is overridden.
os.environ.setdefault("RDS_HOST", "test-host")
os.environ.setdefault("RDS_DB", "test-db")

VALID_KEY = "test-health-api-key-xyz"


# ---------------------------------------------------------------------------
# Mock DB plumbing — minimal shim for SQLAlchemy execute().mappings().first()
# ---------------------------------------------------------------------------

class _MockMappingResult:
    def __init__(self, row: dict | None):
        self._row = row

    def first(self):
        return self._row

    def all(self):
        return [self._row] if self._row else []


class _MockExecuteResult:
    def __init__(self, row: dict | None):
        self._row = row

    def mappings(self):
        return _MockMappingResult(self._row)


class _MockSession:
    def __init__(self, row: dict | None):
        self._row = row

    def execute(self, *args, **kwargs):
        return _MockExecuteResult(self._row)


_FAKE_PLAN_ROW = {
    "plan_id": 1,
    "plan_date": date(2026, 5, 6),
    "phase": 1,
    "week_num": 1,
    "session_type": "strength_a",
    "target_rpe": 6.5,
    "target_hr_zone": 3,
    "est_duration_min": 40,
    "is_skipped": False,
    "blocks": {
        "type": "circuit",
        "rounds": 2,
        "warmup": "5 min bike easy",
        "exercises": [
            {"name": "Goblet squat", "format": "reps", "target_reps": 10, "target_load_lbs": 30},
        ],
        "cooldown": "8 min bike easy",
    },
}


def _build_client(row: dict | None):
    """Build a TestClient with DB and health-key dependencies overridden.

    Patches the cached _HEALTH_API_KEY so verify_health_api_key compares
    against VALID_KEY without touching Secrets Manager.
    """
    try:
        from fastapi.testclient import TestClient
    except ImportError:
        raise unittest.SkipTest("fastapi not installed")

    from api.app.database import get_db
    from api.app.main import app
    from api.app.routers import health as health_router

    def _override_db():
        yield _MockSession(row)

    app.dependency_overrides[get_db] = _override_db
    health_router._HEALTH_API_KEY = VALID_KEY
    return TestClient(app), app


class TestHealthEndpoint(unittest.TestCase):
    def setUp(self):
        self.app = None

    def tearDown(self):
        if self.app is not None:
            self.app.dependency_overrides.clear()
        # Reset cached key so other tests can override
        from api.app.routers import health as health_router
        health_router._HEALTH_API_KEY = None

    # ── auth ────────────────────────────────────────────────────────────

    def test_401_without_api_key(self):
        client, self.app = _build_client(_FAKE_PLAN_ROW)
        resp = client.get("/api/health/today")
        self.assertEqual(resp.status_code, 401)
        body = resp.json()
        # FastAPI wraps dict detail in {"detail": {...}}
        self.assertEqual(body["detail"]["error"], "unauthorized")

    def test_401_with_wrong_key(self):
        client, self.app = _build_client(_FAKE_PLAN_ROW)
        resp = client.get("/api/health/today", headers={"X-API-Key": "wrong"})
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()["detail"]["error"], "unauthorized")

    # ── happy path ──────────────────────────────────────────────────────

    def test_happy_path_returns_plan(self):
        client, self.app = _build_client(_FAKE_PLAN_ROW)
        resp = client.get("/api/health/today", headers={"X-API-Key": VALID_KEY})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()

        self.assertEqual(body["plan_id"], 1)
        self.assertEqual(body["plan_date"], "2026-05-06")
        self.assertEqual(body["phase"], 1)
        self.assertEqual(body["week_num"], 1)
        self.assertEqual(body["session_type"], "strength_a")
        self.assertEqual(body["target_rpe"], 6.5)
        self.assertEqual(body["target_hr_zone"], 3)
        self.assertEqual(body["est_duration_min"], 40)
        self.assertFalse(body["is_skipped"])
        # blocks is an object, not stringified
        self.assertIsInstance(body["blocks"], dict)
        self.assertEqual(body["blocks"]["type"], "circuit")

    def test_blocks_serialized_as_object(self):
        client, self.app = _build_client(_FAKE_PLAN_ROW)
        resp = client.get("/api/health/today", headers={"X-API-Key": VALID_KEY})
        body = json.loads(resp.text)
        self.assertNotIsInstance(body["blocks"], str)
        self.assertIsInstance(body["blocks"], dict)

    # ── 404 ─────────────────────────────────────────────────────────────

    def test_404_when_no_row(self):
        client, self.app = _build_client(None)
        resp = client.get("/api/health/today", headers={"X-API-Key": VALID_KEY})
        self.assertEqual(resp.status_code, 404)
        body = resp.json()
        self.assertEqual(body["detail"]["error"], "no_plan")
        self.assertIn("fallback", body["detail"])

    # ── is_skipped ──────────────────────────────────────────────────────

    def test_skipped_day_returns_200(self):
        skipped_row = dict(_FAKE_PLAN_ROW)
        skipped_row["is_skipped"] = True
        client, self.app = _build_client(skipped_row)
        resp = client.get("/api/health/today", headers={"X-API-Key": VALID_KEY})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["is_skipped"])

    # ── timezone ────────────────────────────────────────────────────────

    def test_timezone_function_uses_central(self):
        from api.app.routers.health import _today_ct
        result = _today_ct()
        self.assertIsInstance(result, date)

    # ── CORS preflight ──────────────────────────────────────────────────

    def test_cors_preflight_from_gym_origin(self):
        """OPTIONS request from gym.rdm.is gets a working CORS response.

        The global CORSMiddleware (allow_origins=["*"]) handles this — the
        actual Access-Control-Allow-Origin will be '*' which is functionally
        equivalent for a non-credentialed request from the gym-display.
        """
        client, self.app = _build_client(_FAKE_PLAN_ROW)
        resp = client.options(
            "/api/health/today",
            headers={
                "Origin": "https://gym.rdm.is",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "X-API-Key",
            },
        )
        # Starlette CORSMiddleware returns 200 (or 204) for valid preflight
        self.assertIn(resp.status_code, (200, 204))
        # ACAO must be present and either '*' or echo the origin
        acao = resp.headers.get("access-control-allow-origin", "")
        self.assertIn(acao, ("*", "https://gym.rdm.is"))
        # Method allowed for the actual request
        acam = resp.headers.get("access-control-allow-methods", "")
        self.assertTrue(
            "GET" in acam or "*" in acam,
            f"GET should be allowed; got: {acam!r}",
        )

    def test_trusted_origins_constant_includes_gym(self):
        """Smoke test: gym.rdm.is is documented as a trusted origin."""
        from api.app.routers.health import TRUSTED_ORIGINS
        self.assertIn("https://gym.rdm.is", TRUSTED_ORIGINS)


class TestHealthAuthDependency(unittest.TestCase):
    """Direct unit tests on verify_health_api_key()."""

    def tearDown(self):
        from api.app.routers import health as health_router
        health_router._HEALTH_API_KEY = None

    def test_missing_key_raises_401(self):
        from fastapi import HTTPException
        from api.app.routers.health import verify_health_api_key

        with self.assertRaises(HTTPException) as ctx:
            verify_health_api_key(api_key=None)
        self.assertEqual(ctx.exception.status_code, 401)
        self.assertEqual(ctx.exception.detail["error"], "unauthorized")

    def test_wrong_key_raises_401(self):
        from fastapi import HTTPException
        from api.app.routers import health as health_router

        health_router._HEALTH_API_KEY = "expected-key"

        with self.assertRaises(HTTPException) as ctx:
            health_router.verify_health_api_key(api_key="wrong-key")
        self.assertEqual(ctx.exception.status_code, 401)

    def test_valid_key_returns_key(self):
        from api.app.routers import health as health_router

        health_router._HEALTH_API_KEY = "expected-key"

        result = health_router.verify_health_api_key(api_key="expected-key")
        self.assertEqual(result, "expected-key")


# ---------------------------------------------------------------------------
# /status endpoint — windowed payload for gym-display /status page
# ---------------------------------------------------------------------------

class _QueryAwareSession:
    """DB shim that returns different rows based on SQL content.

    Routes by substring match — sufficient for /status's distinct queries.
    """

    def __init__(self, fixtures: dict[str, list[dict]]):
        # fixtures keys: 'plan_window', 'most_recent', 'history',
        # 'rpe_trend', 'weight_trend', 'phase_config', 'prev_plan'
        self._f = fixtures

    def execute(self, stmt, *_args, **_kwargs):
        sql = str(stmt).lower()
        if "phase_config" in sql:
            rows = self._f.get("phase_config", [])
        elif "from health.plan" in sql and "between" in sql:
            rows = self._f.get("plan_window", [])
        elif "from health.plan" in sql and "order by plan_date desc" in sql:
            rows = self._f.get("prev_plan", [])
        elif "from health.session_log" in sql and "limit 1" in sql and ":exclude" not in sql and "<>" not in sql:
            rows = self._f.get("most_recent", [])
        elif "from health.session_log" in sql and "limit :n" in sql:
            rows = self._f.get("history", [])
        elif "from health.session_log" in sql and "log_type in" in sql:
            # exercises join for a plan_id
            pid = _args[0].get("pid") if _args else None
            rows = self._f.get("exercises", {}).get(pid, [])
        elif "from health.session_log" in sql and "rpe_actual" in sql and "between" in sql:
            rows = self._f.get("rpe_trend", [])
        elif "from health.daily_state" in sql:
            rows = self._f.get("weight_trend", [])
        else:
            rows = []
        first = rows[0] if rows else None
        return _MockExecuteResult(first) if len(rows) <= 1 else _MockMultiResult(rows)


class _MockMultiResult:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return _MockMultiMapping(self._rows)


class _MockMultiMapping:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None


def _build_status_client(fixtures: dict):
    try:
        from fastapi.testclient import TestClient
    except ImportError:
        raise unittest.SkipTest("fastapi not installed")
    from api.app.database import get_db
    from api.app.main import app
    from api.app.routers import health as health_router

    def _override_db():
        yield _QueryAwareSession(fixtures)

    app.dependency_overrides[get_db] = _override_db
    health_router._HEALTH_API_KEY = VALID_KEY
    return TestClient(app), app


class TestStatusEndpoint(unittest.TestCase):
    def setUp(self):
        self.app = None

    def tearDown(self):
        if self.app is not None:
            self.app.dependency_overrides.clear()
        from api.app.routers import health as health_router
        health_router._HEALTH_API_KEY = None

    def test_401_without_key(self):
        client, self.app = _build_status_client({})
        resp = client.get("/api/health/status")
        self.assertEqual(resp.status_code, 401)

    def test_empty_db_returns_well_formed_payload(self):
        client, self.app = _build_status_client({})
        resp = client.get("/api/health/status", headers={"X-API-Key": VALID_KEY})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        # Shape contract
        self.assertIn("today", body)
        self.assertIn("window_start", body)
        self.assertIn("window_end", body)
        self.assertEqual(len(body["day_strip"]), 11)  # today ±5
        self.assertEqual(body["rpe_trend"], [])
        self.assertEqual(body["weight_trend"], [])
        self.assertEqual(body["same_type_history"], [])
        self.assertIsNone(body["most_recent_session"])
        self.assertIsNone(body["banner"])
        # today_summary present even with no data
        self.assertIn("today_summary", body)
        self.assertFalse(body["today_summary"]["exists"])

    def test_day_strip_marks_today(self):
        client, self.app = _build_status_client({})
        resp = client.get("/api/health/status", headers={"X-API-Key": VALID_KEY})
        body = resp.json()
        today_entries = [d for d in body["day_strip"] if d["is_today"]]
        self.assertEqual(len(today_entries), 1)
        self.assertEqual(today_entries[0]["plan_date"], body["today"])

    def test_with_today_plan_and_summary_populates_today_summary(self):
        today = date.today()
        fixtures = {
            "plan_window": [{
                "plan_id": 42,
                "plan_date": today,
                "session_type": "strength_a",
                "is_skipped": False,
                "is_logged": True,
                "phase": 1,
                "week_num": 3,
            }],
            "phase_config": [{"phase_name": "Foundation"}],
        }
        client, self.app = _build_status_client(fixtures)
        resp = client.get("/api/health/status", headers={"X-API-Key": VALID_KEY})
        body = resp.json()
        self.assertEqual(body["today_summary"]["plan_id"], 42)
        self.assertEqual(body["today_summary"]["session_type"], "strength_a")
        self.assertTrue(body["today_summary"]["exists"])
        self.assertTrue(body["today_summary"]["is_logged"])
        self.assertIsNotNone(body["banner"])
        self.assertEqual(body["banner"]["phase"], 1)
        self.assertEqual(body["banner"]["week_num"], 3)
        self.assertEqual(body["banner"]["phase_name"], "Foundation")


# ---------------------------------------------------------------------------
# /log + /today/logged — write path
# ---------------------------------------------------------------------------

class _LogCaptureSession:
    """DB shim that captures INSERTs (executemany-style) and answers SELECTs
    from a small fixtures dict. Single transaction, no real DB."""

    def __init__(self, fixtures: dict | None = None):
        self.inserted: list[dict] = []
        self.committed = False
        self.rolled_back = False
        self._fixtures = fixtures or {}
        self._log_counter = 100

    def execute(self, stmt, params=None):
        sql = str(stmt).strip().lower()
        if sql.startswith("insert"):
            assert params is not None
            self._log_counter += 1
            row = {
                "log_id": self._log_counter,
                "plan_id": params["plan_id"],
                "log_type": params["log_type"],
                "exercise": params["exercise"],
                "set_num": params["set_num"],
                "reps_done": params["reps_done"],
                "weight_lbs": params["weight_lbs"],
                "duration_sec": params["duration_sec"],
                "distance_m": params["distance_m"],
                "hr_avg": params["hr_avg"],
                "hr_peak": params["hr_peak"],
                "rpe_actual": params["rpe_actual"],
                "notes": params["notes"],
                "is_skipped": params["is_skipped"],
                "logged_at": datetime(2026, 6, 8, 18, 30, 0),
                "logged_via": params["logged_via"],
            }
            self.inserted.append(row)
            return _MockExecuteResult(row)
        # SELECT plan_id for today
        if "from health.plan" in sql:
            return _MockExecuteResult(self._fixtures.get("today_plan"))
        # GET /today/logged group-by
        if "group by exercise" in sql:
            rows = self._fixtures.get("logged_groups", [])
            return _MockMultiResult(rows)
        # GET /today/logged session_summary check
        if "log_type = 'session_summary'" in sql:
            return _MockExecuteResult(self._fixtures.get("summary_row"))
        return _MockExecuteResult(None)

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True


def _build_log_client(fixtures: dict | None = None) -> tuple:
    try:
        from fastapi.testclient import TestClient
    except ImportError:
        raise unittest.SkipTest("fastapi not installed")
    from api.app.database import get_db
    from api.app.main import app
    from api.app.routers import health as health_router

    sess = _LogCaptureSession(fixtures)

    def _override_db():
        yield sess

    app.dependency_overrides[get_db] = _override_db
    health_router._HEALTH_API_KEY = VALID_KEY
    return TestClient(app), app, sess


class TestLogEndpoint(unittest.TestCase):
    def setUp(self):
        self.app = None

    def tearDown(self):
        if self.app is not None:
            self.app.dependency_overrides.clear()
        from api.app.routers import health as health_router
        health_router._HEALTH_API_KEY = None

    def test_401_without_key(self):
        client, self.app, _ = _build_log_client()
        resp = client.post("/api/health/log", json={
            "exercise": "Goblet squat", "sets": [{"set_num": 1, "reps_done": 10, "weight_lbs": 35, "rpe_actual": 7}],
        })
        self.assertEqual(resp.status_code, 401)

    def test_strength_three_sets_inserts_three_rows(self):
        client, self.app, sess = _build_log_client()
        resp = client.post(
            "/api/health/log",
            headers={"X-API-Key": VALID_KEY},
            json={
                "plan_id": 42,
                "exercise": "Goblet squat",
                "log_type": "strength_set",
                "sets": [
                    {"set_num": 1, "reps_done": 10, "weight_lbs": 35, "rpe_actual": 7},
                    {"set_num": 2, "reps_done": 10, "weight_lbs": 35, "rpe_actual": 7.5},
                    {"set_num": 3, "reps_done": 8,  "weight_lbs": 35, "rpe_actual": 8},
                ],
            },
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["inserted"], 3)
        self.assertEqual(len(body["rows"]), 3)
        # logged_via='manual' (CHECK constraint forbids 'gym_display')
        self.assertEqual(body["rows"][0]["logged_via"], "manual")
        self.assertEqual(body["rows"][0]["plan_id"], 42)
        # Transaction committed
        self.assertTrue(sess.committed)
        self.assertFalse(sess.rolled_back)
        # All three INSERTs captured
        self.assertEqual(len(sess.inserted), 3)
        self.assertEqual(sess.inserted[2]["reps_done"], 8)

    def test_resolves_plan_id_from_today_when_omitted(self):
        client, self.app, sess = _build_log_client(fixtures={
            "today_plan": {"plan_id": 99},
        })
        resp = client.post(
            "/api/health/log",
            headers={"X-API-Key": VALID_KEY},
            json={
                "exercise": "Plank",
                "log_type": "strength_set",
                "sets": [{"set_num": 1, "duration_sec": 30, "rpe_actual": 7}],
            },
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["plan_id"], 99)
        self.assertEqual(sess.inserted[0]["plan_id"], 99)

    def test_cardio_block_single_row(self):
        client, self.app, sess = _build_log_client()
        resp = client.post(
            "/api/health/log",
            headers={"X-API-Key": VALID_KEY},
            json={
                "plan_id": 51,
                "exercise": "Long Z2 Bike",
                "log_type": "cardio_block",
                "sets": [{
                    "duration_sec": 2700, "distance_m": 18000,
                    "hr_avg": 132, "hr_peak": 148, "rpe_actual": 5.5,
                }],
                "notes": "Felt easy",
            },
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["inserted"], 1)
        self.assertEqual(body["rows"][0]["log_type"], "cardio_block")
        self.assertEqual(body["rows"][0]["duration_sec"], 2700)
        self.assertEqual(body["rows"][0]["hr_avg"], 132)
        # notes fall-through from body when set-level is null
        self.assertEqual(body["rows"][0]["notes"], "Felt easy")

    def test_400_on_empty_sets(self):
        client, self.app, _ = _build_log_client()
        resp = client.post(
            "/api/health/log",
            headers={"X-API-Key": VALID_KEY},
            json={"exercise": "Plank", "log_type": "strength_set", "sets": []},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["detail"]["error"], "empty_sets")

    def test_400_on_invalid_log_type(self):
        client, self.app, _ = _build_log_client()
        resp = client.post(
            "/api/health/log",
            headers={"X-API-Key": VALID_KEY},
            json={"log_type": "garbage", "sets": [{"set_num": 1}]},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["detail"]["error"], "invalid_log_type")

    def test_pydantic_clamps_rpe_above_10(self):
        client, self.app, _ = _build_log_client()
        resp = client.post(
            "/api/health/log",
            headers={"X-API-Key": VALID_KEY},
            json={
                "exercise": "Plank",
                "log_type": "strength_set",
                "sets": [{"set_num": 1, "reps_done": 10, "rpe_actual": 11}],
            },
        )
        # Field constraint ge=1, le=10 → 422 validation error
        self.assertEqual(resp.status_code, 422)

    def test_session_summary_single_row(self):
        client, self.app, sess = _build_log_client()
        resp = client.post(
            "/api/health/log",
            headers={"X-API-Key": VALID_KEY},
            json={
                "plan_id": 42,
                "log_type": "session_summary",
                "sets": [{"rpe_actual": 7.5}],
                "notes": "Solid session.",
            },
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["inserted"], 1)
        self.assertEqual(sess.inserted[0]["log_type"], "session_summary")
        self.assertEqual(sess.inserted[0]["notes"], "Solid session.")


class TestTodayLoggedEndpoint(unittest.TestCase):
    def setUp(self):
        self.app = None

    def tearDown(self):
        if self.app is not None:
            self.app.dependency_overrides.clear()
        from api.app.routers import health as health_router
        health_router._HEALTH_API_KEY = None

    def test_401_without_key(self):
        client, self.app, _ = _build_log_client()
        resp = client.get("/api/health/today/logged")
        self.assertEqual(resp.status_code, 401)

    def test_no_plan_returns_empty_payload(self):
        client, self.app, _ = _build_log_client(fixtures={"today_plan": None})
        resp = client.get("/api/health/today/logged", headers={"X-API-Key": VALID_KEY})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertIsNone(body["plan_id"])
        self.assertEqual(body["exercises"], [])
        self.assertFalse(body["has_session_summary"])

    def test_logged_exercises_are_grouped(self):
        client, self.app, _ = _build_log_client(fixtures={
            "today_plan": {"plan_id": 42},
            "logged_groups": [
                {"exercise": "Goblet squat", "log_type": "strength_set", "n": 3},
                {"exercise": "Plank",        "log_type": "strength_set", "n": 1},
            ],
            "summary_row": None,
        })
        resp = client.get("/api/health/today/logged", headers={"X-API-Key": VALID_KEY})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["plan_id"], 42)
        self.assertEqual(len(body["exercises"]), 2)
        self.assertEqual(body["exercises"][0]["exercise"], "Goblet squat")
        self.assertEqual(body["exercises"][0]["set_count"], 3)
        self.assertFalse(body["has_session_summary"])

    def test_session_summary_present_flag(self):
        client, self.app, _ = _build_log_client(fixtures={
            "today_plan": {"plan_id": 42},
            "logged_groups": [],
            "summary_row": {"col": 1},
        })
        resp = client.get("/api/health/today/logged", headers={"X-API-Key": VALID_KEY})
        body = resp.json()
        self.assertTrue(body["has_session_summary"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
