"""One structured log line per POST /api/health/ingest (2026-09-22).

Drives real requests through the health router and its middleware with
FastAPI's TestClient: the DB is a mock and the key loader is patched. It checks
every refusal path, including the 401/422 raised BEFORE the handler, and that
neither the key nor any payload content ever reaches the line.

Run:
    python3 -m unittest tests.test_ingest_request_log
"""
import os as _os; _os.environ["ARTEMIS_TEST_NO_DB"] = "1"  # TEST-DB-GUARD: never a real DB
import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "api"))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.routers import health  # noqa: E402

KEY = "right-watch-key-7f3a"
WRONG = "wrong-key-SECRET-91c2"
MARKER = "PAYLOAD-MARKER-5d8e"          # must never appear in a log line


def _app() -> FastAPI:
    app = FastAPI()
    app.middleware("http")(health.log_ingest_request)
    app.include_router(health.router, prefix="/api/health")
    app.dependency_overrides[health.get_db] = lambda: MagicMock()
    return app


class TestIngestRequestLog(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(_app(), raise_server_exceptions=False)
        p = patch.object(health, "_load_watch_key", return_value=KEY)
        p.start()
        self.addCleanup(p.stop)

    def _post(self, **kw):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            resp = self.client.post("/api/health/ingest", **kw)
        lines = [ln for ln in out.getvalue().splitlines() if "watch_ingest_request" in ln]
        self.assertEqual(len(lines), 1, f"expected exactly one log line, got {lines}")
        for secret in (KEY, WRONG, MARKER):
            self.assertNotIn(secret, lines[0])
        return resp, json.loads(lines[0])

    def test_no_key(self):
        resp, rec = self._post(json={"data": {}})
        self.assertEqual((resp.status_code, rec["status"]), (401, 401))
        self.assertEqual(rec["reason"], "no X-API-Key header")

    def test_wrong_key(self):
        resp, rec = self._post(json={"data": {"workouts": [{"name": MARKER}]}},
                               headers={"X-API-Key": WRONG})
        self.assertEqual(rec["status"], 401)
        self.assertEqual(rec["reason"], "X-API-Key is not the watch ingest key")
        self.assertNotIn("received", rec)               # refused before anything was read

    def test_body_not_json_is_a_422_before_the_handler(self):
        resp, rec = self._post(content=f"not json {MARKER}",
                               headers={"X-API-Key": KEY, "Content-Type": "application/json"})
        self.assertEqual((resp.status_code, rec["status"]), (422, 422))
        self.assertIn("request validation failed", rec["reason"])
        self.assertEqual(rec["bytes"], len(f"not json {MARKER}"))

    def test_body_not_an_object(self):
        resp, rec = self._post(json=[MARKER], headers={"X-API-Key": KEY})
        self.assertEqual(rec["status"], 422)
        self.assertIn("reason", rec)

    def test_too_many_workouts_names_the_numbers(self):
        body = {"data": {"workouts": [{"name": MARKER, "start": "2026-09-22 05:00:00 -0500"},
                                      {"name": MARKER, "start": "2026-09-22 06:00:00 -0500"}]}}
        with patch.object(health, "MAX_WORKOUTS_PER_REQUEST", 1):
            resp, rec = self._post(json=body, headers={"X-API-Key": KEY})
        self.assertEqual((resp.status_code, rec["status"]), (413, 413))
        self.assertEqual(rec["reason"], "too many workouts: 2, limit 1")
        self.assertEqual(rec["received"]["workouts_in"], 2)
        self.assertNotIn("inserted", rec)

    def test_an_unhandled_error_is_logged_as_500_with_its_type_only(self):
        with patch("knowledge.watch_payload.parse_counts", side_effect=RuntimeError(MARKER)):
            resp, rec = self._post(json={"data": {}}, headers={"X-API-Key": KEY})
        self.assertEqual((resp.status_code, rec["status"]), (500, 500))
        self.assertEqual(rec["reason"], "unhandled RuntimeError")

    def test_other_paths_are_not_logged(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.client.get("/api/health/overview")
        self.assertNotIn("watch_ingest_request", out.getvalue())


class TestLineShape(unittest.TestCase):
    def test_a_success_line_has_counts_and_no_reason(self):
        state = MagicMock(spec=["ingest"])
        state.ingest = {"received": {"samples": 7312, "workouts_in": 8},
                        "inserted": {"samples": 15, "workouts": 8, "heart_rate": 0},
                        "duplicates": {"samples": 3464, "workouts": 0},
                        "workout_match": {"matched": 5, "unmatched": 3, "changed": 5}}
        rec = json.loads(health.ingest_log_line(200, state=state, content_length=2_345_678,
                                                elapsed_ms=812))
        self.assertEqual((rec["event"], rec["status"], rec["bytes"], rec["ms"]),
                         ("watch_ingest_request", 200, 2_345_678, 812))
        self.assertEqual(rec["inserted"]["workouts"], 8)
        self.assertNotIn("reason", rec)

    def test_numeric_drops_the_metric_name_map(self):
        self.assertEqual(health._numeric({"samples": 3, "ignored_metrics": {"x": 1}, "ok": True}),
                         {"samples": 3})

    def test_main_registers_the_middleware(self):
        src = (ROOT / "api" / "app" / "main.py").read_text()
        self.assertIn('app.middleware("http")(health_router.log_ingest_request)', src)


if __name__ == "__main__":
    unittest.main()
