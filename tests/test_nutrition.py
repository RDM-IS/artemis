"""Tests for the nutrition-capture subsystem.

Two tiers:

  * MOCKED unit tests (always run) — intent routing, on-plan vs off-plan logging,
    staple aggregation (times_per_week math), grocery-function parity after the
    SQLite→Postgres move, the budget coach, and the target propose→commit flow.
    Claude and the DB layer are mocked; no AWS/DB access needed.

  * LIVE integration tests (skipped unless a local Postgres is reachable) —
    migration 016/017 applies clean, the one_open_target partial unique index
    rejects a second open target, and health.remaining_budget() math against a
    seeded target + log. These run against a throwaway database that is created
    and dropped around the module.

Run:
    python3 tests/test_nutrition.py
"""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Repo root on path
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

# Block AWS access
os.environ.setdefault("RDS_HOST", "test-host")
os.environ.setdefault("RDS_DB", "test-db")

from artemis import health, life_ops  # noqa: E402

_MIG_016 = (_REPO_ROOT / "migrations" / "016_nutrition_schema.sql").read_text()
_MIG_017 = (_REPO_ROOT / "migrations" / "017_grocery_to_acos.sql").read_text()


# ============================================================================
# Shared fakes
# ============================================================================

class _FakeKV:
    """In-memory stand-in for the system_state KV (quiet_hours get/set)."""

    def __init__(self):
        self.store = {}

    def get(self, key):
        v = self.store.get(key)
        return v or None

    def set(self, key, value):
        self.store[key] = value


# ============================================================================
# Intent detection
# ============================================================================

class TestNutritionIntent(unittest.TestCase):
    def test_status(self):
        for m in ["macros left", "where am I today", "how much protein left",
                  "remaining budget", "what's left today", "calories remaining"]:
            self.assertEqual(health.detect_nutrition_intent(m),
                             health.INTENT_NUTRITION_STATUS, m)

    def test_set_target(self):
        for m in ["new target from Joy: 1900 cal, 215g protein",
                  "set nutrition target 1800/200",
                  "update my macro target",
                  "Joy sent a new plan"]:
            self.assertEqual(health.detect_nutrition_intent(m),
                             health.INTENT_SET_NUTRITION_TARGET, m)

    def test_log(self):
        for m in ["breakfast done", "had lunch", "ate dinner", "snack done",
                  "log 2 eggs and toast", "for dinner I had pizza"]:
            self.assertEqual(health.detect_nutrition_intent(m),
                             health.INTENT_LOG_NUTRITION, m)

    def test_neither(self):
        for m in ["hello", "what's the weather", "squats RPE 7, plank 30s"]:
            self.assertIsNone(health.detect_nutrition_intent(m), m)

    def test_onplan_slot_match(self):
        self.assertEqual(health._match_onplan_slot("breakfast done"), "breakfast")
        self.assertEqual(health._match_onplan_slot("had lunch"), "lunch")
        self.assertIsNone(health._match_onplan_slot("2 burgers and a beer"))


# ============================================================================
# log_nutrition — on-plan copies meal macros (estimated=false)
# ============================================================================

class TestOnPlanLog(unittest.TestCase):
    _MEAL = {"id": 42, "name": "Eggs & oats", "kcal": 450, "protein_g": 45,
             "carb_g": 40, "fat_g": 12, "fiber_g": 6}

    def test_onplan_copies_macros(self):
        with patch.object(health, "active_meal_for_slot", return_value=self._MEAL), \
             patch("knowledge.db.execute_write") as mock_write:
            reply = health.log_nutrition("breakfast done")

        mock_write.assert_called_once()
        sql, params = mock_write.call_args[0]
        # estimated is the literal `false` in the on-plan insert
        self.assertIn("false", sql)
        self.assertNotIn("true", sql)
        self.assertIn(42, params)            # meal_id copied
        self.assertIn(450, params)           # kcal copied verbatim
        self.assertIn(45, params)            # protein copied verbatim
        self.assertIn("On plan", reply)

    def test_onplan_no_meal_defined(self):
        with patch.object(health, "active_meal_for_slot", return_value=None), \
             patch("knowledge.db.execute_write") as mock_write:
            reply = health.log_nutrition("lunch done")
        mock_write.assert_not_called()
        self.assertIn("No active lunch", reply)


# ============================================================================
# log_nutrition — off-plan writes estimated=true with confidence
# ============================================================================

class TestOffPlanLog(unittest.TestCase):
    def test_offplan_estimated(self):
        est = health.NutritionEstimate(kcal=1250, protein_g=55, carb_g=95,
                                       fat_g=68, fiber_g=6, confidence="medium")
        with patch.object(health, "estimate_nutrition", return_value=est), \
             patch("knowledge.db.execute_write") as mock_write:
            reply = health.log_nutrition("2 burgers, potato salad, a beer")

        mock_write.assert_called_once()
        sql, params = mock_write.call_args[0]
        self.assertIn("true", sql)           # estimated = true
        self.assertIn("NULL", sql)           # meal_id NULL (off-plan)
        self.assertIn("medium", params)      # confidence persisted
        self.assertIn(1250, params)
        self.assertIn("est, medium", reply)

    def test_offplan_estimate_failure_no_write(self):
        with patch.object(health, "estimate_nutrition",
                          side_effect=ValueError("bad json")), \
             patch("knowledge.db.execute_write") as mock_write:
            reply = health.log_nutrition("something vague")
        mock_write.assert_not_called()
        self.assertIn("Couldn't estimate", reply)


# ============================================================================
# Staple generator — aggregates ingredients × times_per_week
# ============================================================================

class TestStapleAggregation(unittest.TestCase):
    _MEALS = [
        {"name": "Eggs & oats", "times_per_week": 7,
         "ingredients": [{"item": "eggs", "qty": 3, "unit": "each"},
                         {"item": "oats", "qty": 0.5, "unit": "cup"}]},
        {"name": "Chicken salad", "times_per_week": 5,
         "ingredients": [{"item": "chicken", "qty": 6, "unit": "oz"},
                         {"item": "eggs", "qty": 1, "unit": "each"}]},
    ]

    def test_aggregate(self):
        with patch.object(life_ops, "_open_nutrition_target_id", return_value=1), \
             patch("knowledge.db.execute_query", return_value=self._MEALS):
            staples = life_ops.aggregate_staples()

        by_item = {(s["item"], s["unit"]): s["qty"] for s in staples}
        # eggs: 3×7 (breakfast) + 1×5 (lunch) = 26
        self.assertEqual(by_item[("eggs", "each")], 26)
        # oats: 0.5×7 = 3.5
        self.assertAlmostEqual(by_item[("oats", "cup")], 3.5)
        # chicken: 6×5 = 30
        self.assertEqual(by_item[("chicken", "oz")], 30)

    def test_no_open_target(self):
        with patch.object(life_ops, "_open_nutrition_target_id", return_value=None):
            self.assertEqual(life_ops.aggregate_staples(), [])

    def test_propose_then_commit(self):
        kv = _FakeKV()
        staples = [{"item": "eggs", "unit": "each", "qty": 26.0},
                   {"item": "oats", "unit": "cup", "qty": 3.5}]
        with patch("artemis.quiet_hours.get_system_value", side_effect=kv.get), \
             patch("artemis.quiet_hours.set_system_value", side_effect=kv.set), \
             patch.object(life_ops, "aggregate_staples", return_value=staples):
            proposal = life_ops.build_grocery_staples("chan1")
        self.assertIn("review and confirm", proposal)
        self.assertIn("eggs", proposal)

        upserts = []
        with patch("artemis.quiet_hours.get_system_value", side_effect=kv.get), \
             patch("artemis.quiet_hours.set_system_value", side_effect=kv.set), \
             patch.object(life_ops, "_upsert_grocery_staple",
                          side_effect=lambda item, qty: upserts.append((item, qty))):
            reply = life_ops.commit_grocery_staples("chan1")

        self.assertEqual(len(upserts), 2)
        self.assertEqual(upserts[0], ("eggs", "26 each"))
        self.assertEqual(upserts[1], ("oats", "3.5 cup"))
        self.assertIn("Added 2 staples", reply)
        # pending cleared on commit
        self.assertIsNone(life_ops.load_staples_pending("chan1"))


# ============================================================================
# Grocery functions — behave identically against Postgres backend
# ============================================================================

class TestGroceryParity(unittest.TestCase):
    def test_categorization_unchanged(self):
        self.assertEqual(life_ops._categorize_item("chicken thigh"), "Protein & Refrigerated")
        self.assertEqual(life_ops._categorize_item("banana"), "Produce & Refrigerated")
        self.assertEqual(life_ops._categorize_item("rolled oats"), "Pantry")
        self.assertEqual(life_ops._categorize_item("frozen broccoli"), "Frozen")
        self.assertEqual(life_ops._categorize_item("widget"), "Other")

    def test_add_item_inserts_acos(self):
        with patch("knowledge.db.execute_write") as mock_write:
            result = life_ops.add_grocery_item("chicken thigh", quantity="2 lb")
        sql, params = mock_write.call_args[0]
        self.assertIn("acos.grocery_list", sql)
        self.assertIn("chicken thigh", params)
        self.assertEqual(result["category"], "Protein & Refrigerated")

    def test_get_list_reads_acos(self):
        rows = [{"item": "eggs", "category": "Protein & Refrigerated", "quantity": "1 dozen"}]
        with patch("knowledge.db.execute_query", return_value=rows) as mock_q:
            out = life_ops.get_grocery_list()
        sql = mock_q.call_args[0][0]
        self.assertIn("acos.grocery_list", sql)
        self.assertIn("is_purchased = false", sql)
        self.assertEqual(out[0]["item"], "eggs")

    def test_mark_purchased_rowcount(self):
        with patch("knowledge.db.get_connection",
                   return_value=_make_conn_cm(rowcount=2)):
            self.assertTrue(life_ops.mark_purchased("eggs"))
        with patch("knowledge.db.get_connection",
                   return_value=_make_conn_cm(rowcount=0)):
            self.assertFalse(life_ops.mark_purchased("nope"))

    def test_clear_returns_count(self):
        with patch("knowledge.db.get_connection",
                   return_value=_make_conn_cm(rowcount=5)):
            self.assertEqual(life_ops.clear_grocery_list(), 5)

    def test_handle_grocery_command_add(self):
        with patch.object(life_ops, "add_grocery_item",
                          return_value={"item": "milk", "category": "Pantry"}) as mock_add:
            reply = life_ops.handle_grocery_command("add milk")
        mock_add.assert_called_once()
        self.assertIn("milk", reply)


def _make_conn_cm(rowcount=0, fetchone=None):
    """Build a get_connection()-style context manager for patching."""
    cur = MagicMock()
    cur.rowcount = rowcount
    cur.fetchone.return_value = fetchone
    cur_cm = MagicMock()
    cur_cm.__enter__.return_value = cur
    cur_cm.__exit__.return_value = False
    conn = MagicMock()
    conn.cursor.return_value = cur_cm
    conn_cm = MagicMock()
    conn_cm.__enter__.return_value = conn
    conn_cm.__exit__.return_value = False
    return conn_cm


# ============================================================================
# Budget coach — nutrition_status
# ============================================================================

class TestBudgetCoach(unittest.TestCase):
    _ROW = {
        "target_kcal": 1900, "consumed_kcal": 1200, "remaining_kcal": 700,
        "target_protein_g": 215, "consumed_protein_g": 150, "remaining_protein_g": 65,
        "target_carb_g": 150, "consumed_carb_g": 100, "remaining_carb_g": 50,
        "target_fat_g": 60, "consumed_fat_g": 40, "remaining_fat_g": 20,
        "target_fiber_g": 30, "consumed_fiber_g": 5, "remaining_fiber_g": 25,
    }

    def test_status_trainer_voice(self):
        with patch("knowledge.db.execute_one", return_value=self._ROW), \
             patch.object(health, "_call_claude_text",
                          return_value="700 left, 65g protein. One protein plate.") as mock_v:
            reply = health.nutrition_status()
        mock_v.assert_called_once()
        self.assertIn("protein", reply)

    def test_status_degrades_without_llm(self):
        with patch("knowledge.db.execute_one", return_value=self._ROW), \
             patch.object(health, "_call_claude_text", side_effect=RuntimeError("no key")):
            reply = health.nutrition_status()
        self.assertIn("700 kcal left", reply)
        self.assertIn("65g protein left", reply)
        # low fiber heuristic fires (consumed 5 < 0.5*30)
        self.assertIn("fiber", reply.lower())

    def test_status_no_target(self):
        with patch("knowledge.db.execute_one", return_value={"target_kcal": None}):
            reply = health.nutrition_status()
        self.assertIn("No nutrition target", reply)


# ============================================================================
# set_nutrition_target — propose → commit (confirmed write)
# ============================================================================

class TestTargetProposeCommit(unittest.TestCase):
    _PARSED = {"kcal": 1900, "protein_g": 215, "effective_from": "2026-06-26",
               "carb_g": 150, "fat_g": 60, "fiber_g": 30, "set_by": "joy",
               "notes": None, "meals": []}

    def test_propose_stores_pending(self):
        kv = _FakeKV()
        with patch.object(health, "_call_claude_json", return_value=dict(self._PARSED)), \
             patch("artemis.quiet_hours.get_system_value", side_effect=kv.get), \
             patch("artemis.quiet_hours.set_system_value", side_effect=kv.set):
            proposal = health.propose_nutrition_target(
                "new target from joy 1900 cal 215 protein", "chan1")
            self.assertIn("review and confirm", proposal)
            self.assertIsNotNone(health.load_nutrition_target_pending("chan1"))

    def test_propose_missing_macros(self):
        with patch.object(health, "_call_claude_json",
                          return_value={"error": "missing kcal or protein"}):
            reply = health.propose_nutrition_target("set a target", "chan1")
        self.assertIn("at least calories and protein", reply)

    def test_commit_closes_prior_and_inserts(self):
        kv = _FakeKV()
        with patch.object(health, "_call_claude_json", return_value=dict(self._PARSED)), \
             patch("artemis.quiet_hours.get_system_value", side_effect=kv.get), \
             patch("artemis.quiet_hours.set_system_value", side_effect=kv.set):
            health.propose_nutrition_target("new target ...", "chan1")
            with patch.object(health, "insert_nutrition_target_tx", return_value=7) as mock_tx:
                reply = health.commit_nutrition_target("chan1")
        mock_tx.assert_called_once()
        self.assertIn("#7", reply)
        self.assertIn("Prior target closed", reply)
        self.assertIsNone(health.load_nutrition_target_pending("chan1"))

    def test_commit_nothing_pending(self):
        kv = _FakeKV()
        with patch("artemis.quiet_hours.get_system_value", side_effect=kv.get), \
             patch("artemis.quiet_hours.set_system_value", side_effect=kv.set):
            reply = health.commit_nutrition_target("chan1")
        self.assertIn("Nothing pending", reply)


# ============================================================================
# Migration file sanity (always-run, no DB)
# ============================================================================

class TestMigrationFiles(unittest.TestCase):
    def test_016_shape(self):
        self.assertIn("health.nutrition_target", _MIG_016)
        self.assertIn("health.meal", _MIG_016)
        self.assertIn("health.nutrition_log", _MIG_016)
        self.assertIn("one_open_target", _MIG_016)
        self.assertIn("WHERE effective_to IS NULL", _MIG_016)
        self.assertIn("FUNCTION health.remaining_budget", _MIG_016)
        self.assertIn("America/Chicago", _MIG_016)

    def test_017_shape(self):
        self.assertIn("acos.grocery_list", _MIG_017)
        for col in ["item", "category", "quantity", "store", "added_at",
                    "purchased_at", "is_purchased", "notes"]:
            self.assertIn(col, _MIG_017)


# ============================================================================
# LIVE Postgres integration (skipped unless a local PG is reachable)
# ============================================================================

import psycopg2  # noqa: E402

_TEST_DB = "artemis_nutrition_test"
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
            cur.execute(_MIG_016)
            cur.execute(_MIG_017)
        conn.close()
        _LIVE = True
    except Exception as e:  # no PG, no createdb priv, etc. — skip live tier
        sys.stderr.write(f"[test_nutrition] live PG unavailable, skipping: {e}\n")
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


class TestLiveSchema(unittest.TestCase):
    def setUp(self):
        # _LIVE is decided in setUpModule (run time), so gate here rather than
        # via a class decorator (which would capture _LIVE=False at import).
        if not _LIVE:
            self.skipTest("no local Postgres")
        self.conn = psycopg2.connect(dbname=_TEST_DB)
        self.conn.autocommit = True
        self.cur = self.conn.cursor()
        self.cur.execute(
            "TRUNCATE health.nutrition_log, health.meal, health.nutrition_target "
            "RESTART IDENTITY CASCADE"
        )

    def tearDown(self):
        self.conn.close()

    def test_migration_applied_objects(self):
        self.cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='health' AND table_name IN "
            "('nutrition_target','meal','nutrition_log')"
        )
        names = {r[0] for r in self.cur.fetchall()}
        self.assertEqual(names, {"nutrition_target", "meal", "nutrition_log"})
        # function exists
        self.cur.execute(
            "SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
            "WHERE n.nspname='health' AND p.proname='remaining_budget'"
        )
        self.assertIsNotNone(self.cur.fetchone())
        # acos.grocery_list exists
        self.cur.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema='acos' AND table_name='grocery_list'"
        )
        self.assertIsNotNone(self.cur.fetchone())

    def test_one_open_target_rejects_second(self):
        self.cur.execute(
            "INSERT INTO health.nutrition_target (effective_from, kcal, protein_g) "
            "VALUES ('2026-06-01', 1900, 215)"
        )
        with self.assertRaises(psycopg2.errors.UniqueViolation):
            self.cur.execute(
                "INSERT INTO health.nutrition_target (effective_from, kcal, protein_g) "
                "VALUES ('2026-06-10', 1800, 200)"
            )

    def test_closed_then_open_allowed(self):
        # A closed (dated) target plus one open target must coexist.
        self.cur.execute(
            "INSERT INTO health.nutrition_target "
            "(effective_from, effective_to, kcal, protein_g) "
            "VALUES ('2026-05-01', '2026-05-31', 2000, 210)"
        )
        self.cur.execute(
            "INSERT INTO health.nutrition_target (effective_from, kcal, protein_g) "
            "VALUES ('2026-06-01', 1900, 215)"
        )  # should not raise
        self.cur.execute("SELECT count(*) FROM health.nutrition_target")
        self.assertEqual(self.cur.fetchone()[0], 2)

    def test_remaining_budget_math(self):
        d = "2026-06-26"
        self.cur.execute(
            "INSERT INTO health.nutrition_target "
            "(effective_from, kcal, protein_g, carb_g, fat_g, fiber_g) "
            "VALUES ('2026-06-01', 1900, 215, 150, 60, 30)"
        )
        self.cur.execute(
            "INSERT INTO health.nutrition_log "
            "(logged_date, description, kcal, protein_g, carb_g, fat_g, fiber_g) "
            "VALUES (%s, 'breakfast', 450, 45, 40, 12, 6), "
            "       (%s, 'lunch', 500, 55, 35, 18, 8)",
            (d, d),
        )
        self.cur.execute("SELECT * FROM health.remaining_budget(%s)", (d,))
        cols = [c.name for c in self.cur.description]
        row = dict(zip(cols, self.cur.fetchone()))
        self.assertEqual(row["target_kcal"], 1900)
        self.assertEqual(row["consumed_kcal"], 950)       # 450 + 500
        self.assertEqual(row["remaining_kcal"], 950)      # 1900 - 950
        self.assertEqual(row["consumed_protein_g"], 100)  # 45 + 55
        self.assertEqual(row["remaining_protein_g"], 115)
        self.assertEqual(row["remaining_fiber_g"], 16)    # 30 - 14

    def test_remaining_budget_empty_day(self):
        self.cur.execute(
            "INSERT INTO health.nutrition_target (effective_from, kcal, protein_g) "
            "VALUES ('2026-06-01', 1900, 215)"
        )
        self.cur.execute("SELECT * FROM health.remaining_budget('2026-06-26')")
        cols = [c.name for c in self.cur.description]
        row = dict(zip(cols, self.cur.fetchone()))
        self.assertEqual(row["consumed_kcal"], 0)
        self.assertEqual(row["remaining_kcal"], 1900)

    def test_grocery_list_roundtrip(self):
        self.cur.execute(
            "INSERT INTO acos.grocery_list (item, category, quantity) "
            "VALUES ('eggs', 'Protein & Refrigerated', '1 dozen')"
        )
        self.cur.execute(
            "SELECT is_purchased FROM acos.grocery_list WHERE item='eggs'"
        )
        self.assertIs(self.cur.fetchone()[0], False)
        self.cur.execute(
            "UPDATE acos.grocery_list SET is_purchased=true, purchased_at=now() "
            "WHERE item ILIKE '%egg%' AND is_purchased=false"
        )
        self.assertEqual(self.cur.rowcount, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
