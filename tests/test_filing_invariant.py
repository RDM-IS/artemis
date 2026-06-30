"""Phase 2 — filing invariant: nothing leaves the inbox without a label + audit
row, and financial documents are never auto-filed.

Covers:
  - billing.is_financial_document: the exact subjects the gate swept as DONE
    (Vercel receipt/refund, Google Workspace invoice) must be recognized so the
    triage gate KEEPS them in the inbox for a manual command.
  - main.file_message_for_automation: routes through the audited primitive with
    source='automation_triage' and triage_state in metadata — i.e. the
    autonomous path now writes an audit row and applies @artemis/archive instead
    of a bare strip.

Heavy third-party imports are stubbed; the DB/Gmail layers are mocked. Run:
    python tests/test_filing_invariant.py
"""

import importlib
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))
os.environ.setdefault("RDS_HOST", "test-host")
os.environ.setdefault("RDS_DB", "test-db")


def _import_with_stubs(modname):
    for _ in range(50):
        try:
            return importlib.import_module(modname)
        except ModuleNotFoundError as e:
            missing = e.name
            if missing and missing.split(".")[0] in {"artemis", "knowledge"}:
                raise
            stub = types.ModuleType(missing)
            stub.__getattr__ = lambda name: MagicMock()  # type: ignore[attr-defined]
            sys.modules[missing] = stub
    raise RuntimeError(f"could not import {modname}")


billing = _import_with_stubs("artemis.billing")
main = _import_with_stubs("artemis.main")


class TestFinancialDocumentGuard(unittest.TestCase):
    def test_swept_documents_are_recognized(self):
        # The exact four (+ the Jun-29 payment) the gate archived as DONE.
        for subj in [
            "Your receipt from Vercel Inc. #2587-8370",
            "Your refund from Vercel Inc. #3146-3063",
            "Google Workspace: Your invoice is available for rdm.is",
            "Fwd: Google Workspace: Your invoice is available for rdm.is",
            "Google Workspace: We've received your payment for rdm.is",
        ]:
            self.assertTrue(billing.is_financial_document(subj), subj)

    def test_marketing_and_noise_not_financial(self):
        for subj in [
            "Tomorrow's Webinar - Win Customers, Build Loyalty",
            "Small-shop treasures await",
            "Choose your track: Cut AI costs & build with generative media",
            "[Notice] Possible unresolved security risks in your Admin Console",
            "Verify your identity",
        ]:
            self.assertFalse(billing.is_financial_document(subj), subj)

    def test_sender_signal(self):
        self.assertTrue(billing.is_financial_document("", "billing@vercel.com"))


class TestAutomationUsesAuditedPrimitive(unittest.TestCase):
    """file_message_for_automation must call the audited primitive with the
    automation source and the triage state in metadata — proving the autonomous
    path is labeled + audited, not a bare strip."""

    def test_routes_through_primitive_with_source_and_metadata(self):
        captured = {}

        def fake_exec(verb, num, message_id, category, *, source="user_directed",
                      gmail_client=None, metadata_extra=None):
            captured.update(
                verb=verb, num=num, message_id=message_id, category=category,
                source=source, gmail_client=gmail_client, metadata_extra=metadata_extra,
            )
            return {"num": num, "ok": True, "display": "x", "detail": ""}

        fake_client = MagicMock(name="scheduler_gmail")
        with patch.object(main, "_execute_disposition", side_effect=fake_exec):
            res = main.file_message_for_automation("msg123", "NOISE", gmail_client=fake_client)

        self.assertTrue(res["ok"])
        self.assertEqual(captured["verb"], "archive")
        self.assertEqual(captured["source"], "automation_triage")
        self.assertEqual(captured["metadata_extra"], {"triage_state": "NOISE"})
        self.assertIs(captured["gmail_client"], fake_client)
        # archive carries no category — it lands under the generic @artemis/archive.
        self.assertIsNone(captured["category"])


class TestPrimitiveAuditsAutomation(unittest.TestCase):
    """End-to-end through the real _execute_disposition with mocked Gmail/DB:
    a verified automation archive writes an audit row tagged automation_triage
    and applies a label (never a bare strip)."""

    def test_verified_archive_logs_audit_with_automation_source(self):
        gmail = MagicMock()
        gmail.service = object()
        gmail.get_message_labels.side_effect = [
            ["INBOX", "UNREAD"],           # prior
            ["Label_archive"],             # post (INBOX gone, archive label id present)
        ]
        gmail.ensure_gmail_label.return_value = "Label_archive"
        gmail.modify_labels.return_value = True

        email_index = types.SimpleNamespace(
            get_by_message_id=lambda mid: {
                "sender": "billing@vercel.com", "sender_domain": "vercel.com",
                "subject": "Your receipt", "thread_id": "t1",
            },
            drop_from_index=MagicMock(),
        )
        audit_calls = []
        db = types.SimpleNamespace(log_audit=lambda **kw: audit_calls.append(kw))

        with patch.dict(sys.modules, {"artemis.email_index": email_index, "knowledge.db": db}):
            res = main.file_message_for_automation("m1", "NOISE", gmail_client=gmail)

        self.assertTrue(res["ok"])
        self.assertEqual(len(audit_calls), 1)
        a = audit_calls[0]
        self.assertEqual(a["source"], "automation_triage")
        self.assertEqual(a["action"], "archive")
        self.assertTrue(a["verified"])
        self.assertIn("Label_archive", a["applied_labels"])
        self.assertIn("INBOX", a["removed_labels"])
        self.assertEqual(a["metadata"], {"triage_state": "NOISE"})
        # Mirror row dropped only after verification.
        email_index.drop_from_index.assert_called_once_with("m1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
