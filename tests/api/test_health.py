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
from datetime import date
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
