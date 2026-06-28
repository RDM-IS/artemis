"""Tests for quiet-hours state after the SQLite→RDS migration.

Two tiers (mirrors tests/test_inbox_rds.py):

  * MOCKED unit tests (always run) — the SQL/behavior with knowledge.db mocked:
    system_state ON CONFLICT upsert, the quiet_state singleton ON CONFLICT (id)
    upsert, is_quiet() priority logic, the active-timezone resolution (override vs
    HOME), that the wall-clock window check goes through get_active_timezone (NOT a
    hardcoded zone), and the timezone-override CRUD (set/clear/expire). No DB.

  * LIVE integration tests (skipped unless a local Postgres is reachable) —
    migration 019 applies clean and the real quiet_hours functions round-trip
    against the acos tables (system_state get/set, enter/exit/start → is_quiet,
    timezone override set/clear, expired-override sweep) by pointing
    knowledge.db.get_connection at a throwaway database.

Run:
    python3 tests/test_quiet_hours_rds.py
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

from artemis import config, quiet_hours  # noqa: E402

_MIG_019 = (_REPO_ROOT / "migrations" / "019_quiet_hours_state.sql").read_text()


# ============================================================================
# system_state KV
# ============================================================================

class TestSystemState(unittest.TestCase):
    def test_set_uses_on_conflict_upsert(self):
        with patch("artemis.quiet_hours.execute_write") as w:
            quiet_hours.set_system_value("k", "v")
        sql, params = w.call_args[0]
        self.assertIn("acos.system_state", sql)
        self.assertIn("ON CONFLICT (key) DO UPDATE", sql)
        self.assertEqual(params, ("k", "v"))

    def test_get_returns_value_or_none(self):
        with patch("artemis.quiet_hours.execute_one", return_value={"value": "v"}):
            self.assertEqual(quiet_hours.get_system_value("k"), "v")
        with patch("artemis.quiet_hours.execute_one", return_value=None):
            self.assertIsNone(quiet_hours.get_system_value("missing"))


# ============================================================================
# active timezone resolution (override vs HOME)
# ============================================================================

class TestActiveTimezone(unittest.TestCase):
    def test_override_active_returns_override(self):
        with patch("artemis.quiet_hours.execute_one", return_value={"timezone": "Europe/Paris"}) as o:
            self.assertEqual(quiet_hours.get_active_timezone(), "Europe/Paris")
        # expiry is filtered in SQL (absolute instant), not in Python
        self.assertIn("expires_at > now()", o.call_args[0][0])

    def test_no_override_falls_to_home(self):
        with patch("artemis.quiet_hours.execute_one", return_value=None):
            self.assertEqual(quiet_hours.get_active_timezone(), config.HOME_TIMEZONE)


# ============================================================================
# quiet_state singleton upsert + is_quiet priority
# ============================================================================

class TestQuietStateUpsert(unittest.TestCase):
    def test_enter_quiet_singleton_on_conflict(self):
        with patch("artemis.quiet_hours.execute_write") as w, \
             patch("artemis.quiet_hours.get_tz_abbrev", return_value="CDT"):
            quiet_hours.enter_quiet(manual=True)
        sql, params = w.call_args[0]
        self.assertIn("INSERT INTO acos.quiet_state", sql)
        self.assertIn("ON CONFLICT (id) DO UPDATE", sql)
        self.assertIn("updated_at = now()", sql)
        self.assertIn(1, params)   # is_quiet=1 among the values


class TestIsQuietPriority(unittest.TestCase):
    def test_override_active_not_quiet(self):
        with patch("artemis.quiet_hours._get_quiet_row", return_value={"override_active": 1, "is_quiet": 1}):
            self.assertFalse(quiet_hours.is_quiet())

    def test_manual_goodnight_is_quiet(self):
        with patch("artemis.quiet_hours._get_quiet_row",
                   return_value={"override_active": 0, "manual_override": 1, "is_quiet": 1}):
            self.assertTrue(quiet_hours.is_quiet())

    def test_manual_morning_not_quiet(self):
        with patch("artemis.quiet_hours._get_quiet_row",
                   return_value={"override_active": 0, "manual_override": 1, "is_quiet": 0}):
            self.assertFalse(quiet_hours.is_quiet())

    def test_no_manual_falls_to_time_window(self):
        with patch("artemis.quiet_hours._get_quiet_row", return_value=None), \
             patch("artemis.quiet_hours._is_in_time_window", return_value=True) as win:
            self.assertTrue(quiet_hours.is_quiet())
        win.assert_called_once()


class TestTimeWindowUsesActiveTz(unittest.TestCase):
    def test_window_check_consults_active_timezone(self):
        # The window check must resolve the module's active tz, never a hardcoded one.
        with patch("artemis.quiet_hours.get_active_timezone", return_value="America/New_York") as tz:
            quiet_hours._is_in_time_window()
        tz.assert_called_once()


# ============================================================================
# timezone override CRUD
# ============================================================================

class TestTimezoneCRUD(unittest.TestCase):
    def test_set_override_on_conflict_and_tzaware_expiry(self):
        with patch("artemis.quiet_hours.execute_write") as w, \
             patch("artemis.quiet_hours.get_tz_abbrev", return_value="CET"):
            quiet_hours.set_timezone_override("Europe/Paris", "Paris", days=7)
        sql, params = w.call_args[0]
        self.assertIn("acos.timezone_overrides", sql)
        self.assertIn("ON CONFLICT (id) DO UPDATE", sql)
        self.assertEqual(params[0], "Europe/Paris")
        self.assertIsInstance(params[1], datetime)       # expires_at tz-aware datetime
        self.assertIsNotNone(params[1].tzinfo)

    def test_clear_deletes_singleton(self):
        with patch("artemis.quiet_hours.execute_write") as w, \
             patch("artemis.quiet_hours.get_tz_abbrev", return_value="CDT"):
            quiet_hours.clear_timezone_override()
        self.assertIn("DELETE FROM acos.timezone_overrides WHERE id = 1", w.call_args[0][0])

    def test_check_expired_deletes_and_announces_only_when_row_returned(self):
        # DELETE ... WHERE expires_at <= now() RETURNING → row means it was expired
        with patch("artemis.quiet_hours.execute_write",
                   return_value={"timezone": "Europe/Paris", "city_name": "Paris"}), \
             patch("artemis.quiet_hours.get_tz_abbrev", return_value="CDT"):
            msg = quiet_hours.check_expired_overrides()
        self.assertIsNotNone(msg)
        self.assertIn("expired", msg)
        # not expired (no row deleted) → None
        with patch("artemis.quiet_hours.execute_write", return_value=None):
            self.assertIsNone(quiet_hours.check_expired_overrides())


# ============================================================================
# Migration 019 shape (always-run, no DB)
# ============================================================================

class TestMigrationShape(unittest.TestCase):
    def test_three_tables(self):
        for t in ["acos.system_state", "acos.quiet_state", "acos.timezone_overrides"]:
            self.assertIn(t, _MIG_019)

    def test_singletons_and_types(self):
        # both singletons keep the id=1 CHECK
        self.assertEqual(_MIG_019.count("PRIMARY KEY CHECK (id = 1)"), 2)
        # instants are TIMESTAMPTZ; HH:MM fields stay TEXT
        self.assertIn("last_interaction  TIMESTAMPTZ", _MIG_019)
        self.assertIn("expires_at  TIMESTAMPTZ NOT NULL", _MIG_019)
        self.assertIn("wake_time         TEXT", _MIG_019)
        self.assertIn("override_until    TEXT", _MIG_019)
        # system_state PK on key
        self.assertIn("key         TEXT PRIMARY KEY", _MIG_019)


# ============================================================================
# LIVE Postgres integration (skipped unless a local PG is reachable)
# ============================================================================

import psycopg2  # noqa: E402

_TEST_DB = "artemis_quiet_test"
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
            cur.execute(_MIG_019)
        conn.close()
        _LIVE = True
    except Exception as e:
        sys.stderr.write(f"[test_quiet_hours_rds] live PG unavailable, skipping: {e}\n")
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


class TestLiveQuietHours(unittest.TestCase):
    def setUp(self):
        if not _LIVE:
            self.skipTest("no local Postgres")
        self._patch = patch("knowledge.db.get_connection", _live_get_connection)
        self._patch.start()
        with _live_get_connection() as c:
            with c.cursor() as cur:
                cur.execute("TRUNCATE acos.system_state")
                cur.execute("TRUNCATE acos.quiet_state")
                cur.execute("TRUNCATE acos.timezone_overrides")

    def tearDown(self):
        if _LIVE:
            self._patch.stop()

    def test_system_value_roundtrip(self):
        self.assertIsNone(quiet_hours.get_system_value("k"))
        quiet_hours.set_system_value("k", "v1")
        self.assertEqual(quiet_hours.get_system_value("k"), "v1")
        quiet_hours.set_system_value("k", "v2")          # upsert overwrites
        self.assertEqual(quiet_hours.get_system_value("k"), "v2")

    def test_enter_start_exit_quiet(self):
        # manual goodnight → quiet (deterministic, bypasses the time window)
        quiet_hours.enter_quiet(manual=True)
        self.assertTrue(quiet_hours.is_quiet())
        st = quiet_hours.get_quiet_state()
        self.assertEqual(st["is_quiet"], 1)
        self.assertEqual(st["manual_override"], 1)
        # working-session override → NOT quiet
        quiet_hours.start_override()
        self.assertFalse(quiet_hours.is_quiet())
        self.assertEqual(quiet_hours.get_quiet_state()["override_active"], 1)
        # good morning → state cleared
        quiet_hours.exit_quiet()
        st = quiet_hours.get_quiet_state()
        self.assertEqual(st["is_quiet"], 0)
        self.assertEqual(st["manual_override"], 0)
        self.assertEqual(st["override_active"], 0)

    def test_timezone_override_set_clear(self):
        quiet_hours.set_timezone_override("Europe/Paris", "Paris", days=7)
        self.assertEqual(quiet_hours.get_active_timezone(), "Europe/Paris")
        self.assertIn("Paris", quiet_hours.quiet_hours_status())
        quiet_hours.clear_timezone_override()
        self.assertEqual(quiet_hours.get_active_timezone(), config.HOME_TIMEZONE)

    def test_expired_override_swept(self):
        # an already-expired override is ignored by get_active_timezone and swept
        with _live_get_connection() as c:
            with c.cursor() as cur:
                cur.execute(
                    "INSERT INTO acos.timezone_overrides (id, timezone, expires_at, city_name) "
                    "VALUES (1, 'Europe/Paris', now() - interval '1 day', 'Paris')"
                )
        self.assertEqual(quiet_hours.get_active_timezone(), config.HOME_TIMEZONE)  # expired → HOME
        msg = quiet_hours.check_expired_overrides()
        self.assertIsNotNone(msg)
        self.assertIn("expired", msg)
        # swept — a second sweep is a no-op
        self.assertIsNone(quiet_hours.check_expired_overrides())

    def test_active_override_not_swept(self):
        quiet_hours.set_timezone_override("Europe/Paris", "Paris", days=7)
        self.assertIsNone(quiet_hours.check_expired_overrides())     # not expired
        self.assertEqual(quiet_hours.get_active_timezone(), "Europe/Paris")


if __name__ == "__main__":
    unittest.main(verbosity=2)
