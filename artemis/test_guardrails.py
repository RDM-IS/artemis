"""Tests for the external-attendee guardrail and its violation logging.

Two tiers (mirrors tests/test_nutrition.py):

  * MOCKED unit tests (always run) — the external-attendee classifier
    (get_external_attendees / check_external_attendees decision logic) and the
    violation-logging path. The DB layer (knowledge.db.log_guardrail_violation)
    is mocked, so no AWS/RDS access is needed. Also asserts the module carries
    no SQLite path after the RDS cut.

  * LIVE integration tests (skipped unless a local Postgres is reachable) —
    migration 002's acos.guardrail_violations applies clean and the exact INSERT
    that knowledge.db.log_guardrail_violation issues round-trips: external
    attendees stored as a TEXT[], created_at defaulted by now(), id a UUID. These
    run against a throwaway database created and dropped around the module.

Run:
    python3 -m artemis.test_guardrails
    python3 artemis/test_guardrails.py
"""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

# Repo root on path
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

# Block AWS access
os.environ.setdefault("RDS_HOST", "test-host")
os.environ.setdefault("RDS_DB", "test-db")

from artemis import guardrails  # noqa: E402

_MIG_002 = (_REPO_ROOT / "migrations" / "002_create_acos_tables.sql").read_text()
_MIG_001 = (_REPO_ROOT / "migrations" / "001_create_acos_schema.sql").read_text()


# ============================================================================
# Classifier — get_external_attendees / check_external_attendees
# ============================================================================

class TestExternalAttendeeClassifier(unittest.TestCase):
    """Pure decision logic. The block/approve branches call log_violation, so the
    DB writer is mocked throughout to keep these unit-pure."""

    def setUp(self):
        p = patch("knowledge.db.log_guardrail_violation")
        self.mock_log = p.start()
        self.addCleanup(p.stop)

    def test_no_attendees_allowed(self):
        self.assertTrue(guardrails.check_external_attendees("Team standup", None)["allowed"])
        self.assertTrue(guardrails.check_external_attendees("Team standup", [])["allowed"])
        self.mock_log.assert_not_called()  # allowed path never logs

    def test_internal_only_allowed(self):
        for att in (["ryan@rdm.is"], ["ryan@gmail.com"], ["ryan@rdm.is", "ryan@gmail.com"]):
            self.assertTrue(guardrails.check_external_attendees("1:1 sync", att)["allowed"], att)
        self.mock_log.assert_not_called()

    def test_external_without_approval_blocked(self):
        result = guardrails.check_external_attendees(
            "Call with Brad", ["ryan@rdm.is", "brad.spaits@external.com"], user_approved=False,
        )
        self.assertFalse(result["allowed"])
        self.assertIn("brad.spaits@external.com", result["external"])
        self.assertIn("BLOCKED", result["reason"])

    def test_external_with_approval_allowed(self):
        result = guardrails.check_external_attendees(
            "Call with Brad", ["ryan@rdm.is", "brad.spaits@external.com"], user_approved=True,
        )
        self.assertTrue(result["allowed"])

    def test_multiple_externals_all_flagged(self):
        result = guardrails.check_external_attendees(
            "Group call", ["ryan@rdm.is", "alice@acme.com", "bob@corp.net"], user_approved=False,
        )
        self.assertFalse(result["allowed"])
        self.assertEqual(len(result["external"]), 2)

    def test_get_external_attendees_helper(self):
        self.assertEqual(
            guardrails.get_external_attendees(["ryan@rdm.is", "alice@acme.com", "bob@gmail.com"]),
            ["alice@acme.com"],
        )
        self.assertEqual(guardrails.get_external_attendees(None), [])
        # case-insensitive, normalized lowercase
        self.assertEqual(
            guardrails.get_external_attendees(["RYAN@RDM.IS", "Alice@ACME.COM"]),
            ["alice@acme.com"],
        )

    def test_approval_without_external_is_noop(self):
        # user_approved=True with no actual external must NOT manufacture a violation
        result = guardrails.check_external_attendees("Solo work", ["ryan@rdm.is"], user_approved=True)
        self.assertTrue(result["allowed"])
        self.mock_log.assert_not_called()


# ============================================================================
# Violation logging — forwards to acos.guardrail_violations via knowledge.db
# ============================================================================

class TestViolationLogging(unittest.TestCase):
    def test_log_violation_forwards_to_rds(self):
        with patch("knowledge.db.log_guardrail_violation") as mock_log:
            guardrails.log_violation("Call with Brad", ["brad@external.com"], "blocked")
        mock_log.assert_called_once()
        kwargs = mock_log.call_args.kwargs
        self.assertEqual(kwargs["guardrail_type"], "external_calendar_attendee")
        self.assertEqual(kwargs["event_summary"], "Call with Brad")
        self.assertEqual(kwargs["outcome"], "blocked")
        # external_attendees forwarded as a list (becomes a Postgres TEXT[])
        self.assertEqual(kwargs["external_attendees"], ["brad@external.com"])

    def test_blocked_path_logs_blocked(self):
        with patch("knowledge.db.log_guardrail_violation") as mock_log:
            guardrails.check_external_attendees(
                "Call with Brad", ["brad@external.com"], user_approved=False)
        self.assertEqual(mock_log.call_args.kwargs["outcome"], "blocked")

    def test_approved_path_logs_approved(self):
        with patch("knowledge.db.log_guardrail_violation") as mock_log:
            guardrails.check_external_attendees(
                "Call with Brad", ["brad@external.com"], user_approved=True)
        self.assertEqual(mock_log.call_args.kwargs["outcome"], "approved")

    def test_denied_outcome_passes_through(self):
        with patch("knowledge.db.log_guardrail_violation") as mock_log:
            guardrails.log_violation("Call with Brad", ["brad@external.com"], "denied")
        self.assertEqual(mock_log.call_args.kwargs["outcome"], "denied")


# ============================================================================
# No SQLite remains in this module after the RDS cut
# ============================================================================

class TestNoSqlite(unittest.TestCase):
    def test_module_has_no_sqlite(self):
        self.assertFalse(hasattr(guardrails, "sqlite3"))
        self.assertFalse(hasattr(guardrails, "get_db"))
        self.assertFalse(hasattr(guardrails, "_ensure_table"))

    def test_source_has_no_sqlite_path(self):
        src = Path(guardrails.__file__).read_text().lower()
        self.assertNotIn("import sqlite3", src)
        self.assertNotIn("guardrail_violations (", src)  # no CREATE TABLE DDL here

    def test_migration_002_defines_table(self):
        self.assertIn("acos.guardrail_violations", _MIG_002)
        for col in ["guardrail_type", "event_summary", "external_attendees",
                    "outcome", "agent", "metadata"]:
            self.assertIn(col, _MIG_002)


# ============================================================================
# LIVE Postgres integration (skipped unless a local PG is reachable)
# ============================================================================

import psycopg2  # noqa: E402

_TEST_DB = "artemis_guardrails_test"
_LIVE = False


def _admin_connect():
    return psycopg2.connect(dbname="postgres", connect_timeout=2)


def setUpModule():
    global _LIVE
    try:
        admin = _admin_connect()
        admin.autocommit = True
        with admin.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS {_TEST_DB}")
            cur.execute(f"CREATE DATABASE {_TEST_DB}")
        admin.close()
        conn = psycopg2.connect(dbname=_TEST_DB)
        conn.autocommit = True
        with conn.cursor() as cur:
            # gen_random_uuid() default needs pgcrypto on PG < 13
            cur.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
            cur.execute(_MIG_001)
            cur.execute(_MIG_002)
        conn.close()
        _LIVE = True
    except Exception as e:  # no PG, no createdb priv, etc. — skip live tier
        sys.stderr.write(f"[test_guardrails] live PG unavailable, skipping: {e}\n")
        _LIVE = False


def tearDownModule():
    if not _LIVE:
        return
    try:
        admin = _admin_connect()
        admin.autocommit = True
        with admin.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS {_TEST_DB}")
        admin.close()
    except Exception:
        pass


class TestLiveGuardrailViolations(unittest.TestCase):
    def setUp(self):
        if not _LIVE:
            self.skipTest("no local Postgres")
        self.conn = psycopg2.connect(dbname=_TEST_DB)
        self.conn.autocommit = True
        self.cur = self.conn.cursor()
        self.cur.execute("TRUNCATE acos.guardrail_violations RESTART IDENTITY")

    def tearDown(self):
        self.conn.close()

    def test_table_shape(self):
        self.cur.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema='acos' AND table_name='guardrail_violations'"
        )
        self.assertIsNotNone(self.cur.fetchone())
        self.cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='acos' AND table_name='guardrail_violations'"
        )
        cols = {r[0] for r in self.cur.fetchall()}
        self.assertEqual(
            cols,
            {"id", "created_at", "guardrail_type", "event_summary",
             "external_attendees", "outcome", "agent", "metadata"},
        )

    def test_helper_insert_roundtrips(self):
        # The EXACT INSERT knowledge.db.log_guardrail_violation issues. A Python
        # list must adapt to the TEXT[] column and created_at must default.
        self.cur.execute(
            """
            INSERT INTO acos.guardrail_violations (
                guardrail_type, event_summary, external_attendees,
                outcome, agent, metadata
            ) VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id, created_at, external_attendees
            """,
            ("external_calendar_attendee", "Call with Brad",
             ["brad.spaits@external.com", "alice@acme.com"], "blocked", None, "{}"),
        )
        row = self.cur.fetchone()
        self.assertIsNotNone(row[0])                       # id (UUID) generated
        self.assertIsNotNone(row[1])                       # created_at defaulted
        self.assertEqual(row[2], ["brad.spaits@external.com", "alice@acme.com"])

    def test_external_attendees_defaults_empty_array(self):
        self.cur.execute(
            "INSERT INTO acos.guardrail_violations (guardrail_type, outcome) "
            "VALUES ('external_calendar_attendee', 'approved') RETURNING external_attendees"
        )
        self.assertEqual(self.cur.fetchone()[0], [])       # TEXT[] DEFAULT '{}'


if __name__ == "__main__":
    unittest.main(verbosity=2)
