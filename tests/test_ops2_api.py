"""OPS-2 — Engagement Ops API tests.

Mirrors the mocked-unit tier of tests/test_commitments_rds.py: knowledge.db is
mocked, so these run anywhere without a live RDS. Four properties the build must hold:

  1. AUTH GATE  — an ops route with no Cloudflare Access token is rejected (401);
     a present-but-invalid token is rejected (403). (Flask required; skips if absent.)
  2. PARITY     — approving a fixture proposal via the API (approve_proposal_by_id)
     writes the SAME creation-path row as approving it via the Mattermost verb
     (adjudicate): both funnel through _approve_proposal and call
     commitments.add_commitment with identical args.
  3. EDIT-THEN-APPROVE — an edited payload is persisted to payload_final and the
     approval writes THROUGH payload_final, not the original payload.
  4. SCOPING    — proposals_for_context selects context = slug, and the unscoped
     path selects context IS NULL (NULL-context items are surfaced, never dropped).

Run:
    python3 tests/test_ops2_api.py
"""

import os
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

os.environ.setdefault("RDS_HOST", "test-host")
os.environ.setdefault("RDS_DB", "test-db")

from artemis import vault  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _fixture_proposal(**over) -> dict:
    """A pending commitment proposal in the row shape get_proposal/_pending_proposals
    return (joined to its note)."""
    p = {
        "id": 1,
        "capture_id": "cap-1",
        "extraction_type": "commitment",
        "payload": {"text": "Send DCAA the cost model", "evidence": "we'll send the model",
                    "due_date": "2026-08-01", "direction": "outbound"},
        "payload_final": None,
        "context": "fca",
        "status": "pending",
        "created_at": datetime(2026, 7, 18, 12, 0, 0),
        "path": "authored/meetings/2026-07-18-dcaa.md",
        "source": "meeting",
    }
    p.update(over)
    return p


def _approve_side_effect(sql, params=None):
    """execute_one router for _approve_proposal internals: the note-created lookup
    and the post-write status re-read."""
    if "created_at FROM vault.notes" in sql:
        return {"created_at": datetime(2026, 7, 18, 12, 0, 0)}
    if "status, target_ref" in sql:
        return {"status": "approved", "target_ref": "commitment:42"}
    return None


# ---------------------------------------------------------------------------
# 2. PARITY — API path == Mattermost path (same row written)
# ---------------------------------------------------------------------------

class TestApproveParity(unittest.TestCase):
    def _run_approve(self, via_api: bool):
        """Approve the same fixture proposal through one of the two surfaces and
        return the commitments.add_commitment call args it produced."""
        p = _fixture_proposal()
        with patch("artemis.vault.execute_one", side_effect=_approve_side_effect), \
             patch("artemis.vault.execute_write", return_value=None), \
             patch("artemis.vault.log_audit"), \
             patch("artemis.commitments.add_commitment", return_value=42) as add:
            if via_api:
                with patch("artemis.vault.get_proposal", return_value=_fixture_proposal()):
                    ok, target = vault.approve_proposal_by_id(1)
            else:
                # The Mattermost surface: a live digest mapping + `approve 1`.
                ok, target = None, None
                reply = vault.adjudicate("approve", [1], {1: p})
                ok = "Approved" in reply
                target = "commitment:42"
        self.assertTrue(ok)
        self.assertEqual(target, "commitment:42")
        return add.call_args

    def test_api_and_mattermost_write_identical_commitment(self):
        api_call = self._run_approve(via_api=True)
        mm_call = self._run_approve(via_api=False)
        # Same creation path, same args → same row. This is the parity guarantee.
        self.assertEqual(api_call, mm_call)
        # And it is the real add_commitment contract (title/due_date/context).
        self.assertEqual(api_call.kwargs.get("title"), "Send DCAA the cost model")
        self.assertEqual(api_call.kwargs.get("due_date"), "2026-08-01")
        self.assertEqual(api_call.kwargs.get("context"), "fca")


# ---------------------------------------------------------------------------
# 3. EDIT-THEN-APPROVE — payload_final stored + approved from
# ---------------------------------------------------------------------------

class TestEditThenApprove(unittest.TestCase):
    def test_approve_proposal_prefers_payload_final(self):
        """The write-through core honours payload_final over payload."""
        edited = {"text": "Send DCAA the REVISED cost model", "due_date": "2026-09-01",
                  "direction": "outbound"}
        p = _fixture_proposal(payload_final=edited)
        with patch("artemis.vault.execute_one", side_effect=_approve_side_effect), \
             patch("artemis.vault.execute_write", return_value=None), \
             patch("artemis.vault.log_audit"), \
             patch("artemis.commitments.add_commitment", return_value=42) as add:
            ok, target = vault._approve_proposal(p)
        self.assertTrue(ok)
        self.assertEqual(add.call_args.kwargs.get("title"), "Send DCAA the REVISED cost model")
        self.assertEqual(add.call_args.kwargs.get("due_date"), "2026-09-01")

    def test_set_payload_final_writes_and_confirms(self):
        edited = {"text": "edited"}
        with patch("artemis.vault.execute_write", return_value=None) as w, \
             patch("artemis.vault.execute_one", return_value={"payload_final": edited}), \
             patch("artemis.vault.log_audit"):
            ok = vault.set_payload_final(7, edited)
        self.assertTrue(ok)
        sql, params = w.call_args[0]
        self.assertIn("SET payload_final", sql)
        self.assertIn("status = 'pending'", sql)  # only edits a still-pending row

    def test_by_id_persists_edit_then_approves_from_it(self):
        """approve_proposal_by_id(pid, edited): set_payload_final first, then
        _approve_proposal sees the edited row."""
        pending = _fixture_proposal()
        edited = {"text": "EDITED"}
        after_edit = _fixture_proposal(payload_final=edited)
        with patch("artemis.vault.get_proposal", side_effect=[pending, after_edit]), \
             patch("artemis.vault.set_payload_final", return_value=True) as sp, \
             patch("artemis.vault._approve_proposal", return_value=(True, "commitment:42")) as ap:
            ok, target = vault.approve_proposal_by_id(1, edited)
        self.assertTrue(ok)
        sp.assert_called_once_with(1, edited)
        # Approved from the RE-READ (edited) row, not the original.
        self.assertIs(ap.call_args[0][0], after_edit)

    def test_by_id_plain_approve_skips_edit(self):
        pending = _fixture_proposal()
        with patch("artemis.vault.get_proposal", return_value=pending), \
             patch("artemis.vault.set_payload_final") as sp, \
             patch("artemis.vault._approve_proposal", return_value=(True, "commitment:42")) as ap:
            ok, _ = vault.approve_proposal_by_id(1)
        self.assertTrue(ok)
        sp.assert_not_called()
        self.assertIs(ap.call_args[0][0], pending)

    def test_by_id_refuses_non_pending(self):
        with patch("artemis.vault.get_proposal", return_value=_fixture_proposal(status="approved")):
            ok, target = vault.approve_proposal_by_id(1)
        self.assertFalse(ok)
        self.assertEqual(target, "")


# ---------------------------------------------------------------------------
# 4. SCOPING — context = slug vs context IS NULL (unscoped surfaced)
# ---------------------------------------------------------------------------

class TestScoping(unittest.TestCase):
    def test_scoped_filters_on_context_equals(self):
        with patch("artemis.vault.execute_query", return_value=[]) as q:
            vault.proposals_for_context("fca")
        sql, params = q.call_args[0]
        self.assertIn("p.context = %s", sql)
        self.assertEqual(params, ("fca",))

    def test_unscoped_filters_on_context_is_null(self):
        with patch("artemis.vault.execute_query", return_value=[]) as q:
            vault.proposals_for_context(None, unscoped=True)
        sql = q.call_args[0][0]
        self.assertIn("p.context IS NULL", sql)
        # No params tuple for the unscoped query (no bound value).
        self.assertEqual(len(q.call_args[0]), 1)


# ---------------------------------------------------------------------------
# 1. AUTH GATE — Cloudflare Access enforced on every ops route
# ---------------------------------------------------------------------------

try:
    import flask  # noqa: F401
    _HAVE_FLASK = True
except Exception:
    _HAVE_FLASK = False


@unittest.skipUnless(_HAVE_FLASK, "Flask not installed in this env")
class TestAccessGate(unittest.TestCase):
    def _client(self):
        import flask
        from artemis import ops_access
        app = flask.Flask(__name__)

        @app.get("/guarded")
        @ops_access.require_access
        def guarded():
            return flask.jsonify({"ok": True, "who": ops_access.current_actor()})

        return app.test_client()

    def test_no_token_is_401(self):
        resp = self._client().get("/guarded")
        self.assertEqual(resp.status_code, 401)

    def test_invalid_token_is_403(self):
        from artemis import ops_access
        with patch("artemis.ops_access.verify_access_token",
                   side_effect=ops_access.AccessError("bad", status=403)):
            resp = self._client().get(
                "/guarded", headers={"Cf-Access-Jwt-Assertion": "garbage"})
        self.assertEqual(resp.status_code, 403)

    def test_valid_token_passes_and_attributes_identity(self):
        with patch("artemis.ops_access.verify_access_token",
                   return_value={"email": "ryan@rdm.is"}):
            resp = self._client().get(
                "/guarded", headers={"Cf-Access-Jwt-Assertion": "good"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["who"], "ryan@rdm.is")

    def test_options_preflight_bypasses_auth(self):
        """A CORS preflight (no token) must not 401 — it returns 204 so the real,
        token-bearing request can follow."""
        resp = self._client().open("/guarded", method="OPTIONS")
        self.assertNotEqual(resp.status_code, 401)
        self.assertLess(resp.status_code, 300)

    def test_unconfigured_is_denied_not_allowed(self):
        """Fail closed: with CF_ACCESS_* unset, a token is denied (403), never allowed."""
        from artemis import ops_access
        with patch.dict(os.environ, {"CF_ACCESS_TEAM_DOMAIN": "", "CF_ACCESS_AUD": ""}, clear=False):
            self.assertFalse(ops_access.is_configured())
            resp = self._client().get(
                "/guarded", headers={"Cf-Access-Jwt-Assertion": "whatever"})
        self.assertEqual(resp.status_code, 403)


if __name__ == "__main__":
    unittest.main(verbosity=2)
