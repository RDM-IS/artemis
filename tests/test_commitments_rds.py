"""Tests for the commitment tracker after the SQLite→RDS migration.

Two tiers (mirrors tests/test_inbox_rds.py / test_quiet_hours_rds.py):

  * MOCKED unit tests (always run) — CRUD/query SQL with knowledge.db mocked:
    RETURNING-id insert, ILIKE client match, the CT-anchoring of get_due_soon /
    get_start_alerts (and that close uses now()), the fuzzy close logic, the
    format_* helpers against TIMESTAMPTZ datetimes, and the audit repoints
    (log_claude_call→acos.audit_log, log_calendar_action→acos.calendar_audit).

  * LIVE integration tests (skipped unless a local Postgres is reachable) —
    migration 020 applies clean and the real functions round-trip against
    acos.commitments: create/list/close and the CT-anchored due-soon / start-alert
    queries, by pointing knowledge.db.get_connection at a throwaway database.

Run:
    python3 tests/test_commitments_rds.py
"""

import os
import sys
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

os.environ.setdefault("RDS_HOST", "test-host")
os.environ.setdefault("RDS_DB", "test-db")

from artemis import commitments as cm  # noqa: E402

_MIG_020 = (_REPO_ROOT / "migrations" / "020_commitments.sql").read_text()


# ============================================================================
# CRUD + query SQL
# ============================================================================

class TestCrud(unittest.TestCase):
    def test_add_returns_new_id(self):
        with patch("artemis.commitments.execute_write", return_value={"id": 7}) as w:
            self.assertEqual(cm.add_commitment("Ship it", "2026-07-01", 2, "Acme"), 7)
        sql, params = w.call_args[0]
        self.assertIn("INSERT INTO acos.commitments", sql)
        self.assertIn("RETURNING id", sql)
        self.assertEqual(params, ("Ship it", "2026-07-01", 2, "Acme"))

    def test_add_no_row_returns_zero(self):
        with patch("artemis.commitments.execute_write", return_value=None):
            self.assertEqual(cm.add_commitment("x", "2026-07-01"), 0)

    def test_list_filters_status(self):
        with patch("artemis.commitments.execute_query", return_value=[]) as q:
            cm.list_commitments("done")
        sql, params = q.call_args[0]
        self.assertIn("WHERE status = %s", sql)
        self.assertIn("ORDER BY due_date", sql)
        self.assertEqual(params, ("done",))

    def test_client_match_uses_ilike(self):
        with patch("artemis.commitments.execute_query", return_value=[]) as q:
            cm.get_commitments_for_client("acme")
        sql, params = q.call_args[0]
        self.assertIn("client ILIKE %s", sql)
        self.assertEqual(params, ("%acme%",))

    def test_update_status(self):
        with patch("artemis.commitments.execute_write") as w:
            cm.update_status(3, "blocked")
        self.assertEqual(w.call_args[0][1], ("blocked", 3))


# ============================================================================
# CT-anchoring of due-date logic
# ============================================================================

class TestCTAnchoring(unittest.TestCase):
    def test_due_soon_ct_anchored(self):
        with patch("artemis.commitments.execute_query", return_value=[]) as q:
            cm.get_due_soon(3)
        sql, params = q.call_args[0]
        self.assertIn("America/Chicago", sql)
        self.assertNotIn("current_date", sql.lower())
        self.assertEqual(params, (3,))

    def test_start_alerts_ct_anchored(self):
        with patch("artemis.commitments.execute_query", return_value=[]) as q:
            cm.get_start_alerts()
        sql = q.call_args[0][0]
        self.assertIn("America/Chicago", sql)
        self.assertIn("<= effort_days", sql)

    def test_close_uses_now_not_a_date_comparison(self):
        with patch("artemis.commitments.list_commitments",
                   return_value=[{"id": 1, "title": "Send the SOW"}]), \
             patch("artemis.commitments.execute_write") as w:
            res = cm.close_commitment("Send the SO")
        self.assertEqual(res["status"], "closed")
        self.assertIn("closed_at = now()", w.call_args[0][0])


# ============================================================================
# close_commitment fuzzy logic
# ============================================================================

class TestClose(unittest.TestCase):
    def test_not_found_when_empty(self):
        with patch("artemis.commitments.list_commitments", return_value=[]):
            self.assertEqual(cm.close_commitment("x"), {"status": "not_found", "open": []})

    def test_ambiguous(self):
        rows = [{"id": 1, "title": "review draft"}, {"id": 2, "title": "review deck"}]
        with patch("artemis.commitments.list_commitments", return_value=rows), \
             patch("artemis.commitments.execute_write"):
            res = cm.close_commitment("review")
        self.assertEqual(res["status"], "ambiguous")
        self.assertEqual(len(res["matches"]), 2)

    def test_no_match(self):
        rows = [{"id": 1, "title": "totally different"}]
        with patch("artemis.commitments.list_commitments", return_value=rows):
            res = cm.close_commitment("zzzzz")
        self.assertEqual(res["status"], "not_found")


# ============================================================================
# format helpers (TIMESTAMPTZ datetime created_at must not break)
# ============================================================================

class TestFormat(unittest.TestCase):
    def test_commitments_list_with_datetime_created_at(self):
        c = {"title": "Ship", "client": "Acme", "due_date": "2026-07-01",
             "created_at": datetime(2026, 6, 20, 14, 30, tzinfo=timezone.utc)}
        out = cm.format_commitments_list([c])
        self.assertIn("**Ship** (Acme)", out)
        self.assertIn("due 2026-07-01", out)
        self.assertIn("created 2026-06-20", out)     # str(datetime)[:10]

    def test_close_result_strings(self):
        self.assertIn("closed", cm.format_close_result({"status": "closed", "title": "T"}))
        self.assertEqual(cm.format_close_result({"status": "not_found", "open": []}),
                         "No open commitments.")

    def test_parse_close_title(self):
        self.assertEqual(cm.parse_close_title('close commitment "Send SOW"'), "Send SOW")
        self.assertEqual(cm.parse_close_title("close the deck"), "the deck")
        self.assertIsNone(cm.parse_close_title("close commitment"))


# ============================================================================
# audit repoints
# ============================================================================

class TestAuditRepoints(unittest.TestCase):
    def test_log_claude_call_routes_to_acos_audit_log(self):
        with patch("knowledge.db.log_audit") as audit:
            cm.log_claude_call("claude-haiku-4-5-20251001", "abc123", 512)
        kw = audit.call_args.kwargs
        self.assertEqual(kw["agent"], "claude")
        self.assertEqual(kw["metadata"]["model"], "claude-haiku-4-5-20251001")
        self.assertEqual(kw["metadata"]["response_length"], 512)

    def test_log_claude_call_best_effort(self):
        with patch("knowledge.db.log_audit", side_effect=RuntimeError("down")):
            cm.log_claude_call("m", "h", 1)   # must not raise

    def test_log_calendar_action_routes_to_acos_calendar_audit(self):
        with patch("knowledge.db.log_calendar_audit") as cal:
            cm.log_calendar_action("create", "evt1", summary="Sync",
                                   attendees="a@x.com, b@y.com", user_approved=True)
        kw = cal.call_args.kwargs
        self.assertEqual(kw["action"], "create")
        self.assertEqual(kw["title"], "Sync")
        self.assertEqual(kw["attendees"], ["a@x.com", "b@y.com"])  # string → list
        self.assertEqual(kw["approved_by"], "ryan")               # user_approved → approved_by

    def test_log_calendar_action_best_effort(self):
        with patch("knowledge.db.log_calendar_audit", side_effect=RuntimeError("down")):
            cm.log_calendar_action("draft", "pending")   # must not raise


# ============================================================================
# Migration 020 shape
# ============================================================================

class TestMigrationShape(unittest.TestCase):
    def test_table_columns_types(self):
        self.assertIn("acos.commitments", _MIG_020)
        self.assertIn("id           BIGSERIAL PRIMARY KEY", _MIG_020)
        self.assertIn("due_date     DATE NOT NULL", _MIG_020)
        self.assertIn("created_at   TIMESTAMPTZ NOT NULL DEFAULT now()", _MIG_020)
        self.assertIn("closed_at    TIMESTAMPTZ", _MIG_020)
        self.assertIn("status       TEXT NOT NULL DEFAULT 'active'", _MIG_020)
        for col in ["title", "effort_days", "client"]:
            self.assertIn(col, _MIG_020)

    def test_indexes(self):
        self.assertIn("idx_acos_commitments_status_due", _MIG_020)
        self.assertIn("idx_acos_commitments_client", _MIG_020)


# ============================================================================
# LIVE Postgres integration (skipped unless a local PG is reachable)
# ============================================================================

import psycopg2  # noqa: E402

_TEST_DB = "artemis_commitments_test"
_LIVE = False


def _admin_connect():
    return psycopg2.connect(dbname="postgres", connect_timeout=2)


@contextmanager
def _live_get_connection():
    conn = psycopg2.connect(dbname=_TEST_DB)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


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
            cur.execute("CREATE SCHEMA IF NOT EXISTS acos")
            cur.execute(_MIG_020)
        conn.close()
        _LIVE = True
    except Exception as e:
        sys.stderr.write(f"[test_commitments_rds] live PG unavailable, skipping: {e}\n")
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


class TestLiveCommitments(unittest.TestCase):
    def setUp(self):
        if not _LIVE:
            self.skipTest("no local Postgres")
        self._patch = patch("knowledge.db.get_connection", _live_get_connection)
        self._patch.start()
        with _live_get_connection() as c:
            with c.cursor() as cur:
                cur.execute("TRUNCATE acos.commitments RESTART IDENTITY")

    def tearDown(self):
        if _LIVE:
            self._patch.stop()

    def _ct_today(self):
        with _live_get_connection() as c:
            with c.cursor() as cur:
                cur.execute("SELECT (now() AT TIME ZONE 'America/Chicago')::date")
                return cur.fetchone()[0]

    def test_create_list_close_roundtrip(self):
        cid = cm.add_commitment("Send the SOW", "2026-07-01", effort_days=2, client="Acme")
        self.assertIsInstance(cid, int)
        self.assertGreater(cid, 0)
        active = cm.list_commitments("active")
        self.assertEqual([c["title"] for c in active], ["Send the SOW"])
        # status default 'active'; due_date is a real DATE
        self.assertEqual(active[0]["status"], "active")
        res = cm.close_commitment("Send the SO")          # fuzzy
        self.assertEqual(res["status"], "closed")
        self.assertEqual(cm.list_commitments("active"), [])
        closed = cm.list_commitments("closed")
        self.assertEqual(len(closed), 1)
        self.assertIsNotNone(closed[0]["closed_at"])   # set by now()

    def test_update_status(self):
        cid = cm.add_commitment("Block me", "2026-07-01")
        cm.update_status(cid, "blocked")
        self.assertEqual(cm.list_commitments("blocked")[0]["id"], cid)

    def test_due_soon_ct_anchored(self):
        today = self._ct_today()
        cm.add_commitment("Due today", today.isoformat())
        cm.add_commitment("Future", (today + timedelta(days=5)).isoformat())
        titles = {c["title"] for c in cm.get_due_soon(0)}      # due on/before CT today
        self.assertIn("Due today", titles)
        self.assertNotIn("Future", titles)
        titles3 = {c["title"] for c in cm.get_due_soon(3)}
        self.assertEqual(titles3, {"Due today"})               # Future is 5 days out

    def test_start_alerts_ct_anchored(self):
        today = self._ct_today()
        # remaining 2 days <= effort 3 → should start now
        cm.add_commitment("Start now", (today + timedelta(days=2)).isoformat(), effort_days=3)
        # remaining 10 days > effort 1 → not yet
        cm.add_commitment("Later", (today + timedelta(days=10)).isoformat(), effort_days=1)
        titles = {c["title"] for c in cm.get_start_alerts()}
        self.assertIn("Start now", titles)
        self.assertNotIn("Later", titles)

    def test_client_match_case_insensitive(self):
        cm.add_commitment("X", "2026-07-01", client="BigCorp")
        self.assertEqual(len(cm.get_commitments_for_client("bigcorp")), 1)  # ILIKE


# ============================================================================
# No-SQLite regression guard — reintroducing SQLite in this module fails CI
# ============================================================================

class TestNoSqlite(unittest.TestCase):
    def test_module_has_no_sqlite_binding(self):
        self.assertFalse(hasattr(cm, "sqlite3"))

    def test_source_has_no_sqlite_import(self):
        src = Path(cm.__file__).read_text().lower()
        self.assertNotIn("import sqlite3", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
