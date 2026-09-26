"""Phase 1 schema probe — the THIN test over the one implementation.

The 15 checks live in `api/app/schema_check.py`, because they are reachable over
HTTP in production through `POST /admin/run-tests` and that makes them runtime
code (PACKAGE-IDENTITY, 2026-09-26). This file used to BE that implementation: a
296-line script with `print()` and `sys.exit()` that the endpoint ran with
`subprocess.run([sys.executable, "/var/task/tests/test_phase1_schema.py"])`.

Two things followed from that, and both are gone:

  * the Lambda package had to ship all of `tests/`, so a test-only commit moved
    the package identity and DRIFT-ALARM reported drift for a change that cannot
    affect runtime;
  * this file needed the single TEST-DB-GUARD exemption, because it was runtime
    code living in `tests/` and connecting to the real database on purpose.

It is now an ordinary guarded unit test: it sets ARTEMIS_TEST_NO_DB like every
other test file and drives `run_checks()` with a fake connection. It asserts on
the CONTRACT — the shape, the accounting, the check set — not on a live schema.
The live schema is what the endpoint checks, against the real database, which is
the only place that assertion means anything.

Run:
    python3.11 -m unittest tests.test_phase1_schema
"""

import os as _os; _os.environ["ARTEMIS_TEST_NO_DB"] = "1"  # TEST-DB-GUARD: never a real DB

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "api"))      # the Lambda's own package root

from app import schema_check  # noqa: E402


class FakeCursor:
    """Answers the probe's queries from a dict of canned rows.

    `healthy=True` returns what a correct schema returns, so every check passes;
    `healthy=False` breaks three of them in three different ways.
    """

    def __init__(self, healthy=True):
        self.healthy = healthy
        self._rows: list = []
        self.executed: list[str] = []

    def execute(self, sql, params=()):
        s = " ".join(sql.split())
        self.executed.append(s)
        h = self.healthy
        if "information_schema.tables" in s:
            schema, table = params
            crm = table in ("contacts", "organizations", "deals")
            # a CRM table in acos is the check-10 failure
            self._rows = [{"1": 1}] if ((h and not crm) or (not h and crm)) else []
        elif "information_schema.views" in s:
            self._rows = [{"1": 1}] if h else []
        elif "v_gold_contacts" in s:
            self._rows = []
        elif "acos.entities WHERE name = 'Brian Pivar'" in s:
            self._rows = [{"id": "e-pivar", "layer": "gold", "confidence": 1.0,
                           "osint_source": None, "tags": [], "domain": None}]
        elif "TTI (Techtronic Industries)" in s:
            self._rows = [{"id": "e-tti", "layer": "gold", "confidence": 1.0,
                           "osint_source": None, "tags": [], "domain": None}]
        elif "Lucint Pilot TTI" in s:
            self._rows = [{"id": "e-lucint", "layer": "gold", "confidence": 1.0,
                           "osint_source": None, "tags": [], "domain": "lucint"}]
        elif "Bradley Spaits" in s:
            # the check-7 failure: gold, but the mentor tag is gone
            self._rows = [{"id": "e-spaits", "layer": "gold", "confidence": 1.0,
                           "osint_source": None, "tags": ["mentor"] if h else [],
                           "domain": None}]
        elif "acos.relationships" in s:
            self._rows = [{"relationship_context": "known via TTI"}]
        elif "INSERT INTO acos.entities" in s:
            self._rows = [{"id": "e-probe"}]
        elif "circuit_breaker_status" in s:
            self._rows = [{"cnt": 0 if h else 3}]      # the check-13 failure
        elif "velocity_ledger" in s:
            self._rows = [{"count": 0}]
        elif "INSERT INTO acos.data_vault_satellites" in s:
            import psycopg2
            raise psycopg2.errors.CheckViolation("chk_sensitive_not_syncable")
        else:
            self._rows = []

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows

    def close(self):
        pass


class FakeConn:
    def __init__(self, healthy=True):
        self.cur = FakeCursor(healthy)
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def cursor(self, cursor_factory=None):
        return self.cur

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


def _run(healthy=True):
    """The probe's two knowledge.db calls are patched: under TEST-DB-GUARD they
    would be refused a connection, and their behaviour is not what this file is
    asserting — that the probe REPORTS them is."""
    from knowledge.db import PromotionBlockedError
    conn = FakeConn(healthy)
    with patch("knowledge.db.promote_entity",
               side_effect=PromotionBlockedError("needs ryan_confirmed")), \
         patch("knowledge.db.create_relationship", side_effect=ValueError("empty context")):
        return schema_check.run_checks(conn=conn), conn


class TestTheContract(unittest.TestCase):
    def setUp(self):
        self.result, self.conn = _run(healthy=True)

    def test_it_returns_data_and_never_exits(self):
        self.assertIsInstance(self.result, dict)
        self.assertEqual(set(self.result), {"ok", "passed", "failed", "total", "checks"})

    def test_a_healthy_schema_passes_every_check(self):
        failed = [c["name"] for c in self.result["checks"] if not c["ok"]]
        self.assertEqual(failed, [], f"unexpected failures: {failed}")
        self.assertTrue(self.result["ok"])

    def test_the_accounting_adds_up(self):
        self.assertEqual(self.result["passed"] + self.result["failed"],
                         self.result["total"])
        self.assertEqual(self.result["total"], len(self.result["checks"]))

    def test_all_sixteen_checks_run(self):
        """15 numbered checks, and 12 has an 'a'/'b' pair — 16 results."""
        self.assertEqual(self.result["total"], 16)
        names = " ".join(c["name"] for c in self.result["checks"])
        for n in range(1, 16):
            with self.subTest(check=n):
                self.assertIn(f"{n}. ", names)
        self.assertIn("12b.", names)

    def test_an_injected_connection_is_left_open_for_its_owner_to_close(self):
        self.assertFalse(self.conn.closed)


class TestItReportsFailuresRatherThanRaising(unittest.TestCase):
    def setUp(self):
        self.result, _ = _run(healthy=False)

    def test_a_broken_schema_is_reported_not_raised(self):
        self.assertFalse(self.result["ok"])
        self.assertGreater(self.result["failed"], 0)

    def test_each_break_lands_on_its_own_check(self):
        failed = {c["name"].split(".")[0] for c in self.result["checks"] if not c["ok"]}
        # 1/2 missing tables, 3 no view, 7 no mentor tag, 10 CRM tables in acos,
        # 13 circuit_breaker not empty
        for n in ("7", "10", "13"):
            with self.subTest(check=n):
                self.assertIn(n, failed)

    def test_a_failed_check_carries_a_detail_and_a_passing_one_does_not(self):
        for c in self.result["checks"]:
            with self.subTest(check=c["name"]):
                if c["ok"]:
                    self.assertEqual(c["detail"], "")


class TestTheReport(unittest.TestCase):
    def test_it_renders_the_same_shape_the_script_printed(self):
        result, _ = _run(healthy=True)
        text = schema_check.format_report(result)
        self.assertIn("=== Phase 1 Schema Validation ===", text)
        self.assertIn("[PASS] 1. All 8 acos tables exist", text)
        self.assertIn(f"Results: {result['passed']}/{result['total']} passed", text)
        self.assertNotIn("FAILED", text)

    def test_a_failure_is_marked_and_counted(self):
        result, _ = _run(healthy=False)
        text = schema_check.format_report(result)
        self.assertIn("[FAIL]", text)
        self.assertIn(f"{result['failed']} FAILED", text)


class TestItNeedsNoDatabaseToBeImported(unittest.TestCase):
    """The reason the TEST-DB-GUARD exemption is gone."""

    def test_importing_the_probe_opens_nothing(self):
        self.assertTrue(hasattr(schema_check, "run_checks"))
        self.assertTrue(hasattr(schema_check, "format_report"))

    def test_a_missing_rds_host_is_a_clear_error_not_a_crash(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(schema_check.SchemaCheckError):
                schema_check.run_checks()

if __name__ == "__main__":
    unittest.main()
