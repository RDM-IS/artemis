"""MAKEUP-2 — RETIRED by SESSION-OPEN (Ryan, 2026-11-09). POST /api/health/makeup
answers 410 with a clear message and writes nothing. Synthetic only."""
import os as _os; _os.environ["ARTEMIS_TEST_NO_DB"] = "1"  # TEST-DB-GUARD: never a real DB
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

KEY = "test-key-makeup"


class _DB:
    def __init__(self):
        self.calls = []

    def execute(self, *a, **k):
        self.calls.append(a)
        raise AssertionError("a retired endpoint must not touch the database")

    def commit(self):
        raise AssertionError("a retired endpoint must not commit")

    def rollback(self):
        pass


def _post(db, body=None, key=KEY):
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
        headers = {"X-API-Key": key} if key else {}
        return TestClient(app).post("/api/health/makeup", headers=headers,
                                    json=body or {"missed_plan_id": 11, "rest_plan_id": 22})
    finally:
        app.dependency_overrides.clear()
        h._HEALTH_API_KEY = None


class TestMakeupRetired(unittest.TestCase):
    def test_410_with_a_clear_message(self):
        db = _DB()
        r = _post(db)
        self.assertEqual(r.status_code, 410)
        d = r.json()["detail"]
        self.assertEqual(d["error"], "retired")
        self.assertIn("retired", d["message"])
        self.assertIn("Sessions tab", d["message"])
        self.assertEqual(db.calls, [])

    def test_any_body_gets_the_same_answer(self):
        self.assertEqual(_post(_DB(), body={"anything": 1}).status_code, 410)

    def test_still_needs_the_key(self):
        self.assertEqual(_post(_DB(), key=None).status_code, 401)


if __name__ == "__main__":
    unittest.main()
