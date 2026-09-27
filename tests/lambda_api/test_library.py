"""SESSION-LIB — GET /api/health/library serves the box-built library, fail closed."""
import os as _os; _os.environ["ARTEMIS_TEST_NO_DB"] = "1"  # TEST-DB-GUARD: never a real DB
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

VALID_KEY = "test-key-library"


class _Result:
    def __init__(self, row):
        self._row = row

    def mappings(self):
        return self

    def first(self):
        return self._row


class _Session:
    def __init__(self, value):
        self.value = value

    def execute(self, stmt, params=None):
        sql = str(stmt).lower()
        if "from acos.system_state" in sql:
            return _Result({"value": self.value} if self.value is not None else None)
        return _Result(None)            # timezone override: none


def _client(value):
    try:
        from fastapi.testclient import TestClient
    except ImportError:
        raise unittest.SkipTest("fastapi not installed")
    from api.app.database import get_db
    from api.app.main import app
    from api.app.routers import health as health_router

    def _db():
        yield _Session(value)
    app.dependency_overrides[get_db] = _db
    health_router._HEALTH_API_KEY = VALID_KEY
    return TestClient(app), app


class TestLibrary(unittest.TestCase):
    def tearDown(self):
        from api.app.main import app
        from api.app.routers import health as health_router
        app.dependency_overrides.clear()
        health_router._HEALTH_API_KEY = None

    def _get(self, value):
        client, _ = _client(value)
        return client.get("/api/health/library", headers={"X-API-Key": VALID_KEY})

    def test_not_built_is_unavailable_with_the_fix(self):
        body = self._get(None).json()
        self.assertFalse(body["available"])
        self.assertIn("session_library", body["reason"])

    def test_unreadable_is_unavailable(self):
        self.assertFalse(self._get("{not json").json()["available"])

    def test_an_old_library_is_marked_stale(self):
        lib = {"generated_on": "2027-01-01", "week_num": 9, "today_location_key": "office",
               "locations": [{"key": "office", "display": "office gym", "sessions": []}]}
        body = self._get(json.dumps(lib)).json()
        self.assertTrue(body["available"])
        self.assertTrue(body["stale"])
        self.assertEqual(body["locations"][0]["key"], "office")

    def test_todays_library_is_not_stale(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from api.app.routers.health import HOME_TIMEZONE
        today = datetime.now(ZoneInfo(HOME_TIMEZONE)).date().isoformat()
        body = self._get(json.dumps({"generated_on": today, "locations": []})).json()
        self.assertFalse(body["stale"])

    def test_needs_the_key(self):
        client, _ = _client(None)
        self.assertEqual(client.get("/api/health/library").status_code, 401)


if __name__ == "__main__":
    unittest.main()
