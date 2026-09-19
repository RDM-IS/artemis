"""TEST-DB-GUARD — no test may reach a real database.

Run:
    python3 tests/test_db_guard.py
"""

import os as _os; _os.environ["ARTEMIS_TEST_NO_DB"] = "1"  # TEST-DB-GUARD: never a real DB

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from knowledge import dbguard as test_guard  # noqa: E402

# Test files allowed to open a real connection, and why. Keep this short.
EXEMPT = {
    "test_phase1_schema.py": "intentional live-schema smoke check, run by the Lambda's "
                             "/admin/run-tests (inserts then deletes a probe row); "
                             "connects with its own psycopg2.connect, never knowledge.db",
}


class TestEveryTestFileIsGuarded(unittest.TestCase):
    def test_every_test_file_sets_the_flag(self):
        # tests/ plus the older test modules that live inside the packages
        # (run as `python -m artemis.test_x`).
        files = sorted([*(_REPO_ROOT / "tests").rglob("test_*.py"),
                        *(_REPO_ROOT / "artemis").glob("test_*.py"),
                        *(_REPO_ROOT / "knowledge").glob("test_*.py")])
        self.assertGreater(len(files), 30)
        missing = [str(p.relative_to(_REPO_ROOT)) for p in files
                   if p.name not in EXEMPT and "ARTEMIS_TEST_NO_DB" not in p.read_text()]
        self.assertEqual(missing, [], "add the TEST-DB-GUARD line to: " + ", ".join(missing))

    def test_no_test_posts_to_mattermost(self):
        # tests/test_mattermost.py used to post "ACOS Phase 1 complete…" to the
        # live channel on every run; it is scripts/check_mattermost.py now.
        files = [*(_REPO_ROOT / "tests").rglob("test_*.py"),
                 *(_REPO_ROOT / "artemis").glob("test_*.py"),
                 *(_REPO_ROOT / "knowledge").glob("test_*.py")]
        live = [str(p.relative_to(_REPO_ROOT)) for p in files
                if "/api/v4/posts" in p.read_text() and "mock" not in p.read_text().lower()]
        self.assertEqual(live, [])

    def test_the_guard_module_is_not_itself_a_test(self):
        self.assertFalse((_REPO_ROOT / "knowledge" / "test_guard.py").exists())

    def test_exemptions_exist(self):
        for name in EXEMPT:
            self.assertTrue((_REPO_ROOT / "tests" / name).exists(), name)


class TestRefusal(unittest.TestCase):
    def test_flag_is_set_here(self):
        self.assertTrue(test_guard.testing())

    def test_get_connection_refuses(self):
        import knowledge.db as db
        with patch.object(db, "_pool", object()):      # even with a live-looking pool
            with self.assertRaises(test_guard.RealDbInTestError):
                with db.get_connection():
                    pass

    def test_init_pool_refuses_before_touching_secrets(self):
        import knowledge.db as db
        with patch("knowledge.secrets.get_rds_credentials") as creds:
            with self.assertRaises(test_guard.RealDbInTestError):
                db.init_pool()
        creds.assert_not_called()

    def test_execute_helpers_refuse(self):
        import knowledge.db as db
        for fn in (db.execute_query, db.execute_write):
            with self.subTest(fn=fn.__name__):
                with self.assertRaises(test_guard.RealDbInTestError):
                    fn("SELECT 1")

    def test_script_database_url_path_refuses(self):
        sys.path.insert(0, str(_REPO_ROOT / "scripts"))
        import reseed_health_plan_v2 as rs
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://x@prod/y"}), \
             patch("psycopg2.connect") as connect:
            with self.assertRaises(test_guard.RealDbInTestError):
                with rs._connect():
                    pass
        connect.assert_not_called()

    def test_api_engine_refuses(self):
        try:
            from api.app import database
        except ImportError as e:  # sqlalchemy not installed locally
            self.skipTest(f"api deps missing: {e}")
        with patch.object(database, "_engine", None), \
             patch.object(database, "create_engine") as ce:
            with self.assertRaises(test_guard.RealDbInTestError):
                database.get_engine()
        ce.assert_not_called()


class TestOffOutsideTests(unittest.TestCase):
    def test_not_testing_without_flag_or_pytest(self):
        env = {k: v for k, v in os.environ.items()
               if k not in ("ARTEMIS_TEST_NO_DB", "PYTEST_CURRENT_TEST")}
        with patch.dict(os.environ, env, clear=True), \
             patch.dict(sys.modules, {"pytest": None}):
            sys.modules.pop("pytest", None)
            self.assertFalse(test_guard.testing())
            test_guard.refuse_real_db("x")   # no raise

    def test_pytest_counts_as_testing(self):
        env = {k: v for k, v in os.environ.items() if k != "ARTEMIS_TEST_NO_DB"}
        with patch.dict(os.environ, {**env, "PYTEST_CURRENT_TEST": "t::x (call)"}, clear=True):
            self.assertTrue(test_guard.testing())


if __name__ == "__main__":
    unittest.main()
