"""Tests for the inbox-zero thread state machine after the SQLite→RDS migration.

Two tiers (mirrors tests/test_nutrition.py):

  * MOCKED unit tests (always run) — upsert/set_state/query SQL is asserted with
    knowledge.db mocked: ON CONFLICT create semantics, state-clear rules, the
    CT-anchoring of every "today" comparison (and the deliberate NON-anchoring of
    the elapsed-time comparisons), get_counts mapping, and the format_* helpers
    against psycopg2-style date objects. No AWS/DB access.

  * LIVE integration tests (skipped unless a local Postgres is reachable) —
    migration 018 applies clean and the real inbox.py functions round-trip
    against acos.inbox_threads (upsert idempotency, set_state transitions,
    get_counts, CT-anchored get_snoozed_due) by pointing knowledge.db.get_connection
    at a throwaway database created and dropped around the module.

Run:
    python3 tests/test_inbox_rds.py
"""

import os
import sys
import unittest
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

# Repo root on path
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

# Block AWS access
os.environ.setdefault("RDS_HOST", "test-host")
os.environ.setdefault("RDS_DB", "test-db")

from artemis import inbox  # noqa: E402

_MIG_018 = (_REPO_ROOT / "migrations" / "018_inbox_threads.sql").read_text()


# ============================================================================
# upsert_thread — ON CONFLICT DO NOTHING create-if-absent
# ============================================================================

class TestUpsert(unittest.TestCase):
    def test_created_returns_true(self):
        with patch("artemis.inbox.execute_write", return_value={"id": "t1"}) as w:
            self.assertTrue(inbox.upsert_thread("t1", "Subj", "a@acme.com"))
        sql, params = w.call_args[0]
        self.assertIn("acos.inbox_threads", sql)
        self.assertIn("ON CONFLICT (id) DO NOTHING", sql)
        self.assertIn("acme.com", params)   # sender_domain extracted

    def test_existing_returns_false(self):
        # ON CONFLICT DO NOTHING → no RETURNING row → execute_write returns None
        with patch("artemis.inbox.execute_write", return_value=None):
            self.assertFalse(inbox.upsert_thread("t1", "Subj", "a@acme.com"))

    def test_sender_without_at_has_blank_domain(self):
        with patch("artemis.inbox.execute_write", return_value={"id": "t1"}) as w:
            inbox.upsert_thread("t1", "Subj", "no-at-sign")
        params = w.call_args[0][1]
        self.assertIn("", params)            # sender_domain blank


# ============================================================================
# set_state — validation, clears, audit best-effort
# ============================================================================

class TestSetState(unittest.TestCase):
    def test_invalid_state_rejected(self):
        with patch("artemis.inbox.execute_write") as w:
            self.assertFalse(inbox.set_state("t1", "BOGUS"))
        w.assert_not_called()

    def test_missing_thread_returns_false(self):
        with patch("artemis.inbox.execute_one", return_value=None), \
             patch("artemis.inbox.execute_write") as w:
            self.assertFalse(inbox.set_state("t1", inbox.DONE))
        w.assert_not_called()

    def test_done_clears_snooze_and_waiting_and_audits(self):
        with patch("artemis.inbox.execute_one", return_value={"id": "t1"}), \
             patch("artemis.inbox.execute_write") as w, \
             patch("knowledge.db.log_audit") as audit:
            self.assertTrue(inbox.set_state("t1", inbox.DONE))
        sql = w.call_args[0][0]
        self.assertIn("UPDATE acos.inbox_threads", sql)
        self.assertIn("last_updated_at = now()", sql)
        self.assertIn("snoozed_until = NULL", sql)     # leaving SNOOZED-able states
        self.assertIn("waiting_on = NULL", sql)
        audit.assert_called_once()                     # acos.audit_log trail

    def test_audit_failure_does_not_break_transition(self):
        with patch("artemis.inbox.execute_one", return_value={"id": "t1"}), \
             patch("artemis.inbox.execute_write"), \
             patch("knowledge.db.log_audit", side_effect=RuntimeError("audit down")):
            self.assertTrue(inbox.set_state("t1", inbox.DONE))

    def test_snoozed_keeps_snooze_column(self):
        with patch("artemis.inbox.execute_one", return_value={"id": "t1"}), \
             patch("artemis.inbox.execute_write") as w, \
             patch("knowledge.db.log_audit"):
            inbox.set_state("t1", inbox.SNOOZED, snoozed_until=date(2026, 7, 1))
        sql = w.call_args[0][0]
        self.assertNotIn("snoozed_until = NULL", sql)  # not cleared when entering SNOOZED
        self.assertIn("snoozed_until = %s", sql)


# ============================================================================
# mark_* helpers
# ============================================================================

class TestMarkHelpers(unittest.TestCase):
    def test_mark_snoozed_invalid_period(self):
        with patch("artemis.inbox.execute_write") as w:
            self.assertFalse(inbox.mark_snoozed("t1", "9y"))
        w.assert_not_called()

    def test_mark_snoozed_uses_ct_today_plus_delta(self):
        captured = {}
        def fake_set_state(tid, state, **kw):
            captured.update(kw); return True
        with patch("artemis.inbox.set_state", side_effect=fake_set_state):
            inbox.mark_snoozed("t1", "3d")
        self.assertEqual(captured["snoozed_until"], inbox._ct_today() + timedelta(days=3))

    def test_mark_waiting_sets_ct_today(self):
        captured = {}
        def fake_set_state(tid, state, **kw):
            captured.update(kw); return True
        with patch("artemis.inbox.set_state", side_effect=fake_set_state):
            inbox.mark_waiting("t1", waiting_on="Bob")
        self.assertEqual(captured["waiting_since"], inbox._ct_today())
        self.assertEqual(captured["waiting_on"], "Bob")


# ============================================================================
# CT-anchoring of "today" comparisons (and deliberate non-anchoring of elapsed)
# ============================================================================

class TestCTAnchoring(unittest.TestCase):
    def _sql_of(self, fn):
        with patch("artemis.inbox.execute_query", return_value=[]) as q:
            fn()
        return q.call_args[0][0]

    def test_due_today_is_ct_anchored(self):
        self.assertIn("America/Chicago", self._sql_of(inbox.get_due_today))

    def test_snoozed_due_is_ct_anchored(self):
        self.assertIn("America/Chicago", self._sql_of(inbox.get_snoozed_due))

    def test_stale_waiting_is_ct_anchored(self):
        self.assertIn("America/Chicago", self._sql_of(lambda: inbox.get_stale_waiting(3)))

    def test_stale_needs_action_is_NOT_ct_anchored(self):
        sql = self._sql_of(lambda: inbox.get_stale_needs_action(24))
        self.assertNotIn("America/Chicago", sql)
        self.assertIn("now() - make_interval", sql)

    def test_can_nudge_is_NOT_ct_anchored(self):
        with patch("artemis.inbox.execute_one", return_value={"can_nudge": True}) as o:
            self.assertTrue(inbox.can_nudge("t1", 12))
        sql = o.call_args[0][0]
        self.assertNotIn("America/Chicago", sql)
        self.assertIn("now() - make_interval", sql)

    def test_can_nudge_missing_thread_true(self):
        with patch("artemis.inbox.execute_one", return_value=None):
            self.assertTrue(inbox.can_nudge("nope"))


# ============================================================================
# get_counts mapping + format_* preserve behavior (date objects from psycopg2)
# ============================================================================

class TestCountsAndFormat(unittest.TestCase):
    def test_get_counts_maps_state_to_cnt(self):
        rows = [{"state": "NEEDS_ACTION", "cnt": 3}, {"state": "WAITING", "cnt": 1}]
        with patch("artemis.inbox.execute_query", return_value=rows):
            self.assertEqual(inbox.get_counts(), {"NEEDS_ACTION": 3, "WAITING": 1})

    def test_format_waiting_list_with_date_object(self):
        ws = inbox._ct_today() - timedelta(days=4)
        out = inbox.format_waiting_list([{"subject": "Re: SOW", "waiting_on": "Bob", "waiting_since": ws}])
        self.assertIn("waiting on Bob (4d)", out)

    def test_format_waiting_list_with_iso_string(self):
        ws = (inbox._ct_today() - timedelta(days=2)).isoformat()
        out = inbox.format_waiting_list([{"subject": "X", "waiting_since": ws}])
        self.assertIn("(2d)", out)
        self.assertIn("waiting on unknown", out)

    def test_format_inbox_status(self):
        out = inbox.format_inbox_status({"NEEDS_ACTION": 2, "WAITING": 1})
        self.assertIn("Needs action: **2**", out)
        self.assertIn("Waiting: **1**", out)

    def test_format_thread_card_short_id_and_commands(self):
        out = inbox.format_thread_card({"id": "abcdef123456789", "subject": "S", "sender": "a@b.com"})
        self.assertIn("done abcdef123456", out)   # 12-char short id
        self.assertIn("S", out)


# ============================================================================
# Migration 018 shape (always-run, no DB)
# ============================================================================

class TestMigrationShape(unittest.TestCase):
    def test_table_and_states(self):
        self.assertIn("acos.inbox_threads", _MIG_018)
        # CHECK enumerates exactly the five states inbox.py writes
        for s in ["NEEDS_ACTION", "WAITING", "SNOOZED", "DONE", "NOISE"]:
            self.assertIn(f"'{s}'", _MIG_018)
        self.assertIn("CHECK (state IN", _MIG_018)
        self.assertIn("DEFAULT 'NEEDS_ACTION'", _MIG_018)
        # the five states inbox.VALID_STATES and the DDL agree
        self.assertEqual(inbox.VALID_STATES, {"NEEDS_ACTION", "WAITING", "SNOOZED", "DONE", "NOISE"})

    def test_columns_and_indexes(self):
        for col in ["id", "subject", "sender", "sender_domain", "state", "snoozed_until",
                    "waiting_on", "waiting_since", "due_date", "client", "notes",
                    "first_seen_at", "last_updated_at", "last_nudged_at", "mattermost_post_id"]:
            self.assertIn(col, _MIG_018)
        for idx in ["idx_inbox_threads_state", "idx_inbox_threads_snoozed_until",
                    "idx_inbox_threads_due_date", "idx_inbox_threads_waiting_since"]:
            self.assertIn(idx, _MIG_018)


# ============================================================================
# LIVE Postgres integration (skipped unless a local PG is reachable)
# ============================================================================

import psycopg2  # noqa: E402

_TEST_DB = "artemis_inbox_test"
_LIVE = False


def _admin_connect():
    return psycopg2.connect(dbname="postgres", connect_timeout=2)


@contextmanager
def _live_get_connection():
    """Stand-in for knowledge.db.get_connection pointing at the throwaway DB:
    commit on clean exit, rollback on error, one connection per checkout."""
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
            cur.execute(_MIG_018)
        conn.close()
        _LIVE = True
    except Exception as e:  # no PG, no createdb priv, etc. — skip live tier
        sys.stderr.write(f"[test_inbox_rds] live PG unavailable, skipping: {e}\n")
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


class TestLiveInbox(unittest.TestCase):
    def setUp(self):
        if not _LIVE:
            self.skipTest("no local Postgres")
        # Point the real inbox.py functions at the throwaway DB.
        self._patch = patch("knowledge.db.get_connection", _live_get_connection)
        self._patch.start()
        with _live_get_connection() as c:
            with c.cursor() as cur:
                cur.execute("TRUNCATE acos.inbox_threads")

    def tearDown(self):
        if _LIVE:
            self._patch.stop()

    def test_upsert_idempotent(self):
        self.assertTrue(inbox.upsert_thread("g1", "Hello", "ceo@acme.com"))
        self.assertFalse(inbox.upsert_thread("g1", "Hello again", "ceo@acme.com"))
        t = inbox.get_thread("g1")
        self.assertEqual(t["sender_domain"], "acme.com")
        self.assertEqual(t["state"], "NEEDS_ACTION")

    def test_set_state_and_counts(self):
        inbox.upsert_thread("g1", "A", "a@x.com")
        inbox.upsert_thread("g2", "B", "b@x.com")
        self.assertTrue(inbox.mark_done("g1"))
        self.assertTrue(inbox.mark_waiting("g2", waiting_on="Bob"))
        counts = inbox.get_counts()
        self.assertEqual(counts.get("DONE"), 1)
        self.assertEqual(counts.get("WAITING"), 1)
        # waiting_since landed as CT today
        self.assertEqual(inbox.get_thread("g2")["waiting_since"], inbox._ct_today())

    def test_snoozed_due_ct_anchored_roundtrip(self):
        inbox.upsert_thread("g3", "Snooze me", "c@x.com")
        # snooze into the past so it is due now
        self.assertTrue(inbox.set_state("g3", inbox.SNOOZED,
                                        snoozed_until=inbox._ct_today() - timedelta(days=1)))
        due = inbox.get_snoozed_due()
        self.assertEqual([t["id"] for t in due], ["g3"])
        # a future snooze is NOT due
        inbox.set_state("g3", inbox.SNOOZED, snoozed_until=inbox._ct_today() + timedelta(days=5))
        self.assertEqual(inbox.get_snoozed_due(), [])

    def test_leaving_snoozed_clears_until(self):
        inbox.upsert_thread("g4", "X", "d@x.com")
        inbox.set_state("g4", inbox.SNOOZED, snoozed_until=inbox._ct_today() + timedelta(days=2))
        inbox.mark_needs_action("g4")
        self.assertIsNone(inbox.get_thread("g4")["snoozed_until"])


# ============================================================================
# No-SQLite regression guard — reintroducing SQLite in this module fails CI
# ============================================================================

class TestNoSqlite(unittest.TestCase):
    def test_module_has_no_sqlite_binding(self):
        self.assertFalse(hasattr(inbox, "sqlite3"))

    def test_source_has_no_sqlite_import(self):
        src = Path(inbox.__file__).read_text().lower()
        self.assertNotIn("import sqlite3", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
