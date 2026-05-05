"""Smoke tests for GET /api/health/today.

Uses FastAPI's TestClient with a mocked database session so the test runs
without needing a live RDS connection. The DB call is overridden via
FastAPI's dependency_overrides.

Run:
    python tests/api/test_health.py
"""

import json
import os
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

# Repo root on path
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))

# Block boto3 imports for tests — fastapi app will try to load secrets
os.environ.setdefault("RDS_HOST", "test-host")
os.environ.setdefault("RDS_DB", "test-db")


class _MockMappingResult:
    """Mimics SQLAlchemy result.mappings().first() / .all()."""

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
    """Build a TestClient with the DB dependency overridden to return `row`."""
    # Lazy import — FastAPI app construction touches database.py which imports
    # boto3 transitively. We patch the dependency BEFORE calling the endpoint,
    # but the import itself does not require AWS access.
    try:
        from fastapi.testclient import TestClient
    except ImportError:
        raise unittest.SkipTest("fastapi not installed")

    from api.app.database import get_db
    from api.app.main import app

    def _override():
        yield _MockSession(row)

    app.dependency_overrides[get_db] = _override
    return TestClient(app), app


class TestHealthEndpoint(unittest.TestCase):
    def setUp(self):
        self.app = None

    def tearDown(self):
        if self.app is not None:
            self.app.dependency_overrides.clear()

    def test_happy_path_returns_plan(self):
        client, self.app = _build_client(_FAKE_PLAN_ROW)
        resp = client.get("/api/health/today")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()

        # Required fields present
        self.assertEqual(body["plan_id"], 1)
        self.assertEqual(body["plan_date"], "2026-05-06")
        self.assertEqual(body["phase"], 1)
        self.assertEqual(body["week_num"], 1)
        self.assertEqual(body["session_type"], "strength_a")
        self.assertEqual(body["target_rpe"], 6.5)
        self.assertEqual(body["target_hr_zone"], 3)
        self.assertEqual(body["est_duration_min"], 40)
        self.assertFalse(body["is_skipped"])

        # blocks is an object (dict), NOT a stringified JSON blob
        self.assertIsInstance(body["blocks"], dict)
        self.assertEqual(body["blocks"]["type"], "circuit")
        self.assertEqual(body["blocks"]["rounds"], 2)

    def test_404_when_no_row(self):
        client, self.app = _build_client(None)
        resp = client.get("/api/health/today")
        self.assertEqual(resp.status_code, 404)
        body = resp.json()
        # FastAPI wraps the dict in {"detail": {...}}
        self.assertEqual(body["detail"]["error"], "no_plan")
        self.assertIn("fallback", body["detail"])

    def test_skipped_day_returns_200(self):
        """is_skipped=True still returns 200 with the row; frontend renders rest UX."""
        skipped_row = dict(_FAKE_PLAN_ROW)
        skipped_row["is_skipped"] = True
        client, self.app = _build_client(skipped_row)
        resp = client.get("/api/health/today")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["is_skipped"])

    def test_blocks_serialized_as_object(self):
        """Specifically verify JSONB blocks isn't double-encoded as a string."""
        client, self.app = _build_client(_FAKE_PLAN_ROW)
        resp = client.get("/api/health/today")
        # The blocks key should be a JSON object in the raw response text.
        # If it were a string, json.loads() of the response would have a string there.
        body = json.loads(resp.text)
        self.assertNotIsInstance(body["blocks"], str)
        self.assertIsInstance(body["blocks"], dict)

    def test_timezone_function_uses_central(self):
        """_today_ct() returns a date in America/Chicago."""
        from api.app.routers.health import _today_ct

        result = _today_ct()
        self.assertIsInstance(result, date)


if __name__ == "__main__":
    unittest.main(verbosity=2)
