"""SCHEMA-DRIFT — migrations on disk vs acos.schema_migrations.

Run:
    python3 tests/test_schema_drift.py
"""

import os as _os; _os.environ["ARTEMIS_TEST_NO_DB"] = "1"  # TEST-DB-GUARD: never a real DB

import re
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from artemis import schema_drift as sd  # noqa: E402


class TestCompare(unittest.TestCase):
    def test_no_drift_no_message(self):
        disk = ["001_a.sql", "002_b.sql"]
        self.assertEqual(sd.compare(disk, set(disk)), {"unapplied": [], "unknown": []})
        self.assertIsNone(sd.message(sd.compare(disk, set(disk))))

    def test_unapplied_file_is_reported_with_the_deploy_hint(self):
        msg = sd.message(sd.compare(["033_x.sql", "034_y.sql"], {"033_x.sql"}))
        self.assertIn("1 migration(s) on the box not applied to RDS: 034_y.sql", msg)
        self.assertIn("scripts/deploy.sh", msg)

    def test_applied_but_missing_from_the_repo_is_reported(self):
        msg = sd.message(sd.compare(["001_a.sql"], {"001_a.sql", "099_gone.sql"}))
        self.assertIn("1 applied migration(s) not in the repo: 099_gone.sql", msg)
        self.assertNotIn("not applied to RDS", msg)

    def test_check_reads_disk_and_rds(self):
        with patch.object(sd, "applied", return_value=set(sd.on_disk())):
            self.assertIsNone(sd.check())


class TestRepoMigrations(unittest.TestCase):
    def test_numbers_are_unique_and_sequential(self):
        names = sd.on_disk()
        nums = [int(re.match(r"(\d+)_", n).group(1)) for n in names]
        self.assertEqual(len(nums), len(set(nums)), "duplicate migration number")
        self.assertEqual(nums, list(range(1, len(nums) + 1)), "gap in migration numbers")


class TestJob(unittest.TestCase):
    def _sched(self):
        try:
            from artemis.scheduler import ArtemisScheduler
        except ImportError as e:  # apscheduler not installed locally
            self.skipTest(f"scheduler deps missing: {e}")
        s = ArtemisScheduler(MagicMock(), MagicMock(), MagicMock())
        return s

    # job_update_check runs THREE checks. These tests were written for two and
    # left the Lambda-drift check real, so every run off CI read the LIVE
    # Lambda with the machine's AWS credentials (GetFunction + a ~24 MB
    # download) and failed whenever it had drifted (found 2026-11-09). A unit
    # test never reaches AWS: the third check is patched, and asked for.
    def test_posts_only_on_drift_and_despite_a_github_failure(self):
        s = self._sched()
        with patch.object(s, "_is_open", return_value=True), \
             patch.object(s, "_post") as post, \
             patch("artemis.lambda_drift.check", return_value=None) as ld, \
             patch("artemis.version.get_latest_github_version", side_effect=OSError("offline")), \
             patch.object(sd, "check", return_value="⚠️ Schema drift — x"):
            s.job_update_check()
        ld.assert_called_once()
        post.assert_called_once()
        self.assertIn("Schema drift", post.call_args[0][1])

    def test_silent_when_clean(self):
        s = self._sched()
        with patch.object(s, "_is_open", return_value=True), \
             patch.object(s, "_post") as post, \
             patch("artemis.lambda_drift.check", return_value=None) as ld, \
             patch("artemis.version.get_commit_hash", return_value="abc"), \
             patch("artemis.version.get_latest_github_version", return_value=("abc123", "d")), \
             patch.object(sd, "check", return_value=None):
            s.job_update_check()
        ld.assert_called_once()
        post.assert_not_called()

    def test_no_aws_client_is_built_by_the_job_tests(self):
        """The guard on the guard: with the drift checks patched, nothing in the
        job may construct a boto3 client."""
        s = self._sched()
        import boto3
        with patch.object(s, "_is_open", return_value=True), \
             patch.object(s, "_post"), \
             patch("artemis.lambda_drift.check", return_value=None), \
             patch("artemis.version.get_commit_hash", return_value="abc"), \
             patch("artemis.version.get_latest_github_version", return_value=("abc123", "d")), \
             patch.object(sd, "check", return_value=None), \
             patch.object(boto3, "client", side_effect=AssertionError("a unit test reached AWS")) as bc:
            s.job_update_check()
        bc.assert_not_called()


if __name__ == "__main__":
    unittest.main()
