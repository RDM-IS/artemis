"""LAMBDA-DRIFT option C — the deploy preflight, tested against the real failure.

The case that matters is the first one: the 2026-09-20 sequence. Migration 039
dropped `health.plan.status` while the code being deployed still listed
`status` in `_plan_days`' SELECT. The smoke test passed because the column
still existed when it ran; `/plan` and `/overview` then 500ed for seven
minutes. The preflight must REFUSE that exact arrangement.

Each case builds a throwaway repo — migrations/ plus api/app — so the tests
pin behaviour rather than the current contents of this checkout.

Run:
    python3 tests/test_lambda_preflight.py
"""

import os as _os; _os.environ["ARTEMIS_TEST_NO_DB"] = "1"  # TEST-DB-GUARD: never a real DB

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import lambda_preflight as pf  # noqa: E402

REPO = Path(__file__).resolve().parent.parent

# Migration 039, reduced to the statement that did the damage.
DROP_039 = """
-- 039_drop_plan_status_debris.sql
-- SHIP ORDER MATTERS: the code that stops reading `status` deploys FIRST.
ALTER TABLE health.plan DROP COLUMN IF EXISTS status;
ALTER TABLE health.plan DROP COLUMN IF EXISTS original_date;
"""

# api/app/routers/health.py as it stood at 394951c — the SELECT still names
# `status`, two lines above its own FROM health.plan.
CODE_BEFORE_THE_FIX = '''
def _plan_days(db, start, end):
    plan_rows = db.execute(
        text("""
            SELECT plan_id, plan_date, phase, week_num, session_type, blocks,
                   target_rpe, est_duration_min, is_skipped, status
            FROM health.plan
            WHERE plan_date BETWEEN :s AND :e
            ORDER BY plan_date
        """),
        {"s": start, "e": end},
    ).mappings().all()
    return plan_rows
'''

# ... and as it stands at d06fb2c, the fix.
CODE_AFTER_THE_FIX = CODE_BEFORE_THE_FIX.replace(", is_skipped, status", ", is_skipped")


def build_repo(tmp: str, *, migration: str = DROP_039,
               code: str = CODE_BEFORE_THE_FIX,
               name: str = "039_drop_plan_status_debris.sql") -> Path:
    repo = Path(tmp)
    (repo / "migrations").mkdir(parents=True)
    (repo / "migrations" / name).write_text(migration)
    (repo / "api" / "app" / "routers").mkdir(parents=True)
    (repo / "api" / "app" / "routers" / "health.py").write_text(code)
    return repo


def run_check(repo: Path, applied: list[str]):
    """check() with `applied` standing in for acos.schema_migrations."""
    with patch.object(pf, "applied_migrations", return_value=set(applied)):
        return pf.check(repo=repo)


class TestTheNineTwentyFailure(unittest.TestCase):
    """The regression this whole script exists for."""

    def test_the_9_20_sequence_would_have_been_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = build_repo(tmp)
            code, lines = run_check(repo, applied=["038_watch_device.sql"])
        report = "\n".join(lines)
        self.assertEqual(code, 1, report)
        self.assertIn("REFUSED", report)
        self.assertIn("health.plan.status", report)
        self.assertIn("039_drop_plan_status_debris.sql", report)
        # It names the line, so the fix is obvious without re-deriving it.
        self.assertIn("api/app/routers/health.py:", report)
        self.assertIn("is_skipped, status", report)

    def test_the_fixed_code_deploys(self):
        """d06fb2c's SELECT — same pending migration, no reference left."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = build_repo(tmp, code=CODE_AFTER_THE_FIX)
            code, lines = run_check(repo, applied=["038_watch_device.sql"])
        self.assertEqual(code, 0, "\n".join(lines))
        self.assertIn("OK", "\n".join(lines))

    def test_an_applied_migration_is_not_pending(self):
        """After 039 has run the column is already gone; the preflight has
        nothing to say, and any surviving reference is a live bug, not a
        deploy-ordering one."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = build_repo(tmp)
            code, lines = run_check(repo, applied=["038_watch_device.sql",
                                                   "039_drop_plan_status_debris.sql"])
        self.assertEqual(code, 0)
        self.assertIn("no pending column drops or renames", "\n".join(lines))


class TestWhatCounts(unittest.TestCase):
    def test_a_rename_is_caught_like_a_drop(self):
        """Migration 038's shape: device -> device_raw. The old name breaks."""
        sql = "ALTER TABLE health.watch_sample RENAME COLUMN device TO device_raw;"
        code_src = ('q = "SELECT metric, device FROM health.watch_sample '
                    'WHERE measured_at > :lo"\n')
        with tempfile.TemporaryDirectory() as tmp:
            repo = build_repo(tmp, migration=sql, code=code_src,
                              name="040_rename_device.sql")
            code, lines = run_check(repo, applied=[])
        report = "\n".join(lines)
        self.assertEqual(code, 1, report)
        self.assertIn("renames", report)
        self.assertIn("health.watch_sample.device", report)

    def test_a_mention_away_from_the_table_is_advisory_not_fatal(self):
        """`status` is a common word. A local variable in a file that never
        touches health.plan must not block a deploy — an unbelievable gate
        just teaches people to --force."""
        code_src = ('def format_ingest(resp):\n'
                    '    status = resp.get("code")\n'
                    '    return f"ingest {status}"\n')
        with tempfile.TemporaryDirectory() as tmp:
            repo = build_repo(tmp, code=code_src)
            code, lines = run_check(repo, applied=[])
        report = "\n".join(lines)
        self.assertEqual(code, 0, report)
        self.assertIn("ADVISORY", report)
        self.assertIn("none near the table", report)

    def test_a_docstring_listing_routes_is_not_a_sql_reference(self):
        """health.py's own docstring names both `status` and health.plan. Prose
        about a column is not a use of it — and a first cut of this script
        refused the FIXED code on exactly these two lines."""
        code_src = ('"""Health routes.\n\n'
                    'GET /api/health/status -> windowed status payload\n'
                    'Reads FROM health.plan and derives status per day.\n"""\n'
                    'ROUTES = ["/status"]\n')
        with tempfile.TemporaryDirectory() as tmp:
            repo = build_repo(tmp, code=code_src)
            code, lines = run_check(repo, applied=[])
        self.assertEqual(code, 0, "\n".join(lines))

    def test_head_deploys_cleanly_with_039_pending(self):
        """The property that makes the gate worth having, checked against the
        real api/app and knowledge/ rather than a fixture: at HEAD, nothing
        references health.plan.status, so a pending 039 would NOT refuse.
        A gate that fires on the fixed code is a gate nobody obeys."""
        applied = [p.name for p in pf.migration_files() if not p.name.startswith("039")]
        with patch.object(pf, "applied_migrations", return_value=set(applied)):
            code, lines = pf.check()
        self.assertEqual(code, 0, "\n".join(lines))

    def test_a_commented_out_reference_does_not_block(self):
        code_src = '# SELECT status FROM health.plan  (removed in #132)\n'
        with tempfile.TemporaryDirectory() as tmp:
            repo = build_repo(tmp, code=code_src)
            code, _ = run_check(repo, applied=[])
        self.assertEqual(code, 0)

    def test_a_migration_comment_is_not_read_as_a_statement(self):
        """039's own header quotes `SELECT plan_date, status ...`; parsing the
        comment would invent changes that aren't there."""
        real = (REPO / "migrations" / "039_drop_plan_status_debris.sql").read_text()
        with tempfile.TemporaryDirectory() as tmp:
            repo = build_repo(tmp, migration=real, code="x = 1\n")
            changes = pf.pending_column_changes(set(), repo)
        self.assertEqual([(c["table"], c["column"], c["action"]) for c in changes],
                         [("health.plan", "status", "drops"),
                          ("health.plan", "original_date", "drops")])


class TestFailsClosed(unittest.TestCase):
    def test_an_unreadable_migration_table_aborts_rather_than_assuming(self):
        """No ssh, no answer — that is not 'nothing is pending'."""
        with patch.object(pf, "applied_migrations", return_value=None):
            code, lines = pf.check()
        self.assertEqual(code, 2)
        self.assertIn("ABORT", lines[0])
        self.assertIn("Failing closed", " ".join(lines))


class TestTheOverride(unittest.TestCase):
    def test_force_needs_a_reason(self):
        """`--force` alone is an argparse error: the reason is the point."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "applied.json"
            path.write_text(json.dumps([]))
            with patch.object(sys, "argv", ["pf", "--force"]), \
                 self.assertRaises(SystemExit) as cm:
                pf.main()
        self.assertNotEqual(cm.exception.code, 0)

    def test_force_deploys_and_prints_the_reason(self):
        out = []
        with patch.object(pf, "check", return_value=(1, ["[preflight] REFUSED: x"])), \
             patch.object(sys, "argv", ["pf", "--force", "iPad needs the API at 5:00"]), \
             patch("builtins.print", side_effect=lambda *a: out.append(" ".join(map(str, a)))):
            rc = pf.main()
        self.assertEqual(rc, 0)
        printed = "\n".join(out)
        self.assertIn("OVERRIDDEN", printed)
        self.assertIn("iPad needs the API at 5:00", printed)
        self.assertIn("REFUSED", printed)      # the finding is still shown

    def test_without_force_a_refusal_is_a_non_zero_exit(self):
        with patch.object(pf, "check", return_value=(1, ["[preflight] REFUSED: x"])), \
             patch.object(sys, "argv", ["pf"]), \
             patch("builtins.print"):
            self.assertEqual(pf.main(), 1)


class TestWiredIntoDeploy(unittest.TestCase):
    """A preflight nobody calls is a comment."""

    def setUp(self):
        self.sh = (REPO / "api" / "deploy.sh").read_text()

    def test_deploy_runs_it_before_the_build(self):
        self.assertIn("lambda_preflight.py", self.sh)
        self.assertLess(self.sh.index("lambda_preflight.py"),
                        self.sh.index("docker run"),
                        "the preflight must run before the build, not after")

    def test_deploy_stops_on_a_refusal(self):
        self.assertIn("set -e", self.sh.splitlines()[1])

    def test_deploy_passes_the_override_through_and_logs_it(self):
        self.assertIn('lambda_preflight.py" "$@"', self.sh)
        self.assertIn("OVERRIDE_LOG", self.sh)
        self.assertIn("FORCED", self.sh)

    def test_it_greps_what_the_lambda_actually_ships(self):
        for root in pf.CODE_ROOTS:
            self.assertIn(Path(root).name + "/", self.sh)


if __name__ == "__main__":
    unittest.main()
