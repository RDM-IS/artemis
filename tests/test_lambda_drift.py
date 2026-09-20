"""LAMBDA-DRIFT option B — the live Lambda package vs the repo.

What matters here is that it compares CONTENTS. A code hash covers 24 MB of
pinned dependencies, changes when nothing of ours did, and cannot name the file
that drifted. These tests pin the file-by-file behaviour and the silence: no
post unless the live package really is not this code.

Run:
    python3 tests/test_lambda_drift.py
"""

import os as _os; _os.environ["ARTEMIS_TEST_NO_DB"] = "1"  # TEST-DB-GUARD: never a real DB

import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from artemis import lambda_drift as ld  # noqa: E402

REPO = Path(__file__).resolve().parent.parent

LIVE = {"app/routers/health.py": b"def plan(): return 1\n",
        "knowledge/secrets.py": b"KEY = 'x'\n"}


def make_zip(tmp: str, members: dict, *, knowledge_prefix: str = "../") -> Path:
    """A package shaped like api/deploy.sh's: `app/...` and `../knowledge/...`,
    with dependency noise that must be ignored."""
    path = Path(tmp) / "live.zip"
    with zipfile.ZipFile(path, "w") as zf:
        for name, body in members.items():
            if name.startswith("knowledge/"):
                name = knowledge_prefix + name
            zf.writestr(name, body)
        zf.writestr("fastapi/__init__.py", b"# a dependency, not ours\n")
        zf.writestr("app/__pycache__/health.cpython-312.pyc", b"\x00\x01")
        zf.writestr("../migrations/039_drop_plan_status_debris.sql", b"ALTER TABLE ...")
    return path


def compare(tmp, live=LIVE, repo_files=None, **kw):
    repo_files = LIVE if repo_files is None else repo_files
    return ld.compare(make_zip(tmp, live, **kw),
                      read_repo=lambda p: repo_files.get(_repo_to_zip(p)),
                      tracked=[_zip_to_repo(k) for k in repo_files])


def _repo_to_zip(path: str) -> str:
    return path[len("api/"):] if path.startswith("api/app/") else path


def _zip_to_repo(name: str) -> str:
    return "api/" + name if name.startswith("app/") else name


class TestComparison(unittest.TestCase):
    def test_a_matching_package_is_silent(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = compare(tmp)
        self.assertEqual(result["differs"], [])
        self.assertEqual(result["only_in_repo"], [])
        self.assertEqual(result["only_in_lambda"], [])
        self.assertEqual(result["checked"], 2)
        self.assertIsNone(ld.message(result))

    def test_one_changed_line_is_caught_and_the_file_is_named(self):
        """The 9/20 shape: the repo has the fix, the package does not."""
        stale = dict(LIVE, **{"app/routers/health.py": b"def plan(): return 0\n"})
        with tempfile.TemporaryDirectory() as tmp:
            result = compare(tmp, live=stale)
        self.assertEqual(result["differs"], ["api/app/routers/health.py"])
        msg = ld.message(result, ref_sha="a9ab7b8c", modified="2026-09-20T17:55:52.000+0000")
        self.assertIn("api/app/routers/health.py", msg)
        self.assertIn("not running this code", msg)
        self.assertIn("bash deploy.sh", msg)
        self.assertIn("a9ab7b8", msg)

    def test_a_merged_file_that_was_never_deployed(self):
        """#114 and #106 sat merged-but-undeployed for a day. This is that."""
        with tempfile.TemporaryDirectory() as tmp:
            result = compare(tmp, repo_files=dict(LIVE, **{"knowledge/watch_payload.py": b"new\n"}))
        self.assertEqual(result["only_in_repo"], ["knowledge/watch_payload.py"])
        self.assertIn("never deployed", ld.message(result))

    def test_a_file_deleted_from_the_repo_but_still_shipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = compare(tmp, live=dict(LIVE, **{"app/routers/ramp.py": b"gone\n"}))
        self.assertEqual(result["only_in_lambda"], ["api/app/routers/ramp.py"])
        self.assertIn("no longer", ld.message(result))

    def test_dependencies_migrations_and_pycache_are_not_our_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = compare(tmp)
        self.assertEqual(result["checked"], 2)          # not fastapi, not the .sql
        self.assertIsNone(ld.message(result))

    def test_knowledge_is_found_whichever_way_lambda_stored_it(self):
        """deploy.sh zips `../knowledge/`; Lambda strips the `../` on
        extraction. Both spellings must resolve to the same repo path."""
        for prefix in ("../", ""):
            with self.subTest(prefix=prefix or "none"), tempfile.TemporaryDirectory() as tmp:
                result = compare(tmp, knowledge_prefix=prefix)
                self.assertEqual(result["checked"], 2)
                self.assertEqual(result["differs"], [])


class TestPathMapping(unittest.TestCase):
    def test_members_map_to_repo_paths(self):
        self.assertEqual(ld.repo_path_for("app/main.py"), "api/app/main.py")
        self.assertEqual(ld.repo_path_for("../knowledge/db.py"), "knowledge/db.py")
        self.assertEqual(ld.repo_path_for("knowledge/db.py"), "knowledge/db.py")

    def test_anything_else_is_not_ours(self):
        for member in ("fastapi/applications.py", "../migrations/039.sql",
                       "../tests/test_billing.py", "boto3/session.py"):
            self.assertIsNone(ld.repo_path_for(member), member)


class TestItFailsLoudly(unittest.TestCase):
    def test_a_missing_permission_is_reported_not_swallowed(self):
        """Silence has to mean 'the package matches', never 'it never ran'."""
        class Denied:
            def get_function(self, **kw):
                raise RuntimeError("AccessDeniedException: lambda:GetFunction")

        msg = ld.check(client=Denied())
        self.assertIn("could not run", msg)
        self.assertIn("lambda:GetFunction", msg)

    def test_any_other_failure_still_posts(self):
        class Broken:
            def get_function(self, **kw):
                raise TimeoutError("s3 download timed out")

        self.assertIn("failed", ld.check(client=Broken()))


class TestTheIamProposal(unittest.TestCase):
    """The permissions are proposed in code so the ask is reviewable."""

    def test_exactly_two_read_only_actions_on_one_function(self):
        stmt = ld.IAM_STATEMENT["Statement"][0]
        self.assertEqual(sorted(stmt["Action"]),
                         ["lambda:GetFunction", "lambda:GetFunctionConfiguration"])
        self.assertEqual(stmt["Effect"], "Allow")
        self.assertTrue(stmt["Resource"].endswith(":function:rdmis-crm-api"))
        self.assertNotIn("*", stmt["Resource"])

    def test_nothing_that_could_change_the_function(self):
        for action in ld.IAM_STATEMENT["Statement"][0]["Action"]:
            self.assertTrue(action.startswith("lambda:Get"), action)


class TestWiredIntoMonday(unittest.TestCase):
    def test_the_update_check_runs_it(self):
        src = (REPO / "artemis" / "scheduler.py").read_text()
        job = src[src.index("def job_update_check"):src.index("def _check_lambda_drift")]
        self.assertIn("_check_lambda_drift()", job)
        self.assertIn("from artemis.lambda_drift import check", src)

    def test_it_posts_only_when_there_is_something_to_say(self):
        src = (REPO / "artemis" / "scheduler.py").read_text()
        body = src[src.index("def _check_lambda_drift"):]
        body = body[:body.index("def _check_github_update")]
        self.assertIn("if msg:", body)


if __name__ == "__main__":
    unittest.main()
