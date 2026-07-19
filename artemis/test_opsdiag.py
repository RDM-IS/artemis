"""Tests for OPS-1 — operational self-diagnosis + version truth.

All mocked (no AWS/RDS): runbook classification from synthetic exceptions, the
unclassified passthrough (raw error preserved), the secret-scrub, and version
resolution (sha suffix present; 'unknown' fallback when git is unavailable).

Run:
    python3 -m artemis.test_opsdiag
    python3 artemis/test_opsdiag.py
"""

import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

os.environ.setdefault("RDS_HOST", "test-host")
os.environ.setdefault("RDS_DB", "test-db")

from artemis import opsdiag  # noqa: E402


def _git_error(stderr: str) -> subprocess.CalledProcessError:
    """A git subprocess failure carrying its stderr (how vault._run_git raises)."""
    return subprocess.CalledProcessError(128, ["git", "fetch"], output="", stderr=stderr)


class TestClassify(unittest.TestCase):
    def test_vault_pat_auth(self):
        exc = _git_error(
            "fatal: Authentication failed for 'https://github.com/RDM-IS/vault.git/'"
        )
        rb = opsdiag.classify(exc, {"stage": "vault sync"})
        self.assertIsNotNone(rb)
        self.assertEqual(rb.failure_class, "vault-pat-auth")

    def test_vault_pat_auth_repo_not_found(self):
        # A fine-grained PAT that lost access / awaits org approval 404s a known-good URL.
        exc = _git_error("remote: Repository not found.\nfatal: repository not found")
        self.assertEqual(opsdiag.classify(exc).failure_class, "vault-pat-auth")

    def test_vault_secret_missing(self):
        exc = Exception(
            "ResourceNotFoundException: Secrets Manager can't find the specified secret."
        )
        self.assertEqual(opsdiag.classify(exc).failure_class, "vault-secret-missing")

    def test_vault_secret_missing_shape(self):
        exc = KeyError("clone_url")
        self.assertEqual(
            opsdiag.classify(exc, {"detail": "vault-repo secret missing clone_url"}).failure_class,
            "vault-secret-missing",
        )

    def test_vault_clone_network(self):
        exc = _git_error(
            "fatal: unable to access 'https://github.com/RDM-IS/vault.git/': "
            "Could not resolve host: github.com"
        )
        self.assertEqual(opsdiag.classify(exc).failure_class, "vault-clone-network")

    def test_vault_clone_timeout_type(self):
        exc = subprocess.TimeoutExpired(["git", "fetch"], 180)
        self.assertEqual(opsdiag.classify(exc).failure_class, "vault-clone-network")

    def test_google_oauth_refresh(self):
        exc = Exception(
            "google.auth.exceptions.RefreshError: ('invalid_grant: "
            "Token has been expired or revoked.', ...)"
        )
        self.assertEqual(opsdiag.classify(exc).failure_class, "google-oauth-refresh")

    def test_rds_unreachable(self):
        exc = Exception(
            "psycopg2.OperationalError: could not connect to server: Connection refused"
        )
        self.assertEqual(opsdiag.classify(exc).failure_class, "rds-unreachable")

    def test_rds_wins_over_generic_network(self):
        # "connection refused" is RDS-specific and must not be grabbed by the network class.
        exc = Exception("OperationalError: connection to server failed: Connection refused")
        self.assertEqual(opsdiag.classify(exc).failure_class, "rds-unreachable")

    def test_unclassified_returns_none(self):
        self.assertIsNone(opsdiag.classify(Exception("a totally novel boom happened")))


class TestReportFailure(unittest.TestCase):
    def test_classified_renders_runbook_and_audits(self):
        exc = _git_error("fatal: Authentication failed for 'https://github.com/RDM-IS/vault.git/'")
        with patch.object(opsdiag, "log_audit") as m:
            msg = opsdiag.report_failure(exc, {"stage": "vault sync"}, agent="vault")
        self.assertIn("vault-pat-auth", msg)
        self.assertIn("aws secretsmanager put-secret-value", msg)
        self.assertIn("Authentication failed", msg)  # the literal error, verbatim
        m.assert_called_once()
        kwargs = m.call_args.kwargs
        self.assertEqual(kwargs["outcome"], "classified")
        self.assertEqual(kwargs["metadata"]["failure_class"], "vault-pat-auth")

    def test_unclassified_passthrough_preserves_raw(self):
        with patch.object(opsdiag, "log_audit") as m:
            msg = opsdiag.report_failure(Exception("a totally novel boom happened"), {"stage": "x"})
        self.assertIn("a totally novel boom happened", msg)
        self.assertIn("no runbook", msg.lower())
        self.assertEqual(m.call_args.kwargs["outcome"], "unclassified")

    def test_audit_failure_never_swallows_reply(self):
        with patch.object(opsdiag, "log_audit", side_effect=RuntimeError("db down")):
            msg = opsdiag.report_failure(Exception("boom"), {"stage": "x"})
        self.assertIn("boom", msg)

    def test_token_scrubbed_from_output(self):
        exc = _git_error("fatal: could not read Username github_pat_ABC123secretTOKEN xyz")
        with patch.object(opsdiag, "log_audit"):
            msg = opsdiag.report_failure(exc)
        self.assertNotIn("github_pat_ABC123secretTOKEN", msg)
        self.assertIn("github_pat_***", msg)


class TestVersion(unittest.TestCase):
    def test_version_has_sha_suffix(self):
        from artemis import version
        self.assertTrue(version.VERSION.startswith("1.4.0+"))
        self.assertEqual(version.VERSION.count("+"), 1)
        suffix = version.VERSION.split("+", 1)[1]
        self.assertTrue(suffix)  # sha7 or 'unknown', never empty

    def test_resolve_sha_fallback_when_git_unavailable(self):
        from artemis import version
        with patch.object(version.subprocess, "check_output", side_effect=OSError("no git")):
            self.assertEqual(version._resolve_sha(), "unknown")

    def test_commit_subject_empty_on_failure(self):
        from artemis import version
        with patch.object(version.subprocess, "check_output", side_effect=OSError("no git")):
            self.assertEqual(version.get_commit_subject(), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
